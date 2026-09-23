"""How complete an item's metadata is, and whether its name is a name.

Scored per type, because the fields that matter differ: a film has a runtime
and a series does not, a season has an image and nothing else worth scoring.
The interesting column is not the score, though. It is ``titled``.

**A name that came from the filename is not a title.** An item called
``Northwind.S01E03.1080p.WEB-DL.x264-EXAMPLE.mkv`` has a name, a complete-looking
record and nothing a person would recognise. Counting it as titled makes a
library look finished when the work has not started, so the classifier here
looks for what a filename carries and a title does not: a resolution, a source
tag, a codec, a release group, a season-and-episode marker, an extension, or
simply the file's own base name.

Three shapes are treated separately, and they are the ones that make the
difference between a useful column and a noisy one.

*Prefixed*: a real title behind a numbering prefix. The title is there; the
prefix is in the way. Counting these as missing overstates the work.

*Generic*: ``Episode 4``, ``Part 2``. Not a title, and not a filename either.

*Bare-numeric or dotted*: three or more dot-joined words, or an underscore
run. That is a release name, whatever the words are.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Literal

from ..report import Column, Survey

__all__ = ["SCORED", "completeness", "title_class"]

#: Tokens that only ever appear in a filename. Deliberately not exhaustive:
#: the aim is to be right about the common shapes and to say so, not to claim
#: a classifier that cannot be wrong.
RELEASE_TOKENS = re.compile(
    r"(?i)"
    r"\b\d{3,4}[pi]\b"
    r"|\b(?:x26[45]|h\.?26[45]|hevc|xvid|divx|avc|av1)\b"
    r"|\b(?:blu-?ray|bdrip|brrip|web-?dl|web-?rip|hdtv|dvdrip|dvdscr|hdrip|remux)\b"
    r"|\b(?:aac|ac3|eac3|dts(?:-hd)?|ddp?5\.1|truehd|atmos|flac|opus)\b"
    r"|\bS\d{1,3}\s?E\d{1,3}\b"
    r"|\.(?:mkv|mp4|avi|m4v|ts|mov)\b"
)
#: A title that is only a number, or a generated placeholder.
GENERIC = re.compile(r"(?i)\A(?:episode|folge|part|teil|e)\s*\d+\Z")
#: Dot-joined words: the shape of a release name whatever the words are.
DOTTED = re.compile(r"\w\.\w")

TitleClass = Literal["titled", "prefixed", "untitled"]

#: A real title survives after a season-and-episode marker, so this is the one
#: shape where a filename-derived prefix does not mean the title is missing.
PREFIXED = re.compile(
    r"(?i)\A(?P<prefix>.*\bS\d{1,3}\s?E\d{1,3}(?:\.\d+)?\b)\s*[-:]\s*(?P<title>\S.*)\Z"
)

#: What each type is scored on. A field absent from the list for a type is
#: not scored for it, rather than scored and forgiven.
SCORED: Mapping[str, tuple[str, ...]] = {
    "Movie": ("identity", "overview", "date", "genres", "primary", "backdrop",
              "people", "rating", "runtime", "studios"),
    "Series": ("identity", "overview", "date", "genres", "primary", "backdrop",
               "people", "rating", "studios"),
    "Episode": ("identity", "numbered", "titled", "overview", "date", "primary",
                "runtime"),
    "Season": ("primary",),
    "BoxSet": ("overview", "primary"),
}


def _basename(path: str | None) -> str:
    if not path:
        return ""
    pure = PureWindowsPath(path) if "\\" in path else PurePosixPath(path)
    return pure.stem


def title_class(name: str | None, path: str | None = None) -> TitleClass:
    """``titled``, ``prefixed`` or ``untitled`` -- what kind of name this is."""
    if not name or not name.strip():
        return "untitled"
    text = name.strip()
    match = PREFIXED.match(text)
    if match and not RELEASE_TOKENS.search(match.group("title")) \
            and len(match.group("title")) > 2:
        return "prefixed"
    base = _basename(path)
    if base and base.casefold() == text.casefold():
        return "untitled"
    if GENERIC.match(text):
        return "untitled"
    if RELEASE_TOKENS.search(text):
        return "untitled"
    if len(DOTTED.findall(text)) >= 3 or text.count("_") >= 3:
        return "untitled"
    return "titled"


def _has(item: Mapping[str, Any], field: str) -> bool:
    if field == "identity":
        return bool(item.get("ProviderIds"))
    if field == "overview":
        return bool((item.get("Overview") or "").strip())
    if field == "date":
        return bool(item.get("PremiereDate") or item.get("ProductionYear"))
    if field == "genres":
        return bool(item.get("Genres"))
    if field == "people":
        return bool(item.get("People"))
    if field == "studios":
        return bool(item.get("Studios"))
    if field == "primary":
        return bool((item.get("ImageTags") or {}).get("Primary"))
    if field == "backdrop":
        return bool(item.get("BackdropImageTags"))
    if field == "rating":
        return item.get("CommunityRating") is not None \
            or bool(item.get("OfficialRating"))
    if field == "runtime":
        return bool(item.get("RunTimeTicks"))
    if field == "numbered":
        return item.get("IndexNumber") is not None
    if field == "titled":
        return title_class(item.get("Name"), item.get("Path")) != "untitled"
    raise KeyError(field)  # pragma: no cover - the table and this list agree


def completeness(items: Sequence[Mapping[str, Any]]) -> Survey:
    """One row per item: what it is missing, and what kind of name it has."""
    columns = [
        Column("type", "Type"),
        Column("name", "Name"),
        Column("title_class", "Name is"),
        Column("missing", "Missing"),
        Column("n_missing", "Missing (n)", kind="number"),
        Column("n_scored", "Scored (n)", kind="number"),
        Column("complete", "Complete", kind="bool"),
    ]
    rows: list[dict[str, Any]] = []
    by_type: dict[str, list[int]] = {}
    untitled = 0
    prefixed = 0
    for item in items:
        kind = str(item.get("Type") or "")
        scored = SCORED.get(kind)
        if scored is None:
            continue
        missing = [field for field in scored if not _has(item, field)]
        naming = title_class(item.get("Name"), item.get("Path"))
        untitled += naming == "untitled"
        prefixed += naming == "prefixed"
        rows.append({
            "type": kind,
            "name": item.get("Name"),
            "title_class": naming,
            "missing": ",".join(missing),
            "n_missing": len(missing),
            "n_scored": len(scored),
            "complete": not missing,
        })
        by_type.setdefault(kind, []).append(len(missing))

    summary: dict[str, Any] = {
        "items scored": len(rows),
        "complete": sum(1 for row in rows if row["complete"]),
        "names that came from a filename": untitled,
        "titles behind a numbering prefix": prefixed,
    }
    for kind in sorted(by_type):
        scores = by_type[kind]
        summary[f"{kind}: complete"] = (
            f"{sum(1 for s in scores if not s)}/{len(scores)}"
        )
    return Survey(
        name="Metadata completeness",
        about="Which fields each item is missing, and whether its name is a title.",
        columns=columns,
        rows=rows,
        summary=summary,
        scope={"types scored": ", ".join(sorted(SCORED)),
               "items seen": len(items)},
        caveats=[
            "Scored per type: a field not listed for a type is not scored for it, "
            "so the counts are not comparable between types.",
            "The name classifier looks for what a filename carries. A title that "
            "genuinely contains a resolution or a codec name is classified as "
            "coming from a filename, and a release name made of ordinary words "
            "with no tokens in it is classified as a title.",
            "Everything here is read from the catalogue. An item whose file has "
            "never been probed is scored on what the catalogue knows, which may "
            "be less than what the file contains.",
        ],
    )
