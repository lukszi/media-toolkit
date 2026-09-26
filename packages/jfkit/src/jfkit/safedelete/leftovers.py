"""jfkit.safedelete.leftovers -- the preconditions of the three leftover categories.

A leftover has no catalogue row of its own, so the checks that key on an
item -- "the item is in the catalogue", "its path is the manifest's path",
"nobody has a position in it" -- cannot be asked of it. What replaces them
is the question those checks were standing in for: **does the catalogue
still point at anything here?** A leftover passes when nothing catalogued
lives at its path or anywhere below it. That is the fix for a release
subfolder (a screenshot folder, a folder of description files), whose own
path never equals any item's path and was refused for it.

``release-junk``
    One file, or one folder of nothing but junk (screenshots, padding),
    that the rules in :mod:`.junk` name positively. Protected kinds are
    checked first and can never be junk.

``dead-release-folder``
    A folder with no video left anywhere below it, nothing catalogued at or
    below it, no link, junction or unreadable folder below it, and nothing
    in it but description files, artwork, preview tiles and junk. It must
    not sit in a folder whose own video is still there: pictures beside a
    live video may be that video's. The evidence says whether the release
    is catalogued elsewhere or not at all; both are candidates, and the
    second is also a line in the missing-content report.

``corrupt-unplayable``
    A video whose payload check failed (:func:`mkvkit.integrity.check`: a
    zero-filled sample, missing packets, decoder errors) with evidence, and
    no other catalogued copy of the same film or episode.

``sample``
    A release sample: a video the rules name as one, by its name or its
    folder, with no catalogue row at its path. Listed by every sweep and
    moved only when somebody released this category on its own. The report is
    kept with the candidate so the audit records what was measured. A file
    that could not be measured is refused: no evidence is not bad evidence.

Every check here reads the world as it is when it runs, so a plan made
yesterday is checked again before anything moves.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any

from mkvkit.integrity import IntegrityReport
from mkvkit.sidecars import VIDEO_SUFFIXES
from mkvkit.walk import Skipped, link_kind, walk

from .catalogue import Catalogue, describe, exists, path_key
from .checks import Check
from .junk import DEFAULT_RULES, Kind, Rules, Verdict, classify, is_protected_folder

__all__ = [
    "CORRUPT",
    "DEAD_FOLDER",
    "LEFTOVER_CATEGORIES",
    "RELEASE_JUNK",
    "SAMPLE",
    "Evidence",
    "Subtree",
    "corrupt_checks",
    "dead_folder_checks",
    "release_evidence",
    "release_junk_checks",
    "sample_checks",
    "subtree",
]

RELEASE_JUNK = "release-junk"
DEAD_FOLDER = "dead-release-folder"
CORRUPT = "corrupt-unplayable"
SAMPLE = "sample"

#: The categories whose candidates need no catalogue row of their own.
LEFTOVER_CATEGORIES = frozenset({RELEASE_JUNK, DEAD_FOLDER, CORRUPT, SAMPLE})

#: What may be left in a dead release folder besides junk.
_LEFTOVER_REASONS = frozenset({
    "a description file", "artwork the server reads", "preview tiles the server built",
})

#: The largest description file read for identifiers. They are a few KiB.
NFO_READ_LIMIT = 256 << 10


# ------------------------------------------------------------------ subtree
@dataclass(frozen=True)
class Subtree:
    """Every file below a folder with its class, and what the walk left out."""

    folder: Path
    files: tuple[tuple[Path, Verdict, int], ...] = ()
    skipped: tuple[Skipped, ...] = ()

    @property
    def size(self) -> int:
        return sum(size for _path, _verdict, size in self.files)

    def of(self, *kinds: Kind) -> list[tuple[Path, Verdict]]:
        return [(p, v) for p, v, _s in self.files if v.kind in kinds]

    @property
    def videos(self) -> list[Path]:
        return [p for p, _v in self.of(Kind.VIDEO, Kind.SAMPLE)]


def subtree(folder: Path | str, *, rules: Rules = DEFAULT_RULES) -> Subtree:
    """Walk one folder (never through a link) and classify every file below it."""
    here = Path(folder)
    tree = walk(here)
    files = tuple(
        (entry.path, classify(PurePath(here.name) / entry.relative, rules=rules,
                              size=entry.size), entry.size or 0)
        for entry in tree
    )
    return Subtree(here, files, tuple(tree.skipped))


def _names(paths: Sequence[Path], limit: int = 5) -> str:
    shown = ", ".join(p.name for p in paths[:limit])
    return shown + (f" and {len(paths) - limit} more" if len(paths) > limit else "")


def _common(path: Path, catalogue: Catalogue) -> list[Check]:
    checks = [
        Check("it is on disk", path.exists(), str(path)),
    ]
    kind = link_kind(path)
    checks.append(Check(
        "it is not a link or a junction", kind is None,
        f"it is a {kind.value}" if kind is not None else "",
    ))
    rows = catalogue.at_or_below(path)
    checks.append(Check(
        "nothing catalogued is at or below it", not rows,
        "; ".join(describe(r) for r in rows[:3]) + (" ..." if len(rows) > 3 else ""),
    ))
    return checks


# ------------------------------------------------------------- release junk
def release_junk_checks(
    path: Path | str, catalogue: Catalogue, *, rules: Rules = DEFAULT_RULES,
) -> list[Check]:
    """A junk file, or a folder of nothing but junk."""
    here = Path(path)
    checks = _common(here, catalogue)
    if here.is_dir() and link_kind(here) is None:
        checks.append(Check(
            "it is not a folder the server or a disc owns",
            not is_protected_folder(here.name), here.name,
        ))
        found = subtree(here, rules=rules)
        others = [(p, v) for p, v, _s in found.files
                  if v.kind not in (Kind.JUNK, Kind.IGNORED)]
        checks.append(Check(
            "everything in it is release junk", not others,
            "; ".join(f"{p.name} ({v.reason})" for p, v in others[:5])
            + (" ..." if len(others) > 5 else ""),
        ))
        checks.append(Check(
            "no link, junction or unreadable folder is below it", not found.skipped,
            "; ".join(str(s) for s in found.skipped[:3]),
        ))
    elif here.is_file():
        verdict = classify(here, rules=rules, size=here.stat().st_size)
        checks.append(Check(
            "the rules call it release junk", verdict.kind is Kind.JUNK, str(verdict),
        ))
    return checks


# ------------------------------------------------------------------- sample
def sample_checks(
    path: Path | str, catalogue: Catalogue, *, rules: Rules = DEFAULT_RULES,
) -> list[Check]:
    """A release sample, which no row points at."""
    here = Path(path)
    checks = _common(here, catalogue)
    verdict = classify(here, rules=rules)
    checks.append(Check(
        "the rules call it a release sample",
        here.is_file() and verdict.kind is Kind.SAMPLE, str(verdict),
    ))
    return checks


# ------------------------------------------------------ dead release folder
def dead_folder_checks(
    folder: Path | str,
    catalogue: Catalogue,
    *,
    rules: Rules = DEFAULT_RULES,
    roots: Sequence[str | os.PathLike[str]] = (),
) -> list[Check]:
    """A release folder with no video left and nothing catalogued in it.

    ``roots`` are the library folders: in one of those every video is an
    item of its own, so a video beside the candidate there is not a reason
    to think the candidate's pictures are its.
    """
    here = Path(folder)
    checks = _common(here, catalogue)
    if not here.is_dir() or link_kind(here) is not None:
        checks.append(Check("it is a folder", False, str(here)))
        return checks
    checks.append(Check(
        "it is not a folder the server or a disc owns",
        not is_protected_folder(here.name), here.name,
    ))
    found = subtree(here, rules=rules)
    checks.append(Check(
        "no video is anywhere below it", not found.videos, _names(found.videos),
    ))
    checks.append(Check(
        "no link, junction or unreadable folder is below it", not found.skipped,
        "; ".join(str(s) for s in found.skipped[:3]),
    ))
    kept = [
        (p, v) for p, v, _s in found.files
        if not (v.kind in (Kind.JUNK, Kind.IGNORED)
                or (v.kind is Kind.PROTECTED and v.reason in _LEFTOVER_REASONS))
    ]
    checks.append(Check(
        "what is left is description files, artwork, preview tiles or junk",
        not kept,
        "; ".join(f"{p.name} ({v.reason})" for p, v in kept[:5])
        + (" ..." if len(kept) > 5 else ""),
    ))
    in_root = path_key(here.parent) in {path_key(r) for r in roots}
    beside = [] if in_root else _videos_beside(here)
    checks.append(Check(
        "the folder it sits in has no video of its own", not beside,
        "its pictures may belong to " + _names(beside) if beside else "",
    ))
    return checks


def _videos_beside(folder: Path) -> list[Path]:
    try:
        with os.scandir(folder.parent) as listing:
            return sorted(
                Path(entry.path) for entry in listing
                if entry.is_file(follow_symlinks=False)
                and os.path.splitext(entry.name)[1].lower() in VIDEO_SUFFIXES
            )
    except OSError:
        return []


# ------------------------------------------------------------------ evidence
_IMDB = re.compile(r"\btt\d{7,9}\b", re.IGNORECASE)
_TAGGED = re.compile(
    r"<(tmdbid|tvdbid|imdbid|imdb_id)>\s*([a-z0-9]+)\s*</\1>"
    r"|<uniqueid[^>]*type=\"(tmdb|tvdb|imdb)\"[^>]*>\s*([a-z0-9]+)\s*</uniqueid>",
    re.IGNORECASE,
)
_YEAR = re.compile(r"(?<!\d)(19\d\d|20\d\d)(?!\d)")
#: Words that start a release's own description when a name carries no year.
_RELEASE_TAG = re.compile(
    r"(?<![a-z0-9])(?:\d{3,4}p|4k|uhd|bluray|blu-ray|bdrip|brrip|dvdrip|webrip|"
    r"web-dl|webhd|web|hdtv|hdrip|remux|x264|x265|h264|h265|hevc|xvid|avc|"
    r"german|dl|dubbed|multi|complete|repack|proper|remastered|extended|unrated|"
    r"ac3|dts|aac|hdr|10bit)(?![a-z0-9])",
    re.IGNORECASE,
)
_EPISODE = re.compile(r"(?<![a-z0-9])s(\d{1,2})[ ._-]?e(\d{1,3})(?!\d)", re.IGNORECASE)


def _normal(text: str) -> str:
    text = text.replace("'", "").replace(chr(0x2019), "")
    return " ".join(re.sub(r"[^0-9a-z]+", " ", text.casefold()).split())


def title_of(name: str) -> tuple[str, int | None]:
    """The title and year a release folder or file name claims.

    Everything after the first year (or the first episode marker) is the
    release's own description and is dropped; brackets are dropped with it.
    """
    text = re.sub(r"[\[(].*?[\])]", lambda m: " " + m.group(0)[1:-1] + " ", name)
    year: int | None = None
    cut = len(text)
    found = _YEAR.search(text, 1)
    if found is not None:
        year, cut = int(found.group(1)), found.start()
    for pattern in (_EPISODE, _RELEASE_TAG):
        marker = pattern.search(text, 1)
        if marker is not None and marker.start() < cut:
            cut = marker.start()
    return _normal(text[:cut]), year


def nfo_ids(paths: Sequence[Path]) -> set[tuple[str, str]]:
    """The provider identifiers the description files name."""
    out: set[tuple[str, str]] = set()
    for path in paths:
        try:
            if path.stat().st_size > NFO_READ_LIMIT:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        out |= {("imdb", m.group(0).lower()) for m in _IMDB.finditer(text)}
        for m in _TAGGED.finditer(text):
            tag = (m.group(1) or m.group(3) or "").casefold().replace("id", "") \
                .replace("_", "") or "imdb"
            value = (m.group(2) or m.group(4) or "").casefold()
            if tag in {"tmdb", "tvdb", "imdb"} and value:
                out.add((tag, value))
    return out


@dataclass(frozen=True)
class Evidence:
    """What the catalogue says about a release folder with no video left."""

    folder: Path
    title: str
    year: int | None
    matches: tuple[tuple[dict[str, Any], str], ...] = ()

    @property
    def status(self) -> str:
        """``elsewhere``, ``no-file`` (a row whose file is missing) or ``unmatched``."""
        if any(exists(row.get("Path")) for row, _how in self.matches):
            return "elsewhere"
        if self.matches:
            return "no-file"
        return "unmatched"

    def lines(self) -> list[str]:
        if not self.matches:
            said = f"{self.title!r}" + (f" ({self.year})" if self.year else "")
            return [f"no catalogue row matches {said} by name or by the ids its "
                    "description files name"]
        out: list[str] = []
        for row, how in self.matches[:5]:
            there = exists(row.get("Path"))
            state = "catalogued elsewhere" if there else "catalogued, but its file is missing"
            out.append(f"{state}: {describe(row)} (matched by {how})")
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status, "title": self.title, "year": self.year,
            "matches": [
                {"id": row.get("Id"), "type": row.get("Type"), "name": row.get("Name"),
                 "path": row.get("Path"), "how": how}
                for row, how in self.matches
            ],
        }


def release_evidence(
    folder: Path | str,
    catalogue: Catalogue,
    *,
    files: Sequence[Path] = (),
) -> Evidence:
    """Whether a folder's release is catalogued somewhere, and how that was decided.

    Three ways, strongest first: an identifier a description file names; an
    episode marker in the folder's or a file's name against the series of
    that name; the film's title and year against the catalogue's.
    """
    here = Path(folder)
    title, year = title_of(here.name)
    found: dict[str, tuple[dict[str, Any], str]] = {}

    def add(row: Mapping[str, Any], how: str) -> None:
        key = str(row.get("Id"))
        if key not in found:
            found[key] = (dict(row), how)

    for name, value in sorted(nfo_ids([p for p in files if p.suffix.lower() == ".nfo"])):
        for row in catalogue.by_provider(name, value):
            if row.get("Type") in ("Movie", "Episode", "Series"):
                add(row, f"{name} id {value}")

    names = [here.name, *(p.name for p in files)]
    for name in names:
        marker = _EPISODE.search(name)
        if marker is None:
            continue
        series, _ = title_of(name)
        series = series or title
        season, number = int(marker.group(1)), int(marker.group(2))
        for row in catalogue.of_type("Episode"):
            if (row.get("ParentIndexNumber"), row.get("IndexNumber")) == (season, number) \
                    and _normal(str(row.get("SeriesName") or "")) == series:
                add(row, f"series name and S{season:02d}E{number:02d}")

    if not found and title:
        for row in catalogue.of_type("Movie", "Series"):
            if _normal(str(row.get("Name") or "")) != title:
                continue
            made = row.get("ProductionYear")
            if year is None or made is None or abs(int(made) - year) <= 1:
                add(row, "title" + (" and year" if year and made else ""))
    return Evidence(here, title, year, tuple(found.values()))


# ------------------------------------------------------------------- corrupt
def corrupt_checks(
    path: Path | str,
    catalogue: Catalogue,
    report: IntegrityReport | None,
    *,
    measured: tuple[int, int] | None = None,
) -> list[Check]:
    """A video that does not play, with evidence, and no copy to keep.

    ``measured`` is the (size, modification time in ns) the evidence was
    taken at; a file that changed since is refused, because the evidence is
    about a file that is no longer there.
    """
    here = Path(path)
    checks = [
        Check("it is on disk", here.is_file(), str(here)),
        Check("it is a video", here.suffix.lower() in VIDEO_SUFFIXES, here.suffix),
    ]
    kind = link_kind(here)
    checks.append(Check(
        "it is not a link or a junction", kind is None,
        f"it is a {kind.value}" if kind is not None else "",
    ))
    if report is None:
        checks.append(Check(
            "integrity evidence says it does not play", False,
            "not measured: an earlier precondition already refuses this one",
        ))
    elif not report.evidence:
        checks.append(Check(
            "integrity evidence says it does not play", False,
            "no evidence: " + "; ".join(report.problems),
        ))
    else:
        checks.append(Check(
            "integrity evidence says it does not play", not report.ok,
            "; ".join(report.problems) if report.problems else "the check passed: it plays",
        ))
    if measured is not None and here.is_file():
        st = here.stat()
        now = (st.st_size, st.st_mtime_ns)
        checks.append(Check(
            "it has not changed since it was measured", now == measured,
            f"measured at {measured[0]} bytes, now {now[0]}" if now != measured else "",
        ))
    rows = catalogue.at(here)
    copies = [
        other for row in rows for other in catalogue.copies_of(row)
        if exists(other.get("Path")) and not _same(other.get("Path"), here)
    ]
    checks.append(Check(
        "no other copy of it is catalogued", not copies,
        "keep " + "; ".join(describe(c) for c in copies[:3])
        + " -- that is superseded-copy, after proving the copy plays"
        if copies else (
            f"{len(rows)} row(s) name this file" if rows
            else "no catalogue row names this file"
        ),
    ))
    return checks


def _same(one: object, other: Path) -> bool:
    return bool(one) and os.path.normcase(os.path.abspath(str(one))) == \
        os.path.normcase(os.path.abspath(other))
