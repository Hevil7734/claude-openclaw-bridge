#!/usr/bin/env python3
"""
OpenAI-compatible REST Gateway for Claude Code CLI (claude -p).
Wraps the Claude CLI headless interface to provide OpenAI-compatible
chat completions, legacy completions, model listing, and health checks.
"""

import asyncio
import json
import logging
import os
import time
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from aiohttp import web

import config
from claude_runner import (
    GLOBAL_POOL,
    resolve_model,
    split_effort_suffix,
    run_claude_non_stream,
    run_claude_stream,
)

# -----------------------------------------------------------------------------
# Logging Configuration
# -----------------------------------------------------------------------------
log_file_path = getattr(config, "LOG_FILE", None) or os.getenv(
    "CLAUDE_GW_LOG_FILE",
    str(Path(__file__).resolve().parent / "claude-gateway.log"),
)
try:
    Path(log_file_path).parent.mkdir(parents=True, exist_ok=True)
except Exception:
    pass

logger = logging.getLogger("claude_api_server")
logger.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

try:
    file_handler = RotatingFileHandler(log_file_path, maxBytes=5 * 1024 * 1024, backupCount=3)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
except Exception as e:
    logger.warning("Could not set up file logger at %s: %s", log_file_path, e)


# -----------------------------------------------------------------------------
# Authentication & CORS Middleware
# -----------------------------------------------------------------------------
def get_auth_error(message: str = "Invalid API key") -> web.Response:
    """Return standard OpenAI-compatible 401 error response."""
    return web.json_response(
        {
            "error": {
                "message": message,
                "type": "invalid_request_error",
                "code": "invalid_api_key",
            }
        },
        status=401,
        headers={"Access-Control-Allow-Origin": "*"},
    )


@web.middleware
async def cors_and_auth_middleware(request: web.Request, handler):
    """
    Middleware handling CORS preflight (OPTIONS) and API key authentication.
    Endpoints /, /health, and /v1/health are always unauthenticated.
    """
    if request.method == "OPTIONS":
        return web.Response(
            status=204,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": (
                    "Content-Type, Authorization, X-Api-Key, x-api-key, "
                    "X-Claude-Worker, X-Session-Id, X-Conversation-Id"
                ),
                "Access-Control-Max-Age": "86400",
            },
        )

    # Check API key if configured
    open_paths = {"/", "/health", "/v1/health"}
    configured_key = getattr(config, "API_KEY", "")
    if configured_key and request.path not in open_paths:
        auth_header = request.headers.get("Authorization", "").strip()
        token = ""
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
        elif auth_header:
            token = auth_header

        if not token:
            token = (
                request.headers.get("x-api-key", "").strip()
                or request.headers.get("X-Api-Key", "").strip()
            )

        if token != configured_key:
            logger.warning("Unauthorized access attempt to %s", request.path)
            return get_auth_error()

    response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


# -----------------------------------------------------------------------------
# Handlers: Root & Health
# -----------------------------------------------------------------------------
async def handle_root(request: web.Request) -> web.Response:
    """Root status endpoint reporting gateway service info and pool status."""
    return web.json_response({
        "service": "claude-openclaw-bridge",
        "description": "OpenAI-compatible Multi-Worker Gateway for Claude Code CLI (claude -p)",
        "status": "online",
        "version": "1.0.0",
        "default_model": getattr(config, "DEFAULT_MODEL", "claude-sonnet-5-5"),
        "pool": GLOBAL_POOL.get_status(),
        "endpoints": [
            "GET  /v1/models",
            "GET  /v1/models/{model_id}",
            "POST /v1/chat/completions",
            "POST /v1/completions",
            "GET  /health",
        ],
    })


async def handle_health(request: web.Request) -> web.Response:
    """Health check endpoint reporting gateway readiness and worker pool load."""
    return web.json_response({
        "status": "ok",
        "service": "claude-openclaw-bridge",
        "claude_bin": getattr(config, "CLAUDE_BIN", "claude"),
        "pool": GLOBAL_POOL.get_status(),
    })


# -----------------------------------------------------------------------------
# Handlers: Model Catalog
# -----------------------------------------------------------------------------
async def handle_list_models(request: web.Request) -> web.Response:
    """OpenAI-compatible GET /v1/models endpoint."""
    models_data = []
    created_time = int(time.time())

    known_models = getattr(config, "KNOWN_MODELS", [])
    for m in known_models:
        models_data.append({
            "id": m["id"],
            "object": "model",
            "created": created_time,
            "owned_by": "anthropic",
            "permission": [],
            "root": m["id"],
            "parent": None,
            "context_window": m.get("context_window", 200000),
            "max_tokens": m.get("max_tokens", 64000),
        })

    # Add per-worker model variants if multiple workers configured
    workers = getattr(config, "DEFAULT_WORKERS", [])
    if len(workers) > 1:
        for w in workers:
            w_id = w["id"]
            for m in known_models:
                models_data.append({
                    "id": f"{m['id']}@{w_id}",
                    "object": "model",
                    "created": created_time,
                    "owned_by": "anthropic",
                    "permission": [],
                    "root": m["id"],
                    "parent": None,
                    "context_window": m.get("context_window", 200000),
                    "max_tokens": m.get("max_tokens", 64000),
                })

    return web.json_response({
        "object": "list",
        "data": models_data,
    })


async def handle_retrieve_model(request: web.Request) -> web.Response:
    """OpenAI-compatible GET /v1/models/{model_id} endpoint."""
    model_id = request.match_info.get("model_id", "")
    target = model_id
    if "/" in target:
        target = target.split("/", 1)[1]

    # Handle @worker suffix if present
    base_id = target.split("@", 1)[0] if "@" in target else target
    resolved = resolve_model(base_id)

    known_models = getattr(config, "KNOWN_MODELS", [])
    matched = next((m for m in known_models if m["id"] == resolved or m["id"] == base_id), None)

    if not matched:
        return web.json_response(
            {
                "error": {
                    "message": f"The model '{model_id}' does not exist",
                    "type": "invalid_request_error",
                    "param": "model",
                    "code": "model_not_found",
                }
            },
            status=404,
        )

    return web.json_response({
        "id": model_id,
        "object": "model",
        "created": int(time.time()),
        "owned_by": "anthropic",
        "permission": [],
        "root": matched["id"],
        "parent": None,
        "context_window": matched.get("context_window", 200000),
        "max_tokens": matched.get("max_tokens", 64000),
    })


# -----------------------------------------------------------------------------
# Request Parameter Parsing Helper
# -----------------------------------------------------------------------------
def parse_chat_request(
    body: Dict[str, Any],
    headers: Any,
) -> Tuple[str, Optional[str], Optional[str], float, bool, bool]:
    """Extract and normalize model, worker, effort, timeout, and streaming settings."""
    raw_model = body.get("model") or getattr(config, "DEFAULT_MODEL", "claude-sonnet-5-5")
    if not isinstance(raw_model, str):
        raw_model = str(raw_model)

    # Strip provider/ prefix if present (e.g. claude/claude-sonnet-5-5)
    if "/" in raw_model:
        raw_model = raw_model.split("/", 1)[1]

    preferred_worker = headers.get("X-Claude-Worker") or body.get("worker")
    if not preferred_worker and "@" in raw_model:
        raw_model, preferred_worker = raw_model.split("@", 1)

    base_model, model_effort = split_effort_suffix(raw_model)
    resolved_model = resolve_model(base_model)

    effort = (
        body.get("reasoning_effort")
        or body.get("effort")
        or model_effort
        or getattr(config, "DEFAULT_EFFORT", None)
        or None
    )
    if not effort:
        effort = None

    timeout_val = body.get("timeout")
    if timeout_val is not None:
        try:
            timeout = float(timeout_val)
        except (ValueError, TypeError):
            timeout = float(getattr(config, "DEFAULT_TIMEOUT", 300))
    else:
        timeout = float(getattr(config, "DEFAULT_TIMEOUT", 300))

    stream = bool(body.get("stream", False))
    stream_options = body.get("stream_options") or {}
    include_usage = bool(stream_options.get("include_usage", False))

    return resolved_model, preferred_worker, effort, timeout, stream, include_usage


# -----------------------------------------------------------------------------
# Handlers: Chat Completions
# -----------------------------------------------------------------------------
async def handle_chat_completions(request: web.Request) -> web.StreamResponse:
    """OpenAI-compatible POST /v1/chat/completions endpoint."""
    try:
        body = await request.json()
    except Exception as e:
        return web.json_response(
            {"error": {"message": f"Invalid JSON body: {str(e)}", "type": "invalid_request_error"}},
            status=400,
        )

    messages = body.get("messages")
    if not messages or not isinstance(messages, list):
        return web.json_response(
            {
                "error": {
                    "message": "'messages' array is required and cannot be empty",
                    "type": "invalid_request_error",
                }
            },
            status=400,
        )

    (
        resolved_model,
        preferred_worker,
        effort,
        timeout,
        stream,
        include_usage,
    ) = parse_chat_request(body, request.headers)

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created_ts = int(time.time())
    start_time = time.time()

    # -------------------------------------------------------------------------
    # Non-Streaming Flow
    # -------------------------------------------------------------------------
    if not stream:
        logger.info(
            "Chat completion request: model=%s (worker=%s, effort=%s, stream=False)",
            resolved_model,
            preferred_worker or "auto-pool",
            effort,
        )
        try:
            res = await run_claude_non_stream(
                messages=messages,
                model=resolved_model,
                effort=effort,
                timeout=timeout,
                preferred_worker=preferred_worker,
            )
            worker_id = res.get("worker", "")
            stop_reason = res.get("stop_reason", "end_turn")
            finish_reason = "length" if stop_reason == "max_tokens" else "stop"

            duration = round(time.time() - start_time, 2)
            logger.info(
                "Completed non-stream in %.2fs (worker=%s, model=%s)",
                duration,
                worker_id,
                resolved_model,
            )

            res_payload = {
                "id": completion_id,
                "object": "chat.completion",
                "created": created_ts,
                "model": res.get("model", resolved_model),
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": res.get("text", ""),
                        },
                        "logprobs": None,
                        "finish_reason": finish_reason,
                    }
                ],
                "usage": res.get(
                    "usage",
                    {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                    },
                ),
                "x_claude": {
                    "worker": worker_id,
                    "session_id": res.get("session_id", ""),
                    "cost_usd": res.get("cost_usd", 0.0),
                },
            }

            headers = {}
            if worker_id:
                headers["X-Claude-Worker"] = worker_id

            return web.json_response(res_payload, headers=headers)

        except (asyncio.TimeoutError, TimeoutError) as ex:
            logger.warning("Claude request timed out after %ss: %s", timeout, ex)
            return web.json_response(
                {
                    "error": {
                        "message": f"Claude execution timed out after {timeout}s",
                        "type": "timeout_error",
                        "code": "gateway_timeout",
                    }
                },
                status=504,
            )
        except RuntimeError as ex:
            logger.error("Claude runtime error: %s", ex)
            return web.json_response(
                {
                    "error": {
                        "message": str(ex),
                        "type": "api_error",
                        "code": "bad_gateway",
                    }
                },
                status=502,
            )
        except Exception as ex:
            logger.exception("Unexpected error during non-streaming chat completion: %s", ex)
            return web.json_response(
                {
                    "error": {
                        "message": str(ex),
                        "type": "api_error",
                        "code": "internal_error",
                    }
                },
                status=500,
            )

    # -------------------------------------------------------------------------
    # Streaming Flow (SSE)
    # -------------------------------------------------------------------------
    logger.info(
        "Chat completion request: model=%s (worker=%s, effort=%s, stream=True)",
        resolved_model,
        preferred_worker or "auto-pool",
        effort,
    )
    sse_response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream; charset=utf-8",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
        },
    )
    await sse_response.prepare(request)

    # Initial chunk establishing assistant role
    initial_chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created_ts,
        "model": resolved_model,
        "choices": [
            {
                "index": 0,
                "delta": {
                    "role": "assistant",
                },
                "logprobs": None,
                "finish_reason": None,
            }
        ],
    }
    await sse_response.write(f"data: {json.dumps(initial_chunk)}\n\n".encode("utf-8"))

    current_model = resolved_model
    current_worker = preferred_worker or ""
    stream_gen = run_claude_stream(
        messages=messages,
        model=resolved_model,
        effort=effort,
        timeout=timeout,
        preferred_worker=preferred_worker,
    )

    try:
        async for event in stream_gen:
            event_type = event.get("type")
            if event_type == "init":
                current_model = event.get("model", current_model)
                current_worker = event.get("worker", current_worker)

            elif event_type == "delta":
                chunk = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created_ts,
                    "model": current_model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "content": event.get("content", ""),
                            },
                            "logprobs": None,
                            "finish_reason": None,
                        }
                    ],
                }
                await sse_response.write(f"data: {json.dumps(chunk)}\n\n".encode("utf-8"))

            elif event_type == "reasoning":
                chunk = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created_ts,
                    "model": current_model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "reasoning_content": event.get("content", ""),
                            },
                            "logprobs": None,
                            "finish_reason": None,
                        }
                    ],
                }
                await sse_response.write(f"data: {json.dumps(chunk)}\n\n".encode("utf-8"))

            elif event_type == "finish":
                finish_chunk = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created_ts,
                    "model": current_model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "logprobs": None,
                            "finish_reason": event.get("finish_reason", "stop"),
                        }
                    ],
                }
                await sse_response.write(f"data: {json.dumps(finish_chunk)}\n\n".encode("utf-8"))

                if include_usage and event.get("usage"):
                    usage_chunk = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created_ts,
                        "model": current_model,
                        "choices": [],
                        "usage": event.get("usage"),
                    }
                    await sse_response.write(f"data: {json.dumps(usage_chunk)}\n\n".encode("utf-8"))

            elif event_type == "error":
                err_msg = event.get("error", "Stream error")
                err_chunk = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created_ts,
                    "model": current_model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "content": f"[Claude gateway error] {err_msg}",
                            },
                            "logprobs": None,
                            "finish_reason": "stop",
                        }
                    ],
                }
                await sse_response.write(f"data: {json.dumps(err_chunk)}\n\n".encode("utf-8"))
                break

        # Standard OpenAI SSE completion delimiter
        try:
            await sse_response.write(b"data: [DONE]\n\n")
        except Exception:
            pass

    except (ConnectionResetError, aiohttp.ClientConnectionResetError):
        logger.info("SSE client disconnected from stream: %s", completion_id)
    except asyncio.CancelledError:
        logger.info("SSE stream cancelled: %s", completion_id)
        raise
    except Exception as ex:
        logger.exception("Error during SSE stream: %s", ex)
        try:
            err_chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created_ts,
                "model": current_model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "content": f"[Claude gateway error] {str(ex)}",
                        },
                        "logprobs": None,
                        "finish_reason": "stop",
                    }
                ],
            }
            await sse_response.write(f"data: {json.dumps(err_chunk)}\n\n".encode("utf-8"))
            await sse_response.write(b"data: [DONE]\n\n")
        except Exception:
            pass
    finally:
        try:
            await stream_gen.aclose()
        except Exception:
            pass
        duration = round(time.time() - start_time, 2)
        logger.info(
            "Completed stream in %.2fs (worker=%s, model=%s)",
            duration,
            current_worker,
            current_model,
        )

    return sse_response


# -----------------------------------------------------------------------------
# Handlers: Legacy Completions
# -----------------------------------------------------------------------------
async def handle_completions(request: web.Request) -> web.StreamResponse:
    """OpenAI-compatible POST /v1/completions (legacy format adapter)."""
    try:
        body = await request.json()
    except Exception as e:
        return web.json_response(
            {"error": {"message": f"Invalid JSON body: {str(e)}", "type": "invalid_request_error"}},
            status=400,
        )

    prompt = body.get("prompt", "")
    if isinstance(prompt, list):
        prompt = "\n".join(str(p) for p in prompt)
    else:
        prompt = str(prompt)

    messages = [{"role": "user", "content": prompt}]
    (
        resolved_model,
        preferred_worker,
        effort,
        timeout,
        stream,
        include_usage,
    ) = parse_chat_request(body, request.headers)

    completion_id = f"cmpl-{uuid.uuid4().hex[:12]}"
    created_ts = int(time.time())
    start_time = time.time()

    if not stream:
        try:
            res = await run_claude_non_stream(
                messages=messages,
                model=resolved_model,
                effort=effort,
                timeout=timeout,
                preferred_worker=preferred_worker,
            )
            worker_id = res.get("worker", "")
            stop_reason = res.get("stop_reason", "end_turn")
            finish_reason = "length" if stop_reason == "max_tokens" else "stop"

            res_payload = {
                "id": completion_id,
                "object": "text_completion",
                "created": created_ts,
                "model": res.get("model", resolved_model),
                "choices": [
                    {
                        "index": 0,
                        "text": res.get("text", ""),
                        "logprobs": None,
                        "finish_reason": finish_reason,
                    }
                ],
                "usage": res.get(
                    "usage",
                    {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                    },
                ),
            }
            headers = {}
            if worker_id:
                headers["X-Claude-Worker"] = worker_id
            return web.json_response(res_payload, headers=headers)

        except (asyncio.TimeoutError, TimeoutError) as ex:
            return web.json_response(
                {
                    "error": {
                        "message": f"Claude execution timed out after {timeout}s",
                        "type": "timeout_error",
                        "code": "gateway_timeout",
                    }
                },
                status=504,
            )
        except RuntimeError as ex:
            return web.json_response(
                {
                    "error": {
                        "message": str(ex),
                        "type": "api_error",
                        "code": "bad_gateway",
                    }
                },
                status=502,
            )
        except Exception as ex:
            return web.json_response(
                {
                    "error": {
                        "message": str(ex),
                        "type": "api_error",
                        "code": "internal_error",
                    }
                },
                status=500,
            )

    # Legacy streaming response
    sse_response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream; charset=utf-8",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
        },
    )
    await sse_response.prepare(request)

    current_model = resolved_model
    stream_gen = run_claude_stream(
        messages=messages,
        model=resolved_model,
        effort=effort,
        timeout=timeout,
        preferred_worker=preferred_worker,
    )

    try:
        async for event in stream_gen:
            event_type = event.get("type")
            if event_type == "init":
                current_model = event.get("model", current_model)
            elif event_type == "delta":
                chunk = {
                    "id": completion_id,
                    "object": "text_completion",
                    "created": created_ts,
                    "model": current_model,
                    "choices": [
                        {
                            "index": 0,
                            "text": event.get("content", ""),
                            "logprobs": None,
                            "finish_reason": None,
                        }
                    ],
                }
                await sse_response.write(f"data: {json.dumps(chunk)}\n\n".encode("utf-8"))
            elif event_type == "finish":
                finish_chunk = {
                    "id": completion_id,
                    "object": "text_completion",
                    "created": created_ts,
                    "model": current_model,
                    "choices": [
                        {
                            "index": 0,
                            "text": "",
                            "logprobs": None,
                            "finish_reason": event.get("finish_reason", "stop"),
                        }
                    ],
                }
                if include_usage and event.get("usage"):
                    finish_chunk["usage"] = event.get("usage")
                await sse_response.write(f"data: {json.dumps(finish_chunk)}\n\n".encode("utf-8"))
            elif event_type == "error":
                err_msg = event.get("error", "Stream error")
                err_chunk = {
                    "id": completion_id,
                    "object": "text_completion",
                    "created": created_ts,
                    "model": current_model,
                    "choices": [
                        {
                            "index": 0,
                            "text": f"[Claude gateway error] {err_msg}",
                            "logprobs": None,
                            "finish_reason": "stop",
                        }
                    ],
                }
                await sse_response.write(f"data: {json.dumps(err_chunk)}\n\n".encode("utf-8"))
                break

        try:
            await sse_response.write(b"data: [DONE]\n\n")
        except Exception:
            pass

    except (ConnectionResetError, aiohttp.ClientConnectionResetError):
        logger.info("SSE client disconnected from completions stream: %s", completion_id)
    except asyncio.CancelledError:
        logger.info("SSE completions stream cancelled: %s", completion_id)
        raise
    except Exception as ex:
        logger.exception("Error during legacy completions SSE stream: %s", ex)
        try:
            err_chunk = {
                "id": completion_id,
                "object": "text_completion",
                "created": created_ts,
                "model": current_model,
                "choices": [
                    {
                        "index": 0,
                        "text": f"[Claude gateway error] {str(ex)}",
                        "logprobs": None,
                        "finish_reason": "stop",
                    }
                ],
            }
            await sse_response.write(f"data: {json.dumps(err_chunk)}\n\n".encode("utf-8"))
            await sse_response.write(b"data: [DONE]\n\n")
        except Exception:
            pass
    finally:
        try:
            await stream_gen.aclose()
        except Exception:
            pass

    return sse_response


# -----------------------------------------------------------------------------
# Application Factory & Main Entrypoint
# -----------------------------------------------------------------------------
def create_app() -> web.Application:
    """Create and configure the aiohttp web application."""
    app = web.Application(middlewares=[cors_and_auth_middleware])
    app.router.add_get("/", handle_root)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/v1/health", handle_health)
    app.router.add_get("/v1/models", handle_list_models)
    app.router.add_get("/v1/models/{model_id}", handle_retrieve_model)
    app.router.add_post("/v1/chat/completions", handle_chat_completions)
    app.router.add_post("/v1/completions", handle_completions)
    return app


def main():
    """Start the Claude Gateway server."""
    app = create_app()
    host = getattr(config, "HOST", "127.0.0.1")
    port = getattr(config, "PORT", 8001)
    default_model = getattr(config, "DEFAULT_MODEL", "claude-sonnet-5-5")

    print("=" * 60)
    print("🚀 Claude CLI-to-API Bridge Server (OpenClaw compatible)")
    print(f"🔒 Host: {host} (Strict Localhost Policy)")
    print(f"🌐 Port: {port}")
    print(f"🤖 Default Model: {default_model}")
    print("=" * 60)

    web.run_app(app, host=host, port=port)


if __name__ == "__main__":
    main()
