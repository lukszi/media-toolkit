"""The logging setup, and the filter that makes a leaked token impossible.

The test that matters is the last one: a secret that only ever appears inside
an exception must still not reach the log, because that is the path nobody
writes on purpose.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import io
import json
import logging
from pathlib import Path

from mkvkit.logging import (
    REDACTED,
    clear_secrets,
    configure_logging,
    redact,
    register_secret,
)

PLANTED = "value-that-must-not-appear"


def test_a_registered_secret_never_reaches_the_stream() -> None:
    stream = io.StringIO()
    configure_logging(logging.INFO, stream=stream, secrets=[PLANTED])
    logging.getLogger("example").info("calling with %s", PLANTED)
    written = stream.getvalue()
    assert PLANTED not in written
    assert REDACTED in written


def test_a_secret_inside_an_exception_is_redacted_too() -> None:
    stream = io.StringIO()
    configure_logging(logging.INFO, stream=stream, secrets=[PLANTED])
    log = logging.getLogger("example")
    try:
        raise RuntimeError(f"Authorization header was {PLANTED}")
    except RuntimeError:
        log.exception("the request failed")
    written = stream.getvalue()
    assert PLANTED not in written
    assert REDACTED in written


def test_json_lines_are_parsable_and_redacted() -> None:
    stream = io.StringIO()
    configure_logging(logging.INFO, stream=stream, json=True, secrets=[PLANTED])
    logging.getLogger("example").warning("token is %s", PLANTED)
    record = json.loads(stream.getvalue().strip())
    assert record["level"] == "WARNING"
    assert record["logger"] == "example"
    assert PLANTED not in record["message"]
    assert "time" in record


def test_a_log_file_is_written_in_utf8(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "run.log"
    configure_logging(logging.INFO, file=path, stream=io.StringIO(), secrets=[PLANTED])
    logging.getLogger("example").info("a cue said %s and %s", "grüß", PLANTED)
    logging.shutdown()
    written = path.read_text(encoding="utf-8")
    assert "grüß" in written
    assert PLANTED not in written


def test_configuring_twice_does_not_double_every_line() -> None:
    stream = io.StringIO()
    configure_logging(logging.INFO, stream=stream)
    configure_logging(logging.INFO, stream=stream)
    logging.getLogger("example").info("once")
    assert stream.getvalue().count("once") == 1


def test_a_short_value_is_not_treated_as_a_secret() -> None:
    clear_secrets()
    register_secret("abc")
    assert redact("abc def") == "abc def"


def test_verbosity_is_the_level_it_says() -> None:
    stream = io.StringIO()
    configure_logging(logging.WARNING, stream=stream)
    logging.getLogger("example").info("not shown")
    logging.getLogger("example").warning("shown")
    written = stream.getvalue()
    assert "not shown" not in written
    assert "shown" in written
