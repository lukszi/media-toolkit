"""jfkit.swap -- replace files in place without losing what the catalogue knows.

The file-side package already parks an original and puts a rebuild in its
path. This is the half that has to happen around it, and it is the half that
decides whether the catalogue survives.

**The path does not change, so the identity does not change.** An item's
identity is derived from its path. Put the rebuild beside the original under
a new name and it is a new item: new identifier, no play state, no place in
anybody's list, and the old item is a missing file. Swap in place and every
reference in the catalogue still points at the same row.

**Nobody is watching.** Stopping a server under somebody's playback is the
failure people remember, and it costs one call to avoid.

**The service is stopped per chunk, and the chunks are sized in bytes.** Not
in files: a chunk of a hundred episodes and a chunk of a hundred films are the
same number of files and two very different outages. A chunk carries a
byte total, and where a copy rate is known the downtime it implies is
computed and compared against a budget *before* the service goes down.

**Play state is snapshotted for every user and replayed afterwards.** It
usually survives -- the row is not deleted, so nothing has to reattach -- but
"usually" is not a thing to find out about afterwards, and a position belonging
to somebody who was not consulted is not a thing to lose.

**Verification is a comparison, not a size check.** A rebuilt file
legitimately has a different size; that is generally why it was rebuilt. What
proves the swap is the record afterwards: the identifier is the same, the
stream table is the one the rebuild has, and the name, overview and provider
identifiers are exactly what they were.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mkvkit import swap as file_swap

from .client import Client
from .dto import Comparison, compare, fetch, user_data
from .refresh import RefreshReport, safe_refresh
from .service import ServiceController, stopped

__all__ = [
    "Chunk",
    "Outcome",
    "Pair",
    "SwapReport",
    "chunks",
    "expected_streams",
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
) -> SwapReport:
    """The whole procedure: wait, stop, park, copy, start, refresh, compare.

    ``check`` is the file-side test applied to each file that arrives, and it
    defaults to the file-side package's own: read the file at the destination
    and compare its track table with the replacement's. It is an argument
    because the right check for an audio-only rebuild is not the right check
    for a full remux.

    Dry run unless the client says otherwise, and the dry run is worth
    running: it reports the chunking, the estimated downtime and where every
    original would be parked, which is the part that is worth checking while
    nothing is at stake.
    """
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
    if client.dry_run:
        return SwapReport(
            chunks=tuple(planned),
            outcomes=tuple(
                Outcome(pair, parked=file_swap.parked_path(pair.keeper, Path(parked)))
                for chunk in planned for pair in chunk.pairs
            ),
            applied=False,
            notes=(*notes, "dry run: nothing was moved and nothing was stopped"),
        )

    outcomes: list[Outcome] = []
    downtime: dict[int, float] = {}
    for chunk in planned:
        log.info("%s", chunk)
        client.wait_idle(timeout_s=wait_timeout_s, poll_s=poll_s, sleep=sleep)

        before: dict[str, dict[str, Any]] = {}
        play_state: dict[str, Mapping[str, Mapping[str, Any]]] = {}
        for pair in chunk.pairs:
            before[pair.item_id] = fetch(client, pair.item_id)
            play_state[pair.item_id] = user_data(client, pair.item_id, users)

        started = time.monotonic()
        results: dict[str, file_swap.SwapResult] = {}
        with stopped(controller):
            for pair in chunk.pairs:
                results[pair.item_id] = file_swap.swap(
                    pair.as_file_pair(), parked_dir=parked, dry_run=False,
                    check=check,
                )
        downtime[chunk.index] = time.monotonic() - started
        log.info("chunk %d: service down %.0fs", chunk.index, downtime[chunk.index])

        stop = False
        for pair in chunk.pairs:
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
            "the catalogue had not caught up before the deadline; the comparison "
            "below may be against the record from before the swap"
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
