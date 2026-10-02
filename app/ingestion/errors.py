"""Failure classes of the ingestion path. Each maps to a handler and a counter in the P0 report; none is swallowed."""


class IngestionError(Exception):
    """Base class. ``kind`` is the stable machine-readable class used in the report."""

    kind = "INGESTION_ERROR"


class AccessDeniedError(IngestionError):
    """SEC HTTP 403: the caller is not accepted (missing/invalid User-Agent contact, or blocked). Never retried."""

    kind = "HTTP_403"


class RateLimitedError(IngestionError):
    """HTTP 429 that persisted after the bounded retries."""

    kind = "HTTP_429"


class NotFoundError(IngestionError):
    """HTTP 404: the resource does not exist (e.g. no companyfacts for an issuer). Not retried."""

    kind = "HTTP_404"


class ServerError(IngestionError):
    kind = "HTTP_5XX"


class NetworkError(IngestionError):
    kind = "NETWORK"


class MalformedResponseError(IngestionError):
    kind = "MALFORMED_RESPONSE"


class MissingUserAgentError(IngestionError):
    """SEC_USER_AGENT is not configured: live SEC ingestion cannot proceed. No contact is ever invented."""

    kind = "SEC_USER_AGENT_MISSING"


class IdentityError(IngestionError):
    """A symbol cannot be resolved to exactly one CIK, or resolves to a different CIK than the audit recorded."""

    kind = "IDENTITY"


class EmptyDataError(IngestionError):
    kind = "EMPTY_DATA"


class NoFactsError(IngestionError):
    """The document is valid but holds none of the allow-listed concepts (e.g. a foreign filer reporting under IFRS)."""

    kind = "NO_ALLOWLISTED_FACTS"
