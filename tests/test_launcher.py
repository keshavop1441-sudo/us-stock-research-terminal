"""scripts/launch_terminal.py: already-running detection and a real start/health/stop cycle."""

import http.server
import importlib.util
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("launch_terminal", ROOT / "scripts" / "launch_terminal.py")
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def fake_streamlit():
    """A server that answers like Streamlit's health endpoint."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            ok = self.path == "/_stcore/health"
            self.send_response(200 if ok else 404)
            self.end_headers()
            self.wfile.write(b"ok" if ok else b"nope")

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def test_is_healthy_recognises_a_running_terminal_and_nothing_else(fake_streamlit):
    assert launcher.is_healthy(fake_streamlit) is True
    assert launcher.is_healthy(free_port()) is False


def test_is_healthy_rejects_a_server_that_is_not_streamlit():
    class Other(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"hello")

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Other)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert launcher.is_healthy(server.server_address[1]) is False
        assert launcher.port_in_use(server.server_address[1]) is True
    finally:
        server.shutdown()
        server.server_close()


def test_running_it_again_is_harmless(fake_streamlit, monkeypatch, capsys):
    opened = []
    monkeypatch.setattr(launcher.webbrowser, "open", opened.append)
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *a, **k: pytest.fail("must not start a second server"))
    assert launcher.main(["--port", str(fake_streamlit)]) == 0
    assert "already running" in capsys.readouterr().out
    assert opened == [f"http://127.0.0.1:{fake_streamlit}"]  # just brings the existing terminal to the front


def test_a_busy_port_that_is_not_the_terminal_is_a_clear_error(capsys):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        assert launcher.main(["--port", str(port), "--no-browser"]) == 1
    assert "in use by another program" in capsys.readouterr().out


def test_smoke_mode_really_starts_streamlit_checks_health_and_stops(app_env):
    """The same path the Windows CI job exercises through START_TERMINAL.bat /smoke."""
    port = free_port()
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "launch_terminal.py"), "--smoke", "--no-browser", "--port", str(port)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Terminal running" in result.stdout
    assert launcher.port_in_use(port) is False  # the server was stopped again
