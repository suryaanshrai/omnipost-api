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
    # Telegram bot tokens live in the URL path: /bot<id>:<secret>/
    re.compile(r"(/bot)\d+:[A-Za-z0-9_-]+"),
    # Discord webhook URLs: /api/webhooks/<id>/<token>
    re.compile(r"(/api/webhooks/\d+/)[A-Za-z0-9_-]+"),
    re.compile(
        r"(\"?(?:access_token|refresh_token|api_key|secret|password)\"?\s*[:=]\s*\"?)[^\s\"',}]+",
        re.IGNORECASE,
    ),
]

_REPLACEMENT = r"\1[REDACTED]"


_formatter = logging.Formatter()


class RedactSecretsFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self._redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: self._redact(v) if isinstance(v, str) else v for k, v in record.args.items()}
            else:
                record.args = tuple(self._redact(arg) if isinstance(arg, str) else arg for arg in record.args)
        # Tracebacks carry request URLs (with tokens in them) in exception
        # messages. Formatters reuse exc_text when it's set, so redacting it
        # here covers every handler.
        if record.exc_info and not record.exc_text:
            record.exc_text = _formatter.formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self._redact(record.exc_text)
        return True

    @staticmethod
    def _redact(text: str) -> str:
        for pattern in _PATTERNS:
            text = pattern.sub(_REPLACEMENT, text)
        return text
