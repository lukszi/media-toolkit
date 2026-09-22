"""jfkit.report -- one shape for every survey, and its caveats in the document.

Every survey produces the same object: a name, a generation time, the scope
it covered, typed columns, rows, a summary and a list of caveats. It renders
to TSV, CSV, JSON, Markdown and HTML from one call, with deterministic
ordering so two runs diff cleanly.

The caveats travel INSIDE the document. A survey without its caveats is how a
number fitted to one collection becomes a claimed universal three documents
later.

Templates are format strings. A handful of reports do not justify a template
engine.

Planned public API:
    @dataclass Survey: name, generated, scope, columns, rows, summary, caveats
    render(survey, fmt) -> str
    write(survey, outdir) -> list[Path]

Status: skeleton. The behaviour lands in a later milestone; this file exists
so the history shows when the work it comes from was actually done.
"""

from __future__ import annotations

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

__all__: list[str] = []


def _not_yet(name: str) -> None:
    raise NotImplementedError(f"{name}: skeleton only, see the module docstring")
