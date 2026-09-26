"""jfkit.leftovers.missing -- what the library should hold and does not. Read-only.

Four kinds of absence, each from a source that can be trusted for it:

``no-file``
    A film or an episode row with no path at all. When the server marks the
    row virtual (``LocationType`` ``Virtual``) it knows the episode from its
    metadata provider only; otherwise a file it once had is gone.
``file-gone``
    A row whose path lies under a library folder that was walked, where the
    walk did not see the file. Nothing is asked of the disk beyond the walk,
    so a path below a folder the walk did not enter (a junction, an
    exclude) is never called gone -- it was not looked at.
``season-gap``
    A season whose episode numbers skip: rows for 1, 2 and 4 and none for
    3. Computed from the rows, so it needs nothing from the provider; a
    number a virtual row stands for is not a gap, it is a ``no-file``.
``release-without-item``
    A release folder with description files or artwork and no video, whose
    release no catalogue row matches -- or matches only a row whose file is
    missing. The release may be missing from the library entirely.

The server's own "show missing episodes" display setting decides only what
a client shows; it is not a source here. Virtual rows are asked for
explicitly, and a server that does not answer that question leaves the gap
kind to do the work.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import csv
import io
import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..client import Client
from ..safedelete.catalogue import CATALOGUE_FIELDS, Catalogue, path_key
from .scan import Finding, Kept, Scan

__all__ = [
    "COLUMNS",
    "KINDS",
    "Missing",
    "fetch_virtual",
    "find_missing",
    "render",
    "write",
]

log = logging.getLogger(__name__)

KINDS = ("no-file", "file-gone", "season-gap", "release-without-item")

COLUMNS = ("kind", "type", "series", "season", "episode", "name", "path", "id", "detail")


@dataclass(frozen=True)
class Missing:
    """One absence, and what shows it."""

    kind: str
    name: str
    detail: str
    type: str = ""
    series: str = ""
    season: int | None = None
    episode: int | None = None
    path: str = ""
    id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {column: getattr(self, column) for column in COLUMNS}


def fetch_virtual(client: Client) -> list[dict[str, Any]]:
    """The episode rows the server marks missing, asked for explicitly.

    A server that refuses the filter answers nothing here, and the report
    says so rather than guessing.
    """
    try:
        return list(client.items(
            recursive=True, includeItemTypes="Episode", isMissing="true",
            fields=",".join(CATALOGUE_FIELDS),
        ))
    except Exception as exc:  # the question is optional; its absence is said
        log.warning("the server would not list its missing episodes: %s", exc)
        return []


def _slot(row: Mapping[str, Any]) -> tuple[str, int | None, int | None]:
    season = row.get("ParentIndexNumber")
    number = row.get("IndexNumber")
    return (
        str(row.get("SeriesName") or ""),
        int(season) if season is not None else None,
        int(number) if number is not None else None,
    )


def _entry(kind: str, row: Mapping[str, Any], detail: str) -> Missing:
    series, season, number = _slot(row)
    return Missing(
        kind=kind, name=str(row.get("Name") or ""), detail=detail,
        type=str(row.get("Type") or ""), series=series, season=season,
        episode=number, path=str(row.get("Path") or ""), id=str(row.get("Id") or ""),
    )


def _under(key: str, roots: Iterable[str]) -> bool:
    return any(key == r or key.startswith(r.rstrip("/") + "/") for r in roots)


def find_missing(
    catalogue: Catalogue,
    scan: Scan | None = None,
    *,
    virtual: Sequence[Mapping[str, Any]] = (),
) -> list[Missing]:
    """Every absence of the four kinds, catalogue order within each kind."""
    out: list[Missing] = []
    rows = {str(r.get("Id")): dict(r) for r in catalogue.of_type("Movie", "Episode")}
    for row in virtual:
        rows.setdefault(str(row.get("Id")), dict(row))

    walked = [path_key(r) for r in (scan.roots if scan is not None else [])]
    for row in rows.values():
        raw = row.get("Path")
        if not raw:
            virtual_row = row.get("LocationType") == "Virtual" or row.get("IsMissing")
            out.append(_entry("no-file", row, (
                "a virtual row: the server knows it from its provider, and has no file"
                if virtual_row else "the row has no path: the file it had is gone"
            )))
            continue
        key = path_key(str(raw))
        if scan is not None and _under(key, walked) and key not in scan.seen \
                and not scan.unseen(str(raw)):
            out.append(_entry("file-gone", row, "the walk of its library folder did "
                              "not find the file"))

    out += _gaps(rows.values())

    if scan is not None:
        things: list[Finding | Kept] = [*scan.findings, *scan.kept]
        for thing in things:
            evidence = thing.evidence
            if evidence is None or evidence.status == "elsewhere":
                continue
            detail = "; ".join(evidence.lines())
            out.append(Missing(
                kind="release-without-item", name=thing.path.name, detail=detail,
                type="Folder", path=str(thing.path),
            ))
    order = {kind: n for n, kind in enumerate(KINDS)}
    out.sort(key=lambda m: (order[m.kind], m.series, m.season or 0, m.episode or 0,
                            m.name, m.path))
    return out


def _gaps(rows: Iterable[Mapping[str, Any]]) -> list[Missing]:
    seasons: dict[tuple[str, str, int], set[int]] = {}
    names: dict[tuple[str, str, int], str] = {}
    for row in rows:
        if row.get("Type") != "Episode":
            continue
        season, number = row.get("ParentIndexNumber"), row.get("IndexNumber")
        if season is None or number is None:
            continue
        key = (str(row.get("SeriesId") or row.get("SeriesName") or ""),
               str(row.get("SeriesName") or ""), int(season))
        names[key] = str(row.get("SeriesName") or "")
        last = row.get("IndexNumberEnd")
        span = range(int(number), int(last) + 1) if last is not None \
            and int(last) >= int(number) else range(int(number), int(number) + 1)
        seasons.setdefault(key, set()).update(span)
    out: list[Missing] = []
    for key, numbers in seasons.items():
        _sid, series, season = key
        if season == 0:
            continue  # specials are numbered by air date, not in a run
        for number in range(1, max(numbers) + 1):
            if number not in numbers:
                out.append(Missing(
                    kind="season-gap", name=f"S{season:02d}E{number:02d}",
                    detail=f"no row for episode {number}; the season has rows up to "
                           f"{max(numbers)}",
                    type="Episode", series=series, season=season, episode=number,
                ))
    return out


def render(found: Sequence[Missing], form: str) -> str:
    """``table`` for a person, ``tsv`` or ``json`` for whatever reads it next."""
    rows = [m.as_dict() for m in found]
    if form == "json":
        return json.dumps(rows, ensure_ascii=False, indent=1)
    if form == "tsv":
        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter="\t", lineterminator="\n")
        writer.writerow(COLUMNS)
        for row in rows:
            writer.writerow(["" if row[c] is None else row[c] for c in COLUMNS])
        return buffer.getvalue().rstrip("\n")
    lines: list[str] = []
    counts = {kind: sum(1 for m in found if m.kind == kind) for kind in KINDS}
    lines.append("missing: " + ", ".join(f"{counts[k]} {k}" for k in KINDS))
    for kind in KINDS:
        these = [m for m in found if m.kind == kind]
        if not these:
            continue
        lines.append("")
        lines.append(f"{kind} ({len(these)})")
        if kind == "season-gap":
            lines += _gap_lines(these)
            continue
        for m in these:
            slot = (
                f"{m.series} S{m.season:02d}E{m.episode:02d} "
                if m.season is not None and m.episode is not None and m.series
                else ""
            )
            name = m.name if kind != "season-gap" else ""
            where = f"  [{m.path}]" if m.path else ""
            lines.append(f"  {slot}{name}{where}".rstrip())
            lines.append(f"      {m.detail}")
    return "\n".join(lines)


def _gap_lines(gaps: Sequence[Missing]) -> list[str]:
    """One line per season, the missing numbers folded into runs."""
    seasons: dict[tuple[str, int], list[int]] = {}
    for gap in gaps:
        if gap.season is not None and gap.episode is not None:
            seasons.setdefault((gap.series, gap.season), []).append(gap.episode)
    lines: list[str] = []
    for (series, season), numbers in seasons.items():
        runs: list[str] = []
        ordered = sorted(numbers)
        start = previous = ordered[0]
        for number in [*ordered[1:], None]:
            if number is not None and number == previous + 1:
                previous = number
                continue
            runs.append(str(start) if start == previous else f"{start}-{previous}")
            if number is not None:
                start = previous = number
        lines.append(f"  {series} S{season:02d}: no row for episode {', '.join(runs)}")
    return lines


def write(found: Sequence[Missing], folder: Path | str) -> list[Path]:
    """The table, the TSV and the JSON, side by side."""
    out = Path(folder)
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for form, name in (("table", "missing.txt"), ("tsv", "missing.tsv"),
                       ("json", "missing.json")):
        target = out / name
        target.write_text(render(found, form) + "\n", encoding="utf-8", newline="\n")
        written.append(target)
    return written
