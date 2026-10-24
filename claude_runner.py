import asyncio
import getpass
import json
import logging
import os
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

import config

logger = logging.getLogger("claude_runner")


class WorkerPool:
    """
    Manages a pool of Claude CLI workers (independent Claude accounts via CLAUDE_CONFIG_DIR).
    Provides least-busy dispatching, round-robin load distribution, active request tracking,
    and concurrency enforcement per worker.
    """

    def __init__(self, worker_configs: Optional[List[Dict[str, Any]]] = None):
        self.workers: List[Dict[str, Any]] = list(worker_configs or config.DEFAULT_WORKERS)
        self._active_counts: Dict[str, int] = {w["id"]: 0 for w in self.workers}
        self._total_served: Dict[str, int] = {w["id"]: 0 for w in self.workers}
        self._rr_index: int = 0
        self._lock: asyncio.Lock = asyncio.Lock()
        self._semaphores: Dict[str, asyncio.Semaphore] = {}

    def _get_semaphore(self, worker_id: str) -> Optional[asyncio.Semaphore]:
        if config.MAX_CONCURRENCY <= 0:
            return None
        if worker_id not in self._semaphores:
            self._semaphores[worker_id] = asyncio.Semaphore(config.MAX_CONCURRENCY)
        return self._semaphores[worker_id]

    def get_status(self) -> Dict[str, Any]:
        """Return pool telemetry conforming to section 3.2 specification."""
        return {
            "total_workers": len(self.workers),
            "dispatch_mode": config.POOL_DISPATCH_MODE,
            "workers": [
                {
                    "id": w["id"],
                    "name": w.get("name", w["id"]),
                    "config_dir": w.get("config_dir"),
                    "active_requests": self._active_counts.get(w["id"], 0),
                    "total_served": self._total_served.get(w["id"], 0),
                }
                for w in self.workers
            ],
        }

    def _resolve_worker(self, name: str) -> Optional[Dict[str, Any]]:
        target = name.strip().lower()
        for w in self.workers:
            if w["id"].lower() == target:
                return w
            if target in [a.lower() for a in w.get("aliases", [])]:
                return w
        return None

    @asynccontextmanager
    async def lease_worker(self, preferred_worker: Optional[str] = None):
        """Lease a worker according to dispatch policy or preferred worker override."""
        async with self._lock:
            selected_worker = None
            if preferred_worker:
                selected_worker = self._resolve_worker(preferred_worker)
                if not selected_worker:
                    logger.warning(
                        "Preferred worker '%s' not recognized, falling back to dynamic pool",
                        preferred_worker,
                    )

            if not selected_worker:
                if config.POOL_DISPATCH_MODE == "round_robin":
                    selected_worker = self.workers[self._rr_index % len(self.workers)]
                    self._rr_index = (self._rr_index + 1) % 1_000_000
                else:
                    # Least-busy worker selection with round-robin tie breaker
                    min_active = min(self._active_counts[w["id"]] for w in self.workers)
                    candidates = [w for w in self.workers if self._active_counts[w["id"]] == min_active]
                    selected_worker = candidates[self._rr_index % len(candidates)]
                    self._rr_index = (self._rr_index + 1) % 1_000_000

            worker_id = selected_worker["id"]
            self._active_counts[worker_id] += 1
            self._total_served[worker_id] += 1
            sem = self._get_semaphore(worker_id)

        if sem is not None:
            await sem.acquire()

        logger.info(
            "Leased worker '%s' (active: %d, total served: %d)",
            worker_id,
            self._active_counts[worker_id],
            self._total_served[worker_id],
        )

        try:
            yield selected_worker
        finally:
            if sem is not None:
                sem.release()
            async with self._lock:
                self._active_counts[worker_id] = max(0, self._active_counts[worker_id] - 1)
            logger.info("Released worker '%s' (active: %d)", worker_id, self._active_counts[worker_id])


GLOBAL_POOL: WorkerPool = WorkerPool(config.DEFAULT_WORKERS)


def resolve_model(requested: Optional[str]) -> str:
    """
    Resolve requested model name or alias to a known Claude model ID.
    Strips 'provider/' prefix and '@worker' suffix; maps aliases; unknown IDs passed through.
    """
    if not requested:
        return config.DEFAULT_MODEL

    clean = requested.strip()
    if "/" in clean:
        clean = clean.split("/")[-1]
    if "@" in clean:
        clean = clean.split("@")[0]

    clean = clean.strip()
    if not clean:
        return config.DEFAULT_MODEL

    lowered = clean.lower()
    if lowered in config.MODEL_ALIASES:
        return config.MODEL_ALIASES[lowered]

    for m in config.KNOWN_MODELS:
        if m["id"].lower() == lowered:
            return m["id"]

    return clean


def split_effort_suffix(model: str) -> Tuple[str, Optional[str]]:
    """
    Split reasoning effort suffix from model name.
    e.g. 'claude-opus-5-5-high' -> ('claude-opus-5-5', 'high')
    Suffixes supported: -low, -medium, -high, -xhigh, -max
    """
    if not model:
        return ("", None)

    lowered = model.lower()
    for suffix in ("-xhigh", "-medium", "-high", "-low", "-max"):
        if lowered.endswith(suffix):
            base_model = model[: -len(suffix)]
            effort = suffix.lstrip("-").lower()
            return (base_model, effort)

    return (model, None)


def extract_text_from_content(content: Any) -> str:
    """Extract plain text from message content (string or multipart array)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                part_type = part.get("type")
                if part_type == "text":
                    parts.append(part.get("text", ""))
                elif part_type == "image_url":
                    parts.append("[Attached Image]")
                elif "text" in part:
                    parts.append(str(part["text"]))
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    return str(content)


def _clean_tool_content(content: str) -> str:
    """Compress repetitive lines in tool output to save tokens and latency."""
    lines = content.splitlines()
    if len(lines) <= 6:
        return content
    cleaned = []
    prev_line = None
    repeat_count = 0
    for line in lines:
        stripped = line.strip()
        if stripped == prev_line:
            repeat_count += 1
            if repeat_count < 3:
                cleaned.append(line)
        else:
            if repeat_count >= 3:
                cleaned.append(f"... [repeated {repeat_count - 2} identical lines omitted] ...")
            prev_line = stripped
            repeat_count = 0
            cleaned.append(line)
    if repeat_count >= 3:
        cleaned.append(f"... [repeated {repeat_count - 2} identical lines omitted] ...")
    return "\n".join(cleaned)


GATEWAY_PREAMBLE = (
    "You are an AI assistant running inside OpenClaw on a Linux (Debian) system.\n"
    f"You have full tool access, including Bash, running as user '{getpass.getuser()}'.\n"
    "When asked to check system status, run commands, inspect logs, or manage services (such as Tailscale, systemd services, network, or files), use your Bash tool to execute the necessary commands and provide real, verified results."
)

MAX_PROMPT_CHARS = 400_000


def format_messages(messages: List[Dict[str, Any]]) -> Tuple[str, str]:
    """
    Format standard OpenAI messages into (system_prompt, prompt).
    System prompt combines gateway preamble with system/developer messages.
    Prompt is raw text for single user message or a structured transcript for multi-turn.
    """
    if not messages:
        return GATEWAY_PREAMBLE, ""

    system_parts: List[str] = [GATEWAY_PREAMBLE]
    dialogue: List[Tuple[str, str]] = []

    for msg in messages:
        role = (msg.get("role") or "user").lower()
        content = extract_text_from_content(msg.get("content", ""))

        if role in ("system", "developer"):
            if content.strip():
                system_parts.append(content.strip())
            continue

        if role == "user":
            dialogue.append(("User", content.strip()))
        elif role == "assistant":
            tool_calls = msg.get("tool_calls")
            if tool_calls and isinstance(tool_calls, list):
                rendered_calls = []
                for tc in tool_calls:
                    if isinstance(tc, dict):
                        fn = tc.get("function")
                        if isinstance(fn, dict):
                            fn_name = fn.get("name", "tool")
                            fn_args = fn.get("arguments", "")
                        else:
                            fn_name = tc.get("name", "tool")
                            fn_args = tc.get("arguments", "")
                        rendered_calls.append(f"{fn_name}({fn_args})")
                    else:
                        rendered_calls.append(str(tc))
                call_desc = ", ".join(rendered_calls)
                speaker = f"Assistant (tool call {call_desc})"
                dialogue.append((speaker, content.strip()))
            else:
                dialogue.append(("Assistant", content.strip()))
        elif role in ("tool", "function"):
            tool_name = msg.get("name") or msg.get("tool_call_id") or "tool"
            speaker = f"Tool Result ({tool_name})"
            cleaned = _clean_tool_content(content.strip())
            dialogue.append((speaker, cleaned))
        else:
            dialogue.append((role.capitalize(), content.strip()))

    system_prompt = "\n\n".join(system_parts)

    if not dialogue:
        return system_prompt, ""

    # If single user message, pass its text directly
    if len(dialogue) == 1 and dialogue[0][0] == "User":
        single_text = dialogue[0][1]
        if len(single_text) > MAX_PROMPT_CHARS:
            single_text = "...[Prompt truncated for length]...\n" + single_text[-MAX_PROMPT_CHARS:]
        return system_prompt, single_text

    # Otherwise format as a transcript:
    # [Conversation History]
    # User: …
    # Assistant: …
    # Tool Result (name): …
    #
    # [Current Request]
    # User: …
    last_speaker, last_text = dialogue[-1]
    current_request_str = f"{last_speaker}: {last_text}" if last_text else last_speaker

    if len(current_request_str) > 150_000:
        current_request_str = "...[Current request truncated for length]...\n" + current_request_str[-150_000:]

    history_turns: List[str] = []
    for spk, txt in dialogue[:-1]:
        turn = f"{spk}: {txt}" if txt else spk
        history_turns.append(turn)

    if not history_turns:
        prompt = f"[Current Request]\n{current_request_str}"
        return system_prompt, prompt

    # Budget calculation: cap total prompt ~400_000 chars, keeping newest history first
    overhead = len("[Conversation History]\n\n\n[Current Request]\n") + len(current_request_str)
    budget_for_history = MAX_PROMPT_CHARS - overhead

    if budget_for_history <= 0:
        prompt = (
            "[Conversation History]\n...[Earlier conversation history truncated for length]...\n\n"
            f"[Current Request]\n{current_request_str}"
        )
        return system_prompt, prompt

    included_turns: List[str] = []
    curr_len = 0
    truncated = False

    for turn in reversed(history_turns):
        turn_len = len(turn) + 2  # account for \n\n separator
        if curr_len + turn_len <= budget_for_history:
            included_turns.append(turn)
            curr_len += turn_len
        else:
            truncated = True
            break

    included_turns.reverse()

    if truncated:
        marker = "...[Earlier conversation history truncated for length]..."
        history_body = marker + "\n\n" + "\n\n".join(included_turns) if included_turns else marker
    else:
        history_body = "\n\n".join(included_turns)

    prompt = f"[Conversation History]\n{history_body}\n\n[Current Request]\n{current_request_str}"
    return system_prompt, prompt


def build_command(
    worker: Dict[str, Any],
    model: str,
    effort: Optional[str],
    system_prompt_file: str,
    stream: bool,
) -> List[str]:
    """
    Construct the headless Claude Code CLI command arguments.
    """
    claude_bin = worker.get("bin") or config.CLAUDE_BIN
    cmd = [
        claude_bin,
        "-p",
        "--output-format",
        "stream-json" if stream else "json",
    ]
    if stream:
        cmd.extend(["--verbose", "--include-partial-messages"])

    cmd.extend(
        [
            "--model",
            model,
            "--no-session-persistence",
            "--system-prompt-file",
            system_prompt_file,
            "--setting-sources",
            "",
            "--strict-mcp-config",
            "--disable-slash-commands",
        ]
    )

    if getattr(config, "ENABLE_TOOLS", True):
        cmd.extend(["--tools", "default"])
        if getattr(config, "AUTO_APPROVE", True):
            cmd.append("--dangerously-skip-permissions")
    else:
        cmd.extend(["--tools", ""])

    if effort and effort.strip():
        cmd.extend(["--effort", effort.strip().lower()])

    return cmd


async def run_claude_non_stream(
    messages: List[Dict[str, Any]],
    model: Optional[str] = None,
    effort: Optional[str] = None,
    timeout: Optional[float] = None,
    preferred_worker: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Execute headless Claude CLI with JSON output and return response dictionary.
    Raises TimeoutError on timeout, RuntimeError on non-zero exit or error response.
    """
    raw_model = model or config.DEFAULT_MODEL
    base_model, suffix_effort = split_effort_suffix(raw_model)
    resolved_model = resolve_model(base_model)
    selected_effort = effort or suffix_effort or config.DEFAULT_EFFORT or None
    run_timeout = float(timeout or config.DEFAULT_TIMEOUT)

    system_prompt, prompt = format_messages(messages)

    temp_sys_file = tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt", encoding="utf-8")
    try:
        temp_sys_file.write(system_prompt)
        temp_sys_file.flush()
    finally:
        temp_sys_file.close()
    sys_file_path = temp_sys_file.name

    proc = None
    try:
        async with GLOBAL_POOL.lease_worker(preferred_worker=preferred_worker) as worker:
            cmd = build_command(
                worker=worker,
                model=resolved_model,
                effort=selected_effort,
                system_prompt_file=sys_file_path,
                stream=False,
            )

            env = os.environ.copy()
            if worker.get("config_dir"):
                env["CLAUDE_CONFIG_DIR"] = worker["config_dir"]
            env.pop("CLAUDECODE", None)
            env.pop("CLAUDE_CODE_ENTRYPOINT", None)

            logger.info(
                "Executing non-streaming claude on worker=%s with model=%s, effort=%s",
                worker["id"],
                resolved_model,
                selected_effort,
            )

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=config.WORKSPACE_DIR,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                limit=16 * 1024 * 1024,
            )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(input=prompt.encode("utf-8")),
                    timeout=run_timeout,
                )
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                raise TimeoutError(f"Claude worker {worker['id']} timed out after {run_timeout} seconds")

            stdout_str = stdout_bytes.decode("utf-8", errors="replace").strip()
            stderr_str = stderr_bytes.decode("utf-8", errors="replace").strip()

            if proc.returncode != 0:
                logger.error(
                    "Claude worker %s exited with code %s. Stderr: %s. Stdout: %s",
                    worker["id"],
                    proc.returncode,
                    stderr_str,
                    stdout_str,
                )
                raise RuntimeError(
                    f"Claude worker {worker['id']} failed with code {proc.returncode}: {stderr_str or stdout_str}"
                )

            try:
                data = json.loads(stdout_str)
            except json.JSONDecodeError as err:
                logger.error("Failed to parse Claude output as JSON: %s. Output: %s", err, stdout_str)
                raise RuntimeError(f"Invalid JSON returned by Claude: {err}. Output: {stdout_str}")

            if data.get("is_error"):
                err_msg = data.get("result") or data.get("error") or "Unknown Claude error"
                raise RuntimeError(f"Claude error: {err_msg}")

            usage_raw = data.get("usage", {})
            input_tokens = int(usage_raw.get("input_tokens", 0) or 0)
            cache_creation = int(usage_raw.get("cache_creation_input_tokens", 0) or 0)
            cache_read = int(usage_raw.get("cache_read_input_tokens", 0) or 0)
            prompt_tokens = input_tokens + cache_creation + cache_read
            completion_tokens = int(usage_raw.get("output_tokens", 0) or 0)
            total_tokens = prompt_tokens + completion_tokens

            cost_usd = float(data.get("total_cost_usd", 0.0) or 0.0)

            return {
                "text": data.get("result", ""),
                "session_id": data.get("session_id", ""),
                "stop_reason": data.get("stop_reason", "end_turn"),
                "model": resolved_model,
                "worker": worker["id"],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                },
                "cost_usd": cost_usd,
            }
    finally:
        if proc and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            except Exception:
                pass
        if os.path.exists(sys_file_path):
            try:
                os.unlink(sys_file_path)
            except OSError:
                pass


async def run_claude_stream(
    messages: List[Dict[str, Any]],
    model: Optional[str] = None,
    effort: Optional[str] = None,
    timeout: Optional[float] = None,
    preferred_worker: Optional[str] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    Execute headless Claude CLI with stream-json output across the worker pool
    and yield delta/event dictionaries.
    """
    raw_model = model or config.DEFAULT_MODEL
    base_model, suffix_effort = split_effort_suffix(raw_model)
    resolved_model = resolve_model(base_model)
    selected_effort = effort or suffix_effort or config.DEFAULT_EFFORT or None
    run_timeout = float(timeout or config.DEFAULT_TIMEOUT)

    system_prompt, prompt = format_messages(messages)

    temp_sys_file = tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt", encoding="utf-8")
    try:
        temp_sys_file.write(system_prompt)
        temp_sys_file.flush()
    finally:
        temp_sys_file.close()
    sys_file_path = temp_sys_file.name

    proc = None
    try:
        async with GLOBAL_POOL.lease_worker(preferred_worker=preferred_worker) as worker:
            cmd = build_command(
                worker=worker,
                model=resolved_model,
                effort=selected_effort,
                system_prompt_file=sys_file_path,
                stream=True,
            )

            env = os.environ.copy()
            if worker.get("config_dir"):
                env["CLAUDE_CONFIG_DIR"] = worker["config_dir"]
            env.pop("CLAUDECODE", None)
            env.pop("CLAUDE_CODE_ENTRYPOINT", None)

            logger.info(
                "Starting streaming claude on worker=%s with model=%s, effort=%s",
                worker["id"],
                resolved_model,
                selected_effort,
            )

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=config.WORKSPACE_DIR,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                limit=16 * 1024 * 1024,
            )

            # Write prompt to stdin and close stdin
            try:
                if proc.stdin:
                    proc.stdin.write(prompt.encode("utf-8"))
                    await proc.stdin.drain()
                    proc.stdin.close()
                    await proc.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass

            finished = False
            init_yielded = False
            session_id = ""
            latest_stop_reason = "end_turn"
            latest_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            latest_cost_usd = 0.0

            while True:
                try:
                    line_bytes = await asyncio.wait_for(proc.stdout.readline(), timeout=run_timeout)
                except asyncio.TimeoutError:
                    logger.error("Timeout waiting for token stream from Claude worker %s", worker["id"])
                    yield {
                        "type": "error",
                        "error": f"Claude worker {worker['id']} stream timed out after {run_timeout} seconds",
                    }
                    finished = True
                    break

                if not line_bytes:
                    break

                line_str = line_bytes.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue

                try:
                    data = json.loads(line_str)
                except json.JSONDecodeError:
                    continue

                msg_type = data.get("type")

                if msg_type == "system" and data.get("subtype") == "init":
                    session_id = data.get("session_id", "")
                    init_yielded = True
                    yield {
                        "type": "init",
                        "session_id": session_id,
                        "model": resolved_model,
                        "worker": worker["id"],
                    }

                elif msg_type == "stream_event":
                    event = data.get("event", {})
                    event_type = event.get("type")

                    if event_type == "content_block_delta":
                        if not init_yielded:
                            init_yielded = True
                            yield {
                                "type": "init",
                                "session_id": session_id,
                                "model": resolved_model,
                                "worker": worker["id"],
                            }

                        delta = event.get("delta", {})
                        delta_type = delta.get("type")
                        if delta_type == "text_delta":
                            text_delta = delta.get("text", "")
                            if text_delta:
                                yield {"type": "delta", "content": text_delta}
                        elif delta_type == "thinking_delta":
                            thinking_delta = delta.get("thinking", "")
                            if thinking_delta:
                                yield {"type": "reasoning", "content": thinking_delta}

                    elif event_type == "message_delta":
                        delta = event.get("delta", {})
                        if delta.get("stop_reason"):
                            latest_stop_reason = delta["stop_reason"]
                        if event.get("usage"):
                            u = event["usage"]
                            inp = int(u.get("input_tokens", 0) or 0)
                            cc = int(u.get("cache_creation_input_tokens", 0) or 0)
                            cr = int(u.get("cache_read_input_tokens", 0) or 0)
                            prompt_tokens = inp + cc + cr
                            comp_tokens = int(u.get("output_tokens", 0) or 0)
                            latest_usage = {
                                "prompt_tokens": prompt_tokens,
                                "completion_tokens": comp_tokens,
                                "total_tokens": prompt_tokens + comp_tokens,
                            }

                elif msg_type == "result":
                    if not init_yielded:
                        init_yielded = True
                        yield {
                            "type": "init",
                            "session_id": session_id or data.get("session_id", ""),
                            "model": resolved_model,
                            "worker": worker["id"],
                        }

                    if data.get("is_error"):
                        err_text = data.get("result") or data.get("error") or "Claude error"
                        yield {"type": "error", "error": str(err_text)}
                        finished = True
                        break

                    stop_reason = data.get("stop_reason") or latest_stop_reason or "end_turn"
                    finish_reason = "length" if stop_reason == "max_tokens" else "stop"

                    usage_raw = data.get("usage", {})
                    if usage_raw:
                        inp = int(usage_raw.get("input_tokens", 0) or 0)
                        cc = int(usage_raw.get("cache_creation_input_tokens", 0) or 0)
                        cr = int(usage_raw.get("cache_read_input_tokens", 0) or 0)
                        prompt_tokens = inp + cc + cr
                        comp_tokens = int(usage_raw.get("output_tokens", 0) or 0)
                        latest_usage = {
                            "prompt_tokens": prompt_tokens,
                            "completion_tokens": comp_tokens,
                            "total_tokens": prompt_tokens + comp_tokens,
                        }

                    latest_cost_usd = float(data.get("total_cost_usd", 0.0) or 0.0)

                    yield {
                        "type": "finish",
                        "finish_reason": finish_reason,
                        "usage": latest_usage,
                        "cost_usd": latest_cost_usd,
                    }
                    finished = True

            await proc.wait()
            if not finished and proc.returncode != 0:
                stderr_bytes = await proc.stderr.read()
                stderr_str = stderr_bytes.decode("utf-8", errors="replace").strip()
                yield {
                    "type": "error",
                    "error": f"Claude worker {worker['id']} exited with code {proc.returncode}: {stderr_str}",
                }

    except asyncio.CancelledError:
        logger.info("Client disconnected, terminating Claude subprocess PID %s", proc.pid if proc else "unknown")
        if proc and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.sleep(0.5)
                if proc.returncode is None:
                    proc.kill()
            except Exception:
                pass
        raise
    finally:
        if proc and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            except Exception:
                pass
        if os.path.exists(sys_file_path):
            try:
                os.unlink(sys_file_path)
            except OSError:
                pass
