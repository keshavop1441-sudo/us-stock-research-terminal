"""OpenBB V5 (openbb-core 2.x) imports and API surface. Makes no network calls."""

import pytest

from app.data import openbb_client
from app.data.openbb_client import EXPECTED_PROVIDERS, EXPECTED_ROUTERS, OPENBB_PACKAGES


def test_required_packages_are_installed():
    status = openbb_client.installed_packages()
    assert [p.name for p in status.packages] == list(OPENBB_PACKAGES)
    assert status.all_installed, [p.name for p in status.packages if not p.installed]


def test_openbb_core_is_v5():
    import openbb_core
    from openbb_core.app.version import VERSION

    assert openbb_core.__name__ == "openbb_core"
    assert VERSION.startswith("2."), f"openbb-core 2.x is OpenBB V5, found {VERSION}"


def test_unneeded_extensions_are_not_installed():
    from importlib.metadata import PackageNotFoundError, version

    for name in ("openbb-equity", "openbb-yfinance", "openbb-fmp", "openbb-charting"):
        with pytest.raises(PackageNotFoundError):
            version(name)


@pytest.fixture(scope="module")
def runtime():
    return openbb_client.runtime_check()  # imports OpenBB once for this module (slow)


def test_runtime_check_loads_expected_providers(runtime):
    assert runtime.runtime_ok, runtime.detail
    assert runtime.providers == sorted(EXPECTED_PROVIDERS)
    assert runtime.routers == sorted(EXPECTED_ROUTERS)


def test_v5_provider_namespaced_api(runtime):
    obb = openbb_client.get_obb()
    assert callable(obb.sec.income_statement)
    assert callable(obb.sec.company_filings)
    assert callable(obb.nasdaq.equity.historical)
    assert callable(obb.cboe.equity.quote)
    assert callable(obb.news.company)
    assert not hasattr(obb, "equity"), "V4-style obb.equity.* must not be used"


def test_runtime_check_reports_missing_packages(monkeypatch):
    from importlib.metadata import PackageNotFoundError

    def fake_version(name):
        if name == "openbb-news":
            raise PackageNotFoundError(name)
        return "2.0.0"

    monkeypatch.setattr(openbb_client, "version", fake_version)
    status = openbb_client.runtime_check()
    assert status.runtime_ok is False
    assert "openbb-news" in status.detail
