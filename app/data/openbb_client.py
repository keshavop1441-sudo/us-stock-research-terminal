"""Access to OpenBB V5 (openbb-core 2.x).

V5 organises the API by provider namespace, e.g. ``obb.sec.income_statement(...)``,
``obb.nasdaq.equity.historical(...)``, ``obb.news.company(...)``. The V4 layout
(``obb.equity.price.historical(provider=...)``) does not exist here - do not copy V4 examples.

``import openbb`` is slow (several seconds, and builds static assets on first use), so it
is only done on demand. Checking which packages are installed is cheap and import-free.
"""

from functools import cache
from importlib.metadata import PackageNotFoundError, version

from app.models.status import OpenBBStatus, PackageStatus

OPENBB_PACKAGES = ("openbb-core", "openbb-sec", "openbb-nasdaq", "openbb-cboe", "openbb-news")
EXPECTED_PROVIDERS = ("cboe", "nasdaq", "sec")
EXPECTED_ROUTERS = ("cboe", "nasdaq", "news", "sec")


def installed_packages() -> OpenBBStatus:
    """Which of the required OpenBB packages are installed (no OpenBB import)."""
    packages = []
    for name in OPENBB_PACKAGES:
        try:
            packages.append(PackageStatus(name=name, version=version(name)))
        except PackageNotFoundError:
            packages.append(PackageStatus(name=name))
    return OpenBBStatus(packages=packages)


@cache
def get_obb():
    """Import and return the OpenBB V5 application object (``obb``). Slow on first call."""
    from openbb import obb

    return obb


def runtime_check() -> OpenBBStatus:
    """Import OpenBB and report the providers and routers it actually loaded. Never raises."""
    status = installed_packages()
    status.runtime_checked = True
    if not status.all_installed:
        missing = ", ".join(p.name for p in status.packages if not p.installed)
        status.runtime_ok = False
        status.detail = f"Not installed: {missing}"
        return status
    try:
        coverage = get_obb().coverage
        status.providers = sorted(coverage.providers)
        status.routers = sorted({key.split(".")[1] for key in coverage.commands})
    except Exception as exc:  # noqa: BLE001 - any import/build failure must become a status
        status.runtime_ok = False
        status.detail = f"{type(exc).__name__}: {exc}"
        return status
    missing_providers = set(EXPECTED_PROVIDERS) - set(status.providers)
    missing_routers = set(EXPECTED_ROUTERS) - set(status.routers)
    status.runtime_ok = not (missing_providers or missing_routers)
    status.detail = (
        "OpenBB V5 loaded."
        if status.runtime_ok
        else f"Missing providers {sorted(missing_providers)} / routers {sorted(missing_routers)}"
    )
    return status
