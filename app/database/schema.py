"""DuckDB schema for the research terminal (schema version 2).

Design rules
------------
* Raw facts and calculated values stay separate. Tables hold *raw* data as reported by a source.
  Ratios and growth rates are computed at query time in ``app/screening`` and never stored.
* Missing real data is NULL. Nothing here is pre-populated.
* Identity: ``cik`` (10-digit zero-padded text, see ``app.models.identifiers``) identifies the issuer and
  keys issuer-level data. A ticker is only a label on a listed security, so listed-security data
  (``price_daily``, watchlists) is keyed by the surrogate ``security_id``.

Business keys (what makes two rows "the same logical record" - this is what makes reloads idempotent)
------------------------------------------------------------------------------------------------
securities       (ticker, cik)  - cik may be unknown (NULL); a later load that learns the cik fills it in.
                 exchange/name/sector/... are attributes and are updated, never part of identity.
                 Enforced by ``WriteRepository.upsert_security`` (see DuckDB limitation below).
financial_facts  (cik, taxonomy, concept, unit, period_start, period_end, accession_no)
                 = one value as reported in one filing. The same period restated in a later filing is a
                 different row (as-reported history is kept). Stored as ``fact_key``.
filings          accession_no (globally unique at the SEC).
price_daily      (security_id, trade_date).
earnings         (cik, fiscal_year, fiscal_period) - the period being reported. report_date is an attribute
                 because announced dates move. Source rows without a fiscal period cannot be ingested.
ownership        (cik, holder_type, holder_key, as_of_date, accession_no, line_no)
                 holder_key = holder CIK if known, else the normalised holder name; line_no distinguishes
                 several transactions/positions within one filing. Stored as ``ownership_key``.
events           (cik, event_type, source_ref) - source_ref is a stable reference from the source
                 (URL, accession number + item, provider id). Stored as ``event_key``.
sources          append-only: one row per retrieval (provenance), deliberately not de-duplicated.
query_history, research_runs, watchlists, watchlist_items: created by the application, not ingested.

``*_key`` columns are the primary keys of their tables and a CHECK constraint ties each one to its
component columns, so a key can never disagree with the data. The keys are computed in one place:
the ``key`` property of the record models in ``app.models.records``.

DuckDB limitation
-----------------
Updating an indexed column (UNIQUE/PRIMARY KEY) of a row that is referenced by a foreign key fails.
Tables referenced by foreign keys (securities, watchlists, sources, query_history) therefore keep
constraints only on their immutable surrogate id; identity that must stay mutable (a ticker can change,
a watchlist can be renamed) is enforced by the single-writer repository inside a transaction instead.
"""

SCHEMA_VERSION = 2

# All timestamps are stored as naive UTC. DuckDB would otherwise convert ``current_timestamp`` using the
# session time zone, which on a user's machine is local time.
NOW_UTC = "(current_timestamp AT TIME ZONE 'UTC')"
# (table, column) pairs that default to NOW_UTC. v1 created these with a zone-dependent default.
TIMESTAMP_DEFAULT_COLUMNS = (
    ("sources", "retrieved_at"),
    ("securities", "updated_at"),
    ("query_history", "created_at"),
    ("research_runs", "started_at"),
    ("watchlists", "created_at"),
    ("watchlist_items", "added_at"),
)

# Tables in dependency order. ``schema_meta`` is bookkeeping, not a data table.
TABLES = (
    "sources",
    "securities",
    "financial_facts",
    "price_daily",
    "filings",
    "earnings",
    "ownership",
    "events",
    "query_history",
    "research_runs",
    "watchlists",
    "watchlist_items",
)
META_TABLE = "schema_meta"

# Tables whose key changed in v2. They had no write path in v1, so they are empty and can be recreated.
V2_RECREATED_TABLES = ("financial_facts", "earnings", "ownership", "events")
V1_OBSOLETE_SEQUENCES = ("seq_fact_id", "seq_earnings_id", "seq_ownership_id", "seq_event_id")


def _cik(col: str = "cik", *, required: bool = False) -> str:
    """CIK column definition: 10-digit zero-padded text, e.g. '0000320193'."""
    not_null = " NOT NULL" if required else ""
    return f"VARCHAR{not_null} CHECK ({col} IS NULL OR regexp_full_match({col}, '[0-9]{{10}}'))"


_SEQUENCES = [
    "CREATE SEQUENCE IF NOT EXISTS seq_source_id START 1",
    "CREATE SEQUENCE IF NOT EXISTS seq_security_id START 1",
    "CREATE SEQUENCE IF NOT EXISTS seq_query_id START 1",
    "CREATE SEQUENCE IF NOT EXISTS seq_run_id START 1",
    "CREATE SEQUENCE IF NOT EXISTS seq_watchlist_id START 1",
]

# Tables created or recreated by the v1 -> v2 migration. Kept separate so the migration can reuse them.
DDL_V2_TABLES = {
    "financial_facts": f"""CREATE TABLE IF NOT EXISTS financial_facts (
        fact_key      VARCHAR PRIMARY KEY CHECK (fact_key =
            cik || '|' || taxonomy || '|' || concept || '|' || unit || '|' ||
            coalesce(CAST(period_start AS VARCHAR), '') || '|' || CAST(period_end AS VARCHAR) || '|' ||
            coalesce(accession_no, '')),
        cik           {_cik(required=True)},
        taxonomy      VARCHAR NOT NULL,          -- 'us-gaap', 'dei', ...
        concept       VARCHAR NOT NULL,          -- e.g. 'Revenues'
        unit          VARCHAR NOT NULL,          -- 'USD', 'shares', 'USD/shares'
        value         DOUBLE NOT NULL,
        period_start  DATE,                      -- NULL for point-in-time (instant) facts
        period_end    DATE NOT NULL,
        fiscal_year   INTEGER,
        fiscal_period VARCHAR,                   -- 'FY', 'Q1'..'Q4'
        form          VARCHAR,
        filed_date    DATE,
        accession_no  VARCHAR,
        source_id     BIGINT REFERENCES sources(source_id)
    )""",
    "earnings": f"""CREATE TABLE IF NOT EXISTS earnings (
        cik              {_cik(required=True)},
        fiscal_year      INTEGER NOT NULL,
        fiscal_period    VARCHAR NOT NULL,
        report_date      DATE NOT NULL,
        eps_actual       DOUBLE,
        eps_estimate     DOUBLE,
        revenue_actual   DOUBLE,
        revenue_estimate DOUBLE,
        source_id        BIGINT REFERENCES sources(source_id),
        PRIMARY KEY (cik, fiscal_year, fiscal_period)
    )""",
    # Insider, institutional and beneficial-owner positions/transactions in an issuer.
    "ownership": f"""CREATE TABLE IF NOT EXISTS ownership (
        ownership_key    VARCHAR PRIMARY KEY CHECK (ownership_key =
            cik || '|' || holder_type || '|' || holder_key || '|' || CAST(as_of_date AS VARCHAR) || '|' ||
            coalesce(accession_no, '') || '|' || CAST(line_no AS VARCHAR)),
        cik              {_cik(required=True)},   -- the issuer
        holder_type      VARCHAR NOT NULL,        -- 'insider', 'institution', 'beneficial_owner'
        holder_key       VARCHAR NOT NULL,        -- holder_cik if known, else normalised holder name
        holder_name      VARCHAR NOT NULL,
        holder_cik       {_cik("holder_cik")},
        as_of_date       DATE NOT NULL,
        accession_no     VARCHAR,
        line_no          INTEGER NOT NULL DEFAULT 0,   -- 0 = the filing has a single line for this holder
        shares           DOUBLE,
        value_usd        DOUBLE,
        shares_change    DOUBLE,
        transaction_code VARCHAR,
        form             VARCHAR,
        source_id        BIGINT REFERENCES sources(source_id)
    )""",
    # Contracts, partnerships, acquisitions, lawsuits, regulatory items, news, ...
    "events": f"""CREATE TABLE IF NOT EXISTS events (
        event_key    VARCHAR PRIMARY KEY CHECK (
            event_key = coalesce(cik, '') || '|' || event_type || '|' || source_ref),
        cik          {_cik()},                    -- NULL = not tied to one issuer
        event_type   VARCHAR NOT NULL,
        source_ref   VARCHAR NOT NULL,            -- stable reference from the source (URL, accession+item, id)
        event_date   DATE,
        title        VARCHAR NOT NULL,
        summary      VARCHAR,
        url          VARCHAR,
        accession_no VARCHAR,
        source_id    BIGINT REFERENCES sources(source_id)
    )""",
}

DDL = [
    f"""CREATE TABLE IF NOT EXISTS {META_TABLE} (
        key   VARCHAR PRIMARY KEY,
        value VARCHAR NOT NULL
    )""",
    *_SEQUENCES,
    # Provenance: one row per retrieval of a dataset. MAX(retrieved_at) is the last sync.
    """CREATE TABLE IF NOT EXISTS sources (
        source_id    BIGINT PRIMARY KEY DEFAULT nextval('seq_source_id'),
        provider     VARCHAR NOT NULL,           -- 'sec', 'nasdaq', 'cboe', ...
        dataset      VARCHAR NOT NULL,           -- e.g. 'company_filings'
        url          VARCHAR,
        retrieved_at TIMESTAMP NOT NULL DEFAULT current_timestamp,
        content_hash VARCHAR,
        detail       VARCHAR
    )""",
    # Listed securities. Identity (ticker, cik) is enforced by the repository; see module docstring.
    f"""CREATE TABLE IF NOT EXISTS securities (
        security_id   BIGINT PRIMARY KEY DEFAULT nextval('seq_security_id'),
        cik           {_cik()},
        ticker        VARCHAR NOT NULL,
        name          VARCHAR,
        exchange      VARCHAR,
        security_type VARCHAR,
        sector        VARCHAR,
        industry      VARCHAR,
        sic           VARCHAR,
        is_active     BOOLEAN,                   -- NULL = unknown
        updated_at    TIMESTAMP NOT NULL DEFAULT current_timestamp
    )""",
    DDL_V2_TABLES["financial_facts"],
    # Raw daily bars for a listed security.
    """CREATE TABLE IF NOT EXISTS price_daily (
        security_id BIGINT NOT NULL REFERENCES securities(security_id),
        trade_date  DATE NOT NULL,
        open        DOUBLE,
        high        DOUBLE,
        low         DOUBLE,
        close       DOUBLE,
        adj_close   DOUBLE,
        volume      BIGINT,
        source_id   BIGINT REFERENCES sources(source_id),
        PRIMARY KEY (security_id, trade_date)
    )""",
    f"""CREATE TABLE IF NOT EXISTS filings (
        accession_no     VARCHAR PRIMARY KEY,
        cik              {_cik(required=True)},
        form             VARCHAR NOT NULL,
        filing_date      DATE NOT NULL,
        report_date      DATE,
        primary_document VARCHAR,
        description      VARCHAR,
        url              VARCHAR,
        source_id        BIGINT REFERENCES sources(source_id)
    )""",
    DDL_V2_TABLES["earnings"],
    DDL_V2_TABLES["ownership"],
    DDL_V2_TABLES["events"],
    # Every natural-language request typed into the terminal.
    """CREATE TABLE IF NOT EXISTS query_history (
        query_id       BIGINT PRIMARY KEY DEFAULT nextval('seq_query_id'),
        created_at     TIMESTAMP NOT NULL DEFAULT current_timestamp,
        query_text     VARCHAR NOT NULL,
        interpretation VARCHAR,                   -- JSON text, filled once interpreted
        status         VARCHAR NOT NULL DEFAULT 'received'
    )""",
    # One execution of a research workflow for a query.
    """CREATE TABLE IF NOT EXISTS research_runs (
        run_id      BIGINT PRIMARY KEY DEFAULT nextval('seq_run_id'),
        query_id    BIGINT REFERENCES query_history(query_id),
        started_at  TIMESTAMP NOT NULL DEFAULT current_timestamp,
        finished_at TIMESTAMP,
        status      VARCHAR NOT NULL DEFAULT 'pending',
        criteria    VARCHAR,                      -- JSON text
        result      VARCHAR,                      -- JSON text
        error       VARCHAR
    )""",
    # Name uniqueness is enforced in the repository (see module docstring).
    """CREATE TABLE IF NOT EXISTS watchlists (
        watchlist_id BIGINT PRIMARY KEY DEFAULT nextval('seq_watchlist_id'),
        name         VARCHAR NOT NULL,
        description  VARCHAR,
        created_at   TIMESTAMP NOT NULL DEFAULT current_timestamp
    )""",
    """CREATE TABLE IF NOT EXISTS watchlist_items (
        watchlist_id BIGINT NOT NULL REFERENCES watchlists(watchlist_id),
        security_id  BIGINT NOT NULL REFERENCES securities(security_id),
        added_at     TIMESTAMP NOT NULL DEFAULT current_timestamp,
        note         VARCHAR,
        PRIMARY KEY (watchlist_id, security_id)
    )""",
]

DDL = [statement.replace("DEFAULT current_timestamp", f"DEFAULT {NOW_UTC}") for statement in DDL]
