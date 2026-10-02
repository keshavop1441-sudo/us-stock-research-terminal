"""docs/data_coverage.yaml is the machine-readable Phase 2 coverage matrix. These tests keep it honest.

They check structure and cross-references only (statuses, evidence ids, derivation inputs, OpenBB commands that really
exist in the installed V5 providers, rule functions that really exist). They cannot check that a LIVE_VERIFIED claim is
true: that evidence is the probe runs cited by id in the file.
"""

import importlib.util
import json
import re
from pathlib import Path

import pytest
import yaml

from app.screening import metrics as metric_rules

ROOT = Path(__file__).resolve().parent.parent
DATA = yaml.safe_load((ROOT / "docs" / "data_coverage.yaml").read_text(encoding="utf-8"))
METRICS = DATA["metrics"]
BY_ID = {m["metric"]: m for m in METRICS}
STATUSES = {"LIVE_VERIFIED", "API_SHAPE_VERIFIED", "FIXTURE_VERIFIED", "NOT_VERIFIED", "UNAVAILABLE", "DERIVED"}
TEXT_FIELDS = (
    "category", "canonical_definition", "source", "provider", "command", "identifier", "period",
    "missing_data_behavior", "negative_data_behavior", "restatement_behavior", "fallback", "limitations",
)  # fmt: skip


def test_controlled_status_vocabulary_matches_the_specification():
    assert set(DATA["statuses"]) == STATUSES
    assert {m["status"] for m in METRICS} <= STATUSES


def test_every_entry_has_every_required_field_with_content():
    assert len(METRICS) >= 60
    for metric in METRICS:
        name = metric.get("metric", "?")
        missing = [f for f in DATA["required_metric_fields"] if f not in metric]
        assert not missing, f"{name}: missing {missing}"
        for field in TEXT_FIELDS:
            assert isinstance(metric[field], str) and metric[field].strip(), f"{name}.{field} must be non-empty text"
        assert isinstance(metric["inputs"], list) and isinstance(metric["sample_tested"], dict)
        assert metric["direct_or_derived"] in ("direct", "derived"), name
        assert metric["formula"] is None or isinstance(metric["formula"], str), name


def test_metric_ids_are_unique_snake_case():
    names = [m["metric"] for m in METRICS]
    assert len(names) == len(set(names))
    assert all(re.fullmatch(r"[a-z][a-z0-9_]*", n) for n in names)


def test_derived_entries_declare_inputs_that_exist_and_have_a_formula():
    for metric in METRICS:
        name = metric["metric"]
        if metric["status"] == "DERIVED":
            assert metric["direct_or_derived"] == "derived", name
        if metric["direct_or_derived"] == "derived":
            assert metric["status"] in ("DERIVED", "NOT_VERIFIED"), (
                f"{name}: derived metrics are DERIVED or NOT_VERIFIED"
            )
            assert metric["formula"], f"{name}: a derived metric must state its formula"
        for dependency in metric["inputs"]:
            assert dependency in BY_ID, f"{name}: input {dependency!r} is not a metric"
            assert dependency != name


def test_derivation_graph_has_no_cycles():
    state: dict[str, int] = {}

    def visit(name: str) -> None:
        assert state.get(name) != 1, f"cycle through {name}"
        if state.get(name) == 2:
            return
        state[name] = 1
        for dependency in BY_ID[name]["inputs"]:
            visit(dependency)
        state[name] = 2

    for name in BY_ID:
        visit(name)


def test_sample_tested_claims_point_at_evidence_that_supports_the_status():
    evidence = DATA["evidence"]
    for metric in METRICS:
        name, tested, status = metric["metric"], metric["sample_tested"], metric["status"]
        assert set(tested) == {"live", "evidence", "symbols"}, name
        assert isinstance(tested["live"], bool) and isinstance(tested["symbols"], list), name
        assert tested["evidence"] and set(tested["evidence"]) <= set(evidence), f"{name}: unknown evidence id"
        kinds = {evidence[e]["kind"] for e in tested["evidence"]}
        kinds |= {
            "live" for e in tested["evidence"] if evidence[e].get("from_live")
        }  # fixture transcribed from a live run
        if status == "LIVE_VERIFIED":
            assert tested["live"] and "live" in kinds, f"{name}: LIVE_VERIFIED needs live evidence"
        if status == "FIXTURE_VERIFIED":
            assert "fixture" in kinds and not tested["live"], name
        if status == "API_SHAPE_VERIFIED":
            assert "installed_code" in kinds and not tested["live"], name
        if tested["live"]:
            assert "live" in kinds, f"{name}: live=true requires an E_LIVE_* evidence id"
            assert tested["symbols"] or name in {
                "market_calendar",
                "litigation_and_enforcement",
                "splits_dividends_calendar",
            }


def test_evidence_entries_are_well_formed_and_runs_match_the_fixture_provenance():
    for key, entry in DATA["evidence"].items():
        assert entry["kind"] in ("live", "fixture", "installed_code", "unit_tests"), key
        assert entry["description"].strip()
        if entry["kind"] == "live":
            assert isinstance(entry["github_run"], int) and re.fullmatch(r"[0-9a-f]{40}", entry["commit"]), key
        if "path" in entry:
            assert (ROOT / entry["path"]).is_dir(), f"{key}: {entry['path']} does not exist"
    runs = {e["github_run"] for e in DATA["evidence"].values() if e["kind"] == "live"}
    provenance = json.loads((ROOT / "tests/fixtures/live_probe_2026_10_02/PROVENANCE.json").read_text(encoding="utf-8"))
    for name, entry in provenance.items():
        evidence_runs = entry["evidence_run"] if isinstance(entry["evidence_run"], list) else [entry["evidence_run"]]
        assert {r["run"] for r in evidence_runs} <= runs, f"{name}: fixture cites a run that the matrix does not list"


def test_every_openbb_command_cited_exists_in_the_installed_v5_providers():
    from app.data.openbb_client import get_obb

    available = {key.lstrip(".") for key in get_obb().coverage.commands}
    cited = set()
    for metric in METRICS:
        cited |= set(re.findall(r"\bobb\.([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*)", metric["command"]))
    assert len(cited) >= 20
    unknown = {c for c in cited if c not in available}
    assert not unknown, f"cited but not installed in OpenBB V5: {sorted(unknown)}"


def test_rule_functions_named_in_the_matrix_exist():
    named = {m["rule_function"] for m in METRICS if "rule_function" in m}
    named |= {r["function"] for r in DATA["comparability_rules"] if r["function"]}
    assert len(named) >= 12
    for function in named:
        assert callable(getattr(metric_rules, function, None)), f"app.screening.metrics.{function} does not exist"


def test_every_required_data_domain_is_covered():
    categories = {m["category"] for m in METRICS}
    assert categories >= {
        "identity", "market", "income_statement", "cash_flow", "balance_sheet", "derived", "filings",
        "ownership", "earnings", "news", "government", "events",
    }  # fmt: skip
    required = {
        "issuer_cik", "ticker_history", "share_class", "price_daily_ohlcv", "week52_high_low", "price_return",
        "market_cap_issuer", "shares_outstanding", "revenue", "free_cash_flow", "net_debt", "debt_to_equity",
        "revenue_growth_yoy", "eps_growth_yoy", "price_to_earnings", "filing_index", "amended_filing_flag",
        "insider_transactions", "insider_transaction_class", "institutional_holders_nasdaq",
        "institutional_13f_holdings_sec", "earnings_calendar", "guidance", "company_news", "government_awards",
        "corporate_events_8k_items",
    }  # fmt: skip
    assert required <= set(BY_ID)


def test_unavailable_items_are_never_claimed_as_sampled_data():
    for metric in METRICS:
        if metric["status"] == "UNAVAILABLE":
            assert metric["provider"] == "none" and metric["command"] == "none", metric["metric"]
    assert BY_ID["guidance"]["status"] == "UNAVAILABLE" and BY_ID["ticker_history"]["status"] == "UNAVAILABLE"


def test_representative_companies_include_the_required_set_and_stress_cases():
    companies = set(DATA["representative_companies"])
    assert {"AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "AMD", "AVGO"} <= companies
    assert {
        "RIVN",
        "PTON",
        "KOSS",
        "BRK-B",
        "TWTR",
        "COST",
    } <= companies  # loss-making, small, multi-class, delisted, 52/53


def test_source_evidence_review_lists_each_gap_with_proof_and_a_before_phase3_flag():
    review = DATA["source_evidence_model"]
    assert set(review["sufficient_for"]) == {"sec_filing", "market_data", "news", "government_award"}
    for gap in review["gaps_proven_by_audit"]:
        assert gap["proof"].strip() and isinstance(gap["required_before_phase3"], bool), gap["id"]
    required = {g["id"] for g in review["gaps_proven_by_audit"] if g["required_before_phase3"]}
    assert {"S1_command_and_parameters", "S2_provider_version", "S3_provider_as_of"} <= required
    assert DATA["schema_gaps"] and all(g["proof"].strip() for g in DATA["schema_gaps"])


def test_comparability_rules_are_unique_and_documented():
    ids = [r["id"] for r in DATA["comparability_rules"]]
    assert len(ids) == len(set(ids)) >= 10
    assert all(r["rule"].strip() for r in DATA["comparability_rules"])


def test_markdown_metric_table_is_in_sync_with_the_yaml():
    spec = importlib.util.spec_from_file_location("render_coverage", ROOT / "tests" / "render_coverage.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    markdown = (ROOT / "docs" / "data_coverage.md").read_text(encoding="utf-8")
    assert module.rendered_markdown(markdown, DATA) == markdown, "run: python tests/render_coverage.py"
    for name in BY_ID:
        assert f"`{name}`" in markdown, f"{name} missing from the markdown table"


def test_no_secrets_or_personal_email_in_the_coverage_files():
    for path in (ROOT / "docs").glob("data_coverage.*"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text.replace("definitelynot@fakecompany.com", "")), path.name


@pytest.mark.parametrize("name", ["data_coverage.yaml", "data_coverage.md"])
def test_docs_exist(name):
    assert (ROOT / "docs" / name).stat().st_size > 1000
