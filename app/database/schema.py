"""DuckDB schema for the research terminal.

Design rules
------------
* Raw facts and calculated values stay separate. Tables below hold *raw* data as
  reported by a source (SEC filings, price feeds, ...). Ratios and growth rates
  (P/S, EPS growth, margins, % off the 52-week high, ...) are computed at query
  time in ``app/screening`` and are never stored as per-ratio tables.
* Identity: ``cik`` (10-digit, zero-padded text) identifies the issuer and is the key
  for issuer-level data (facts, filings, earnings, ownership, events). A ticker is
  only an attribute of a listed security and can change or be reused, so listed-security
  data (``price_daily``, watchlists) is keyed by the surrogate ``security_id``.
  One issuer can have several securities (e.g. GOOG / GOOGL share one CIK).
* Missing real data is NULL. Nothing here is pre-populated.
* DuckDB limitation: updating an indexed column (e.g. a UNIQUE name) of a row that is
  referenced by a foreign key fails. Therefore only surrogate primary keys carry
  constraints that child tables reference; natural-key uniqueness that must stay
  mutable (watchlist names) is enforced in the repository instead.
"""

SCHEMA_VERSION = 1

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


def _cik(col: str = "cik", *, required: bool = False) -> str:
    """CIK column definition: 10-digit zero-padded text, e.g. '0000320193'."""
    not_null = " NOT NULL" if required else ""
    return f"VARCHAR{not_null} CHECK ({col} IS NULL OR regexp_full_match({col}, '[0-9]{{10}}'))"


DDL = [
    f"""CREATE TABLE IF NOT EXISTS {META_TABLE} (
        key   VARCHAR PRIMARY KEY,
        value VARCHAR NOT NULL
    )""",
    "CREATE SEQUENCE IF NOT EXISTS seq_source_id START 1",
    "CREATE SEQUENCE IF NOT EXISTS seq_security_id START 1",
    "CREATE SEQUENCE IF NOT EXISTS seq_fact_id START 1",
    "CREATE SEQUENCE IF NOT EXISTS seq_earnings_id START 1",
    "CREATE SEQUENCE IF NOT EXISTS seq_ownership_id START 1",
    "CREATE SEQUENCE IF NOT EXISTS seq_event_id START 1",
    "CREATE SEQUENCE IF NOT EXISTS seq_query_id START 1",
    "CREATE SEQUENCE IF NOT EXISTS seq_run_id START 1",
    "CREATE SEQUENCE IF NOT EXISTS seq_watchlist_id START 1",
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
    # Listed securities. cik is the preferred issuer identity; ticker is just a label.
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
    # Raw reported facts in long (XBRL-like) form. No derived values.
    f"""CREATE TABLE IF NOT EXISTS financial_facts (
        fact_id      BIGINT PRIMARY KEY DEFAULT nextval('seq_fact_id'),
        cik          {_cik(required=True)},
        taxonomy     VARCHAR NOT NULL,           -- 'us-gaap', 'dei', ...
        concept      VARCHAR NOT NULL,           -- e.g. 'Revenues'
        unit         VARCHAR NOT NULL,           -- 'USD', 'shares', 'USD/shares'
        value        DOUBLE NOT NULL,
        period_start DATE,                       -- NULL for point-in-time (instant) facts
        period_end   DATE NOT NULL,
        fiscal_year  INTEGER,
        fiscal_period VARCHAR,                   -- 'FY', 'Q1'..'Q4'
        form         VARCHAR,
        filed_date   DATE,
        accession_no VARCHAR,
        source_id    BIGINT REFERENCES sources(source_id)
    )""",
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
    f"""CREATE TABLE IF NOT EXISTS earnings (
        earnings_id      BIGINT PRIMARY KEY DEFAULT nextval('seq_earnings_id'),
        cik              {_cik(required=True)},
        report_date      DATE NOT NULL,
        fiscal_year      INTEGER,
        fiscal_period    VARCHAR,
        eps_actual       DOUBLE,
        eps_estimate     DOUBLE,
        revenue_actual   DOUBLE,
        revenue_estimate DOUBLE,
        source_id        BIGINT REFERENCES sources(source_id)
    )""",
    # Insider, institutional and beneficial-owner positions/transactions in an issuer.
    f"""CREATE TABLE IF NOT EXISTS ownership (
        ownership_id     BIGINT PRIMARY KEY DEFAULT nextval('seq_ownership_id'),
        cik              {_cik(required=True)},   -- the issuer
        holder_name      VARCHAR NOT NULL,
        holder_cik       {_cik("holder_cik")},
        holder_type      VARCHAR NOT NULL,        -- 'insider', 'institution', 'beneficial_owner'
        as_of_date       DATE NOT NULL,
        shares           DOUBLE,
        value_usd        DOUBLE,
        shares_change    DOUBLE,
        transaction_code VARCHAR,
        form             VARCHAR,
        accession_no     VARCHAR,
        source_id        BIGINT REFERENCES sources(source_id)
    )""",
    # Contracts, partnerships, acquisitions, lawsuits, regulatory items, news, ...
    f"""CREATE TABLE IF NOT EXISTS events (
        event_id     BIGINT PRIMARY KEY DEFAULT nextval('seq_event_id'),
        cik          {_cik()},                    -- NULL = not tied to one issuer
        event_type   VARCHAR NOT NULL,
        event_date   DATE,
        title        VARCHAR NOT NULL,
        summary      VARCHAR,
        url          VARCHAR,
        accession_no VARCHAR,
        source_id    BIGINT REFERENCES sources(source_id)
    )""",
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
