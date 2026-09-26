"""Which catalogue rows are copies of one film or one episode, and which only look it.

**Films are grouped by provider identifier.** Two rows that share a TMDB,
IMDb or TVDB identifier are candidates, and so is any row linked to them
through another shared identifier. A group whose rows then disagree on an
identifier both carry -- one TMDB number, two different IMDb ones -- is not
resolved: somebody identified one of them wrongly, and the verdict says so.

**Episodes are grouped by their slot:** the series' identifier, the season
number, the episode number and the number the episode ends at. Never by the
series *name*: two different shows can share a name, and grouping by it
once produced a verdict about two unrelated episodes. A multi-episode file
(``E01-E02``) has a different slot from a single ``E01``.

**A slot alone is not an identity.** A folder that holds several shows
numbered in one sequence, or a numbering clash, puts different episodes in
one slot. So an episode group stands only when every member carries one
provider identifier they all share; the catalogue's *name* is no evidence
either (the server can give three different parts one name). Anything less
is reported as not shown to be copies.

**Segments are not copies.** A file whose episode number carries a letter
(``S01E01a``, ``S01E01b``), a sub-number (``S02E00.1``, ``S02E00.2``) or a
part marker (``part1``, ``Part II``, ``cd2``) is one segment of something,
and the server may give every segment the same slot and the same
identifiers. The segment marker is part of the key, so two
segments are never grouped; a slot that holds several segments is reported
as its own class, :data:`SEGMENTS`.

**Alternate versions are copies too.** An item that holds more than one
media source (two files the server merged into one row) contributes one
member per source.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePath, PureWindowsPath
from typing import Any

__all__ = [
    "PROVIDERS",
    "SEGMENTS",
    "Group",
    "GroupScan",
    "Member",
    "find_groups",
    "members_of",
    "segment_of",
]

#: The provider identifiers a film is grouped by, in the order they are shown.
PROVIDERS: tuple[str, ...] = ("Tmdb", "Imdb", "Tvdb")

#: The class of a slot that holds several distinct segments.
SEGMENTS = "segments"

# A letter straight after the episode number, and nothing alphanumeric after
# it: S01E01a, s1e1b. A digit after the letter is another episode (E01E02)
# or a version (E01v2), which are not segments.
_EPISODE_LETTER = re.compile(r"(?i)s\d{1,4}[ ._-]?e\d{1,4}([a-z])(?![a-z0-9])")
# A sub-number straight after the episode number: S02E00.1, S02E00.2 -- and
# not the start of a resolution (S01E01.1080p).
_EPISODE_SUB = re.compile(r"(?i)s\d{1,4}[ ._-]?e\d{1,4}\.(\d{1,2})(?![a-z0-9])")
# A part marker: part1, pt 2, Part II, cd1, disc 2, dvd1.
_PART = re.compile(
    r"(?i)(?<![a-z0-9])(?:(part|pt)[ ._-]?(\d{1,2}|[ivx]{1,4})|(cd|dvd|disc|disk)[ ._-]?(\d{1,2}))"
    r"(?![a-z0-9])"
)
_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8,
          "ix": 9, "x": 10}


def _name(path: str) -> str:
    if "\\" in path:
        return PureWindowsPath(path).name
    return PurePath(path).name


def segment_of(path: str | None) -> str | None:
    """The segment marker in a file name, lower case, or None for a whole file."""
    if not path:
        return None
    name = _name(path)
    stem = name.rsplit(".", 1)[0] if "." in name else name
    found = _EPISODE_LETTER.search(stem)
    if found:
        return found.group(1).lower()
    sub = _EPISODE_SUB.search(stem)
    if sub:
        return f".{int(sub.group(1))}"
    part = _PART.search(stem)
    if part is None:
        return None
    word = (part.group(1) or part.group(3)).lower()
    number = (part.group(2) or part.group(4)).lower()
    value = int(number) if number.isdigit() else _ROMAN.get(number)
    if value is None:
        return None
    return f"{'part' if word in ('part', 'pt') else word}{value}"


def _norm_id(value: Any) -> str:
    return str(value or "").replace("-", "").lower()


@dataclass(frozen=True)
class Member:
    """One copy: a catalogue row, or one media source of a row that has several."""

    item_id: str
    name: str
    path: str
    kind: str
    year: int | None = None
    series_id: str | None = None
    series_name: str | None = None
    season: int | None = None
    episode: int | None = None
    episode_end: int | None = None
    provider_ids: Mapping[str, str] = field(default_factory=dict)
    runtime_s: float | None = None
    #: the row the member was read from, where it is one source of several
    parent_id: str | None = None

    @property
    def segment(self) -> str | None:
        return segment_of(self.path)

    @property
    def file_name(self) -> str:
        return _name(self.path)

    def title(self) -> str:
        if self.kind == "Episode":
            number = f"S{self.season or 0:02d}E{self.episode or 0:02d}"
            if self.episode_end and self.episode_end != self.episode:
                number += f"-E{self.episode_end:02d}"
            return f"{self.series_name or '?'} {number}"
        return f"{self.name} ({self.year})" if self.year else self.name


@dataclass(frozen=True)
class Group:
    """Members that share one identity, and what is already known about them."""

    key: str
    kind: str
    members: tuple[Member, ...]
    #: set when the group is known not to be copies before anything is read
    not_duplicate: str | None = None
    #: the class of a group reported for information: ``SEGMENTS``, or None
    cls: str | None = None

    @property
    def title(self) -> str:
        return self.members[0].title() if self.members else self.key


@dataclass(frozen=True)
class GroupScan:
    """Every group, and what was not looked at and why."""

    groups: tuple[Group, ...]
    items_seen: int = 0
    no_identifier: int = 0
    no_slot: int = 0
    no_path: int = 0

    @property
    def candidates(self) -> tuple[Group, ...]:
        return tuple(g for g in self.groups if g.not_duplicate is None)

    def notes(self) -> list[str]:
        out = [f"{self.items_seen} film and episode row(s) read"]
        if self.no_identifier:
            out.append(f"{self.no_identifier} film(s) carry no provider identifier "
                       "and are grouped with nothing")
        if self.no_slot:
            out.append(f"{self.no_slot} episode(s) have no series, season or episode "
                       "number and are grouped with nothing")
        if self.no_path:
            out.append(f"{self.no_path} row(s) have no path")
        return out


def members_of(item: Mapping[str, Any]) -> list[Member]:
    """The copies one catalogue row stands for: one, or one per media source."""
    kind = str(item.get("Type") or "")
    runtime = item.get("RunTimeTicks")
    base: dict[str, Any] = {
        "name": str(item.get("Name") or ""),
        "kind": kind,
        "year": item.get("ProductionYear"),
        "series_id": _norm_id(item.get("SeriesId")) or None,
        "series_name": item.get("SeriesName"),
        "season": item.get("ParentIndexNumber"),
        "episode": item.get("IndexNumber"),
        "episode_end": item.get("IndexNumberEnd"),
        "provider_ids": {
            str(k): str(v).strip() for k, v in (item.get("ProviderIds") or {}).items()
            if v and str(v).strip()
        },
        "runtime_s": float(runtime) / 10_000_000 if runtime else None,
    }
    sources = [s for s in item.get("MediaSources") or [] if isinstance(s, Mapping)]
    item_id = str(item.get("Id") or "")
    if len(sources) > 1:
        out = []
        for source in sources:
            path = str(source.get("Path") or "")
            if not path:
                continue
            ticks = source.get("RunTimeTicks")
            out.append(Member(
                item_id=str(source.get("Id") or item_id), path=path,
                parent_id=item_id,
                **{**base, "runtime_s": float(ticks) / 10_000_000 if ticks
                   else base["runtime_s"]},
            ))
        return out
    path = str(item.get("Path") or (sources[0].get("Path") if sources else "") or "")
    if not path:
        return []
    return [Member(item_id=item_id, path=path, **base)]


class _Union:
    def __init__(self) -> None:
        self.parent: dict[int, int] = {}

    def find(self, x: int) -> int:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def join(self, a: int, b: int) -> None:
        self.parent[self.find(a)] = self.find(b)


def _same_file(members: Sequence[Member]) -> bool:
    paths = {m.path.replace("\\", "/").casefold() for m in members}
    return len(paths) == 1


def _conflicts(members: Sequence[Member]) -> list[str]:
    """Identifiers two members both carry and disagree on."""
    out = []
    for provider in PROVIDERS:
        values = sorted({m.provider_ids[provider] for m in members
                         if provider in m.provider_ids})
        if len(values) > 1:
            out.append(f"{provider} {' / '.join(values)}")
    return out


def _split_segments(key: str, kind: str, members: list[Member]) -> list[Group]:
    """One group per segment marker; a slot of several segments reported too."""
    by_segment: dict[str | None, list[Member]] = {}
    for member in members:
        by_segment.setdefault(member.segment, []).append(member)
    out: list[Group] = []
    if len(by_segment) > 1:
        shown = ", ".join(sorted(str(s) if s else "(whole)" for s in by_segment))
        out.append(Group(
            key=key, kind=kind, members=tuple(members), cls=SEGMENTS,
            not_duplicate=f"distinct segments sharing one identity ({shown}), "
                          "not copies of each other",
        ))
    for segment, rows in sorted(by_segment.items(), key=lambda kv: str(kv[0])):
        if len(rows) < 2:
            continue
        sub_key = key if segment is None else f"{key} segment {segment}"
        out.append(_checked(sub_key, kind, rows))
    return out


def _checked(key: str, kind: str, members: list[Member]) -> Group:
    ordered = tuple(sorted(members, key=lambda m: m.path.casefold()))
    if _same_file(ordered):
        return Group(key, kind, ordered,
                     not_duplicate="every row names the same file: one copy, "
                                   "catalogued more than once")
    conflicts = _conflicts(ordered)
    if conflicts:
        return Group(key, kind, ordered,
                     not_duplicate="the rows disagree on an identifier: "
                                   + "; ".join(conflicts))
    if kind == "Episode" and not _shared(ordered):
        return Group(key, kind, ordered, not_duplicate=(
            "nothing but the season and episode number links these rows: they share "
            "no provider identifier (a folder of several shows, or a numbering "
            "clash, looks exactly like this), so they are not treated as copies"
        ))
    return Group(key, kind, ordered)


def _shared(members: Sequence[Member]) -> set[tuple[str, str]]:
    """The provider identifiers every member carries, with the same value."""
    sets = [
        {(p, v.casefold()) for p, v in m.provider_ids.items() if p in PROVIDERS}
        for m in members
    ]
    return set.intersection(*sets) if sets else set()


def find_groups(items: Iterable[Mapping[str, Any]]) -> GroupScan:
    """Every group of two or more copies, films by identifier, episodes by slot."""
    films: list[Member] = []
    episodes: dict[tuple[str, int, int, int], list[Member]] = {}
    seen = no_identifier = no_slot = no_path = 0
    for item in items:
        kind = item.get("Type")
        if kind not in {"Movie", "Episode"}:
            continue
        seen += 1
        rows = members_of(item)
        if not rows:
            no_path += 1
            continue
        for member in rows:
            if kind == "Movie":
                if not any(member.provider_ids.get(p) for p in PROVIDERS):
                    no_identifier += 1
                    continue
                films.append(member)
            else:
                if not member.series_id or member.season is None or member.episode is None:
                    no_slot += 1
                    continue
                end = member.episode_end if member.episode_end is not None else member.episode
                key = (member.series_id, int(member.season), int(member.episode), int(end))
                episodes.setdefault(key, []).append(member)

    groups: list[Group] = []
    union = _Union()
    owner: dict[tuple[str, str], int] = {}
    for index, member in enumerate(films):
        union.find(index)
        for provider in PROVIDERS:
            value = member.provider_ids.get(provider)
            if not value:
                continue
            ident = (provider, value.casefold())
            if ident in owner:
                union.join(index, owner[ident])
            else:
                owner[ident] = index
    clusters: dict[int, list[Member]] = {}
    for index, member in enumerate(films):
        clusters.setdefault(union.find(index), []).append(member)
    for rows in clusters.values():
        if len(rows) < 2:
            continue
        ids = rows[0].provider_ids
        first = next(p for p in PROVIDERS if ids.get(p))
        groups += _split_segments(f"{first}={ids[first]}", "Movie", rows)

    for (series, season, episode, end), rows in episodes.items():
        if len(rows) < 2:
            continue
        number = f"S{season:02d}E{episode:02d}" + (f"-E{end:02d}" if end != episode else "")
        groups += _split_segments(f"series {series} {number}", "Episode", rows)

    groups.sort(key=lambda g: (g.kind != "Movie", g.title.casefold(), g.key))
    return GroupScan(
        groups=tuple(groups), items_seen=seen, no_identifier=no_identifier,
        no_slot=no_slot, no_path=no_path,
    )
