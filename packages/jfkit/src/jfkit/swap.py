"""jfkit.swap -- replace files in place without losing what the catalogue knows.

The file-side package already parks an original and puts a rebuild in its
path. This is the half that has to happen around it, and it is the half that
decides whether the catalogue survives.

**The path does not change, so the identity does not change.** An item's
identity is derived from its path. Put the rebuild beside the original under
a new name and it is a new item: new identifier, no play state, no place in
anybody's list, and the old item is a missing file. Swap in place and every
reference in the catalogue still points at the same row.

**The plan names the file the catalogue names.** A plan line is checked
against the item's catalogued path before anything stops: a line that pairs
one item's identifier with another file would otherwise swap that file,
refresh the item nothing happened to, and report success.

**Nobody is watching.** Stopping a server under somebody's playback is the
failure people remember, and it costs one call to avoid.

**The service is stopped per chunk, and the chunks are sized in bytes.** Not
in files: a chunk of a hundred episodes and a chunk of a hundred films are the
same number of files and two very different outages. A chunk carries a
byte total, and where a copy rate is known the downtime it implies is
computed and compared against a budget *before* the service goes down.

**Play state is snapshotted for every user and replayed afterwards.** Every
user the server lists, unless the caller names some; a server whose user list
cannot be read is not swapped against. It usually survives -- the row is not
deleted, so nothing has to reattach -- but "usually" is not a thing to find out
about afterwards, and a position belonging to somebody who was not consulted
is not a thing to lose.

**Verification is a comparison, not a size check.** A rebuilt file
legitimately has a different size; that is generally why it was rebuilt. What
proves the swap is the record afterwards: the identifier is the same, the
stream table is the one the rebuild has, and the name, overview and provider
identifiers are exactly what they were. The record is polled until it shows
something true of the new file only -- a stream count the plan gives, or else
a stream table or chapter list that differs from the one before -- and a
record that never gets there is a problem, not a pass: the first read after a
refresh is usually the record from before it.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import functools
import logging
import os
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mkvkit import integrity
from mkvkit import swap as file_swap
from mkvkit.integrity import IntegrityReport

from .client import Client
from .dto import Comparison, compare, fetch, user_data
from .errors import ItemNotFound
from .refresh import RefreshReport, safe_refresh
from .safedelete import _same_path, resolve_users
from .service import ServiceController, stopped

__all__ = [
    "Chunk",
    "Outcome",
    "Pair",
    "SwapReport",
    "chunks",
    "default_replacement_check",
    "expected_streams",
    "preflight",
    "replay_play_state",
    "swap",
]

log = logging.getLogger(__name__)

#: A chunk this big is roughly the largest outage worth taking in one go.
#: A default, and an argument: the right number depends on how fast the
#: storage copies and how much of an outage anybody minds.
DEFAULT_CHUNK_GIB = 200.0

#: What a swap is expected to change about a record, and nothing else.
EXPECTED: tuple[str, ...] = ("MediaStreams", "Chapters")


@dataclass(frozen=True)
class Pair:
    """One item, the file it has now, and the file that should take its place."""

    item_id: str
    keeper: Path
    replacement: Path
    #: how many streams the record shows once the refresh has caught up, where
    #: the plan knows; otherwise any change to the stream table or chapters
    streams: int | None = None

    @property
    def size(self) -> int:
        try:
            return self.replacement.stat().st_size
        except OSError:
            return 0

    def as_file_pair(self) -> file_swap.SwapPair:
        return file_swap.SwapPair(keeper=self.keeper, replacement=self.replacement)


@dataclass(frozen=True)
class Chunk:
    """One outage: the pairs that will be swapped while the service is down."""

    index: int
    pairs: tuple[Pair, ...]
    total_bytes: int

    @property
    def gib(self) -> float:
        return self.total_bytes / 2**30

    def estimated_seconds(self, mib_per_second: float) -> float:
        """How long the copying alone should take at a known rate."""
        return (self.total_bytes / 2**20) / max(mib_per_second, 1e-9)

    def __str__(self) -> str:
        return f"chunk {self.index}: {len(self.pairs)} file(s), {self.gib:.1f} GiB"


@dataclass(frozen=True)
class Outcome:
    """What happened to one pair, and what the record said afterwards."""

    pair: Pair
    swapped: bool = False
    parked: Path | None = None
    refresh: RefreshReport | None = None
    comparison: Comparison | None = None
    play_state_restored: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems and (
            self.comparison is None or self.comparison.ok
        )

    def __str__(self) -> str:
        state = "swapped" if self.swapped else "not swapped"
        lines = [f"{self.pair.item_id}: {state}"]
        if self.parked is not None:
            lines.append(f"  the original is parked at {self.parked}")
        if self.play_state_restored:
            lines.append(
                f"  play state put back for {len(self.play_state_restored)} user(s)"
            )
        if self.comparison is not None and not self.comparison.ok:
            lines += ["  " + line for line in str(self.comparison).splitlines()[1:]]
        lines += [f"  problem: {p}" for p in self.problems]
        return "\n".join(lines)


@dataclass(frozen=True)
class SwapReport:
    """Every chunk, every pair, and how long the service was down for each."""

    chunks: tuple[Chunk, ...] = ()
    outcomes: tuple[Outcome, ...] = ()
    downtime_s: Mapping[int, float] = field(default_factory=dict)
    applied: bool = False
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return all(outcome.ok for outcome in self.outcomes)

    @property
    def swapped(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.swapped)

    def __str__(self) -> str:
        head = "applied" if self.applied else "dry run, nothing moved"
        lines = [
            f"{len(self.chunks)} chunk(s), {len(self.outcomes)} pair(s): {head}; "
            f"{self.swapped} swapped"
        ]
        for chunk in self.chunks:
            down = self.downtime_s.get(chunk.index)
            suffix = f", service down {down:.0f}s" if down is not None else ""
            lines.append(f"  {chunk}{suffix}")
        lines += ["  " + line for outcome in self.outcomes
                  if not outcome.ok for line in str(outcome).splitlines()]
        lines += [f"  note: {n}" for n in self.notes]
        return "\n".join(lines)


# ------------------------------------------------------------------ chunking
def chunks(
    pairs: Iterable[Pair],
    *,
    chunk_gib: float = DEFAULT_CHUNK_GIB,
    mib_per_second: float | None = None,
    budget_s: float | None = None,
) -> list[Chunk]:
    """Split the work into outages, by bytes rather than by file count.

    A budget, where one is given together with an observed copy rate, lowers
    the chunk size until the estimated downtime fits. Estimating before the
    service goes down is the only time the estimate is useful.
    """
    limit = chunk_gib * 2**30
    if budget_s is not None and mib_per_second is not None:
        by_budget = budget_s * mib_per_second * 2**20
        if by_budget < limit:
            log.info(
                "chunk size reduced from %.0f to %.1f GiB to fit a %.0fs budget",
                chunk_gib, by_budget / 2**30, budget_s,
            )
            limit = by_budget

    out: list[Chunk] = []
    current: list[Pair] = []
    total = 0
    for pair in pairs:
        size = pair.size
        if current and total + size > limit:
            out.append(Chunk(len(out) + 1, tuple(current), total))
            current, total = [], 0
        current.append(pair)
        total += size
    if current:
        out.append(Chunk(len(out) + 1, tuple(current), total))
    return out


def expected_streams(count: int) -> Callable[[Mapping[str, Any]], bool]:
    """A settle condition: the record shows this many streams.

    The refresh is queued, so the first read afterwards is very often the
    record from before. This is the shape of condition that makes waiting mean
    something -- something about the new file that was not true of the old one.
    """

    def settled(item: Mapping[str, Any]) -> bool:
        return len(item.get("MediaStreams") or []) == count

    return settled


def _settle_condition(
    pair: Pair, before: Mapping[str, Any]
) -> Callable[[Mapping[str, Any]], bool]:
    """The plan's stream count where it has one; otherwise "not the old record".

    A rebuild whose stream table and chapters the server reads exactly as it
    read the original's cannot be told apart from a refresh that has not run,
    and is reported as unsettled rather than guessed to be fine.
    """
    if pair.streams is not None:
        return expected_streams(pair.streams)
    streams, chapters = before.get("MediaStreams"), before.get("Chapters")

    def changed(item: Mapping[str, Any]) -> bool:
        return item.get("MediaStreams") != streams or item.get("Chapters") != chapters

    return changed


# ------------------------------------------------------------- preconditions
def default_replacement_check(*, decode: bool = True) -> file_swap.PayloadCheck:
    """The full payload check, decode included unless asked otherwise."""
    return functools.partial(integrity.check, decode=decode)


def preflight(
    client: Client,
    pair: Pair,
    replacement_check: file_swap.PayloadCheck | None = None,
) -> tuple[list[str], dict[str, Any] | None]:
    """Every read-only check for one pair: what would stop it, and the record.

    With ``replacement_check``, the replacement's payload is read as well:
    the original is parked because the replacement takes its place, so the
    replacement has to be proved to play, not merely to exist.

    Run by the dry run as well as before each outage, and never writes. The
    catalogued path is compared the way the deletion tool compares it, before
    the service is stopped: a plan line that pairs an identifier with a file
    that is not that item's would otherwise swap the wrong file and report
    success, because the item it names was never touched.
    """
    problems: list[str] = []
    record: dict[str, Any] | None = None
    try:
        record = fetch(client, pair.item_id)
    except (ItemNotFound, LookupError) as exc:
        problems.append(f"refused: the item is not in the catalogue ({exc})")
    if record is not None:
        catalogued = str(record.get("Path") or "")
        if not _same_path(catalogued, pair.keeper):
            problems.append(
                f"refused: the plan's path is not the item's -- the catalogue says "
                f"{catalogued!r}, the plan says {str(pair.keeper)!r}"
            )
    if not pair.keeper.is_file():
        problems.append(f"refused: nothing is on disk at {pair.keeper}")
    if not pair.replacement.is_file():
        problems.append(f"refused: the replacement is not there: {pair.replacement}")
    elif replacement_check is not None:
        problems += [
            f"refused: {p}"
            for p in file_swap.payload_problems(pair.replacement, replacement_check)
        ]
    return problems, record


# -------------------------------------------------------------- play state
def replay_play_state(
    client: Client,
    item_id: str,
    before: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    """Put back any user's position that is not what it was. Returns who.

    Usually there is nothing to do: the row was never deleted, so nothing had
    to reattach. It is done anyway because the cost is one call per user and
    the alternative is finding out from somebody who lost their place.
    """
    restored: list[str] = []
    for user, state in before.items():
        found = client.get(f"/Users/{user}/Items/{item_id}")
        current = (found or {}).get("UserData") or {}
        if all(current.get(key) == value for key, value in state.items()):
            continue
        client.post(f"/UserItems/{item_id}/UserData", dict(state), userId=user)
        restored.append(user)
    return restored


# ------------------------------------------------------------------- the run
def swap(
    client: Client,
    pairs: Sequence[Pair],
    *,
    controller: ServiceController,
    parked: Path | str,
    chunk_gib: float = DEFAULT_CHUNK_GIB,
    users: Sequence[str] = (),
    mib_per_second: float | None = None,
    budget_s: float | None = None,
    wait_timeout_s: float = 3600.0,
    settle_timeout_s: float = 900.0,
    poll_s: float = 15.0,
    sleep: Callable[[float], None] | None = None,
    stop_on_problem: bool = True,
    check: file_swap.Check | None = None,
    replacement_check: file_swap.PayloadCheck | None = None,
) -> SwapReport:
    """The whole procedure: wait, stop, park, copy, start, refresh, compare.

    ``check`` is the file-side test applied to each file that arrives, and it
    defaults to the file-side package's own: read the file at the destination
    and compare its track table with the replacement's. It is an argument
    because the right check for an audio-only rebuild is not the right check
    for a full remux.

    ``users`` names whose play state is snapshotted and replayed; naming
    nobody means every user the server lists. A user list that cannot be read,
    or is empty, refuses the whole run before anything stops.

    Dry run unless the client says otherwise, and the dry run is worth
    running: it reports the chunking, the estimated downtime and where every
    original would be parked, and it runs every read-only precondition -- the
    item is there, the plan's path is its path, both files exist, the users
    can be listed -- so a plan that would be refused is refused while nothing
    is at stake.

    ``replacement_check`` proves each replacement plays (default: the full
    :func:`mkvkit.integrity.check`, decode included). Every replacement is
    read once, before the first chunk and so before any outage; a pair whose
    replacement fails it, or cannot be checked, is refused.
    """
    measure = replacement_check or default_replacement_check()
    measured: dict[str, IntegrityReport] = {}

    def once(path: Path) -> IntegrityReport:
        key = os.path.normcase(os.path.abspath(path))
        if key not in measured:
            measured[key] = measure(path)
        return measured[key]

    planned = chunks(
        pairs, chunk_gib=chunk_gib, mib_per_second=mib_per_second, budget_s=budget_s
    )
    notes: list[str] = []
    if mib_per_second is not None:
        for chunk in planned:
            notes.append(
                f"{chunk}: about {chunk.estimated_seconds(mib_per_second) / 60:.0f} "
                "minute(s) of copying"
            )
    names, why_not = resolve_users(client, users)
    no_users = () if names else (f"refused: {why_not}",)
    # the slow reads happen here, while nothing is stopped
    for chunk in planned:
        for pair in chunk.pairs:
            if pair.replacement.is_file():
                once(pair.replacement)

    if client.dry_run:
        playing = len(client.playing())
        if playing:
            notes.append(
                f"{playing} session(s) playing now; an applied run waits for them"
            )
        return SwapReport(
            chunks=tuple(planned),
            outcomes=tuple(
                Outcome(
                    pair, parked=file_swap.parked_path(pair.keeper, Path(parked)),
                    problems=(*preflight(client, pair, once)[0], *no_users),
                )
                for chunk in planned for pair in chunk.pairs
            ),
            applied=False,
            notes=(*notes, "dry run: every precondition was read; nothing was "
                   "moved and nothing was stopped"),
        )

    if not names:
        return SwapReport(
            chunks=tuple(planned),
            outcomes=tuple(
                Outcome(pair, problems=no_users)
                for chunk in planned for pair in chunk.pairs
            ),
            applied=True,
            notes=(*notes, "nothing was stopped: whose play state to keep is unknown"),
        )

    outcomes: list[Outcome] = []
    downtime: dict[int, float] = {}
    for chunk in planned:
        log.info("%s", chunk)
        client.wait_idle(timeout_s=wait_timeout_s, poll_s=poll_s, sleep=sleep)

        before: dict[str, dict[str, Any]] = {}
        play_state: dict[str, Mapping[str, Mapping[str, Any]]] = {}
        ready: list[Pair] = []
        for pair in chunk.pairs:
            problems, record = preflight(client, pair, once)
            if problems or record is None:
                outcomes.append(Outcome(pair, problems=tuple(problems)))
                continue
            before[pair.item_id] = record
            play_state[pair.item_id] = user_data(client, pair.item_id, names)
            ready.append(pair)
        if len(ready) < len(chunk.pairs) and stop_on_problem:
            notes.append(
                f"stopped before chunk {chunk.index}: a pair in it was refused, and "
                "the service was not stopped for any of it"
            )
            break
        if not ready:
            continue

        started = time.monotonic()
        results: dict[str, file_swap.SwapResult] = {}
        with stopped(controller):
            for pair in ready:
                results[pair.item_id] = file_swap.swap(
                    pair.as_file_pair(), parked_dir=parked, dry_run=False,
                    check=check, payload_check=once,
                )
        downtime[chunk.index] = time.monotonic() - started
        log.info("chunk %d: service down %.0fs", chunk.index, downtime[chunk.index])

        stop = False
        for pair in ready:
            result = results[pair.item_id]
            if not result.ok or not result.applied:
                outcomes.append(
                    Outcome(pair, swapped=False, parked=result.parked,
                            problems=result.problems)
                )
                stop = stop_on_problem
                continue
            outcomes.append(
                _verify(
                    client, pair, before[pair.item_id], play_state[pair.item_id],
                    result, settle_timeout_s=settle_timeout_s, poll_s=poll_s,
                    sleep=sleep,
                )
            )
            stop = stop or (stop_on_problem and not outcomes[-1].ok)
        if stop:
            notes.append(
                f"stopped after chunk {chunk.index}: the second failure usually has "
                "the same cause as the first, and the cheapest time to look at it is "
                "before the rest has moved"
            )
            break

    return SwapReport(
        chunks=tuple(planned), outcomes=tuple(outcomes), downtime_s=downtime,
        applied=True, notes=tuple(notes),
    )


def _verify(
    client: Client,
    pair: Pair,
    before: Mapping[str, Any],
    play_state: Mapping[str, Mapping[str, Any]],
    result: file_swap.SwapResult,
    *,
    settle_timeout_s: float,
    poll_s: float,
    sleep: Callable[[float], None] | None,
) -> Outcome:
    """Refresh, wait for it, compare the record, and put back what was lost."""
    report = safe_refresh(
        client, pair.item_id,
        expected_changes=EXPECTED,
        until=_settle_condition(pair, before),
        before=before,
        timeout_s=settle_timeout_s,
        poll_s=poll_s,
        sleep=sleep,
    )
    problems: list[str] = []
    after = fetch(client, pair.item_id)
    if after.get("Id") != before.get("Id"):
        problems.append(
            "the identifier changed: the swap did not happen in place, and this is "
            "a different row at the same path"
        )
    if not report.settled:
        problems.append(
            "the catalogue had not caught up before the deadline: the record never "
            "showed the new file's streams, so nothing below proves the swap"
        )
    restored = replay_play_state(client, pair.item_id, play_state)
    return Outcome(
        pair=pair,
        swapped=True,
        parked=result.parked,
        refresh=report,
        comparison=compare(before, after, expected=EXPECTED),
        play_state_restored=tuple(restored),
        problems=tuple(problems),
    )
