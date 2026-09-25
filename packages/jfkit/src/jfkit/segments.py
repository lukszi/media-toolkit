"""jfkit.segments -- media segments, and scoping an analysis to what is not covered.

A segment is a marked stretch of an item: an opening, a recap, closing
credits. Some come from a shared database through a plugin, and the rest have
to be found by analysing the audio of every episode -- which is a full read of
everything, and on mechanical storage that is a day.

So the useful operation is not "analyse the library". It is "analyse what is
not already covered", and that is what this module builds: the coverage, the
exclusion lists it implies, and a start that refuses while the disk is busy.

**No identifier is ever written down here.** A plugin's instance identifier
and a task's identifier are per-installation, and a hardcoded one does not
fail loudly -- it addresses a plugin that is not there, or worse, one that is.
Both are looked up by name, and a name that matches two things is an error
rather than a coin toss.

**A series is covered when most of it is.** Not all of it: a series with one
episode missing from the shared database is not worth a full analysis pass
over the other forty. The fraction is an argument, and the default is
written down rather than buried.

**Starting an analysis is starting a reader.** It reads every file it is
scoped to, so it goes through the same device gate as any other heavy job,
and the gate counts the server's own background work as the reader it is.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .client import Client
from .devices import Device
from .jobs import Gate, Process, gate
from .maintenance import running_tasks

__all__ = [
    "COVERED_FRACTION",
    "Coverage",
    "ScopeReport",
    "Segment",
    "Task",
    "cancel_task",
    "coverage",
    "find_plugin",
    "find_task",
    "plugins",
    "running",
    "scope_plugin",
    "segments_for",
    "start_task",
    "tasks",
]

log = logging.getLogger(__name__)

#: How much of a series has to be covered before analysing the rest is not
#: worth a full read of all of it. A default, and an argument everywhere.
COVERED_FRACTION = 0.8


class Ambiguous(LookupError):
    """A name matched more than one thing, so nothing was chosen."""


@dataclass(frozen=True)
class Segment:
    """One marked stretch of one item."""

    item_id: str
    kind: str
    start_ticks: int = 0
    end_ticks: int = 0

    @property
    def length_ticks(self) -> int:
        return max(0, self.end_ticks - self.start_ticks)


@dataclass(frozen=True)
class Task:
    """One scheduled task, by the name a person would recognise it by."""

    id: str
    name: str
    state: str = ""
    progress: float | None = None

    @property
    def running(self) -> bool:
        return self.state.lower() == "running"


@dataclass(frozen=True)
class Coverage:
    """What is already covered, and what a pass would have to read."""

    covered_series: tuple[str, ...] = ()
    uncovered_series: tuple[str, ...] = ()
    covered_movies: tuple[str, ...] = ()
    uncovered_movies: tuple[str, ...] = ()
    episodes_to_read: int = 0
    fraction: float = COVERED_FRACTION

    def __str__(self) -> str:
        return (
            f"{len(self.covered_series)} series and {len(self.covered_movies)} "
            f"film(s) already covered; {len(self.uncovered_series)} series "
            f"({self.episodes_to_read} episode(s)) and "
            f"{len(self.uncovered_movies)} film(s) would be read"
        )


@dataclass(frozen=True)
class ScopeReport:
    """What the plugin's configuration would become, and whether it was written."""

    plugin_id: str
    applied: bool
    series_excluded: int = 0
    movies_excluded: int = 0
    before: Mapping[str, Any] = field(default_factory=dict)
    notes: tuple[str, ...] = ()
    #: exclusions the configuration already carried, kept in the new lists
    kept: int = 0
    #: where the configuration as it was was written before the change
    backup: Path | None = None

    def __str__(self) -> str:
        head = "written" if self.applied else "dry run, nothing written"
        lines = [
            f"{self.plugin_id}: {head}; excluding {self.series_excluded} series "
            f"and {self.movies_excluded} film(s) that are already covered"
            + (f", keeping {self.kept} exclusion(s) already there" if self.kept else "")
        ]
        if self.backup is not None:
            lines.append(f"  the configuration as it was is at {self.backup}")
        lines += [f"  note: {n}" for n in self.notes]
        return "\n".join(lines)


# ------------------------------------------------------------------- lookup
def plugins(client: Client) -> list[dict[str, Any]]:
    found = client.get("/Plugins")
    return list(found) if isinstance(found, list) else []


def find_plugin(client: Client, name_contains: str) -> dict[str, Any]:
    """The one plugin whose name contains this, or an error saying why not.

    By name rather than by identifier because an identifier written into a
    source file is per-installation: on another machine it addresses nothing,
    and the call succeeds quietly against nothing.
    """
    needle = name_contains.casefold()
    matches = [
        plugin for plugin in plugins(client)
        if needle in str(plugin.get("Name") or "").casefold()
    ]
    if not matches:
        known = ", ".join(sorted(str(p.get("Name")) for p in plugins(client))) or "none"
        raise LookupError(f"no plugin named like {name_contains!r}; installed: {known}")
    if len(matches) > 1:
        raise Ambiguous(
            f"{name_contains!r} matches "
            + ", ".join(sorted(str(p.get("Name")) for p in matches))
        )
    return matches[0]


def tasks(client: Client) -> list[Task]:
    found = client.get("/ScheduledTasks")
    rows = found if isinstance(found, list) else []
    return [
        Task(
            id=str(row.get("Id") or ""),
            name=str(row.get("Name") or row.get("Key") or ""),
            state=str(row.get("State") or ""),
            progress=row.get("CurrentProgressPercentage"),
        )
        for row in rows
    ]


def find_task(client: Client, name_contains: str) -> Task:
    """The one task whose name contains this. Same reasoning as the plugin."""
    needle = name_contains.casefold()
    matches = [task for task in tasks(client) if needle in task.name.casefold()]
    if not matches:
        known = ", ".join(sorted(task.name for task in tasks(client))) or "none"
        raise LookupError(f"no task named like {name_contains!r}; there are: {known}")
    if len(matches) > 1:
        raise Ambiguous(
            f"{name_contains!r} matches " + ", ".join(sorted(t.name for t in matches))
        )
    return matches[0]


def running(client: Client) -> list[Task]:
    """Every task the server says is running right now."""
    return [task for task in tasks(client) if task.running]


# ----------------------------------------------------------------- segments
def segments_for(client: Client, item_id: str) -> list[Segment]:
    """Every marked stretch the server holds for one item."""
    found = client.get(f"/MediaSegments/{item_id}")
    rows = (found or {}).get("Items") if isinstance(found, dict) else found
    return [
        Segment(
            item_id=str(row.get("ItemId") or item_id),
            kind=str(row.get("Type") or row.get("SegmentType") or ""),
            start_ticks=int(row.get("StartTicks") or 0),
            end_ticks=int(row.get("EndTicks") or 0),
        )
        for row in (rows or [])
    ]


def coverage(
    items: Sequence[Mapping[str, Any]],
    covered_ids: Iterable[str],
    *,
    fraction: float = COVERED_FRACTION,
) -> Coverage:
    """Which series and films already have segments, and which would be read.

    Takes records and a set of identifiers rather than a connection, so the
    decision is testable without a server and can be re-run over a saved
    fetch. Identifiers are compared without their separators, because the two
    places they come from spell them differently.
    """
    covered = {_bare(item_id) for item_id in covered_ids}
    by_series: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    covered_movies: list[str] = []
    uncovered_movies: list[str] = []

    for item in items:
        kind = item.get("Type")
        if kind == "Episode":
            key = (str(item.get("SeriesId") or ""), str(item.get("SeriesName") or ""))
            by_series.setdefault(key, []).append(item)
        elif kind == "Movie":
            target = (
                covered_movies if _bare(str(item.get("Id") or "")) in covered
                else uncovered_movies
            )
            target.append(str(item.get("Id") or ""))

    covered_series: list[str] = []
    uncovered_series: list[str] = []
    to_read = 0
    for (series_id, _name), episodes in sorted(by_series.items()):
        have = sum(1 for e in episodes if _bare(str(e.get("Id") or "")) in covered)
        if episodes and have / len(episodes) >= fraction:
            if series_id:
                covered_series.append(series_id)
        else:
            if series_id:
                uncovered_series.append(series_id)
            to_read += len(episodes) - have

    return Coverage(
        covered_series=tuple(covered_series),
        uncovered_series=tuple(uncovered_series),
        covered_movies=tuple(covered_movies),
        uncovered_movies=tuple(uncovered_movies),
        episodes_to_read=to_read,
        fraction=fraction,
    )


def _bare(item_id: str) -> str:
    return item_id.replace("-", "").lower()


def scope_plugin(
    client: Client,
    plugin: Mapping[str, Any],
    found: Coverage,
    *,
    backup_dir: Path | str | None = None,
    series_key: str = "SeriesExclusions",
    movie_key: str = "MovieExclusions",
    auto_key: str = "AutoDetectIntros",
) -> ScopeReport:
    """Add the exclusions the coverage implies, and leave automatic detection off.

    The configuration is read whole and sent back whole, for the same reason
    every other record in this package is: a key left out of the body is not
    left alone.

    **Exclusions already there are kept.** Somebody put them there -- a
    series nobody wants analysed, a film whose segments were placed by hand
    -- and the coverage cannot know why. The new lists are the old ones plus
    whatever the coverage adds, in that order, without duplicates. An
    exclusion value that is not a list is refused rather than guessed at.

    **An applied write keeps a rollback.** The configuration as it was is
    written to ``backup_dir`` before anything is sent, and an applied call
    without one is refused before anything is sent.
    """
    plugin_id = str(plugin.get("Id") or "")
    if not plugin_id:
        raise ValueError("this plugin record has no identifier")
    if not client.dry_run and backup_dir is None:
        raise ValueError(
            "an applied scope writes the plugin's configuration as it was first "
            "and needs somewhere to put it: pass backup_dir (--backup-dir DIR). "
            "Nothing was sent."
        )
    before = client.get(f"/Plugins/{plugin_id}/Configuration") or {}
    body = dict(before)
    kept = 0
    for key, adding in (
        (series_key, found.covered_series), (movie_key, found.covered_movies)
    ):
        existing = before.get(key)
        if existing is None:
            existing = []
        if not isinstance(existing, list):
            raise ValueError(
                f"{key} is {type(existing).__name__}, not a list; refusing to merge "
                "exclusions into a shape this does not understand. Nothing was sent."
            )
        kept += len(existing)
        merged = list(existing)
        seen = {_bare(str(value)) for value in existing}
        for value in adding:
            if _bare(value) not in seen:
                merged.append(value)
                seen.add(_bare(value))
        body[key] = merged
    notes: list[str] = []
    if auto_key in body and body.get(auto_key):
        body[auto_key] = False
        notes.append(
            f"{auto_key} was on: it analyses everything the exclusions do not "
            "cover, whenever it likes, which is the opposite of scoping a pass"
        )
    backup = (
        _save_configuration(before, backup_dir, str(plugin.get("Name") or plugin_id))
        if backup_dir is not None and not client.dry_run else None
    )
    client.post(f"/Plugins/{plugin_id}/Configuration", body)
    return ScopeReport(
        plugin_id=plugin_id,
        applied=not client.dry_run,
        series_excluded=len(found.covered_series),
        movies_excluded=len(found.covered_movies),
        before=before,
        notes=tuple(notes),
        kept=kept,
        backup=backup,
    )


def _save_configuration(
    configuration: Mapping[str, Any], directory: Path | str, name: str
) -> Path:
    """Write a plugin's configuration aside, stamped, never over another one."""
    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in name).strip("-")
    target = out / f"{safe or 'plugin'}.configuration.{stamp}.json"
    with target.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(
            json.dumps(dict(configuration), ensure_ascii=False, indent=1,
                       sort_keys=True) + "\n"
        )
    return target


# --------------------------------------------------------------- the switch
def start_task(
    client: Client,
    task: Task,
    *,
    device: Device | None = None,
    processes: Iterable[Process] | None = None,
    own_tag: str | None = None,
) -> Gate | None:
    """Start a task, unless the disk it will read is already busy.

    Returns the gate it checked, or ``None`` when no device was named. A held
    gate is returned rather than raised: the caller usually wants to wait and
    try again, and an exception is a poor shape for "not yet".
    """
    if device is not None:
        held = gate(
            device, processes=processes,
            running_tasks=running_tasks(client), own_tag=own_tag,
        )
        if not held.open:
            log.info("not starting %s: %s", task.name, held)
            return held
    client.post(f"/ScheduledTasks/Running/{task.id}")
    log.info("started %s", task.name)
    return None


def cancel_task(client: Client, task: Task) -> None:
    """Stop a running task.

    Worth having as a verb of its own because the alternative, when an
    analysis pass turns out to be reading a disk something else needs, is
    restarting the server -- which is a much larger interruption than the one
    being fixed.
    """
    client.request("DELETE", f"/ScheduledTasks/Running/{task.id}")
    log.info("asked %s to stop", task.name)
