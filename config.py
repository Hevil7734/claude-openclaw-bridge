import os
import shutil
from pathlib import Path
from typing import Any, Dict, List

# Server Network Settings
# STRICT SECURITY POLICY: Bind only to 127.0.0.1 (localhost)
HOST: str = os.getenv("CLAUDE_GW_HOST", "127.0.0.1")
PORT: int = int(os.getenv("CLAUDE_GW_PORT", "8001"))

# Security / Authentication (optional API key)
# If empty or None, requests from localhost are allowed without an API key
API_KEY: str = os.getenv("CLAUDE_GW_API_KEY", "")

# Path to Claude Code CLI binary
_detected_claude = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")
CLAUDE_BIN: str = os.path.expanduser(os.getenv("CLAUDE_GW_BIN", _detected_claude))

# Working directory for claude subprocess; create if missing
WORKSPACE_DIR: str = os.path.expanduser(os.getenv("CLAUDE_GW_WORKSPACE_DIR", str(Path.home())))
try:
    os.makedirs(WORKSPACE_DIR, exist_ok=True)
except Exception:
    pass

# Tool execution & permission settings
ENABLE_TOOLS: bool = os.getenv("CLAUDE_GW_ENABLE_TOOLS", "true").lower() in ("true", "1", "yes")
AUTO_APPROVE: bool = os.getenv("CLAUDE_GW_AUTO_APPROVE", "true").lower() in ("true", "1", "yes")

# Default Model & Reason Effort
DEFAULT_MODEL: str = os.getenv("CLAUDE_GW_DEFAULT_MODEL", "claude-sonnet-5-5")
DEFAULT_EFFORT: str = os.getenv("CLAUDE_GW_DEFAULT_EFFORT", "")  # "" = don't pass --effort

# Process timeout in seconds
DEFAULT_TIMEOUT: int = int(os.getenv("CLAUDE_GW_TIMEOUT", "300"))

# Concurrency & Worker Pool Settings
MAX_CONCURRENCY: int = int(os.getenv("CLAUDE_GW_MAX_CONCURRENCY", "4"))
POOL_DISPATCH_MODE: str = os.getenv("CLAUDE_GW_POOL_DISPATCH_MODE", "least_busy").lower()

# Server Log File
LOG_FILE: str = os.getenv("CLAUDE_GW_LOG_FILE", str(Path(__file__).parent / "claude-gateway.log"))

# ==============================================================================
# MULTI-WORKER POOL CONFIGURATION
# Workers = independent Claude accounts via CLAUDE_CONFIG_DIR.
# Default: one worker using ~/.claude (config_dir=None).
# Extra workers via CLAUDE_GW_WORKERS="claude2:~/.claude-profiles/claude2,claude3:/path"
# ==============================================================================
def _parse_workers() -> List[Dict[str, Any]]:
    workers: List[Dict[str, Any]] = [
        {
            "id": "claude1",
            "name": "Claude Primary",
            "aliases": ["claude", "default", "claude1", "worker1"],
            "bin": CLAUDE_BIN,
            "config_dir": None,
        }
    ]
    raw = os.getenv("CLAUDE_GW_WORKERS", "").strip().strip('"').strip("'")
    if raw:
        for entry in raw.split(","):
            entry = entry.strip()
            if not entry:
                continue
            if ":" in entry:
                w_id, w_dir = entry.split(":", 1)
                w_id = w_id.strip()
                w_dir = str(Path(os.path.expanduser(w_dir.strip())).resolve())
                workers.append({
                    "id": w_id,
                    "name": f"Claude {w_id.capitalize()}",
                    "aliases": [w_id],
                    "bin": CLAUDE_BIN,
                    "config_dir": w_dir,
                })
    return workers

DEFAULT_WORKERS: List[Dict[str, Any]] = _parse_workers()

# Known Claude Models supported by the bridge
KNOWN_MODELS: List[Dict[str, Any]] = [
    {
        "id": "claude-opus-5-5",
        "name": "Claude Opus 5.5",
        "context_window": 200000,
        "max_tokens": 64000,
        "cli_model": "claude-opus-5-5",
    },
    {
        "id": "claude-sonnet-5-5",
        "name": "Claude Sonnet 5.5",
        "context_window": 200000,
        "max_tokens": 64000,
        "cli_model": "claude-sonnet-5-5",
    },
    {
        "id": "claude-haiku-5-5",
        "name": "Claude Haiku 5.5",
        "context_window": 200000,
        "max_tokens": 64000,
        "cli_model": "claude-haiku-5-5",
    },
    {
        "id": "claude-fable-5-1",
        "name": "Claude Fable 5.1",
        "context_window": 200000,
        "max_tokens": 64000,
        "cli_model": "claude-fable-5-1",
    },
    {
        "id": "claude-default",
        "name": "Claude Default (gateway default model)",
        "context_window": 200000,
        "max_tokens": 64000,
        "cli_model": DEFAULT_MODEL,
    },
]

# Model aliases mapping shorthand/convenience names to canonical IDs
MODEL_ALIASES: Dict[str, str] = {
    "opus": "claude-opus-5-5",
    "sonnet": "claude-sonnet-5-5",
    "haiku": "claude-haiku-5-5",
    "fable": "claude-fable-5-1",
    "claude-opus": "claude-opus-5-5",
    "claude-sonnet": "claude-sonnet-5-5",
    "claude-haiku": "claude-haiku-5-5",
    "claude-fable": "claude-fable-5-1",
    "default": DEFAULT_MODEL,
    "claude-default": DEFAULT_MODEL,
    "auto": DEFAULT_MODEL,
}
