"""jfkit.rename.plan -- the file side of a rename: predict, carry, park, order.

Nothing here talks to a server or changes a file. :func:`plan_files` takes the
old-to-new pairs and returns a :class:`FilePlan`: what the server will make of
every target, every file that moves (the video first, then each sidecar), the
stale metadata documents that are parked instead, the folders a notification
should name, and the :mod:`mkvkit.steps` steps that carry it all out -- or the
list of reasons it refuses to.

**What is intended.** Each pair may say what the target should be read as
(:func:`parse_expect`): ``S01E03`` (or ``E03``, or ``S01E03-E04`` for a range
named on purpose), ``extra`` or ``extra:featurette``, ``none`` for a video
that should carry no number at all, or ``movie``. Without one it is inferred
from the new name (:func:`infer_expect`): a file the extras rules claim is an
extra; an ``SxxEyy`` token names the season and episode; a target in a film
library is a film; anything else should be read as no number. A name the
parser reads differently from what was intended refuses the plan, with the
parser's own account of why -- the documentary whose ``8000`` became season
80, and the ``8.000`` that would have become season 0, are both refused.

**Checks.** Besides the reading: the old path exists and is a video or a
folder; the container does not change; the target is on the same volume;
no path the server will write beside the target (the preview tiles are the
deepest) passes the classic Windows limit; no two targets are the same; no
target, and no file that would be read as the target's sidecar, exists unless
this plan moves it away; no two videos of one series end up in one season and
episode slot (in one folder the server merges them into one item, for good);
and nothing is renamed inside a folder that is itself renamed.

**Chains and cycles.** ``A -> B`` with ``B -> A`` cannot be done in either
order. Every set whose target is held by another set of the same plan goes
out of the way first, under a temporary name no parser reads as a video
(the old name plus ``.rename-<digest>``), and into place after everything
else has moved. The temporary name is derived from the mapping, so a resumed
run finds the files where the first run left them.

**Stale metadata documents.** The ``<stem>.nfo`` beside a video carries the
metadata of the episode the server *thought* it was, and the server reads it
before any provider. When the reading changes -- a new season or episode, an
extra that was an episode, a number that was not meant -- the document is
parked in a folder outside the library instead of renamed (``nfo="stale"``,
the default; ``"park"`` parks every one and ``"carry"`` none).
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mkvkit.devices import same_device
from mkvkit.sidecars import FolderListing, SidecarKind, SidecarSet, sidecars_in_folder, sidecars_of
from mkvkit.steps import Plan, Step
from mkvkit.walk import walk

from ..naming import (
    EXTRA_FOLDER_NAMES,
    MAX_PATH,
    VIDEO_SUFFIXES,
    Parse,
    extra_type,
    longest_derived_path,
    parse,
    season_folder,
)
from ..validation import warnings_for

__all__ = [
    "EXPECT_KINDS",
    "NFO_POLICIES",
    "Expect",
    "FilePlan",
    "Options",
    "Pair",
    "Problem",
    "Video",
    "disk_key",
    "infer_expect",
    "mapping_digest",
    "parse_expect",
    "plan_files",
    "read_mapping",
    "within",
]

#: What a target can be meant to be read as.
EXPECT_KINDS = ("episode", "extra", "none", "movie")

#: What happens to the ``.nfo`` beside a renamed video.
NFO_POLICIES = ("stale", "park", "carry")

#: The collection type the server gives a film library.
MOVIES = "movies"


# ---------------------------------------------------------------- paths
def disk_key(path: Path | str) -> str:
    """A path as the file system compares it: normalised, and case-folded on Windows."""
    return os.path.normcase(os.path.normpath(str(path)))


def within(child: str, parent: str) -> bool:
    """True when the key ``child`` is ``parent`` or below it."""
    if child == parent:
        return True
    return child.startswith(parent.rstrip("\\/") + os.sep)


def _absolute(path: Path | str) -> Path:
    return Path(os.path.abspath(str(path)))


def _existing(path: Path) -> Path:
    """The path, or its nearest ancestor that exists."""
    here = path
    while not here.exists() and here.parent != here:
        here = here.parent
    return here


# ------------------------------------------------------------ intentions
_EXPECT = re.compile(
    r"^(?:s(?P<season>\d{1,4}))?e(?P<episode>\d{1,4})(?:-?e?(?P<end>\d{1,4}))?$", re.I,
)
_TOKEN = re.compile(
    r"(?<![a-z0-9])s(?P<season>\d{1,4})[ ._-]?e(?P<episode>\d{1,4})"
    r"(?:-?e(?P<end>\d{1,4}))?(?![0-9])", re.I,
)


@dataclass(frozen=True)
class Expect:
    """What a target is meant to be read as."""

    kind: str
    season: int | None = None
    episode: int | None = None
    end: int | None = None
    #: the extra type, for ``extra:<type>``; any type when unset
    extra: str | None = None
    #: where the intention came from: given, name, folder or library
    source: str = "given"

    def __str__(self) -> str:
        if self.kind == "episode":
            season = f"S{self.season:02d}" if self.season is not None else ""
            end = f"-E{self.end:02d}" if self.end is not None else ""
            return f"{season}E{self.episode or 0:02d}{end}"
        if self.kind == "extra":
            return f"extra ({self.extra.lower()})" if self.extra else "extra"
        return "a film" if self.kind == "movie" else "no number"

    def problems(self, found: Parse) -> list[str]:
        """Why ``found`` is not what was intended; empty when it is."""
        out = [f"warning: {w}" for w in found.warnings]
        if self.kind == "extra":
            if not found.is_extra:
                out.append(f"read as {found.describe()}, not as an extra")
            elif self.extra and (found.extra or "").casefold() != self.extra.casefold():
                out.append(f"read as an extra of type {found.extra}, not {self.extra}")
            return out
        if found.is_extra:
            out.append(f"read as an extra ({found.extra}), not as {self}")
            return out
        if self.kind == "movie":
            return out
        if self.kind == "none":
            if found.episode is not None or found.season is not None or found.date:
                out.append(
                    f"read as {found.describe()} although no number was intended"
                    + (f" (expression {found.expression})" if found.expression else "")
                )
            return out
        if found.episode is None:
            out.append(f"no episode number is read from it ({found.describe()})")
            return out
        season = found.effective_season
        if self.season is not None and season != self.season:
            out.append(f"read as season {season if season is not None else 'none'}, "
                       f"not {self.season}")
        if found.episode != self.episode:
            out.append(f"read as episode {found.episode}, not {self.episode}")
        if found.end != self.end:
            out.append(
                f"read as a range ending at {found.end}" if found.end is not None
                else f"not read as a range ending at {self.end}"
            )
        return out

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "season": self.season, "episode": self.episode,
                "end": self.end, "extra": self.extra, "source": self.source}

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> Expect:
        return cls(kind=str(raw["kind"]), season=raw.get("season"),
                   episode=raw.get("episode"), end=raw.get("end"),
                   extra=raw.get("extra"), source=str(raw.get("source", "given")))


def parse_expect(spec: str) -> Expect | None:
    """``S01E03``, ``E03``, ``S01E03-E04``, ``extra[:type]``, ``none`` or ``movie``.

    An empty spec, or ``auto``, is no intention: it is inferred instead.
    """
    text = spec.strip()
    low = text.casefold()
    if low in ("", "auto"):
        return None
    if low in ("none", "unnumbered"):
        return Expect("none")
    if low in ("movie", "film"):
        return Expect("movie")
    if low == "extra" or low.startswith("extra:"):
        kind = text.partition(":")[2].strip()
        return Expect("extra", extra=kind or None)
    found = _EXPECT.match(text)
    if found is None:
        raise ValueError(
            f"{spec!r}: not an intention; use S01E03, E03, S01E03-E04, extra, "
            "extra:TYPE, none or movie"
        )
    end = found.group("end")
    return Expect(
        "episode",
        season=int(found.group("season")) if found.group("season") else None,
        episode=int(found.group("episode")),
        end=int(end) if end else None,
    )


def infer_expect(path: Path | str, library: str | None = None) -> Expect:
    """What a person reading the new name would take it to be.

    An extras folder or ending makes it an extra; an ``SxxEyy`` token names
    the numbers; in a film library it is a film; anything else is meant to
    carry no number.
    """
    text = str(path)
    anchored = text if ("/" in text or "\\" in text) else "/" + text
    rule = extra_type(anchored)
    if rule is not None:
        return Expect("extra", source="folder" if rule.kind == "directory" else "name")
    tokens = list(_TOKEN.finditer(Path(text).stem))
    token = tokens[-1] if tokens else None
    if token is not None:
        end = token.group("end")
        return Expect("episode", season=int(token.group("season")),
                      episode=int(token.group("episode")),
                      end=int(end) if end else None, source="name")
    if library == MOVIES:
        return Expect("movie", source="library")
    return Expect("none", source="name")


# ---------------------------------------------------------------- input
@dataclass(frozen=True)
class Pair:
    """One line of the mapping: a video or folder, where it goes, what it should be."""

    old: Path
    new: Path
    expect: Expect | None = None
    #: the line of the mapping it came from, for messages
    line: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {"old": str(self.old), "new": str(self.new), "line": self.line,
                "expect": self.expect.to_json() if self.expect else None}

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> Pair:
        expect = raw.get("expect")
        return cls(Path(raw["old"]), Path(raw["new"]),
                   Expect.from_json(expect) if expect else None, raw.get("line"))


def read_mapping(text: str, *, origin: str = "mapping") -> list[Pair]:
    """Pairs from tab-separated text: old, new, and an optional intention.

    Blank lines and lines starting with ``#`` are skipped, and so is a first
    line whose first cell is ``old``. Every path is one cell, spaces and all;
    a mapping is a file (or standard input) rather than arguments because a
    few hundred paths do not fit on one Windows command line.
    """
    out: list[Pair] = []
    for number, row in enumerate(csv.reader(text.splitlines(), delimiter="\t"), start=1):
        cells = [cell.strip() for cell in row]
        if not any(cells) or cells[0].startswith("#"):
            continue
        if not out and cells[0].casefold() in ("old", "from", "source"):
            continue
        if len(cells) < 2 or not cells[0] or not cells[1]:
            raise ValueError(f"{origin}, line {number}: a line needs an old and a new path")
        try:
            expect = parse_expect(cells[2]) if len(cells) > 2 else None
        except ValueError as exc:
            raise ValueError(f"{origin}, line {number}: {exc}") from None
        out.append(Pair(Path(cells[0]), Path(cells[1]), expect, number))
    return out


def mapping_digest(pairs: Iterable[Pair]) -> str:
    """A digest of the mapping: what ties a work folder and temporary names to it."""
    canonical = json.dumps(
        [[str(p.old), str(p.new), str(p.expect) if p.expect else ""] for p in pairs],
        ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- output
@dataclass(frozen=True)
class Problem:
    """One reason the plan is refused."""

    path: str
    reason: str

    def __str__(self) -> str:
        return f"{self.path}: {self.reason}"


def _identity(found: Parse) -> tuple[Any, ...]:
    return (found.extra, found.effective_season, found.episode, found.end, found.date)


@dataclass(frozen=True)
class Video:
    """One video that moves: where from, where to, and both readings."""

    old: Path
    new: Path
    expect: Expect
    before: Parse
    after: Parse
    #: the pair (1-based) it belongs to
    pair: int

    @property
    def changed(self) -> bool:
        """The server reads the new name as something else than the old one."""
        return _identity(self.before) != _identity(self.after)

    @property
    def problems(self) -> list[str]:
        return self.expect.problems(self.after)

    def to_json(self) -> dict[str, Any]:
        return {"old": str(self.old), "new": str(self.new), "pair": self.pair,
                "expect": self.expect.to_json(), "before": self.before.describe(),
                "after": self.after.describe(), "changed": self.changed}


@dataclass(frozen=True)
class Options:
    """How a plan is made."""

    #: where stale ``.nfo`` files go; outside every library
    park: Path | None = None
    nfo: str = "stale"
    #: library root -> its collection type (``movies``, ``tvshows``, or None)
    roots: Mapping[str, str | None] = field(default_factory=dict)
    #: notify a library root, or a folder that holds one, anyway
    allow_library_scan: bool = False
    max_path: int = MAX_PATH


@dataclass(frozen=True)
class FilePlan:
    """Everything that moves, in order, or the reasons nothing may."""

    pairs: tuple[Pair, ...]
    videos: tuple[Video, ...]
    #: every (source, destination) the plan moves: folders, videos, sidecars
    moves: tuple[tuple[Path, Path], ...]
    #: every stale document parked, as (source, destination)
    parked: tuple[tuple[Path, Path], ...]
    #: the deepest folders whose contents change, for the notification
    notify: tuple[Path, ...]
    #: the series folders the targets belong to
    series: tuple[Path, ...]
    steps: tuple[Step, ...]
    problems: tuple[Problem, ...] = ()
    notes: tuple[str, ...] = ()
    digest: str = ""

    @property
    def ok(self) -> bool:
        return not self.problems

    def plan(self, extra: Sequence[Step] = (), notes: Sequence[str] = ()) -> Plan:
        return Plan(verb="rename", steps=(*self.steps, *extra),
                    notes=(*self.notes, *notes))

    def to_json(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "pairs": [p.to_json() for p in self.pairs],
            "videos": [v.to_json() for v in self.videos],
            "moves": [[str(a), str(b)] for a, b in self.moves],
            "parked": [[str(a), str(b)] for a, b in self.parked],
            "notify": [str(p) for p in self.notify],
            "series": [str(p) for p in self.series],
            "problems": [str(p) for p in self.problems],
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------- the plan
@dataclass
class _Unit:
    pair: int
    kind: str  # "video" or "folder"
    old: Path
    new: Path
    moves: list[tuple[Path, Path]] = field(default_factory=list)
    parks: list[Path] = field(default_factory=list)
    videos: list[Video] = field(default_factory=list)


class _Roots:
    def __init__(self, roots: Mapping[str, str | None]) -> None:
        self.rows = sorted(
            ((disk_key(root), Path(root), kind) for root, kind in roots.items()),
            key=lambda row: -len(row[0]),
        )

    def of(self, path: Path) -> tuple[Path, str | None] | None:
        """The deepest root holding ``path`` (strictly below it), with its type."""
        key = disk_key(path)
        for root_key, root, kind in self.rows:
            if key != root_key and within(key, root_key):
                return root, kind
        return None

    def kind(self, path: Path) -> str | None:
        found = self.of(path)
        return found[1] if found else None

    def holds_root(self, path: Path) -> Path | None:
        """A root that ``path`` is, or holds."""
        key = disk_key(path)
        for root_key, root, _kind in self.rows:
            if within(root_key, key):
                return root
        return None

    def series_folder(self, path: Path) -> Path:
        """The folder of the series ``path`` belongs to.

        The first folder below its library root when a root is known; else
        the nearest folder above it that is neither a season folder nor an
        extras folder.
        """
        found = self.of(path)
        if found is not None:
            root = found[0]
            parts = path.parts
            if len(parts) > len(root.parts) + 1:
                return Path(*parts[:len(root.parts) + 1])
            return path.parent
        folder = path.parent
        while folder.parent != folder:
            name = folder.name
            if (name.casefold() in EXTRA_FOLDER_NAMES
                    or season_folder(name, folder.parent.name).is_season_folder):
                folder = folder.parent
                continue
            break
        return folder


def _slot(found: Parse) -> tuple[int | None, int | None]:
    return (found.effective_season, found.episode)


def _slot_text(slot: tuple[int | None, int | None]) -> str:
    season, episode = slot
    return (f"S{season:02d}" if season is not None else "S--") + f"E{episode or 0:02d}"


def plan_files(pairs: Sequence[Pair], options: Options | None = None) -> FilePlan:
    """Check every pair and turn the mapping into ordered file steps.

    Refusals are collected, not raised: the caller shows every reason at
    once, and a plan with any is not applied.
    """
    opts = options or Options()
    if opts.nfo not in NFO_POLICIES:
        raise ValueError(f"nfo policy {opts.nfo!r}: not one of {', '.join(NFO_POLICIES)}")
    problems: list[Problem] = []
    notes: list[str] = []
    digest = mapping_digest(pairs)
    token = f".rename-{digest[:8]}"
    roots = _Roots(opts.roots)
    listings: dict[str, FolderListing | None] = {}
    folder_sets: dict[str, dict[str, SidecarSet]] = {}

    def listing(folder: Path) -> FolderListing | None:
        key = disk_key(folder)
        if key not in listings:
            listings[key] = FolderListing.read(folder) if folder.is_dir() else None
        return listings[key]

    def sets(video: Path) -> SidecarSet:
        """The video's set, from one reading of its folder shared by every video in it."""
        key = disk_key(video.parent)
        if key not in folder_sets:
            found = sidecars_in_folder(video.parent)
            folder_sets[key] = {disk_key(v): s for v, s in found.sets.items()}
        return folder_sets[key].get(disk_key(video)) or sidecars_of(video)

    def refuse(path: Path | str, reason: str) -> None:
        problems.append(Problem(str(path), reason))

    units: list[_Unit] = []
    sources: dict[str, int] = {}
    for index, raw in enumerate(pairs, start=1):
        old, new = _absolute(raw.old), _absolute(raw.new)
        if str(old) == str(new):
            refuse(old, "the old and the new path are the same")
            continue
        if disk_key(old) in sources:
            refuse(old, f"is renamed by rename {sources[disk_key(old)]} and by rename {index}; "
                        "a path can go one place only")
            continue
        sources[disk_key(old)] = index
        for side in (old, new):
            root = roots.holds_root(side)
            if root is not None:
                refuse(side, f"is the library root {root}, or holds it; a root is never "
                             "renamed")
        if not os.path.lexists(old):
            refuse(old, "does not exist")
            continue
        if not same_device(old, _existing(new.parent)):
            refuse(new, "is on another volume; a rename does not cross volumes")
            continue
        if old.is_dir():
            units.append(_folder_unit(index, raw, old, new, opts, roots, sets, refuse))
        elif old.is_file():
            unit = _video_unit(index, raw, old, new, opts, roots, sets, refuse)
            if unit is not None:
                units.append(unit)
        else:
            refuse(old, "is neither a file nor a folder")

    # ------------------------------------------------ parking
    park_moves: list[tuple[Path, Path]] = []
    park_steps: list[Step] = []
    all_parks = [path for unit in units for path in unit.parks]
    if all_parks:
        if opts.park is None:
            refuse(all_parks[0].parent, f"{len(all_parks)} stale .nfo file(s) to park: name a "
                                        "folder outside the library (--park, or [paths] "
                                        "parked in the configuration)")
        else:
            park = _absolute(opts.park)
            inside = roots.of(park) or roots.holds_root(park)
            touched = [u.old.parent for u in units] + [u.new.parent for u in units]
            if inside is not None or any(
                within(disk_key(park), disk_key(roots.series_folder(t / "x")))
                for t in touched
            ):
                refuse(park, "the park folder is inside a library; the server would read "
                             "the parked documents")
            base = park / f"rename-{digest[:8]}"
            cross = False
            for number, src in enumerate(all_parks, start=1):
                dst = base / f"{number:04d}-{src.name}"
                if os.path.lexists(dst):
                    refuse(dst, "already exists in the park folder")
                park_moves.append((src, dst))
                if same_device(src, _existing(base)):
                    park_steps.append(Step(
                        id=f"park:{number:04d}", action="rename",
                        params={"src": str(src), "dst": str(dst), "parents": True},
                        summary=f"park {src} -> {dst}",
                    ))
                else:
                    cross = True
                    park_steps.append(Step(
                        id=f"park:{number:04d}", action="move",
                        params={"src": str(src), "dst": str(dst)},
                        summary=f"park {src} -> {dst} (verified copy, another volume)",
                    ))
            if cross:
                park_steps.insert(0, Step(id="park:0000", action="mkdir",
                                          params={"path": str(base)},
                                          summary=f"create the park folder {base}"))

    # ------------------------------------------------ collisions
    moving: set[str] = {disk_key(src) for u in units for src, _dst in u.moves}
    moving |= {disk_key(src) for src, _dst in park_moves}
    folders_away = [disk_key(u.old) for u in units if u.kind == "folder"]

    def away(key: str) -> bool:
        return key in moving or any(within(key, f) for f in folders_away)

    for unit in units:
        if unit.kind != "folder":
            continue
        for other in units:
            if other is unit:
                continue
            for side in (other.old, other.new):
                if (within(disk_key(side), disk_key(unit.old))
                        or within(disk_key(side), disk_key(unit.new))):
                    refuse(side, f"is inside the folder line {unit.pair} renames; rename the "
                                 "folder and its contents in two runs")

    destinations: dict[str, Path] = {}
    blocked: set[int] = set()
    for position, unit in enumerate(units):
        for src, dst in unit.moves:
            dst_key, src_key = disk_key(dst), disk_key(src)
            if dst_key in destinations:
                refuse(dst, f"two renames end here, from {destinations[dst_key]} and {src}")
                continue
            destinations[dst_key] = src
            if dst_key == src_key:
                continue  # a change of case only
            if os.path.lexists(dst):
                if away(dst_key):
                    blocked.add(position)
                else:
                    refuse(dst, "already exists and this plan does not move it away; a "
                                "rename never replaces anything")
    for unit in units:
        if unit.kind != "video":
            continue
        video = unit.videos[0]
        here = listing(video.new.parent)
        if here is None:
            continue
        for path in _adopted(video.new, here, away):
            if disk_key(path) in destinations:
                continue
            refuse(path, f"already sits beside the new name {video.new.name} and would be "
                         "read as belonging to it; move it away first")
    for position in sorted(blocked):
        for src, _dst in units[position].moves:
            temporary = src.with_name(src.name + token)
            if os.path.lexists(temporary):
                refuse(temporary, "the temporary name for a chained rename already exists")
            elif len(str(temporary)) > opts.max_path:
                refuse(temporary, f"the temporary name for a chained rename is "
                                  f"{len(str(temporary))} characters, over the limit of "
                                  f"{opts.max_path}")

    # ------------------------------------------------ slots
    videos = [v for u in units for v in u.videos]
    series: dict[str, Path] = {}
    by_series: dict[str, list[Video]] = {}
    for video in videos:
        folder = roots.series_folder(video.new)
        series.setdefault(disk_key(folder), folder)
        if (video.after.episode is not None and not video.after.is_extra
                and roots.kind(video.new) != MOVIES):
            by_series.setdefault(disk_key(folder), []).append(video)
    for folder_key, targets in by_series.items():
        folder = series[folder_key]
        holders: dict[tuple[int | None, int | None], list[Path]] = {}
        if folder.is_dir():
            for entry in walk(folder, suffixes=VIDEO_SUFFIXES, sizes=False):
                if away(disk_key(entry.path)):
                    continue
                found = parse(entry.path)
                if found.episode is None or found.is_extra:
                    continue
                holders.setdefault(_slot(found), []).append(entry.path)
        for video in targets:
            holders.setdefault(_slot(video.after), []).append(video.new)
        target_keys = {disk_key(v.new) for v in targets}
        for slot, paths in sorted(holders.items(), key=lambda kv: str(kv[0])):
            if len(paths) < 2 or not any(disk_key(p) in target_keys for p in paths):
                continue
            one_folder = len({disk_key(p.parent) for p in paths}) < len(paths)
            refuse(
                paths[0],
                f"{_slot_text(slot)} would be held by {len(paths)} files: "
                + "; ".join(str(p) for p in paths)
                + (" -- in one folder the server merges them into one item, for good"
                   if one_folder else ""),
            )

    # ------------------------------------------------ notification targets
    changed: dict[str, Path] = {}
    for unit in units:
        for folder in (unit.old.parent, unit.new.parent):
            changed.setdefault(disk_key(folder), folder)
    for folder in changed.values():
        root = roots.holds_root(folder)
        if root is None:
            continue
        if opts.allow_library_scan:
            notes.append(f"{folder} is, or holds, the library root {root}: the notification "
                         "starts a validation of the whole library (allowed)")
        else:
            refuse(folder, f"a change here must be notified at the library root {root}, which "
                           "starts a validation of the whole library; pass "
                           "--allow-library-scan to accept that")
    if not opts.roots:
        notes.append("no library roots are known: a notification of a root could not be "
                     "ruled out here")
    else:
        # A top-level folder that goes or appears costs a validation of the
        # whole library whether or not anybody notifies; say so beforehand.
        notes += warnings_for(
            list(opts.roots),
            removes=[unit.old for unit in units if unit.kind == "folder"],
            creates=[unit.new for unit in units],
        )

    # ------------------------------------------------ steps
    mkdirs: dict[str, Path] = {}
    for unit in units:
        if not unit.new.parent.is_dir():
            mkdirs.setdefault(disk_key(unit.new.parent), unit.new.parent)
    steps: list[Step] = list(park_steps)
    steps += [Step(id=f"mkdir:{n:04d}", action="mkdir", params={"path": str(path)},
                   summary=f"create {path}")
              for n, path in enumerate(mkdirs.values(), start=1)]
    counter = {"stage": 0, "rename": 0, "settle": 0}

    def step(kind: str, src: Path, dst: Path, summary: str) -> Step:
        counter[kind] += 1
        return Step(id=f"{kind}:{counter[kind]:04d}", action="rename",
                    params={"src": str(src), "dst": str(dst)}, summary=summary)

    for position in sorted(blocked):
        for src, _dst in units[position].moves:
            temporary = src.with_name(src.name + token)
            steps.append(step("stage", src, temporary,
                              f"rename {src} -> {temporary.name} (out of the way of "
                              "another rename)"))
    for position, unit in enumerate(units):
        if position in blocked:
            continue
        for src, dst in unit.moves:
            steps.append(step("rename", src, dst, f"rename {src} -> {dst}"))
    for position in sorted(blocked):
        for src, dst in units[position].moves:
            temporary = src.with_name(src.name + token)
            steps.append(step("settle", temporary, dst, f"rename {temporary.name} -> {dst}"))

    if blocked:
        notes.append(f"{len(blocked)} set(s) move through a temporary name, because their "
                     "target is held by another rename of this plan")
    reused = {disk_key(v.old) for v in videos} & {disk_key(v.new) for v in videos}
    if reused:
        notes.append(f"{len(reused)} target(s) take a path another video of this plan "
                     "leaves: the server keeps the item at that path, with the metadata "
                     "it read for the old file; --refresh items reads it again")
    if park_moves:
        notes.append(f"{len(park_moves)} stale .nfo file(s) parked, not renamed")
    return FilePlan(
        pairs=tuple(pairs),
        videos=tuple(videos),
        moves=tuple(m for u in units for m in u.moves),
        parked=tuple(park_moves),
        notify=tuple(sorted(changed.values(), key=str)),
        series=tuple(sorted(series.values(), key=str)),
        steps=tuple(steps),
        problems=tuple(problems),
        notes=tuple(notes),
        digest=digest,
    )


def _stem(name: str) -> str:
    return name[: len(name) - len(Path(name).suffix)] if Path(name).suffix else name


def _claims(stem: str, name: str) -> bool:
    folded, key = name.casefold(), stem.casefold()
    return len(folded) > len(key) and folded.startswith(key) and folded[len(key)] in ".-"


def _adopted(new: Path, here: FolderListing, away: Any) -> list[Path]:
    """Files already in the target folder that the new name would claim.

    A file a longer stem of a video that stays claims is that video's, as
    the sidecar rules have it; another video is never a sidecar.
    """
    stem = _stem(new.name)
    staying = [_stem(name) for name in here.videos()
               if not away(disk_key(here.folder / name))]
    out: list[Path] = []
    for name, is_dir in here.entries:
        path = here.folder / name
        if not _claims(stem, name) or away(disk_key(path)):
            continue
        if is_dir and not name.casefold().endswith(".trickplay"):
            continue
        if not is_dir and Path(name).suffix.lower() in VIDEO_SUFFIXES:
            continue
        if any(len(other) > len(stem) and _claims(other, name) for other in staying):
            continue
        out.append(path)
    return out


def _park_nfo(video: Video, policy: str) -> bool:
    if policy == "park":
        return True
    if policy == "carry":
        return False
    return video.changed


def _check_video(video: Video, refuse: Any, max_path: int) -> None:
    for reason in video.problems:
        refuse(video.new, f"intended {video.expect} ({video.expect.source}), but {reason}")
    longest = longest_derived_path(video.new)
    if longest > max_path:
        refuse(video.new, f"with the preview tiles the server writes beside it the longest "
                          f"path is {longest} characters, over the limit of {max_path}")


def _video_unit(
    index: int, raw: Pair, old: Path, new: Path, opts: Options, roots: _Roots,
    sets: Any, refuse: Any,
) -> _Unit | None:
    if old.suffix.lower() not in VIDEO_SUFFIXES:
        refuse(old, "is not a video; name the video and its sidecars follow it")
        return None
    if new.suffix.lower() != old.suffix.lower():
        refuse(new, f"changes the container ({old.suffix} to {new.suffix or 'nothing'}); a "
                    "rename keeps it")
        return None
    expect = raw.expect or infer_expect(new, roots.kind(new))
    video = Video(old, new, expect, parse(old), parse(new), index)
    _check_video(video, refuse, opts.max_path)
    found = sets(old)
    renames = found.renames(new)
    unit = _Unit(index, "video", old, new, moves=[renames[0]], videos=[video])
    park = _park_nfo(video, opts.nfo)
    for sidecar, (src, dst) in zip(found.sidecars, renames[1:], strict=True):
        if park and sidecar.kind is SidecarKind.NFO and sidecar.read_by_server:
            unit.parks.append(src)
            continue
        unit.moves.append((src, dst))
        _check_length(dst, src, sidecar.is_dir, refuse, opts.max_path)
    return unit


def _check_length(dst: Path, src: Path, is_dir: bool, refuse: Any, max_path: int) -> None:
    if len(str(dst)) > max_path:
        refuse(dst, f"{len(str(dst))} characters, over the limit of {max_path}")
    if is_dir and src.is_dir():
        for entry in walk(src, sizes=False):
            target = dst.joinpath(*entry.relative.parts)
            if len(str(target)) > max_path:
                refuse(target, f"{len(str(target))} characters, over the limit of {max_path}")
                break


def _folder_unit(
    index: int, raw: Pair, old: Path, new: Path, opts: Options, roots: _Roots,
    sets: Any, refuse: Any,
) -> _Unit:
    unit = _Unit(index, "folder", old, new, moves=[(old, new)])
    if old.name.casefold().endswith(".trickplay"):
        refuse(old, "is a preview folder; it moves with its video")
        return unit
    tree = walk(old, sizes=False)
    entries = list(tree)
    for skipped in tree.skipped:
        refuse(skipped.path, f"not walked ({skipped.reason.name.lower()}); a folder with a "
                             "link or an unreadable part in it is not renamed")
    too_long = [e for e in entries if len(str(new.joinpath(*e.relative.parts))) > opts.max_path]
    for entry in too_long[:5]:
        target = new.joinpath(*entry.relative.parts)
        refuse(target, f"{len(str(target))} characters, over the limit of {opts.max_path}")
    if len(too_long) > 5:
        refuse(new, f"{len(too_long) - 5} more path(s) below it over the limit")
    for entry in entries:
        if entry.path.suffix.lower() not in VIDEO_SUFFIXES:
            continue
        target = new.joinpath(*entry.relative.parts)
        expect = raw.expect or infer_expect(target, roots.kind(target))
        video = Video(entry.path, target, expect, parse(entry.path), parse(target), index)
        _check_video(video, refuse, opts.max_path)
        unit.videos.append(video)
        if _park_nfo(video, opts.nfo):
            found = sets(entry.path)
            unit.parks += [s.path for s in found.of_kind(SidecarKind.NFO) if s.read_by_server]
    if not unit.videos:
        refuse(old, "holds no video; nothing here is a library item to rename")
    return unit
