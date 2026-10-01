"""Start the Streamlit terminal on localhost, or just open it if it is already running.

Used by START_TERMINAL.bat. Running the launcher twice is harmless: the second run detects the running
terminal, opens the browser and exits 0 instead of failing on a busy port.

Options: --port N, --no-browser, --smoke (start, wait until healthy, stop, exit 0/1 - used by CI).
"""

import argparse
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOST = "127.0.0.1"
HEALTH_TIMEOUT_SECONDS = 90
# Named tuple: unparenthesised `except A, B:` is 3.14-only (see app/database/locking.py).
_UNREACHABLE = (urllib.error.URLError, OSError, ValueError)


def terminal_url(port: int) -> str:
    return f"http://{HOST}:{port}"


def is_healthy(port: int, timeout: float = 1.0) -> bool:
    """True if a Streamlit server answers its health endpoint on this port."""
    try:
        with urllib.request.urlopen(f"{terminal_url(port)}/_stcore/health", timeout=timeout) as response:
            return response.status == 200 and response.read().strip() == b"ok"
    except _UNREACHABLE:
        return False


def port_in_use(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((HOST, port)) == 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8501)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args(argv)

    if is_healthy(args.port):
        print(f"The terminal is already running at {terminal_url(args.port)}.")
        if not (args.no_browser or args.smoke):
            webbrowser.open(terminal_url(args.port))
        return 0
    if port_in_use(args.port):
        print(f"ERROR: port {args.port} is in use by another program. Close it or choose another with --port.")
        return 1

    env = {**os.environ, "STREAMLIT_SERVER_HEADLESS": "true", "STREAMLIT_BROWSER_GATHER_USAGE_STATS": "false"}
    command = [
        sys.executable, "-m", "streamlit", "run", str(ROOT / "app" / "main.py"),
        "--server.address", HOST, "--server.port", str(args.port),
    ]  # fmt: skip
    server = subprocess.Popen(command, cwd=ROOT, env=env)
    try:
        deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
        while time.monotonic() < deadline and server.poll() is None and not is_healthy(args.port):
            time.sleep(0.5)
        healthy = is_healthy(args.port)
        if healthy:
            print(f"Terminal running at {terminal_url(args.port)}  (Ctrl+C to stop)")
            if args.smoke:
                return 0
            if not args.no_browser:
                webbrowser.open(terminal_url(args.port))
            return server.wait()
        print("ERROR: the terminal did not become healthy.")
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        if server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()


if __name__ == "__main__":
    raise SystemExit(main())
