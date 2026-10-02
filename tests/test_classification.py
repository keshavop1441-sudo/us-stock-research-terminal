"""Nasdaq sector/industry and SEC SIC stay separate taxonomies (docs/data_coverage.yaml, `classification`).

Expectations are pinned to the labels the providers returned LIVE on 2026-10-02 (taxonomy probe, run 36957138239).
"""

import json
from pathlib import Path

import pytest

from app.screening import classification as c
from app.screening.classification import Classification, TaxonomyError

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "live_probe_2026_10_02" / "taxonomy.json"
ISSUERS = json.loads(FIXTURE.read_text(encoding="utf-8"))["issuers"]
REQUIRED = {"AAPL", "MSFT", "NVDA", "AMD", "AVGO", "META", "AMZN", "GOOGL"}


def nasdaq(symbol):
    n = ISSUERS[symbol]["nasdaq"]
    return c.nasdaq_classification(n["sector"], n["industry"])


def sic(symbol):
    s = ISSUERS[symbol]["sec_sic"]
    return c.sic_classification(s["code"], s["description"])


def test_all_required_representatives_were_observed():
    assert set(ISSUERS) >= REQUIRED


def test_semiconductors_in_the_nasdaq_taxonomy_are_exactly_the_industry_string_Semiconductors():
    semis = {s for s in ISSUERS if c.matches_nasdaq(nasdaq(s), industries=["Semiconductors"])}
    assert semis == {"NVDA", "AMD", "AVGO", "INTC", "MU", "TXN", "TSM"}
    assert all(
        ISSUERS[s]["nasdaq"]["sector"] == "Technology" for s in semis
    )  # sector is Technology, industry is the discriminator


def test_semiconductors_in_the_sic_taxonomy_are_code_3674_and_the_two_systems_disagree():
    sic_semis = {s for s in ISSUERS if c.matches_sic(sic(s), codes=[3674])}
    nasdaq_semis = {s for s in ISSUERS if c.matches_nasdaq(nasdaq(s), industries=["Semiconductors"])}
    assert sic_semis == nasdaq_semis  # same set on this sample ...
    # ... but the systems are NOT interchangeable: QCOM (a chip designer) is neither SIC 3674 nor Nasdaq
    # "Semiconductors"; ASML (chip equipment) is "Industrial Machinery/Components" at Nasdaq and SIC 3559;
    # and SIC has no sector at all
    assert "QCOM" not in nasdaq_semis and "ASML" not in nasdaq_semis
    assert ISSUERS["QCOM"]["sec_sic"]["code"] == "3663" and ISSUERS["ASML"]["sec_sic"]["code"] == "3559"


def test_technology_excluding_semiconductors_is_a_nasdaq_query_with_exact_strings():
    """'Technology excluding semiconductors' = Nasdaq sector 'Technology' AND Nasdaq industry != 'Semiconductors'."""
    result = {
        s for s in ISSUERS if c.matches_nasdaq(nasdaq(s), sector="Technology", exclude_industries=["Semiconductors"])
    }
    assert result == {"AAPL", "MSFT", "META", "GOOGL", "QCOM", "ASML"}
    assert "AMZN" not in result  # Nasdaq files Amazon under Consumer Discretionary
    # QCOM and ASML stay: the source does not call them semiconductors and nothing here overrides it


def test_nasdaq_does_not_follow_gics_for_communication_services():
    # GICS would call META and GOOGL Communication Services; Nasdaq says Technology. Nasdaq's label is kept as is.
    for symbol in ("META", "GOOGL"):
        assert nasdaq(symbol).sector == "Technology"


def test_filters_never_cross_taxonomies():
    with pytest.raises(TaxonomyError):
        c.matches_nasdaq(sic("NVDA"), sector="Technology")  # a Nasdaq filter on a SIC classification
    with pytest.raises(TaxonomyError):
        c.matches_sic(nasdaq("NVDA"), codes=[3674])  # a SIC filter on a Nasdaq classification
    with pytest.raises(TaxonomyError):
        Classification(c.SEC_SIC, sector="Technology", code="3674")  # SIC has no sector: refuse to store one
    with pytest.raises(TaxonomyError):
        Classification("gics", sector="Information Technology")  # no GICS in this project
    assert not hasattr(c, "sic_to_nasdaq") and not hasattr(c, "nasdaq_to_gics")


def test_raw_values_are_preserved_unchanged_next_to_the_classification():
    n = nasdaq("QCOM")
    assert n.industry == "Radio And Television Broadcasting And Communications Equipment"  # not tidied, not remapped
    s = sic("QCOM")
    assert (s.code, s.industry) == ("3663", "Radio & Tv Broadcasting & Communications Equipment")


def test_missing_or_na_classification_is_unknown_not_false():
    assert c.nasdaq_classification("N/A", "N/A") is None  # Nasdaq returns the string 'N/A' for BRK.B
    assert c.matches_nasdaq(None, sector="Technology") is None
    assert c.matches_nasdaq(c.nasdaq_classification("Technology", None), exclude_industries=["Semiconductors"]) is None
    assert c.sic_classification(None, None) is None and c.matches_sic(None, codes=[3674]) is None
    assert c.sic_classification(900, "x").code == "0900"  # SIC codes keep their 4 digits


def test_industry_strings_are_free_text_and_sector_dependent():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    tech = data["screener_technology_all"]
    assert tech["n_industries"] == 13 and tech["industries"]["Semiconductors"] == tech["semiconductors_rows"] == 106
    assert "Telecommunications Equipment" in data["screener_consumer_discretionary_all"]["note"]
