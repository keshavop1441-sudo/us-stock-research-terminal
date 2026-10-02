"""Environment readiness check for the research engine (what works here, what does not, and why).

Run it first in any new environment (a Claude sandbox, a developer machine). It never prints secrets: SEC_USER_AGENT is
only reported as "configured / not configured / invalid".
"""

import importlib.util
import platform
import sys
from pathlib import Path
from typing import Any

import httpx

from app.data.openbb_client import installed_packages, runtime_check
from app.database import access
from app.database.errors import DatabaseUnavailableError
from app.ingestion.errors import IngestionError
from app.ingestion.sec_http import SecHttpClient
from app.ingestion.sec_source import fetch_submissions

REQUIRED_MODULES = ("duckdb", "polars", "pydantic", "httpx", "filelock", "dotenv")
# Reachability only (any HTTP answer, even 403, proves the host is reachable from this environment).
PROVIDER_HOSTS = {
    "sec_www": "https://www.sec.gov/",
    "sec_data": "https://data.sec.gov/",
    "nasdaq_api": "https://api.nasdaq.com/",
    "cboe_cdn": "https://cdn.cboe.com/",
}


def _check(name: str, status: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "status": status, "detail": detail, **extra}


def probe_hosts(timeout: float = 8.0, transport: httpx.BaseTransport | None = None) -> list[dict[str, Any]]:
    out = []
    with httpx.Client(timeout=timeout, follow_redirects=False, transport=transport) as client:
        for name, url in PROVIDER_HOSTS.items():
            try:
                status = client.head(url).status_code
                out.append(_check(f"network:{name}", "OK", f"reachable (HTTP {status})", host=url))
            except httpx.HTTPError as exc:
                out.append(_check(f"network:{name}", "FAIL", f"not reachable: {type(exc).__name__}", host=url))
    return out


def doctor(
    db_path: Path,
    *,
    user_agent: str | None,
    user_agent_error: str | None = None,
    network: bool = False,
    deep: bool = False,
    sec: SecHttpClient | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = [
        _check(
            "python",
            "OK" if sys.version_info >= (3, 11) else "FAIL",
            f"{platform.python_version()} on {platform.system()} (target 3.14; tests also pass on 3.11 and 3.13)",
        ),
    ]
    missing = [m for m in REQUIRED_MODULES if importlib.util.find_spec(m) is None]
    checks.append(
        _check(
            "python_packages",
            "FAIL" if missing else "OK",
            f"missing: {', '.join(missing)}" if missing else "duckdb, polars, pydantic, httpx, filelock, python-dotenv",
        )
    )
    openbb = installed_packages()
    checks.append(
        _check(
            "openbb_packages",
            "OK" if openbb.all_installed else "WARN",
            ", ".join(f"{p.name} {p.version or 'MISSING'}" for p in openbb.packages)
            + ("" if openbb.all_installed else " (prices/quotes need these; stored data can still be read)"),
        )
    )
    if deep:
        runtime = runtime_check()
        checks.append(
            _check(
                "openbb_runtime", "OK" if runtime.runtime_ok else "FAIL", runtime.detail, providers=runtime.providers
            )
        )
    if user_agent_error:
        checks.append(_check("sec_user_agent", "FAIL", user_agent_error))
    elif user_agent:
        checks.append(_check("sec_user_agent", "OK", "configured (value not shown)"))
    else:
        checks.append(
            _check(
                "sec_user_agent",
                "WARN",
                "not configured: live SEC access (resolve, ingest facts, filing events) is disabled. "
                "Set SEC_USER_AGENT='<ApplicationName> <contact email or URL>' or pass --sec-user-agent.",
            )
        )
    # Read-only: a check must not create or upgrade anything (the first ingest creates the database).
    if not db_path.is_file():
        checks.append(_check("database", "OK", "no database yet: the first `ingest` creates it"))
    else:
        try:
            with access.reader(db_path) as r:
                version, counts = r.schema_version(), r.table_counts()
            checks.append(
                _check(
                    "database", "OK",
                    f"schema v{version}; {counts.get('securities', 0)} securities, {counts.get('financial_facts', 0)} "
                    f"fact rows, {counts.get('price_daily', 0)} price rows",
                )
            )  # fmt: skip
        except (DatabaseUnavailableError, OSError) as exc:
            checks.append(_check("database", "FAIL", f"{type(exc).__name__}: {exc}"))
    if network:
        checks.extend(probe_hosts(transport=transport))
        if user_agent and sec is not None:
            try:
                fetch_submissions(sec, 320193)
                checks.append(_check("sec_access", "OK", "SEC accepted the User-Agent (one submissions request)"))
            except IngestionError as exc:
                checks.append(_check("sec_access", "FAIL", f"{exc.kind}: {exc}"))
    status = {c["name"]: c["status"] for c in checks}
    core_ok = status["python"] == "OK" and status["python_packages"] == "OK" and status.get("database") == "OK"
    return {
        "ready": core_ok,
        "capabilities": {
            "read_stored_data_and_screen": core_ok,
            "live_prices_and_quotes": core_ok and status["openbb_packages"] == "OK",
            "live_sec_data_and_filing_events": core_ok and status["sec_user_agent"] == "OK",
        },
        "checks": checks,
    }
