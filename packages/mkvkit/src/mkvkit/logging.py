"""mkvkit.logging -- stdlib logging, configured once, with a redacting filter.

Libraries only ever take a module logger. Each command-line entry point
configures logging once and takes the usual verbosity switches, plus a log
file and a JSON-lines mode so a detached run can be parsed afterwards.

A redacting filter is installed unconditionally: it holds the resolved secret
values and replaces them in any record it sees, so a token cannot reach a log
file even through an exception's representation of a request header.

Printing survives in exactly one place: result tables on standard output,
which are data rather than logging.

Planned public API:
    configure_logging(level, *, file=None, json=False) -> None
    RedactingFilter

Status: skeleton. The behaviour lands in a later milestone; this file exists
so the history shows when the work it comes from was actually done.
"""

from __future__ import annotations

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

__all__: list[str] = []


def _not_yet(name: str) -> None:
    raise NotImplementedError(f"{name}: skeleton only, see the module docstring")
