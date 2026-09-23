"""jfkit.dto -- one record round-trip, and one way to compare two of them.

Reading an item, changing two fields and sending it back is not as simple as
it sounds, and getting it wrong empties fields nobody asked about. A folder of
near-identical scripts learned that separately; this is the one they collapse
into.

**Fetch the whole record, from the user-scoped route.** The unscoped
single-item route answers 400 on this server version, which is at least
honest. The record that comes back is the one that has to go back.

**Take the preview-image block out before sending.** A record posted with it
fails with a server error, and the error says nothing about which field caused
it.

**Send the whole object.** Scalars are assigned unconditionally at the other
end: a field left out of the body is not left alone, it is set to nothing.
This is the failure that made a two-field edit wipe an overview.

**Order the phases, and mean it.** Numbers first, then a non-replacing
refresh, then names and overviews, then dates LAST -- a refresh re-seeds an
empty air date and production year from the container's creation time, so a
date written before a refresh is a date that will not survive it. The
metadata lock goes last of all, and never on a folder, a series or a season,
where it cascades to every child underneath.

The list of fields the update route keeps and the list it drops are data here,
with a test behind them, because "I thought it kept the path" is a whole
afternoon.

Comparing two records is the other half. After any operation worth doing --
a refresh, a file swap, a header edit -- the question is not "did the command
succeed" but "what else moved". :func:`compare` answers it over names, the
overview, provider identifiers, chapters (with the time deltas in
milliseconds, not raw ticks), media streams and play state, and it knows the
one difference that is never a difference: a chapter called ``Chapter 5``
carries no information at all, because that is what the server writes when a
file has marks and no names for them.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import copy
import json
import logging
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .client import Client

__all__ = [
    "COPIES",
    "DISCARDS",
    "GENERIC_CHAPTER",
    "STRIP_BEFORE_POST",
    "TICKS_PER_SECOND",
    "Comparison",
    "Difference",
    "Phase",
    "chapter_rows",
    "compare",
    "fetch",
    "load",
    "normalise_chapter_name",
    "phases_for",
    "save",
    "stream_rows",
    "update_item",
    "user_data",
]

log = logging.getLogger(__name__)

#: One second, in the unit the server counts positions in.
TICKS_PER_SECOND = 10_000_000

#: Blocks that must come out of a record before it is sent back. The preview
#: block is the one that fails; it is a cache the server rebuilds anyway.
STRIP_BEFORE_POST: tuple[str, ...] = ("Trickplay",)

#: What the update route copies out of the body it is given. Measured against
#: the fixture server, not remembered.
COPIES = frozenset({
    "Name", "OriginalTitle", "ForcedSortName", "OriginalLanguage",
    "Overview", "Taglines", "Genres", "Studios", "People", "Tags",
    "ProductionLocations", "IndexNumber", "ParentIndexNumber",
    "PremiereDate", "ProductionYear", "EndDate", "DateCreated",
    "OfficialRating", "CustomRating", "CommunityRating", "CriticRating",
    "ProviderIds", "LockedFields", "LockData",
    "PreferredMetadataLanguage", "PreferredMetadataCountryCode",
    "DisplayOrder", "AspectRatio", "Video3DFormat",
    "AirsAfterSeasonNumber", "AirsBeforeSeasonNumber", "AirsBeforeEpisodeNumber",
    "Status", "AirDays", "AirTime", "RunTimeTicks",
})

#: What it drops on the floor, whatever the body says. Sending these is not an
#: error and not a change: it is a silence that reads as success.
DISCARDS = frozenset({
    "Path", "ExtraType", "OwnerId", "ParentId", "SeasonId",
    "IndexNumberEnd", "Width", "Height", "ChannelNumber", "MediaStreams",
    "MediaSources", "UserData", "Id", "Type",
})

#: The generated chapter name, in the languages a server writes it in. A mark
#: named like this is a mark with no name, and comparing two of them as
#: strings turns a renumbering into a fleet of false differences.
GENERIC_CHAPTER = re.compile(
    r"\A\s*(?:chapter|kapitel|chapitre|cap[ií]tulo|capitolo|scene|szene|part|teil)"
    r"\s*[-_.#]?\s*0*(\d+)\s*\Z",
    re.IGNORECASE,
)

#: A field that may only be written in a particular phase. Anything not named
#: here belongs to the general phase, which runs with the names.
_PHASE_OF: dict[str, str] = {
    "IndexNumber": "numbers",
    "ParentIndexNumber": "numbers",
    "AirsAfterSeasonNumber": "numbers",
    "AirsBeforeSeasonNumber": "numbers",
    "AirsBeforeEpisodeNumber": "numbers",
    "ProviderIds": "numbers",
    "PremiereDate": "dates",
    "ProductionYear": "dates",
    "EndDate": "dates",
    "LockData": "lock",
    "LockedFields": "lock",
}

#: Types whose metadata lock reaches every item beneath them. Locking one of
#: these to protect a single episode locks the whole series, and finding that
#: out later costs an unlock pass over everything it touched.
CASCADING_TYPES = frozenset({"Folder", "CollectionFolder", "BoxSet", "Series", "Season"})

#: The order the phases run in, and the reason each one is where it is.
PHASE_ORDER: tuple[str, ...] = ("numbers", "refresh", "names", "dates", "lock")


@dataclass(frozen=True)
class Phase:
    """One write, and what it carries."""

    name: str
    fields: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_refresh(self) -> bool:
        return self.name == "refresh"

    def __str__(self) -> str:
        if self.is_refresh:
            return "refresh (non-replacing)"
        return f"{self.name}: " + ", ".join(sorted(self.fields))


@dataclass(frozen=True)
class Difference:
    """One field that is not what it was."""

    where: str
    before: Any
    after: Any
    note: str = ""

    def __str__(self) -> str:
        tail = f"  ({self.note})" if self.note else ""
        return f"{self.where}: {self.before!r} -> {self.after!r}{tail}"


@dataclass(frozen=True)
class Comparison:
    """Everything that moved between two records, and everything that was allowed to."""

    item_id: str
    differences: tuple[Difference, ...] = ()
    expected: tuple[Difference, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """True when nothing outside the declared changes moved."""
        return not self.differences

    def __str__(self) -> str:
        head = f"{self.item_id}: {'no drift' if self.ok else f'{len(self.differences)} change(s)'}"
        lines = [head]
        lines += [f"  {d}" for d in self.differences]
        lines += [f"  expected: {d}" for d in self.expected]
        lines += [f"  note: {n}" for n in self.notes]
        return "\n".join(lines)


# ------------------------------------------------------------------- reading
def fetch(client: Client, item_id: str) -> dict[str, Any]:
    """The full record for one item, from the route that returns all of it."""
    return client.item(item_id, user_scoped=True)


def user_data(
    client: Client, item_id: str, users: Iterable[str]
) -> dict[str, dict[str, Any]]:
    """Play state for one item, for every user named.

    Play state is per user, and an operation that looks harmless against the
    administrator's view can still discard somebody else's position. Anything
    that deletes or re-creates a row reads this first.
    """
    out: dict[str, dict[str, Any]] = {}
    for user in users:
        found = client.get(f"/Users/{user}/Items/{item_id}")
        if isinstance(found, dict) and found.get("UserData"):
            out[user] = dict(found["UserData"])
    return out


def save(item: Mapping[str, Any], path: Path | str) -> Path:
    """Write a record to disk, stable enough to diff two of them by eye."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(item, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return out


def load(path: Path | str) -> dict[str, Any]:
    """Read a record back. The other half of a rollback artefact."""
    found = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(found, dict):
        raise ValueError(f"{path}: not a record")
    return found


# ------------------------------------------------------------------- writing
def phases_for(
    fields: Mapping[str, Any], *, item_type: str | None = None, refresh: bool = True
) -> list[Phase]:
    """Split one set of changes into the order they have to be written in.

    The order is not a style preference. A number written after a refresh is
    fine; a date written before one is overwritten by the probe that runs at
    the front of every refresh. A name written before a refresh loses to the
    container's own title where the library is configured to believe it.
    """
    buckets: dict[str, dict[str, Any]] = {name: {} for name in PHASE_ORDER}
    for key, value in fields.items():
        buckets[_PHASE_OF.get(key, "names")][key] = value

    if item_type in CASCADING_TYPES and buckets["lock"]:
        raise ValueError(
            f"refusing to write {sorted(buckets['lock'])} on a {item_type}: the lock "
            "reaches every item underneath it, and nothing undoes that in one call"
        )

    out: list[Phase] = []
    for name in PHASE_ORDER:
        if name == "refresh":
            if refresh and out:
                out.append(Phase("refresh"))
            continue
        if buckets[name]:
            out.append(Phase(name, dict(buckets[name])))
    return out


def update_item(
    client: Client,
    item_id: str,
    fields: Mapping[str, Any],
    *,
    phase_order: bool = True,
    refresh: Callable[[str], None] | None = None,
    backup: Path | str | None = None,
) -> list[Phase]:
    """Apply named fields to one item, whole record at a time, in phase order.

    The record is fetched, the preview block is removed, the named fields are
    assigned and the whole object goes back. Nothing else in it is touched,
    and nothing else in it is omitted either.

    ``refresh`` is injected rather than imported so this module does not have
    to know how a refresh is asked for or waited on; :mod:`jfkit.refresh`
    supplies one. Without it the refresh phase is skipped and said to be.

    Returns the phases it ran. The client's own dry-run state decides whether
    anything was sent -- there is no second switch here.
    """
    unknown = sorted(set(fields) & DISCARDS)
    if unknown:
        raise ValueError(
            f"{', '.join(unknown)}: the update route discards these, so writing them "
            "reports success and changes nothing"
        )

    current = fetch(client, item_id)
    if backup is not None:
        save(current, backup)
    plan = phases_for(
        fields, item_type=current.get("Type"), refresh=refresh is not None
    ) if phase_order else [Phase("all", dict(fields))]

    for phase in plan:
        if phase.is_refresh:
            if refresh is None:  # pragma: no cover - guarded by phases_for
                continue
            log.info("%s: refresh between phases", item_id)
            refresh(item_id)
            current = fetch(client, item_id)
            continue
        body = copy.deepcopy(current)
        for block in STRIP_BEFORE_POST:
            body.pop(block, None)
        body.update(phase.fields)
        log.info("%s: %s", item_id, phase)
        client.post(f"/Items/{item_id}", body)
        current = body
    return plan


# ----------------------------------------------------------------- comparing
def normalise_chapter_name(name: str | None) -> str | None:
    """A generated chapter name becomes ``None``; anything else is itself.

    ``Chapter 5`` is what a server writes when a file has marks and no names.
    Two files whose marks were renumbered differ in every one of those
    strings and in nothing that matters, so they are normalised away before
    anything is compared -- and a real name that happens to be generic-looking
    is normalised too, which is the right trade: a name nobody can tell from
    the generated one is not evidence either.
    """
    if name is None:
        return None
    if GENERIC_CHAPTER.match(name):
        return None
    return name.strip() or None


def chapter_rows(item: Mapping[str, Any]) -> list[tuple[int, str | None]]:
    """Marks as (ticks, name), with the generated names normalised out."""
    return [
        (int(c.get("StartPositionTicks") or 0), normalise_chapter_name(c.get("Name")))
        for c in (item.get("Chapters") or [])
    ]


def stream_rows(item: Mapping[str, Any], kind: str | None = None) -> list[dict[str, Any]]:
    """The stream table, reduced to the fields a swap or an edit can move."""
    rows = []
    for stream in item.get("MediaStreams") or []:
        if kind is not None and stream.get("Type") != kind:
            continue
        rows.append({
            "Type": stream.get("Type"),
            "Index": stream.get("Index"),
            "Codec": (stream.get("Codec") or "").lower(),
            "Language": (stream.get("Language") or "").lower(),
            "Channels": stream.get("Channels"),
            "IsDefault": bool(stream.get("IsDefault")),
            "IsForced": bool(stream.get("IsForced")),
            "Title": stream.get("Title"),
        })
    return rows


#: Fields compared one to one, because a change in any of them is a change
#: somebody will notice in the interface.
SCALARS: tuple[str, ...] = (
    "Name", "OriginalTitle", "Overview", "IndexNumber", "ParentIndexNumber",
    "PremiereDate", "ProductionYear", "OfficialRating", "LockData", "Id", "Path",
)


def compare(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    expected: Iterable[str] = (),
    chapter_tolerance_ms: float = 1.0,
    scalars: Sequence[str] = SCALARS,
) -> Comparison:
    """What moved between two records of the same item.

    ``expected`` names the fields the operation was supposed to change. They
    are reported separately rather than hidden: an expected change that did
    not happen is as interesting as an unexpected one that did.

    Chapter times are compared with a tolerance and reported in milliseconds,
    because a mark that moved by a quarter of a frame is a rounding difference
    and a mark that moved by two seconds is a different cut.
    """
    allowed = set(expected)
    differences: list[Difference] = []
    expected_seen: list[Difference] = []
    notes: list[str] = []

    def record(where: str, was: Any, now: Any, note: str = "") -> None:
        diff = Difference(where, was, now, note)
        (expected_seen if where.split(".")[0] in allowed else differences).append(diff)

    for key in scalars:
        if before.get(key) != after.get(key):
            record(key, before.get(key), after.get(key))

    if (before.get("ProviderIds") or {}) != (after.get("ProviderIds") or {}):
        record("ProviderIds", before.get("ProviderIds"), after.get("ProviderIds"))

    differences += _chapter_differences(
        before, after, allowed, chapter_tolerance_ms, expected_seen
    )
    differences += _stream_differences(before, after, allowed, expected_seen)

    play_before = (before.get("UserData") or {})
    play_after = (after.get("UserData") or {})
    for key in ("PlayCount", "PlaybackPositionTicks", "Played", "IsFavorite"):
        if play_before.get(key) != play_after.get(key):
            record(f"UserData.{key}", play_before.get(key), play_after.get(key))

    if before.get("Id") and after.get("Id") and before["Id"] != after["Id"]:
        notes.append(
            "the identifier changed: this is a different row at the same path, and "
            "play state does not follow a row that was deleted and re-created"
        )
    return Comparison(
        item_id=str(after.get("Id") or before.get("Id") or ""),
        differences=tuple(differences),
        expected=tuple(expected_seen),
        notes=tuple(notes),
    )


def _chapter_differences(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    allowed: set[str],
    tolerance_ms: float,
    expected_seen: list[Difference],
) -> list[Difference]:
    was, now = chapter_rows(before), chapter_rows(after)
    if not was and not now:
        return []
    out: list[Difference] = []
    target = expected_seen if "Chapters" in allowed else out
    if len(was) != len(now):
        target.append(
            Difference("Chapters", f"{len(was)} mark(s)", f"{len(now)} mark(s)")
        )
        return out
    tolerance_ticks = tolerance_ms * TICKS_PER_SECOND / 1000.0
    for index, ((t_was, n_was), (t_now, n_now)) in enumerate(zip(was, now, strict=True)):
        delta = t_now - t_was
        if abs(delta) > tolerance_ticks:
            target.append(
                Difference(
                    f"Chapters[{index}].start",
                    f"{t_was / TICKS_PER_SECOND:.3f}s",
                    f"{t_now / TICKS_PER_SECOND:.3f}s",
                    f"{delta / TICKS_PER_SECOND * 1000:+.1f} ms",
                )
            )
        if n_was != n_now:
            target.append(Difference(f"Chapters[{index}].name", n_was, n_now))
    return out


def _stream_differences(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    allowed: set[str],
    expected_seen: list[Difference],
) -> list[Difference]:
    was, now = stream_rows(before), stream_rows(after)
    if not was and not now:
        return []
    out: list[Difference] = []
    target = expected_seen if "MediaStreams" in allowed else out
    if len(was) != len(now):
        target.append(
            Difference("MediaStreams", f"{len(was)} stream(s)", f"{len(now)} stream(s)")
        )
        return out
    for index, (row_was, row_now) in enumerate(zip(was, now, strict=True)):
        for key in row_was:
            if row_was[key] != row_now[key]:
                target.append(
                    Difference(f"MediaStreams[{index}].{key}", row_was[key], row_now[key])
                )
    return out
