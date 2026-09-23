"""mkvkit.logging -- standard library logging, configured once, redacted always.

A library only ever takes a module logger. Each entry point calls
:func:`configure_logging` exactly once, with the verbosity switches, an
optional log file, and a JSON-lines mode so a detached run can be parsed
afterwards instead of read.

A redacting filter is installed unconditionally. It holds the resolved secret
values and replaces them everywhere a record can carry text -- the message,
its arguments, and the formatted traceback -- so a token cannot reach a log
file through a request header in an exception. Redaction is a property of the
logging setup rather than a rule people follow at each call site, because the
call sites that leak are the ones nobody wrote on purpose.

Printing survives in exactly one place: result tables on standard output,
which are data rather than logging.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

__all__ = [
    "REDACTED",
    "JsonLinesFormatter",
    "PlainFormatter",
    "RedactingFilter",
    "clear_secrets",
    "configure_logging",
    "redact",
    "register_secret",
]

REDACTED = "***"

#: Shorter than this and a "secret" would redact ordinary words.
MIN_SECRET_LENGTH = 6

_SECRETS: list[str] = []


def register_secret(value: str | None) -> None:
    """Add a resolved secret to the redaction set. Safe to call repeatedly."""
    if not value or len(value) < MIN_SECRET_LENGTH:
        return
    if value not in _SECRETS:
        _SECRETS.append(value)
        _SECRETS.sort(key=len, reverse=True)  # longest first, so a prefix cannot win


def clear_secrets() -> None:
    """Forget every registered secret. A long-lived process has no use for this;
    a test that must not leak one into the next test does."""
    _SECRETS.clear()


def redact(text: str) -> str:
    for secret in _SECRETS:
        text = text.replace(secret, REDACTED)
    return text


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, tuple):
        return tuple(_redact_value(v) for v in value)
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    return value


class RedactingFilter(logging.Filter):
    """Replace every registered secret in anything a record carries."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not _SECRETS:
            return True
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            record.args = _redact_value(record.args)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


class PlainFormatter(logging.Formatter):
    """The human format, with the final text redacted as well as the record."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


class JsonLinesFormatter(logging.Formatter):
    """One JSON object per line: a detached run is parsed, not read."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return redact(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def configure_logging(
    level: int = logging.INFO,
    *,
    file: Path | str | None = None,
    json: bool = False,
    stream: TextIO | None = None,
    secrets: Iterable[str] = (),
) -> logging.Logger:
    """Configure the root logger once, for one entry point.

    Existing handlers are replaced rather than added to, so calling this twice
    in one process (a test, a wrapper script) does not double every line.
    """
    for secret in secrets:
        register_secret(secret)

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.setLevel(level)

    redacting = RedactingFilter()
    console = logging.StreamHandler(stream if stream is not None else sys.stderr)
    console.setLevel(level)
    console.addFilter(redacting)
    console.setFormatter(
        JsonLinesFormatter() if json else PlainFormatter("%(levelname)-7s %(name)s: %(message)s")
    )
    root.addHandler(console)

    if file is not None:
        path = Path(file)
        path.parent.mkdir(parents=True, exist_ok=True)
        # utf-8 and \n everywhere: a log read on the other platform is still a log
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.terminator = "\n"
        file_handler.setLevel(level)
        file_handler.addFilter(redacting)
        file_handler.setFormatter(
            JsonLinesFormatter()
            if json
            else PlainFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        )
        root.addHandler(file_handler)

    return root
