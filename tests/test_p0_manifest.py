"""The P0 selection is deterministic, documented and cannot change silently."""

from app.ingestion.manifest import MANIFEST, ROLES, SYMBOLS, coverage, manifest_digest, primary_symbol_for
from app.models.symbols import canonical_symbol

# If this digest changes the P0 set changed: that must be a deliberate, reviewed edit (update the digest AND the docs).
PINNED_DIGEST = "3f9f6593d6d620e8016ba81901c915b0da75a3bb66497d4e71513e76765802c6"


def test_the_manifest_is_pinned():
    assert manifest_digest() == PINNED_DIGEST
    assert SYMBOLS == (
        "AAPL", "NVDA", "AMD", "GOOGL", "GOOG", "BRK-B", "META", "RIVN", "PTON", "KOSS", "COST", "JPM", "TSM", "TSLA",
    )  # fmt: skip


def test_size_is_ten_to_fifteen_and_symbols_are_canonical_and_unique():
    assert 10 <= len(MANIFEST) <= 15
    assert len(set(SYMBOLS)) == len(SYMBOLS)
    assert all(canonical_symbol(s) == s for s in SYMBOLS)


def test_every_requested_coverage_category_has_a_member():
    assert all(coverage()[role] for role in ROLES), {r: m for r, m in coverage().items() if not m}


def test_every_member_cites_phase_2_evidence_for_why_it_is_there():
    assert all(len(s.why) > 30 and s.roles for s in MANIFEST)


def test_multi_class_listings_share_one_primary():
    assert primary_symbol_for("GOOG") == primary_symbol_for("GOOGL") == "GOOGL"
    assert primary_symbol_for("BRK-B") == "BRK-B"
    assert [s.symbol for s in MANIFEST if s.issuer == "Alphabet" and s.primary_listing] == ["GOOGL"]


def test_recorded_phase_2_ciks_are_ten_digit_text():
    assert all(s.phase2_cik is None or (len(s.phase2_cik) == 10 and s.phase2_cik.isdigit()) for s in MANIFEST)
