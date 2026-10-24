#!/usr/bin/env python3
import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PID_FILE = BASE_DIR / ".claude-gateway.pid"


def get_server_config():
    host = os.getenv("CLAUDE_GW_HOST", "127.0.0.1")
    port = int(os.getenv("CLAUDE_GW_PORT", "8001"))
    log_file = Path(os.getenv("CLAUDE_GW_LOG_FILE", str(BASE_DIR / "claude-gateway.log")))

    try:
        if str(BASE_DIR) not in sys.path:
            sys.path.insert(0, str(BASE_DIR))
        import config
        host = getattr(config, "HOST", host)
        port = getattr(config, "PORT", port)
        log_file = Path(getattr(config, "LOG_FILE", str(log_file)))
    except Exception:
        pass

    return host, port, log_file


def is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def get_running_pid() -> int | None:
    if PID_FILE.exists():
        try:
            with open(PID_FILE, "r") as f:
                pid = int(f.read().strip())
            if is_running(pid):
                return pid
            else:
                PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    # Fallback: check if server.py in this repo is running
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", "python3.*claude-openclaw-bridge/server.py"]
        ).decode().strip()
        pids = [int(p) for p in out.split() if p.isdigit() and int(p) != os.getpid()]
        if pids:
            return pids[0]
    except Exception:
        pass

    return None


def start_server(foreground: bool = False):
    pid = get_running_pid()
    if pid:
        print(f"⚠️  Claude Gateway is already running (PID: {pid}).")
        return

    host, port, log_file = get_server_config()

    if foreground:
        print(f"🚀 Starting Claude Gateway in foreground mode on http://{host}:{port}...")
        try:
            if str(BASE_DIR) not in sys.path:
                sys.path.insert(0, str(BASE_DIR))
            import server
            server.main()
        except KeyboardInterrupt:
            print("\n🛑 Gateway stopped by user.")
        return

    log_file.parent.mkdir(parents=True, exist_ok=True)
    # Raw stdout/stderr (crash tracebacks) go to a separate file; server.py's
    # RotatingFileHandler already writes log_file, so sharing it duplicates every line.
    out_file = log_file.with_suffix(".out")
    log_fd = open(out_file, "a")

    print("🚀 Starting Claude Gateway daemon in background...")
    proc = subprocess.Popen(
        [sys.executable, str(BASE_DIR / "server.py")],
        stdout=log_fd,
        stderr=log_fd,
        cwd=str(BASE_DIR),
        start_new_session=True,
    )

    with open(PID_FILE, "w") as f:
        f.write(str(proc.pid))

    time.sleep(1.0)
    if is_running(proc.pid):
        print(f"✅ Claude Gateway running successfully (PID: {proc.pid})")
        print(f"📄 Logs: {log_file}")
        print(f"🌐 Endpoint: http://{host}:{port}/v1")
    else:
        print("❌ Server failed to start. Recent logs:")
        for f_path in (log_file, log_file.with_suffix(".out")):
            if f_path.exists():
                with open(f_path, "r") as f:
                    lines = f.readlines()
                    print("".join(lines[-15:]))


def stop_server():
    pid = get_running_pid()
    if not pid:
        print("ℹ️  Claude Gateway is not running.")
        return

    print(f"🛑 Stopping Claude Gateway (PID: {pid})...")
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(30):
            time.sleep(0.1)
            if not is_running(pid):
                break
        if is_running(pid):
            os.kill(pid, signal.SIGKILL)
        PID_FILE.unlink(missing_ok=True)
        print("✅ Server stopped.")
    except Exception as e:
        print(f"Error stopping process: {e}")


def restart_server(foreground: bool = False):
    stop_server()
    time.sleep(1)
    start_server(foreground=foreground)


def check_status():
    pid = get_running_pid()
    host, port, _ = get_server_config()

    if pid:
        print(f"🟢 Claude Gateway is RUNNING (PID: {pid})")
        print(f"🌐 Base URL: http://{host}:{port}/v1")
        try:
            req = urllib.request.Request(f"http://{host}:{port}/health")
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode())
                print(f"📊 Health: {data.get('status')}")
                if "service" in data:
                    print(f"📦 Service: {data.get('service')}")
                if "claude_bin" in data:
                    print(f"⚙️  Claude Binary: {data.get('claude_bin')}")

                pool = data.get("pool", {})
                workers = pool.get("workers", [])
                if workers:
                    mode = pool.get("dispatch_mode", "least_busy")
                    print(f"⚡ Worker Pool ({len(workers)} worker(s), mode: {mode}):")
                    for w in workers:
                        w_id = w.get("id", "worker")
                        w_name = w.get("name", "Claude")
                        w_cfg = w.get("config_dir") or "default (~/.claude)"
                        served = w.get("total_served", 0)
                        active = w.get("active_requests", 0)
                        print(f"   • {w_id:<8} [{w_name} | {w_cfg}]: {served} served, {active} active")
        except Exception as e:
            print(f"⚠️  Warning: Service did not respond to /health: {e}")
    else:
        print("🔴 Claude Gateway is STOPPED.")


def run_tests():
    print("🧪 Running Claude Gateway end-to-end tests...")
    test_script = BASE_DIR / "test_client.py"
    if not test_script.exists():
        print(f"❌ Test script not found at {test_script}")
        sys.exit(1)
    res = subprocess.run([sys.executable, str(test_script)])
    sys.exit(res.returncode)


def print_openclaw_config():
    config_file = BASE_DIR / "openclaw.config.json"
    if config_file.exists():
        print("============================================================")
        print("📋 OpenClaw Configuration Snippet")
        print("Add this to ~/.openclaw/openclaw.json or models.json:")
        print("============================================================")
        with open(config_file, "r") as f:
            print(f.read())
        print("============================================================")
    else:
        print(f"❌ Configuration file not found at {config_file}")


def show_logs(lines: int = 50):
    _, _, log_file = get_server_config()
    if not log_file.exists():
        print(f"ℹ️  No log file found at {log_file}")
        return

    print(f"📄 Showing last {lines} lines of {log_file}:\n")
    try:
        with open(log_file, "r") as f:
            all_lines = f.readlines()
            print("".join(all_lines[-lines:]))
    except Exception as e:
        print(f"❌ Error reading log file: {e}")


def install_systemd_service():
    service_dir = Path.home() / ".config" / "systemd" / "user"
    service_dir.mkdir(parents=True, exist_ok=True)
    target_service = service_dir / "claude-api.service"
    source_service = BASE_DIR / "claude-api.service"

    if not source_service.exists():
        print(f"❌ Source service file not found at {source_service}")
        return

    with open(source_service, "r") as f:
        content = f.read()
    with open(target_service, "w") as f:
        f.write(content)

    subprocess.run(["systemctl", "--user", "daemon-reload"])
    print(f"✅ Systemd service installed at {target_service}")
    print("You can manage it with:")
    print("  systemctl --user start claude-api")
    print("  systemctl --user enable claude-api")
    print("  systemctl --user status claude-api")


def main():
    parser = argparse.ArgumentParser(description="Claude CLI-to-API Bridge Manager")
    parser.add_argument(
        "command",
        choices=[
            "start",
            "stop",
            "restart",
            "status",
            "test",
            "config",
            "logs",
            "install-service",
        ],
        help="Command to run",
    )
    parser.add_argument(
        "-f",
        "--foreground",
        action="store_true",
        help="Run in foreground instead of daemon",
    )
    parser.add_argument(
        "-n",
        "--lines",
        type=int,
        default=50,
        help="Number of log lines to show (for logs command)",
    )

    args = parser.parse_args()

    if args.command == "start":
        start_server(foreground=args.foreground)
    elif args.command == "stop":
        stop_server()
    elif args.command == "restart":
        restart_server(foreground=args.foreground)
    elif args.command == "status":
        check_status()
    elif args.command == "test":
        run_tests()
    elif args.command == "config":
        print_openclaw_config()
    elif args.command == "logs":
        show_logs(lines=args.lines)
    elif args.command == "install-service":
        install_systemd_service()


if __name__ == "__main__":
    main()
