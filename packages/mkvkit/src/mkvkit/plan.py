"""mkvkit.plan -- one file, everything that would change to it, printed first.

A plan is the thing a person reads before a program writes. It holds, for one
file, every edit that is about to be made to it: the chapter document, the
tags, the track headers, the container title. It is produced by whatever
decided those edits, printed by the dry run, executed by
:mod:`mkvkit.propedit`, and checked afterwards by :mod:`mkvkit.verify` against
the fact that a header edit cannot have moved a packet.

Three properties are the reason this is an object rather than a function call.

**One file is edited once.** Every pending change to a file is folded into a
single invocation -- chapters and tags in the same call, not one after the
other. Two calls are two modification-time bumps, and a modification time is
what makes a media server regenerate an item's preview tiles and re-extract
its chapter images, on demand, on the same disk the pass is reading. Halving
the number of edits halves that.

**A refusal is part of the plan, not an exception.** A pass over a collection
has to say what it did not do as carefully as what it did, so a file that
cannot be edited gets a plan carrying the reason and no edits. Nothing is
dropped silently.

**A plan can be compared with the last one.** :func:`explain_churn` says
which files entered the plan since the previous run, which left, and which
changed -- which is what makes a policy-driven pass trustworthy across
re-runs. Without it there is no way to tell a policy change from a bug: both
look like a different list.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from .chapters.names import NameMatch, provenance
from .chapters.xml import ChapterSet
from .config import Config
from .propedit import PropeditResult, TrackEdit, safe_propedit
from .run import Runner
from .tags import TagSet, merge
from .verify import Comparison, Evidence, HeaderOnly, collect, compare

__all__ = [
    "ChurnReport",
    "FilePlan",
    "apply",
    "chapter_names_plan",
    "explain_churn",
    "summarise",
    "verify_after",
]

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FilePlan:
    """Everything that would change about one file."""

    path: Path
    chapters: ChapterSet | None = None
    tags: TagSet | None = None
    track_edits: tuple[TrackEdit, ...] = ()
    title: str | None = None
    notes: tuple[str, ...] = ()
    refusals: tuple[str, ...] = ()
    source: str = ""

    @property
    def blocked(self) -> bool:
        """Whether anything refused this file. A blocked plan never writes."""
        return bool(self.refusals)

    @property
    def empty(self) -> bool:
        """Whether there is nothing to do, which is a perfectly good answer."""
        return not (
            self.chapters is not None
            or self.tags is not None
            or self.track_edits
            or self.title is not None
        )

    @property
    def changes(self) -> tuple[str, ...]:
        """One line per thing that would change, in the order it is written."""
        out: list[str] = []
        if self.chapters is not None:
            out.append(
                f"chapters: {len(self.chapters)} mark(s), "
                f"{self.chapters.named_count} named"
            )
        if self.tags is not None:
            out.append(f"tags: {len(self.tags)} tag(s)")
        for edit in self.track_edits:
            out.append(f"track {edit.uid}: " + ", ".join(
                f"{key}={value}" for key, value in edit.properties()
            ))
        if self.title is not None:
            out.append(f"title: {self.title!r}")
        return tuple(out)

    def describe(self) -> list[str]:
        """The dry run, as lines. This is what ``--dry-run`` prints."""
        head = f"{self.path}"
        if self.blocked:
            return [head, *(f"  refused: {reason}" for reason in self.refusals)]
        if self.empty:
            return [head, "  nothing would change"]
        return [
            head,
            *(f"  would change {line}" for line in self.changes),
            *(f"  note: {note}" for note in self.notes),
            *( [f"  source: {self.source}"] if self.source else [] ),
        ]

    def __str__(self) -> str:
        return "\n".join(self.describe())

    def with_note(self, note: str) -> FilePlan:
        return replace(self, notes=(*self.notes, note))

    def refuse(self, reason: str) -> FilePlan:
        """The same file, with the edits dropped and the reason kept."""
        return replace(
            self, chapters=None, tags=None, track_edits=(), title=None,
            refusals=(*self.refusals, reason),
        )


# ------------------------------------------------------------------- a builder
def chapter_names_plan(
    path: Path | str,
    match: NameMatch,
    *,
    source: str,
    existing_tags: TagSet | None = None,
    when: dt.date | None = None,
    tool: str | None = None,
) -> FilePlan:
    """The plan that writes a name match into a file, with its provenance.

    Both halves go in together: the document and the tag that says where the
    names came from. The tag is **merged** into whatever the file already
    carries rather than replacing it, because one of the tags a file may
    already carry is a track language that is the only thing making the file
    read correctly, and a pass that writes chapter names must not be the
    thing that loses it.
    """
    target = Path(path)
    if not match.accepted or match.chapters is None:
        return FilePlan(target, source=source).refuse(match.reason)
    tag = provenance(source, when=when, tool=tool)
    tags = merge(existing_tags if existing_tags is not None else TagSet(), [tag])
    plan = FilePlan(
        target, chapters=match.chapters, tags=tags, source=source,
        notes=(match.reason,),
    )
    for check in match.checks:
        if not check.ok:
            plan = plan.with_note(str(check))
    return plan


# ------------------------------------------------------------------ the writing
def apply(
    plan: FilePlan,
    *,
    dry_run: bool = True,
    runner: Runner | None = None,
    config: Config | None = None,
) -> PropeditResult:
    """Carry out one plan, in one invocation.

    A blocked plan is not attempted and comes back as a refusal rather than
    as an exception, so a pass can record it and carry on.
    """
    if plan.blocked:
        return PropeditResult(plan.path, problems=tuple(plan.refusals))
    if plan.empty:
        return PropeditResult(plan.path, notes=("nothing to change",))
    return safe_propedit(
        plan.path,
        plan.track_edits,
        chapters=plan.chapters,
        tags=plan.tags,
        title=plan.title,
        dry_run=dry_run,
        runner=runner,
        config=config,
    )


def verify_after(
    plan: FilePlan,
    before: Evidence,
    *,
    runner: Runner | None = None,
    config: Config | None = None,
) -> Comparison:
    """Prove a carried-out plan changed the header and nothing else.

    The payload hashes are not recomputed. A header edit provably cannot move
    a packet, so measuring them again measures the editor rather than the
    file -- and on a large file it is the most expensive thing in the pass.
    Knowing what cannot have changed, and not measuring it, is the point.
    """
    after = collect(plan.path, hashes=False, runner=runner, config=config)
    return compare(before, after, HeaderOnly())


# -------------------------------------------------------------------- the churn
@dataclass(frozen=True)
class ChurnReport:
    """Why each file entered or left the plan between two runs."""

    entered: tuple[tuple[Path, str], ...] = ()
    left: tuple[tuple[Path, str], ...] = ()
    changed: tuple[tuple[Path, str], ...] = ()
    unchanged: int = 0

    @property
    def quiet(self) -> bool:
        return not (self.entered or self.left or self.changed)

    def __str__(self) -> str:
        lines = [
            f"{len(self.entered)} entered, {len(self.left)} left, "
            f"{len(self.changed)} changed, {self.unchanged} unchanged"
        ]
        for label, rows in (
            ("entered", self.entered), ("left", self.left), ("changed", self.changed)
        ):
            for path, why in rows:
                lines.append(f"  {label}: {path.name} -- {why}")
        return "\n".join(lines)


def explain_churn(
    previous: Iterable[FilePlan], current: Iterable[FilePlan]
) -> ChurnReport:
    """Compare two runs of the same pass, file by file.

    A file that left the plan is the interesting half and the one a diff of
    two lists hides: it left because it was edited, or because the policy
    changed, or because it is no longer there, and those are very different
    pieces of news.
    """
    before = {plan.path: plan for plan in previous}
    after = {plan.path: plan for plan in current}
    entered: list[tuple[Path, str]] = []
    left: list[tuple[Path, str]] = []
    changed: list[tuple[Path, str]] = []
    unchanged = 0

    for path, plan in after.items():
        if path not in before:
            entered.append((path, _why(plan)))
            continue
        old = before[path]
        if old.changes != plan.changes or old.refusals != plan.refusals:
            changed.append((path, f"{_why(old)} -> {_why(plan)}"))
        else:
            unchanged += 1
    for path, plan in before.items():
        if path not in after:
            left.append((path, _why(plan)))
    return ChurnReport(
        tuple(sorted(entered)), tuple(sorted(left)), tuple(sorted(changed)), unchanged
    )


def _why(plan: FilePlan) -> str:
    if plan.blocked:
        return f"refused: {plan.refusals[0]}"
    if plan.empty:
        return "nothing to change"
    return "; ".join(plan.changes)


def summarise(plans: Sequence[FilePlan]) -> str:
    """One line per file plus a count, for the end of a dry run."""
    writable = [p for p in plans if not p.blocked and not p.empty]
    refused = [p for p in plans if p.blocked]
    lines = [line for plan in plans for line in plan.describe()]
    lines.append(
        f"{len(writable)} file(s) would change, {len(refused)} refused, "
        f"{len(plans) - len(writable) - len(refused)} already as they should be"
    )
    return "\n".join(lines)
