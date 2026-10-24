# 🚀 Claude Code CLI-to-API Gateway for OpenClaw

An OpenAI-compatible REST API gateway wrapping the **Claude Code CLI (`claude -p`)**, purpose-built to integrate Anthropic Claude models directly into **OpenClaw** (and any other OpenAI-compatible client).

---

## 🌟 Key Features

* **⚡ Headless Claude CLI Execution**:
  * Executes non-interactive requests via `claude -p` passing prompts on **stdin** (avoiding shell argv length limitations).
  * Runs with `--tools ""` to disable built-in local tools, ensuring safe, pure completion backend responses without unwanted local bash or filesystem actions.
  * Disables user hooks and MCP configurations with `--setting-sources ""` and `--strict-mcp-config`.
* **🧹 Zero Session Bloat**:
  * Uses `--no-session-persistence` so no throwaway conversation or project files are saved to `~/.claude/projects/`.
* **OpenAI-Compatible Endpoints**:
  * `POST /v1/chat/completions` (Both Server-Sent Events SSE streaming and non-streaming JSON)
  * `POST /v1/completions` (Legacy single-prompt completions)
  * `GET  /v1/models` & `GET /v1/models/{model_id}`
  * `GET  /health` & `GET /`
* **📡 Real-Time SSE Streaming**:
  * Streams tokens live over `text/event-stream` using Claude CLI's `stream-json` events (`text_delta` and `thinking_delta` forwarded as `reasoning_content`).
  * Emits standard `data: [DONE]` on completion and handles client disconnections gracefully.
* **👥 Multi-Worker Account Pool**:
  * Connects multiple independent Claude accounts via separate profile directories (`CLAUDE_CONFIG_DIR`).
  * Concurrency load balancing (`least_busy` or `round_robin`).
  * Direct worker routing via `model@<worker_id>` or the `X-Claude-Worker` header.
* **🧠 Model & Reasoning Effort Support**:
  * Built-in support for `claude-opus-5-5`, `claude-sonnet-5-5`, `claude-haiku-5-5`, and `claude-fable-5-1`.
  * Convenient aliases (`opus`, `sonnet`, `haiku`, `fable`, `default`).
  * Supports reasoning effort levels (`low`, `medium`, `high`, `xhigh`, `max`) via model suffixes (e.g. `claude-opus-5-5-high`) or request parameters.
* **🔒 Strict Security Compliance**:
  * Binds strictly to `127.0.0.1` (localhost only) on port `8001`.

---

## 🛠️ Quick Start

### 1. Manage the Gateway with `claude-api`

The `bin/claude-api` executable provides command-line lifecycle management:

```bash
# Add bin to your PATH or run directly
./bin/claude-api start

# Check process status and /health endpoint
./bin/claude-api status

# View recent gateway logs
./bin/claude-api logs -n 50
```

To run in the foreground for debugging:

```bash
./bin/claude-api start --foreground
```

### 2. Run Self-Tests

Verify health check, model discovery, non-streaming completions, and streaming SSE tokens:

```bash
./bin/claude-api test
# or directly:
python3 test_client.py
```

### 3. Stop or Restart

```bash
# Stop daemon
./bin/claude-api stop

# Restart daemon
./bin/claude-api restart
```

---

## 🤖 OpenClaw Integration Guide

OpenClaw connects to external models via custom provider configuration.

### Step 1: Add Claude Provider to OpenClaw

Add the following provider configuration to your OpenClaw settings (e.g. `~/.openclaw/openclaw.json` or `~/.openclaw/agents/<agentId>/agent/models.json`):

```json
{
  "models": {
    "mode": "merge",
    "providers": {
      "claude": {
        "baseUrl": "http://127.0.0.1:8001/v1",
        "apiKey": "claude-local",
        "api": "openai-completions",
        "models": [
          {
            "id": "claude-sonnet-5-5",
            "name": "Claude Sonnet 5.5 (Claude Gateway)",
            "contextWindow": 200000,
            "maxTokens": 64000,
            "reasoning": true
          },
          {
            "id": "claude-opus-5-5",
            "name": "Claude Opus 5.5 (Claude Gateway)",
            "contextWindow": 200000,
            "maxTokens": 64000,
            "reasoning": true
          },
          {
            "id": "claude-haiku-5-5",
            "name": "Claude Haiku 5.5 (Claude Gateway)",
            "contextWindow": 200000,
            "maxTokens": 64000
          },
          {
            "id": "claude-fable-5-1",
            "name": "Claude Fable 5.1 (Claude Gateway)",
            "contextWindow": 200000,
            "maxTokens": 64000,
            "reasoning": true
          },
          {
            "id": "claude-default",
            "name": "Claude Default (Gateway Default Model)",
            "contextWindow": 200000,
            "maxTokens": 64000,
            "reasoning": true
          }
        ]
      }
    }
  },
  "agents": {
    "defaults": {
      "models": {
        "claude/claude-sonnet-5-5": {
          "alias": "claude"
        }
      }
    }
  }
}
```

You can print this snippet at any time by running:

```bash
./bin/claude-api config
```

### Step 2: Apply Configuration in OpenClaw

Reload or apply the configuration:

```bash
openclaw gateway config.apply --file ~/.openclaw/openclaw.json
# or list active models
openclaw models list
```

---

## ⚙️ Configuration & Environment Variables

All settings can be customized via environment variables or a `.env` file:

| Variable | Default | Description |
| :--- | :--- | :--- |
| `CLAUDE_GW_HOST` | `127.0.0.1` | Binding address (strictly localhost for security) |
| `CLAUDE_GW_PORT` | `8001` | Local listening port |
| `CLAUDE_GW_API_KEY` | *(None)* | Optional API token authentication (empty = no auth required) |
| `CLAUDE_GW_BIN` | `claude` | Path to Claude Code CLI binary |
| `CLAUDE_GW_WORKSPACE_DIR` | `/tmp/claude-gateway` | Subprocess working directory |
| `CLAUDE_GW_DEFAULT_MODEL` | `claude-sonnet-5-5` | Default model if omitted in request |
| `CLAUDE_GW_DEFAULT_EFFORT`| *(None)* | Default reasoning effort (`low`, `medium`, `high`, `xhigh`, `max`) |
| `CLAUDE_GW_TIMEOUT` | `300` | Subprocess timeout in seconds |
| `CLAUDE_GW_MAX_CONCURRENCY` | `4` | Max concurrent requests per worker |
| `CLAUDE_GW_POOL_DISPATCH_MODE`| `least_busy` | Dispatch strategy: `least_busy` or `round_robin` |
| `CLAUDE_GW_WORKERS` | *(None)* | Extra worker accounts (`worker_id:config_dir,...`) |
| `CLAUDE_GW_LOG_FILE` | `<project>/claude-gateway.log` | Log file location |

---

## 🔄 Running as a Systemd Service

To run the Claude Gateway automatically as a background systemd user service:

```bash
# 1. Install user service unit
./bin/claude-api install-service

# 2. Start and enable on boot
systemctl --user start claude-api
systemctl --user enable claude-api

# 3. Check service status
systemctl --user status claude-api
```
