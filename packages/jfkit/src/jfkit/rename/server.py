"""jfkit.rename.server -- the server side of a rename: ids, state, notify, wait, verify.

The file side (:mod:`.plan`) decides what moves. This module finds the items
the old paths are (:func:`read_catalogue`), adds the notification steps to the
plan (:func:`prepare`), waits for the server to show the new items
(:func:`wait_for`), maps old identifiers to new ones through the paths, and
checks the end state (:func:`verify_end`).

**Finding items by path.** The server has no query by path. The items of a
rename are found through the series that holds them: every series whose
folder holds an old or new path is read with its videos and extras (the
extras of the series and of each season, which ordinary listings leave out).
A path in no series -- a film -- is found in one pass over the library's
videos, with the extras of the films in its folder. ``parents`` names the
series (or any container) directly and skips the search.

**The notification.** The deepest folders whose contents changed, in batches
(:data:`NOTIFY_BATCH` paths per request). A library root is refused by the
file plan already; here, a new folder is refused too when the server knows
nothing between it and the library root, because the server walks up from a
path it does not know to the nearest item it does -- the library -- and
validates all of it.

**Waiting.** A notification is a request; the scan runs when the server gets
to it. :func:`wait_for` reads the catalogue again every ``poll_s`` seconds
until every new path is an item and no old path is, or until ``timeout_s``.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mkvkit.steps import Action, FunctionAction, Step

from .. import query
from ..client import Client
from ..refresh import notify_changed
from .plan import FilePlan, Problem, Video, disk_key

__all__ = [
    "NOTIFY",
    "NOTIFY_BATCH",
    "VIDEO_TYPES",
    "Arrival",
    "Catalogue",
    "EndState",
    "Prepared",
    "actions",
    "id_map",
    "library_kinds",
    "prepare",
    "read_catalogue",
    "server_key",
    "verify_end",
    "wait_for",
]

log = logging.getLogger(__name__)

#: The items that carry play state, and that a rename moves.
VIDEO_TYPES = ("Episode", "Movie", "Video", "MusicVideo")

#: The action name of a notification step.
NOTIFY = "rename.notify"

#: Paths per notification request. The route takes a list; a few hundred
#: folders in one body is legal, but a request per batch keeps each one
#: small enough to read in the audit.
NOTIFY_BATCH = 100


def server_key(path: Path | str) -> str:
    """A path as the server compares one: either separator, any case."""
    return str(path).replace("\\", "/").casefold().rstrip("/")


def _below(key: str, folder: str) -> bool:
    return key == folder or key.startswith(folder + "/")


def library_kinds(client: Client) -> dict[str, str | None] | None:
    """Every library folder the server has, with its collection type; None if unreadable."""
    try:
        found = client.get("/Library/VirtualFolders")
    except Exception:  # unreadable: the caller treats it as unknown, never as none
        return None
    if not isinstance(found, list):
        return None
    out: dict[str, str | None] = {}
    for library in found:
        if not isinstance(library, dict):
            continue
        kind = library.get("CollectionType")
        for location in library.get("Locations") or []:
            out[str(location)] = str(kind) if kind else None
    return out


# ------------------------------------------------------------- the catalogue
@dataclass(frozen=True)
class Catalogue:
    """The items around a rename, by path."""

    #: path key -> item
    items: Mapping[str, Mapping[str, Any]]
    #: the series (or other containers) read
    owners: tuple[Mapping[str, Any], ...] = ()
    #: identifiers of the items that are extras
    extras: frozenset[str] = frozenset()
    #: paths more than one item claims
    doubled: tuple[str, ...] = ()

    def at(self, path: Path | str) -> Mapping[str, Any] | None:
        return self.items.get(server_key(path))

    def knows(self, folder: Path | str) -> bool:
        """True when the server has an item at this folder or below it."""
        key = server_key(folder)
        return any(_below(k, key) for k in self.items) or any(
            _below(server_key(str(o.get("Path") or "")), key) for o in self.owners
        )

    @property
    def ids(self) -> list[str]:
        return [str(item["Id"]) for item in self.items.values()]


def read_catalogue(
    client: Client, paths: Iterable[Path | str], *, parents: Sequence[str] = (),
) -> Catalogue:
    """Every video and extra of the series (or films) the paths belong to."""
    keys = [server_key(p) for p in paths]
    owners: list[Mapping[str, Any]]
    if parents:
        owners = [client.item(parent) for parent in parents]
    else:
        owners = [
            s for s in query.find(client, types=("Series",))
            if s.get("Path") and any(_below(k, server_key(s["Path"])) for k in keys)
        ]
    rows: list[Mapping[str, Any]] = []
    extras: set[str] = set()

    def add_extras(owner_id: str) -> None:
        for row in query.children(client, owner_id, extras=True):
            extras.add(str(row["Id"]))
            rows.append(row)

    for owner in owners:
        owner_id = str(owner["Id"])
        rows += query.children(client, owner_id, recursive=True, types=VIDEO_TYPES)
        add_extras(owner_id)
        for season in query.children(client, owner_id, types=("Season",)):
            add_extras(str(season["Id"]))
    covered = [server_key(str(o.get("Path") or "")) for o in owners if o.get("Path")]
    rest = [k for k in keys if not any(_below(k, c) for c in covered)]
    if rest and not parents:
        folders = {k.rsplit("/", 1)[0] for k in rest}
        for item in query.find(client, types=VIDEO_TYPES):
            where = server_key(str(item.get("Path") or ""))
            if where and any(_below(where, f) for f in folders):
                rows.append(item)
                if item.get("Type") == "Movie":
                    add_extras(str(item["Id"]))
    items: dict[str, Mapping[str, Any]] = {}
    doubled: list[str] = []
    for row in rows:
        path = str(row.get("Path") or "")
        if not path:
            continue
        key = server_key(path)
        if key in items and str(items[key]["Id"]) != str(row["Id"]):
            doubled.append(path)
        items.setdefault(key, row)
    return Catalogue(items=items, owners=tuple(owners), extras=frozenset(extras),
                     doubled=tuple(doubled))


# --------------------------------------------------------------- the plan
@dataclass(frozen=True)
class Prepared:
    """What the server adds to a file plan: identifiers, scope, notification."""

    catalogue: Catalogue
    #: path key of each old video -> its item id
    old_ids: Mapping[str, str]
    #: old videos the server has no item for
    unknown: tuple[Path, ...]
    steps: tuple[Step, ...]
    problems: tuple[Problem, ...] = ()
    notes: tuple[str, ...] = ()
    #: the identifiers of the containers read, for the wait and the replay
    parents: tuple[str, ...] = ()

    @property
    def scope(self) -> list[str]:
        """Every item whose state is snapshot: all the videos and extras around."""
        return self.catalogue.ids


def prepare(
    client: Client,
    files: FilePlan,
    *,
    roots: Iterable[str],
    parents: Sequence[str] = (),
    allow_library_scan: bool = False,
) -> Prepared:
    """Find the old items, check the notification, and make its steps."""
    paths = [v.old for v in files.videos] + [v.new for v in files.videos]
    paths += [a for a, _b in files.moves] + [b for _a, b in files.moves]
    catalogue = read_catalogue(client, paths, parents=parents)
    old_ids: dict[str, str] = {}
    unknown: list[Path] = []
    for video in files.videos:
        item = catalogue.at(video.old)
        if item is None:
            unknown.append(video.old)
        else:
            old_ids[server_key(video.old)] = str(item["Id"])
    problems: list[Problem] = []
    notes: list[str] = []
    root_list = list(roots)
    root_keys = {disk_key(r) for r in root_list}
    for folder in files.notify:
        here = folder
        while (not catalogue.knows(here) and disk_key(here) not in root_keys
               and here.parent != here):
            here = here.parent
        if disk_key(here) in root_keys and disk_key(folder) not in root_keys:
            message = ("is new to the server, and so is every folder above it up to the "
                       "library root: a notification walks up to the library and "
                       "validates all of it")
            if allow_library_scan:
                notes.append(f"{folder} {message} (allowed)")
            else:
                problems.append(Problem(str(folder), message + "; pass --allow-library-scan "
                                                               "to accept that"))
    if unknown:
        notes.append(f"{len(unknown)} old video(s) are not in the catalogue; no watched "
                     "state can be carried for them")
    if catalogue.doubled:
        notes.append(f"{len(catalogue.doubled)} path(s) are held by more than one item "
                     "already")
    guard = [] if allow_library_scan else root_list
    folders = [str(p) for p in files.notify]
    steps = tuple(
        Step(id=f"notify:{n:04d}", action=NOTIFY,
             params={"paths": folders[i:i + NOTIFY_BATCH], "guard": guard},
             summary=f"notify the server that {len(folders[i:i + NOTIFY_BATCH])} "
                     f"folder(s) changed: {'; '.join(folders[i:i + NOTIFY_BATCH])}")
        for n, i in enumerate(range(0, len(folders), NOTIFY_BATCH), start=1)
    )
    return Prepared(catalogue=catalogue, old_ids=old_ids, unknown=tuple(unknown),
                    steps=steps, problems=tuple(problems), notes=tuple(notes),
                    parents=tuple(parents) or tuple(str(o["Id"]) for o in catalogue.owners))


def actions(client: Client) -> dict[str, Action]:
    """The notification action; it re-checks the roots it was planned with."""
    def notify(step: Step) -> Mapping[str, Any]:
        sent = notify_changed(client, [Path(p) for p in step.params["paths"]],
                              roots=step.params.get("guard") or ())
        return {"sent": len(sent)}

    return {NOTIFY: FunctionAction(notify)}


# ------------------------------------------------------------- the wait
@dataclass(frozen=True)
class Arrival:
    """The catalogue after the scan: which new paths are items, which old ones still are."""

    catalogue: Catalogue
    missing: tuple[Path, ...]
    lingering: tuple[Path, ...]
    polls: int
    waited_s: float

    @property
    def ok(self) -> bool:
        return not self.missing and not self.lingering

    def __str__(self) -> str:
        state = "every new item is there" if self.ok else "the server has not caught up"
        lines = [f"after {self.waited_s:.0f}s ({self.polls} read(s)): {state}"]
        lines += [f"  NOT YET AN ITEM {p}" for p in self.missing]
        lines += [f"  STILL AN ITEM   {p}" for p in self.lingering]
        return "\n".join(lines)


def wait_for(
    client: Client,
    videos: Sequence[Video],
    *,
    parents: Sequence[str] = (),
    timeout_s: float = 600.0,
    poll_s: float = 10.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Arrival:
    """Read until every new path is an item and no old one is, within a bound.

    ``poll_s`` of zero or less reads once and does not wait.
    """
    paths = [v.old for v in videos] + [v.new for v in videos]
    targets = {server_key(v.new) for v in videos}
    started = clock()
    polls = 0
    while True:
        polls += 1
        catalogue = read_catalogue(client, paths, parents=parents)
        missing = tuple(v.new for v in videos if catalogue.at(v.new) is None)
        lingering = tuple(v.old for v in videos if _lingers(catalogue, v, targets))
        waited = clock() - started
        # a poll of zero reads once; the count bounds the loop even when the
        # clock does not move, as under a test's sleep
        last = poll_s <= 0 or waited >= timeout_s or polls > timeout_s / poll_s
        if (not missing and not lingering) or last:
            return Arrival(catalogue, missing, lingering, polls, waited)
        log.info("%d new item(s) not there yet, %d old one(s) still are; waiting %.0fs",
                 len(missing), len(lingering), poll_s)
        sleep(poll_s)


def _lingers(catalogue: Catalogue, video: Video, targets: set[str]) -> bool:
    """True while the old path is still an item, and nothing else moved there.

    In a cycle the old path of one video is the new path of another. The
    server derives an item's identifier from its path, so the item there
    keeps its identifier and is the other video's new item, not a leftover.
    """
    key = server_key(video.old)
    return key not in targets and catalogue.at(video.old) is not None


def id_map(
    videos: Sequence[Video], old_ids: Mapping[str, str], arrival: Arrival,
) -> dict[str, str]:
    """Old identifier -> new identifier, through each video's old and new path."""
    out: dict[str, str] = {}
    for video in videos:
        old = old_ids.get(server_key(video.old))
        new = arrival.catalogue.at(video.new)
        if old is not None and new is not None:
            out[old] = str(new["Id"])
    return out


# ------------------------------------------------------------- the end state
@dataclass(frozen=True)
class EndState:
    """What the server made of every target."""

    lines: tuple[str, ...]
    problems: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems

    def __str__(self) -> str:
        head = (f"{len(self.lines)} target(s) checked: "
                + ("as intended" if self.ok else f"{len(self.problems)} problem(s)"))
        return "\n".join([head, *(f"  {line}" for line in self.lines),
                          *(f"  PROBLEM {p}" for p in self.problems)])


def _numbers(item: Mapping[str, Any]) -> str:
    season, episode, end = (item.get("ParentIndexNumber"), item.get("IndexNumber"),
                            item.get("IndexNumberEnd"))
    text = (f"S{season:02d}" if isinstance(season, int) else "S--") + (
        f"E{episode:02d}" if isinstance(episode, int) else "E--")
    return text + (f"-E{end:02d}" if isinstance(end, int) else "")


def verify_end(videos: Sequence[Video], arrival: Arrival) -> EndState:
    """Each new item is what was intended; no item has an old path; nothing is doubled."""
    catalogue = arrival.catalogue
    lines: list[str] = []
    problems: list[str] = []
    moved: set[str] = set()
    for video in videos:
        item = catalogue.at(video.new)
        if item is None:
            problems.append(f"{video.new}: no item has this path")
            continue
        item_id = str(item["Id"])
        moved.add(item_id)
        is_extra = item_id in catalogue.extras or bool(item.get("ExtraType"))
        kind = str(item.get("Type") or "?")
        seen = f"{kind} {'extra' if is_extra else _numbers(item)}"
        lines.append(f"{item_id}  {seen:<22} intended {video.expect}  {video.new.name}")
        want = video.expect
        if want.kind == "extra":
            if not is_extra:
                problems.append(f"{video.new}: not an extra ({seen})")
            continue
        if is_extra:
            problems.append(f"{video.new}: an extra, not {want}")
            continue
        if want.kind == "movie":
            if kind not in ("Movie", "Video"):
                problems.append(f"{video.new}: a {kind}, not a film")
            continue
        if want.kind == "none":
            if item.get("IndexNumber") is not None:
                problems.append(f"{video.new}: numbered {_numbers(item)}, though no number "
                                "was intended")
            continue
        if kind != "Episode":
            problems.append(f"{video.new}: a {kind}, not an episode")
        if want.season is not None and item.get("ParentIndexNumber") != want.season:
            problems.append(f"{video.new}: season {item.get('ParentIndexNumber')}, not "
                            f"{want.season}")
        if item.get("IndexNumber") != want.episode:
            problems.append(f"{video.new}: episode {item.get('IndexNumber')}, not "
                            f"{want.episode}")
        if item.get("IndexNumberEnd") != want.end:
            problems.append(f"{video.new}: end number {item.get('IndexNumberEnd')}, not "
                            f"{want.end}")
    problems += [f"{p}: still an item under its old path" for p in arrival.lingering]
    problems += [f"{p}: held by more than one item" for p in catalogue.doubled]
    slots: dict[tuple[Any, ...], list[str]] = {}
    for item in catalogue.items.values():
        if item.get("Type") != "Episode" or str(item["Id"]) in catalogue.extras:
            continue
        if item.get("IndexNumber") is None:
            continue
        slot = (item.get("SeriesId") or item.get("SeriesName"),
                item.get("ParentIndexNumber"), item.get("IndexNumber"))
        slots.setdefault(slot, []).append(str(item["Id"]))
    for slot, ids in slots.items():
        if len(ids) > 1 and moved.intersection(ids):
            problems.append(f"{slot[0]} S{slot[1]}E{slot[2]}: doubled, held by "
                            + ", ".join(ids))
    return EndState(tuple(lines), tuple(problems))

