"""jfkit.report -- one shape for every survey, and its caveats inside the document.

Every survey in this package produces the same object: a name, the moment it
was generated, the scope it actually covered, typed columns, rows, a summary
and a list of caveats. One shape means one renderer, one set of tests, and a
second survey that costs an afternoon rather than a week.

**The caveats travel inside the document.** Not in the code that produced it,
not in a message somebody wrote in a chat window. A survey is copied,
attached, pasted into an issue and quoted back six months later, and by then
the only thing that can still say "this counted episodes, not files, and it
skipped the libraries the server would not list" is the document itself.
This is how a number fitted to one collection becomes a claimed universal, and
putting the caveats in the same file is the cheapest thing that prevents it.

**The scope is data, not prose.** What was asked for, how many answered, what
was left out and why. A survey whose scope is "the library" cannot be
compared with the same survey run next month.

**Deterministic, so two runs diff cleanly.** Columns in declared order, rows
in the order the survey produced them, numbers formatted the same way every
time, and no timestamp anywhere but the one field that is meant to be one.

Templates are format strings. A handful of reports do not justify a template
engine, and a dependency taken to save thirty lines is a dependency that
outlives the thirty lines.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import csv
import html
import io
import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

__all__ = [
    "FORMATS",
    "Column",
    "Format",
    "Survey",
    "render",
    "write",
]

log = logging.getLogger(__name__)

Format = Literal["tsv", "csv", "json", "md", "html"]

#: Every format a survey renders to, and the extension each one is written at.
FORMATS: Mapping[str, str] = {
    "tsv": ".tsv", "csv": ".csv", "json": ".json", "md": ".md", "html": ".html"
}

#: How a missing value is written, in the formats that are read by people.
ABSENT = ""


@dataclass(frozen=True)
class Column:
    """One column: where the value comes from, and how to read it."""

    key: str
    title: str
    kind: Literal["text", "number", "bool"] = "text"
    #: decimal places, for a number column. ``None`` prints it as it is.
    places: int | None = None

    def format(self, value: Any) -> str:
        if value is None:
            return ABSENT
        if self.kind == "bool":
            return "yes" if value else "no"
        if self.kind == "number" and self.places is not None:
            return f"{float(value):.{self.places}f}"
        return str(value)


@dataclass
class Survey:
    """One survey's whole answer, in the shape every survey here produces."""

    name: str
    columns: Sequence[Column]
    rows: Sequence[Mapping[str, Any]] = ()
    summary: Mapping[str, Any] = field(default_factory=dict)
    caveats: Sequence[str] = ()
    scope: Mapping[str, Any] = field(default_factory=dict)
    generated: datetime = field(
        default_factory=lambda: datetime.now(UTC).replace(microsecond=0)
    )
    #: One sentence saying what the survey answers. It heads the document.
    about: str = ""

    def __post_init__(self) -> None:
        keys = {column.key for column in self.columns}
        for index, row in enumerate(self.rows):
            unknown = sorted(set(row) - keys)
            if unknown:
                raise ValueError(
                    f"{self.name}: row {index} carries {unknown}, which no column "
                    "describes. Add the column or drop the value; a field nothing "
                    "renders is a field nobody sees."
                )

    @property
    def stamp(self) -> str:
        return self.generated.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    def cells(self, row: Mapping[str, Any]) -> list[str]:
        return [column.format(row.get(column.key)) for column in self.columns]

    def __str__(self) -> str:
        return f"{self.name}: {len(self.rows)} row(s), generated {self.stamp}"


# ----------------------------------------------------------------- rendering
def _delimited(survey: Survey, delimiter: str) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=delimiter, lineterminator="\n")
    writer.writerow([column.title for column in survey.columns])
    for row in survey.rows:
        writer.writerow(survey.cells(row))
    return buffer.getvalue()


def _json(survey: Survey) -> str:
    payload = {
        "survey": survey.name,
        "about": survey.about,
        "generated": survey.stamp,
        "scope": dict(survey.scope),
        "columns": [
            {"key": c.key, "title": c.title, "kind": c.kind} for c in survey.columns
        ],
        "summary": dict(survey.summary),
        "caveats": list(survey.caveats),
        "rows": [
            {column.key: row.get(column.key) for column in survey.columns}
            for row in survey.rows
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=False) + "\n"


def _markdown(survey: Survey) -> str:
    lines = [f"# {survey.name}", ""]
    if survey.about:
        lines += [survey.about, ""]
    lines += [f"Generated {survey.stamp}.", ""]
    if survey.scope:
        lines += ["## Scope", ""]
        lines += [f"- **{key}**: {value}" for key, value in survey.scope.items()]
        lines.append("")
    if survey.summary:
        lines += ["## Summary", ""]
        lines += [f"- **{key}**: {value}" for key, value in survey.summary.items()]
        lines.append("")
    lines += ["## Rows", ""]
    lines.append("| " + " | ".join(c.title for c in survey.columns) + " |")
    lines.append("|" + "|".join("---" for _ in survey.columns) + "|")
    for row in survey.rows:
        lines.append("| " + " | ".join(_escape_cell(c) for c in survey.cells(row)) + " |")
    lines.append("")
    lines += ["## Caveats", ""]
    lines += [f"- {caveat}" for caveat in survey.caveats] or [
        "- None were recorded, which is itself worth doubting."
    ]
    lines.append("")
    return "\n".join(lines)


def _escape_cell(text: str) -> str:
    """A cell containing the column separator would end the row early."""
    return text.replace("|", "\\|").replace("\n", " ")


def _html(survey: Survey) -> str:
    def cell(text: str) -> str:
        return html.escape(text)

    lines = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        f"<title>{cell(survey.name)}</title>",
        "<style>body{font-family:system-ui,sans-serif;margin:2rem;max-width:70rem}"
        "table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:.25rem .5rem}"
        "th{text-align:left}</style></head><body>",
        f"<h1>{cell(survey.name)}</h1>",
    ]
    if survey.about:
        lines.append(f"<p>{cell(survey.about)}</p>")
    lines.append(f"<p>Generated {cell(survey.stamp)}.</p>")
    for title, mapping in (("Scope", survey.scope), ("Summary", survey.summary)):
        if mapping:
            lines.append(f"<h2>{title}</h2><ul>")
            lines += [
                f"<li><b>{cell(str(k))}</b>: {cell(str(v))}</li>"
                for k, v in mapping.items()
            ]
            lines.append("</ul>")
    lines.append("<h2>Rows</h2><table><thead><tr>")
    lines += [f"<th>{cell(c.title)}</th>" for c in survey.columns]
    lines.append("</tr></thead><tbody>")
    for row in survey.rows:
        lines.append(
            "<tr>" + "".join(f"<td>{cell(v)}</td>" for v in survey.cells(row)) + "</tr>"
        )
    lines.append("</tbody></table>")
    lines.append("<h2>Caveats</h2><ul>")
    lines += [f"<li>{cell(caveat)}</li>" for caveat in survey.caveats]
    lines.append("</ul></body></html>")
    return "\n".join(lines) + "\n"


def render(survey: Survey, fmt: Format) -> str:
    """One survey, one format. The same bytes every time for the same survey."""
    if fmt == "tsv":
        return _delimited(survey, "\t")
    if fmt == "csv":
        return _delimited(survey, ",")
    if fmt == "json":
        return _json(survey)
    if fmt == "md":
        return _markdown(survey)
    if fmt == "html":
        return _html(survey)
    raise ValueError(f"{fmt}: not one of {', '.join(sorted(FORMATS))}")


def write(
    survey: Survey,
    outdir: Path | str,
    *,
    formats: Iterable[str] = tuple(FORMATS),
    stem: str | None = None,
) -> list[Path]:
    """Write every format at once, and return what was written.

    Every format from one call because the tabular one is for the next
    program, the readable one is for the person who asked, and a survey that
    exists in only one of them gets converted by hand, badly, at some point.
    """
    directory = Path(outdir)
    directory.mkdir(parents=True, exist_ok=True)
    base = stem or survey.name.lower().replace(" ", "-")
    written: list[Path] = []
    for fmt in formats:
        if fmt not in FORMATS:
            raise ValueError(f"{fmt}: not one of {', '.join(sorted(FORMATS))}")
        path = directory / f"{base}{FORMATS[fmt]}"
        path.write_text(render(survey, fmt), encoding="utf-8", newline="\n")  # type: ignore[arg-type]
        written.append(path)
    log.info("%s: wrote %d file(s) to %s", survey.name, len(written), directory)
    return written
