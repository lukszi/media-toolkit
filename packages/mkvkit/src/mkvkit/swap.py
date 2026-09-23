"""mkvkit.swap -- put the rebuilt file where the old one was, and keep the old one.

A rebuild is only useful once it is in the place everything else points at.
That move is the most dangerous minute in the whole pipeline, because it is
the only step that touches a file somebody would miss. Four rules make it
survivable, and they are the whole module.

**The original is moved, never deleted.** It goes to a parking directory that
mirrors its own layout, and it stays there until a person decides otherwise.
Disk is cheaper than a file you cannot get back, and "the rebuild verified" is
a claim about the checks that were run, not about the ones nobody thought of.

**The new file takes the old one's exact path.** Not a new name beside it:
everything downstream -- a catalogue entry, a playback position, a link
somebody made -- is keyed on that path, and a rename is a deletion followed by
an unrelated arrival.

**The file that arrived is read again, in place.** Never a size comparison: a
legitimately rebuilt file legitimately has a different size, so size says
nothing here. The check that means something is that the file at the
destination opens, identifies, and has the tracks the replacement had.

**Any failure puts the original back.** The parked file is moved into its old
path again before the error is reported, so a failed swap leaves the tree
exactly as it was and not in a state that needs a person to reason about.

And, as everywhere in this package, nothing happens without ``dry_run=False``.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .probe import probe
from .run import Runner, default_runner

__all__ = [
    "SwapPair",
    "SwapResult",
    "parked_path",
    "probe_check",
    "swap",
    "swap_all",
]

log = logging.getLogger(__name__)

#: What a check does: look at the file that arrived and return what is wrong
#: with it. An empty list means it is fine.
Check = Callable[[Path], list[str]]


@dataclass(frozen=True)
class SwapPair:
    """The file that is live, and the rebuilt file that should take its place."""

    keeper: Path
    replacement: Path

    @property
    def same_name(self) -> bool:
        return self.keeper.name == self.replacement.name


@dataclass(frozen=True)
class SwapResult:
    path: Path
    applied: bool = False
    parked: Path | None = None
    problems: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems

    def __str__(self) -> str:
        state = "swapped" if self.applied else "not swapped"
        lines = [f"{self.path.name}: {state}"]
        if self.parked is not None:
            lines.append(f"  the original is parked at {self.parked}")
        lines += [f"  problem: {p}" for p in self.problems]
        lines += [f"  note: {n}" for n in self.notes]
        return "\n".join(lines)


def parked_path(keeper: Path, parked_dir: Path) -> Path:
    """Where the original goes: the parking directory, keeping its own layout.

    The layout is kept because a directory of hundreds of files all called
    the same thing as each other is not a backup, it is a puzzle. The root of
    the original path is dropped, since a root is a property of one machine.
    """
    relative = Path(*keeper.parts[1:]) if keeper.is_absolute() else keeper
    return parked_dir / relative


def probe_check(
    replacement: Path,
    *,
    runner: Runner | None = None,
    config: Config | None = None,
) -> Check:
    """The default check: the arrived file opens and has the tracks it should."""
    run = runner if runner is not None else default_runner(config)
    expected = probe(replacement, runner=run, elements=False)

    def check(arrived: Path) -> list[str]:
        found = probe(arrived, runner=run, elements=False)
        problems: list[str] = []
        if len(found.tracks) != len(expected.tracks):
            problems.append(
                f"the file at the destination has {len(found.tracks)} track(s) and "
                f"the replacement had {len(expected.tracks)}"
            )
        if found.container.type != expected.container.type:
            problems.append(
                f"the file at the destination is {found.container.type!r} and the "
                f"replacement was {expected.container.type!r}"
            )
        return problems

    return check


def swap(
    pair: SwapPair,
    *,
    parked_dir: Path | str,
    dry_run: bool = True,
    check: Check | None = None,
    runner: Runner | None = None,
    config: Config | None = None,
) -> SwapResult:
    """Park the original, copy the replacement into its path, read it back."""
    keeper, replacement = pair.keeper, pair.replacement
    parking = Path(parked_dir)
    problems: list[str] = []

    if not pair.same_name:
        problems.append(
            f"the replacement is called {replacement.name} and the file it would "
            f"replace is called {keeper.name}; a swap never renames"
        )
    if not replacement.is_file():
        problems.append(f"the replacement is not there: {replacement}")
    if not keeper.is_file():
        problems.append(f"the file to replace is not there: {keeper}")
    destination = parked_path(keeper, parking)
    if destination.exists():
        problems.append(
            f"something is already parked at {destination}; it is not overwritten, "
            "because that is the copy somebody may still need"
        )
    if problems:
        return SwapResult(keeper, problems=tuple(problems))

    if dry_run:
        return SwapResult(
            keeper,
            applied=False,
            parked=destination,
            notes=(
                "dry run: nothing was moved",
                f"the original would be parked at {destination}",
            ),
        )

    verify = check if check is not None else probe_check(
        replacement, runner=runner, config=config
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(keeper), str(destination))
    log.info("parked %s at %s", keeper.name, destination)
    try:
        shutil.copyfile(replacement, keeper)
    except OSError as exc:
        _restore(destination, keeper)
        return SwapResult(
            keeper, problems=(f"the copy failed and the original is back: {exc}",)
        )

    found = verify(keeper)
    if found:
        _restore(destination, keeper)
        return SwapResult(
            keeper,
            problems=(*found, "the original has been put back"),
        )
    return SwapResult(keeper, applied=True, parked=destination)


def _restore(parked: Path, keeper: Path) -> None:
    """Undo the move. Anything left at the destination is removed first."""
    if keeper.exists():
        keeper.unlink()
    shutil.move(str(parked), str(keeper))
    log.warning("restored %s from %s", keeper.name, parked)


def swap_all(
    pairs: Iterable[SwapPair],
    *,
    parked_dir: Path | str,
    dry_run: bool = True,
    stop_on_problem: bool = True,
    check: Check | None = None,
    runner: Runner | None = None,
    config: Config | None = None,
) -> tuple[SwapResult, ...]:
    """Swap a batch, in order, stopping at the first problem by default.

    Stopping is the default because the second failure usually has the same
    cause as the first, and the cheapest moment to look at it is before the
    rest of the batch has moved.
    """
    results: list[SwapResult] = []
    for pair in pairs:
        result = swap(
            pair, parked_dir=parked_dir, dry_run=dry_run, check=check,
            runner=runner, config=config,
        )
        results.append(result)
        if not result.ok and stop_on_problem:
            log.error("stopping the batch: %s", result.problems[0])
            break
    return tuple(results)


def summarise(results: Sequence[SwapResult]) -> str:
    done = sum(1 for r in results if r.applied)
    failed = [r for r in results if not r.ok]
    return (
        f"{len(results)} pair(s): {done} swapped, {len(failed)} with problems, "
        f"{len(results) - done - len(failed)} not attempted"
    )
