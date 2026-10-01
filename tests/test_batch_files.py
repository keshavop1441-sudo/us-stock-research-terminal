"""Static checks of the Windows .bat files.

These cannot prove the scripts WORK on Windows. That is what the `windows-scripts` CI job does
(real windows-latest runner, Python 3.14). They only guard properties that are easy to break silently.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BATS = {name: (ROOT / name).read_bytes() for name in ("START_TERMINAL.bat", "UPDATE_DATA.bat", "scripts/setup_env.bat")}


def text(name: str) -> str:
    return BATS[name].decode("ascii")


def test_batch_files_use_windows_line_endings_and_are_ascii():
    for name, raw in BATS.items():
        assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b""), name
        raw.decode("ascii")  # cmd.exe misreads UTF-8 punctuation


def test_gitattributes_keeps_bat_files_crlf():
    assert "*.bat text eol=crlf" in (ROOT / ".gitattributes").read_text()


def test_installs_go_through_the_hash_locked_bootstrap_not_a_bare_pip_install():
    for name in BATS:
        assert not re.search(r"pip install", text(name), re.IGNORECASE), name
    assert "scripts\\bootstrap_env.py" in text("scripts/setup_env.bat")
    assert "--require-hashes" in (ROOT / "scripts" / "bootstrap_env.py").read_text()


def test_every_step_that_can_fail_is_checked():
    setup = text("scripts/setup_env.bat")
    for command in ("-m venv .venv", "scripts\\bootstrap_env.py"):
        line = next(i for i, ln in enumerate(setup.splitlines()) if command in ln)
        following = "\n".join(setup.splitlines()[line + 1 : line + 3])
        assert "errorlevel" in following, f"no error check after: {command}"
    for name, called in (("START_TERMINAL.bat", "scripts\\init_db.py"), ("UPDATE_DATA.bat", "scripts\\update_data.py")):
        lines = text(name).splitlines()
        assert any("call scripts\\setup_env.bat" in ln for ln in lines)
        idx = next(i for i, ln in enumerate(lines) if "call scripts\\setup_env.bat" in ln)
        assert "errorlevel" in lines[idx + 1], f"{name}: setup failure is not checked"
        idx = next(i for i, ln in enumerate(lines) if called in ln)
        assert "errorlevel" in lines[idx + 1], f"{name}: {called} failure is not checked"


def test_a_broken_or_wrong_version_venv_is_detected_and_rebuilt():
    setup = text("scripts/setup_env.bat")
    assert "sys.version_info[:2] == (3, 14)" in setup
    assert "rmdir /s /q" in setup and "Recreating" in setup


def test_non_interactive_modes_exist_for_ci_and_scheduled_runs():
    assert "/setup-only" in text("START_TERMINAL.bat") and "/smoke" in text("START_TERMINAL.bat")
    assert "/nopause" in text("UPDATE_DATA.bat")


def test_no_first_run_prompts_and_utf8_is_forced():
    for name in ("START_TERMINAL.bat", "UPDATE_DATA.bat"):
        assert "PYTHONUTF8=1" in text(name)
    launcher = (ROOT / "scripts" / "launch_terminal.py").read_text()
    assert "STREAMLIT_SERVER_HEADLESS" in launcher  # headless mode skips Streamlit's first-run e-mail prompt
    assert "STREAMLIT_BROWSER_GATHER_USAGE_STATS" in launcher


def test_the_terminal_listens_on_localhost_only():
    assert "--server.address" in (ROOT / "scripts" / "launch_terminal.py").read_text()
    assert "127.0.0.1" in (ROOT / ".streamlit" / "config.toml").read_text()
