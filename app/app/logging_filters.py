"""Log filters shared across the project.

RedactSecretsFilter exists because Notification.notification currently stores
raw upstream response bodies (which can echo back access tokens), and print()
calls elsewhere have historically dumped request payloads containing
credentials. This filter is a last line of defense: it scrubs common secret
shapes out of any log record before it reaches a handler.
"""

import logging
import re

_PATTERNS = [
    re.compile(r"(Bearer\s+)[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE),
    re.compile(
        r"(\"?(?:access_token|refresh_token|api_key|secret|password)\"?\s*[:=]\s*\"?)[^\s\"',}]+",
        re.IGNORECASE,
    ),
]

_REPLACEMENT = r"\1[REDACTED]"


class RedactSecretsFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self._redact(record.msg)
        if record.args:
            record.args = tuple(
                self._redact(arg) if isinstance(arg, str) else arg for arg in record.args
            )
        return True

    @staticmethod
    def _redact(text: str) -> str:
        for pattern in _PATTERNS:
            text = pattern.sub(_REPLACEMENT, text)
        return text
