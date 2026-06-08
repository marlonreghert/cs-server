"""Redact secrets from log records.

`httpx` logs full outbound request URLs at INFO — which carry the BestTime
private key (`api_key_private=pri_...`) and the Google Places key (`key=AIza...`)
as query params — and some API clients log request params at DEBUG. A
handler-level filter masks the secret VALUES in the final message so credentials
never reach the logs, regardless of which logger emitted the record.

Attach it to the root handler(s) (see `install_secret_redaction`): handler
filters see every propagated record (httpx, app loggers, ...), whereas a logger
filter would not catch records propagated from child loggers.
"""
from __future__ import annotations

import logging
import re

# (compiled pattern, replacement) — applied to the fully-formatted message.
_REDACTIONS: list[tuple[re.Pattern, str]] = [
    # Secret-bearing query params / kwargs: mask the value (stop at &, space, quote).
    (re.compile(
        r'(api_key_private|api_key|apikey|access_token|token|password)=[^&\s"\'<>}]+',
        re.IGNORECASE,
    ), r"\1=***REDACTED***"),
    # Google Maps/Places `key=` param — scoped to the AIza-prefixed key value so
    # benign `key=...` log lines (e.g. key=venue_id) are left untouched.
    (re.compile(r"\bkey=AIza[\w-]+"), "key=***REDACTED***"),
    # Value-shaped secrets, masked wherever they appear (e.g. inside a params dict
    # repr): BestTime private keys (pri_<hex>) and Google API keys (AIza...).
    (re.compile(r"\bpri_[0-9a-fA-F]{16,}\b"), "pri_***REDACTED***"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}\b"), "AIza***REDACTED***"),
]


class SecretRedactingFilter(logging.Filter):
    """Mask known secret values in the final log message. Never drops a record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed record; never block logging
            return True
        redacted = message
        for pattern, repl in _REDACTIONS:
            redacted = pattern.sub(repl, redacted)
        if redacted != message:
            # Replace the formatted message and drop args so the handler's
            # formatter re-emits the redacted text verbatim.
            record.msg = redacted
            record.args = ()
        return True


def install_secret_redaction(logger: logging.Logger | None = None) -> None:
    """Attach the redaction filter to every handler of `logger` (root by default).
    Idempotent — a handler is never given two of these filters."""
    target = logger if logger is not None else logging.getLogger()
    for handler in target.handlers:
        if not any(isinstance(f, SecretRedactingFilter) for f in handler.filters):
            handler.addFilter(SecretRedactingFilter())
