"""Error taxonomy for the AI layer — deliberately small. Unlike
connectors.errors.ConnectorError (which classifies failures for retry
routing), a failed AI call is never retried automatically: the caller
(gateway.run_text/run_image) releases the quota reservation and lets the
request fail, and the user just tries again."""

from __future__ import annotations


class AIError(Exception):
    """Base for everything this package raises. `safe_detail` is fit for an
    API response; provider response bodies are never included in it since
    they can echo request content back (and, for a misconfigured request,
    occasionally the key itself in an error message)."""

    def __init__(self, safe_detail: str):
        super().__init__(safe_detail)
        self.safe_detail = safe_detail


class AIConfigurationError(AIError):
    """No usable credentials for the requested provider — neither a BYOK key
    nor a managed key configured server-side."""


class AIProviderError(AIError):
    """The provider rejected the request or returned something unusable."""


class QuotaExceeded(AIError):
    pass
