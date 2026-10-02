"""The Claude Skill: SKILL.md validity, progressive-disclosure layout, deterministic packaging, the packaged skill runs,
Project materials, and the removal of the retired local-LLM / Streamlit architecture."""

import ast
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.research.screen_spec import ScreenSpec

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "claude" / "skills" / "us-stock-research"
PROJECT = ROOT / "claude" / "project"


def load_packager():
    spec = importlib.util.spec_from_file_location("package_skill", ROOT / "scripts" / "package_skill.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["package_skill"] = module
    spec.loader.exec_module(module)
    return module


pk = load_packager()


def frontmatter(text: str) -> dict:
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, "SKILL.md must start with YAML frontmatter"
    return yaml.safe_load(match.group(1))


# --- SKILL.md validity ---


def test_skill_md_has_valid_frontmatter_per_the_documented_rules():
    meta = frontmatter((SKILL / "SKILL.md").read_text(encoding="utf-8"))
    assert set(meta) == {"name", "description"}  # nothing the upload validator might reject
    assert meta["name"] == SKILL.name == "us-stock-research"
    assert (
        re.fullmatch(r"[a-z0-9-]{1,64}", meta["name"])
        and "claude" not in meta["name"]
        and "anthropic" not in meta["name"]
    )
    description = meta["description"]
    assert 0 < len(description) <= 1024 and not re.search(r"<[^>]+>", description)


def test_the_description_says_when_to_invoke_and_when_not_to():
    description = frontmatter((SKILL / "SKILL.md").read_text(encoding="utf-8"))["description"].lower()
    for topic in (
        "screening",
        "financial metric",
        "company research",
        "filings",
        "earnings",
        "ownership",
        "insider",
        "news",
        "contracts",
        "partnerships",
        "m&a",
        "legal",
        "regulatory",
        "why did this stock fall",
        "comparisons",
    ):
        assert topic in description, topic
    assert "do not use" in description


def test_skill_body_is_concise_and_uses_progressive_disclosure():
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    assert len(text.splitlines()) < 120 and len(text) < 8000
    linked = set(re.findall(r"`(references/[\w.-]+\.md)`", text))
    assert linked and all((SKILL / link).is_file() for link in linked), linked
    assert len(list((SKILL / "references").glob("*.md"))) >= len(linked)


def test_the_skill_covers_the_workflows_and_the_hard_rules():
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    for needle in (
        "**A. Screen**",
        "**B. Metric analysis**",
        "**C. Company research**",
        '**D. "Why did it fall?"**',
        "**E. Comparison**",
        "**F-H.",
        "Do **not** use it for",
        "Numbers come from the scripts",
        "never zero",
        "No buy/sell/hold",
        "UNSUPPORTED_TAXONOMY",
        "SEC_USER_AGENT",
    ):
        assert needle in text, needle
    for command in (
        "catalog",
        "validate-screen",
        "resolve",
        "ingest",
        "universe",
        "metrics",
        "screen",
        "evidence",
        "events",
    ):
        assert command in text


def test_every_documented_command_exists_in_the_cli():
    from app.cli.research import COMMANDS

    reference = (SKILL / "references" / "cli-reference.md").read_text(encoding="utf-8")
    for command in COMMANDS:
        assert f"`{command}`" in reference, command


def test_templates_are_valid_screen_specifications_and_a_bad_one_is_not():
    templates = sorted((SKILL / "templates").glob("*.json"))
    assert len(templates) >= 3
    for path in templates:
        ScreenSpec.model_validate_json(path.read_text(encoding="utf-8"))
    with pytest.raises(ValidationError):
        ScreenSpec.model_validate({"criteria": [{"metric": "revenue_growth_yoy", "op": "gt", "value": 15}]})


def test_documented_metric_names_are_real_catalog_entries():
    from app.research.catalog import CATALOG

    text = (SKILL / "references" / "screen-translation.md").read_text(encoding="utf-8")
    named = set(re.findall(r"`([a-z0-9_]+)`", text)) & {n for n in re.findall(r"`([a-z0-9_]+)`", text) if "_" in n}
    candidates = {
        n
        for n in named
        if n.startswith(("price_", "revenue_", "eps_", "fcf", "gross_", "operating_", "net_", "debt_", "drawdown_"))
    }
    assert candidates and candidates <= set(CATALOG), candidates - set(CATALOG)


# --- no secrets, no personal data, no local paths ---


def text_files():
    for base in (SKILL, PROJECT):
        yield from (p for p in base.rglob("*") if p.is_file())


def test_skill_and_project_files_contain_no_secrets_emails_or_local_paths():
    for path in text_files():
        text = path.read_text(encoding="utf-8")
        assert not pk.LOCAL_PATH.search(text) and not pk.SECRET.search(text), path.name
        for email in re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", text):
            assert email.split("@")[1] in {"example.org", "example.com"}, (path.name, email)  # placeholders only
        assert "ANTHROPIC_API_KEY" not in text


# --- packaging ---


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    out = tmp_path_factory.mktemp("pkg") / "us-stock-research.zip"
    assert pk.main(["--out", str(out)]) == 0
    return out


def test_the_package_is_reproducible_byte_for_byte(built, tmp_path):
    again = tmp_path / "again.zip"
    assert pk.main(["--out", str(again)]) == 0
    assert again.read_bytes() == built.read_bytes()
    assert pk.main(["--out", str(built), "--verify"]) == 0
    recorded = built.with_name(built.name + ".sha256").read_text().split()[0]
    assert recorded == hashlib.sha256(built.read_bytes()).hexdigest()


def test_the_package_layout_has_one_top_level_folder_named_like_the_skill(built):
    with zipfile.ZipFile(built) as archive:
        names = archive.namelist()
        assert names == sorted(names)
        assert {n.split("/")[0] for n in names} == {"us-stock-research"}
        assert "us-stock-research/SKILL.md" in names and "us-stock-research/scripts/research.py" in names
        for required in (
            "references/cli-reference.md",
            "references/research-methodology.md",
            "references/metric-definitions.md",
            "references/sources-coverage.md",
            "references/output-templates.md",
            "scripts/requirements.txt",
            "scripts/requirements.lock",
            "scripts/_engine/app/cli/research.py",
            "scripts/_engine/app/research/screening.py",
            "templates/screen-growth-drawdown.example.json",
        ):
            assert f"us-stock-research/{required}" in names, required
        assert all(i.date_time == (2020, 1, 1, 0, 0, 0) for i in archive.infolist())
        mode = {i.filename: i.external_attr >> 16 for i in archive.infolist()}
        assert mode["us-stock-research/scripts/research.py"] == 0o755 and mode["us-stock-research/SKILL.md"] == 0o644
        assert frontmatter(archive.read("us-stock-research/SKILL.md").decode())["name"] == "us-stock-research"


def test_the_package_contains_nothing_that_must_not_ship(built):
    with zipfile.ZipFile(built) as archive:
        names = archive.namelist()
        for name in names:
            parts = name.split("/")
            assert not set(parts) & {".git", "__pycache__", ".venv", "tests", "dist", "research_data", ".github"}, name
            assert not name.endswith((".pyc", ".duckdb", ".wal", ".env", ".pem", ".key", ".log")), name
            assert Path(name).name not in {".env", ".env.example", "CLAUDE.md"}, name
        assert not any(n.startswith("us-stock-research/scripts/_engine/app/ui") for n in names)
        for name in names:
            text = archive.read(name).decode("utf-8")
            assert not pk.LOCAL_PATH.search(text) and not pk.SECRET.search(text), name
            assert "\r" not in text, name


def test_packaging_fails_loudly_on_bad_input(tmp_path, monkeypatch):
    with pytest.raises(pk.PackagingError, match="frontmatter"):
        pk.validate_skill_md("# no frontmatter")
    with pytest.raises(pk.PackagingError, match="name must be"):
        pk.validate_skill_md("---\nname: other-name\ndescription: x\n---\n")
    with pytest.raises(pk.PackagingError, match="description"):
        pk.validate_skill_md("---\nname: us-stock-research\ndescription: " + "x" * 1025 + "\n---\n")
    with pytest.raises(pk.PackagingError, match="description"):
        pk.validate_skill_md("---\nname: us-stock-research\ndescription: <b>bold</b>\n---\n")
    leaky = tmp_path / "leak.py"
    leaky.write_text("PATH = '/home/someone/project'\n")
    with pytest.raises(pk.PackagingError, match="local absolute path"):
        pk.check_file("scripts/leak.py", leaky)
    secret = tmp_path / "s.py"
    secret.write_text("KEY = 'sk-ant-abcdefghijklmnopqrstuvwxyz'\n")
    with pytest.raises(pk.PackagingError, match="secret"):
        pk.check_file("scripts/s.py", secret)
    for name in (".env", "x.duckdb", "tests/a.py", "__pycache__/a.py"):
        (tmp_path / "f").mkdir(exist_ok=True)
        path = tmp_path / "f" / Path(name).name
        path.write_text("x = 1\n")
        with pytest.raises(pk.PackagingError, match="forbidden"):
            pk.check_file(name, path)


def test_crlf_checkouts_package_to_the_same_bytes(tmp_path):
    crlf = tmp_path / "a.md"
    crlf.write_bytes(b"line one\r\nline two\r\n")
    assert pk.check_file("references/a.md", crlf) == b"line one\nline two\n"


def test_the_packaged_skill_runs_without_the_repository(built, tmp_path):
    """Unzip somewhere with no repository above it and run the launcher: it must use the bundled engine."""
    target = tmp_path / "skills"
    with zipfile.ZipFile(built) as archive:
        archive.extractall(target)
    launcher = target / "us-stock-research" / "scripts" / "research.py"
    assert not any((p / "CLAUDE.md").exists() for p in launcher.parents)  # nothing but the package is above it
    workdir = tmp_path / "work"
    workdir.mkdir()
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    run = subprocess.run(
        [sys.executable, str(launcher), "catalog"], capture_output=True, text=True, cwd=workdir, env=env, timeout=120
    )
    assert run.returncode == 0, run.stderr
    envelope = json.loads(run.stdout)
    assert envelope["status"] == "OK" and len(envelope["data"]["metrics"]) > 30
    done = subprocess.run(
        [sys.executable, str(launcher), "universe"], capture_output=True, text=True, cwd=workdir, env=env, timeout=120
    )
    assert json.loads(done.stdout)["errors"][0]["code"] == "NO_DATABASE" and done.returncode == 2
    assert not (workdir / "research_data").exists()  # a read command never creates a database
    doctor = subprocess.run(
        [sys.executable, str(launcher), "doctor"], capture_output=True, text=True, cwd=workdir, env=env, timeout=120
    )
    assert json.loads(doctor.stdout)["data"]["engine"] == "bundled with the packaged Skill"


def test_the_packaged_requirements_match_the_repository_pins():
    assert (ROOT / "requirements.txt").read_text() and pk.collect()[
        "scripts/requirements.txt"
    ] == ROOT / "requirements.txt"
    pins = [line.split("#")[0].strip() for line in (ROOT / "requirements.txt").read_text().splitlines() if "==" in line]
    assert all(re.fullmatch(r"[\w.-]+==[\w.]+", p) for p in pins) and not any(
        p.startswith(("streamlit", "plotly")) for p in pins
    )


# --- Project materials ---


def test_project_instructions_define_the_role_without_duplicating_the_manual():
    text = (PROJECT / "project-instructions.md").read_text(encoding="utf-8")
    assert 1500 < len(text) < 6500  # concise: pasted into one field, not the repository manual
    for needle in (
        "Python is the source of truth",
        "never zero",
        "No buy/sell/hold",
        "SEC",
        "web research",
        "publication date",
        "Output templates",
    ):
        assert needle in text, needle
    assert "def " not in text and "```" not in text


def test_setup_guide_gives_the_steps_in_order_and_names_the_real_files():
    text = (PROJECT / "SETUP.md").read_text(encoding="utf-8")
    positions = [
        text.index(s)
        for s in (
            "## 1. Build the Skill package",
            "## 2. Install the Skill",
            "## 3. Create the Project",
            "## 4. Use it",
        )
    ]
    assert positions == sorted(positions)
    for needle in (
        "python scripts/package_skill.py",
        "dist/us-stock-research.zip",
        "Set project instructions",
        "project-instructions.md",
        "research-methodology.md",
        "metric-definitions.md",
        "sources-coverage.md",
        "output-templates.md",
        "www.sec.gov",
        "api.nasdaq.com",
        "cdn.cboe.com",
    ):
        assert needle in text, needle
    for ref in re.findall(
        r"`((?:research-methodology|metric-definitions|sources-coverage|output-templates)\.md)`", text
    ):
        assert (SKILL / "references" / ref).is_file()


# --- the retired architecture is gone ---

RETIRED = re.compile(r"ollama|qwen|deepseek|streamlit|plotly|fast_think|deep_think", re.IGNORECASE)
ALLOWED_MENTIONS = {"README.md", "CLAUDE.md", "test_skill_package.py", "test_dependencies.py", "architecture.md"}


def test_no_runtime_code_script_config_or_dependency_refers_to_the_retired_stack():
    scanned = [
        *(ROOT / "app").rglob("*.py"),
        *(ROOT / "scripts").rglob("*.py"),
        *SKILL.rglob("*"),
        *PROJECT.rglob("*"),
        ROOT / "requirements.txt",
        ROOT / "requirements-dev.txt",
        ROOT / ".env.example",
        ROOT / "ruff.toml",
        ROOT / "pytest.ini",
        *(ROOT / ".github" / "workflows").glob("*.yml"),
    ]
    for path in scanned:
        if path.is_file() and path.name not in ALLOWED_MENTIONS:
            assert not RETIRED.search(path.read_text(encoding="utf-8")), path.relative_to(ROOT)
    for lock in ("requirements.lock", "requirements-dev.lock"):
        names = {m.lower() for m in re.findall(r"^([A-Za-z0-9_.-]+)==", (ROOT / lock).read_text(), re.MULTILINE)}
        assert not names & {"streamlit", "plotly", "ollama", "pyarrow"}, lock


def test_the_streamlit_app_and_launchers_are_removed():
    for gone in (
        "app/ui",
        "app/main.py",
        "app/agent",
        "app/tools",
        ".streamlit",
        "START_TERMINAL.bat",
        "UPDATE_DATA.bat",
        "scripts/launch_terminal.py",
        "scripts/bootstrap_env.py",
        "scripts/setup_env.bat",
        "scripts/update_data.py",
    ):
        assert not (ROOT / gone).exists(), gone


def test_no_module_imports_the_retired_stack():
    banned = {"streamlit", "plotly", "ollama"}
    for path in [*(ROOT / "app").rglob("*.py"), *(ROOT / "scripts").rglob("*.py"), *(ROOT / "tests").glob("*.py")]:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            assert not {n.split(".")[0] for n in names} & banned, path


def test_settings_have_no_llm_configuration_and_the_app_needs_no_server():
    text = (ROOT / "app" / "config.py").read_text(encoding="utf-8")
    assert "base_url" not in text and "think" not in text.lower() and "llm" not in text.lower()
    for path in (ROOT / "app").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "uvicorn" not in source and "serve(" not in source and "run_server" not in source, path
