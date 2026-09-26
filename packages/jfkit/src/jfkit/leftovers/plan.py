"""jfkit.leftovers.plan -- a sweep's findings as a plan that parks, checked twice.

Every finding becomes a :class:`jfkit.safedelete.Candidate` with no item
identifier and is put through :func:`jfkit.safedelete.preconditions`, the
same checks ``jfkit delete`` makes. A candidate whose category somebody
released and whose checks all pass becomes one ``leftovers.park`` step; the
rest are reported with their refusals.

The plan is an :mod:`mkvkit.steps` plan: printed by the dry run, saved with
``--plan-out``, applied with an audit and resumed from it. **Each step checks
its candidate again when it runs**, against the world as it is then, and
fails rather than moves when anything changed. A step for a corrupt video
carries the integrity evidence it was proposed on, and the file's size and
modification time at the moment it was measured: a file that changed since
is refused, because the evidence is about a file that is no longer there.

Parking moves, never deletes. On one volume a move is a rename; across
volumes it is :func:`mkvkit.transfer.verified_copy`, which hashes both sides
before the original goes. A folder is parked file by file, then its emptied
folders are removed; a step interrupted half way resumes by finishing the
files still in place, and a file already at the parking place is only
accepted there when it is byte for byte the one left behind.

The parking directory keeps the layout, drive letter included, so two
library folders with the same path on two drives never meet there.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mkvkit.devices import same_device
from mkvkit.integrity import IntegrityReport
from mkvkit.sidecars import VIDEO_SUFFIXES, sidecars_of
from mkvkit.steps import Action, FunctionAction, Plan, Step
from mkvkit.transfer import verified_copy
from mkvkit.walk import walk

from ..client import Client
from ..refresh import NotifyRefused, library_roots, notify_changed
from ..safedelete import Candidate, Check, parked_location, preconditions
from ..safedelete.catalogue import Catalogue, path_key
from ..safedelete.evidence import identical
from ..safedelete.junk import DEFAULT_RULES, Rules
from ..safedelete.leftovers import CORRUPT, SAMPLE
from .scan import Finding

__all__ = [
    "NOTIFY",
    "PARK",
    "Assessed",
    "actions",
    "assess",
    "build_plan",
    "park_target",
]

log = logging.getLogger(__name__)

PARK = "leftovers.park"
NOTIFY = "leftovers.notify"


def park_target(parked: Path | str, path: Path | str) -> Path:
    """Where a path lands in the parking directory, drive letter kept.

    ``C:/Media/Movies/x`` lands at ``<parked>/C/Media/Movies/x``; a path without a drive
    keeps its layout below the parking directory.
    """
    return parked_location(parked, path)


@dataclass(frozen=True)
class Assessed:
    """One finding, the candidate made of it, and what its preconditions said."""

    finding: Finding
    checks: tuple[Check, ...] = ()
    released: bool = False
    integrity: IntegrityReport | None = None
    measured: tuple[int, int] | None = None

    @property
    def allowed(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)

    @property
    def refusals(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if not c.ok)

    @property
    def state(self) -> str:
        """``park`` (will move), ``not released`` or ``refused``."""
        if self.allowed:
            return "park"
        if not self.released and all(
            c.ok for c in self.checks if c.name != RELEASED_CHECK
        ):
            return "not released"
        return "refused"

    def as_dict(self) -> dict[str, Any]:
        out = self.finding.as_dict()
        out["state"] = self.state
        out["checks"] = [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in self.checks]
        if self.integrity is not None:
            out["integrity"] = self.integrity.as_dict()
        return out


RELEASED_CHECK = "the category is one somebody released"


def assess(
    client: Client,
    findings: Iterable[Finding],
    *,
    released: Iterable[str],
    catalogue: Catalogue,
    rules: Rules = DEFAULT_RULES,
    roots: Sequence[str] = (),
    integrity: Mapping[str, IntegrityReport] | None = None,
) -> list[Assessed]:
    """Every finding through the safe-delete preconditions, released or not.

    Each is checked whether or not its category was released, so a dry run
    says what releasing a category would move. ``integrity`` holds the
    payload reports of corrupt candidates, by path key.
    """
    allowed = tuple(sorted(set(released)))
    reports = integrity or {}
    out: list[Assessed] = []
    for finding in findings:
        report = reports.get(path_key(finding.path))
        measured = _measured(finding.path) if finding.category == CORRUPT else None
        candidate = Candidate(
            item_id="", path=finding.path, category=finding.category,
            reason=finding.reason, integrity=report, measured=measured,
        )
        checks, _contents = preconditions(
            client, candidate, allowed_categories=allowed, catalogue=catalogue,
            rules=rules, roots=list(roots),
            integrity_check=_no_measuring,
        )
        out.append(Assessed(
            finding, tuple(checks), finding.category in allowed, report, measured,
        ))
    return out


def _no_measuring(path: Path) -> IntegrityReport:
    """A corrupt candidate is measured before it is assessed, never during."""
    return IntegrityReport(
        path=Path(path), evidence=False,
        problems=("no integrity report was made for this file",),
    )


def _measured(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


def build_plan(assessed: Sequence[Assessed], *, parked: Path | str) -> Plan:
    """One park step per candidate that may move; notes for the rest."""
    steps: list[Step] = []
    notify: list[str] = []
    for index, one in enumerate((a for a in assessed if a.allowed), start=1):
        finding = one.finding
        params: dict[str, Any] = {
            "category": finding.category,
            "src": str(finding.path),
            "dst": str(park_target(parked, finding.path)),
            "parked": str(parked),
            "is_dir": finding.is_dir,
            "size": finding.size,
            "reason": finding.reason,
        }
        if finding.evidence is not None:
            params["evidence"] = finding.evidence.lines()
        if one.integrity is not None:
            params["integrity"] = one.integrity.as_dict()
        if one.measured is not None:
            params["measured"] = list(one.measured)
        if finding.category == CORRUPT:
            notify.append(str(finding.path))
        steps.append(Step(
            id=f"park:{index:05d}", action=PARK, params=params,
            summary=f"park {finding.category} {finding.path}",
        ))
    if notify:
        steps.append(Step(
            id="notify:00001", action=NOTIFY,
            params={"paths": notify, "kind": "Deleted"},
            summary=f"tell the server {len(notify)} parked video(s) are gone",
        ))
    counts: dict[str, int] = {}
    for one in assessed:
        counts[one.state] = counts.get(one.state, 0) + 1
    notes = [
        f"{counts.get('park', 0)} to park, {counts.get('not released', 0)} not "
        f"released, {counts.get('refused', 0)} refused",
        f"parked at {parked}; nothing is deleted",
    ]
    return Plan(verb="leftovers", steps=tuple(steps), notes=tuple(notes))


# ------------------------------------------------------------------ actions
@dataclass
class _Apply:
    """The world an applied plan checks again: the server and its catalogue."""

    client: Client
    rules: Rules = DEFAULT_RULES
    _catalogue: Catalogue | None = field(default=None, repr=False)
    _roots: list[str] | None = field(default=None, repr=False)

    def catalogue(self) -> Catalogue:
        if self._catalogue is None:
            self._catalogue = Catalogue.fetch(self.client)
        return self._catalogue

    def roots(self) -> list[str]:
        if self._roots is None:
            self._roots = library_roots(self.client) or []
        return self._roots

    # --------------------------------------------------------------- park
    def recheck(self, step: Step) -> list[Check]:
        params = step.params
        category = str(params["category"])
        report: IntegrityReport | None = None
        measured: tuple[int, int] | None = None
        if category == CORRUPT:
            recorded = params.get("integrity") or {}
            report = IntegrityReport(
                path=Path(params["src"]),
                evidence=bool(recorded.get("evidence", False)),
                problems=tuple(recorded.get("problems") or ()),
            )
            raw = params.get("measured")
            measured = (int(raw[0]), int(raw[1])) if raw else None
        candidate = Candidate(
            item_id="", path=Path(params["src"]), category=category,
            integrity=report, measured=measured,
        )
        checks, _ = preconditions(
            self.client, candidate, allowed_categories=[category],
            catalogue=self.catalogue(), rules=self.rules, roots=self.roots(),
            integrity_check=_no_measuring,
        )
        return checks

    def park(self, step: Step) -> Mapping[str, Any]:
        src, dst = Path(step.params["src"]), Path(step.params["dst"])
        parked = Path(step.params["parked"])
        moved: list[str] = []
        if src.exists():
            refused = [c for c in self.recheck(step) if not c.ok]
            if refused:
                raise RuntimeError(
                    "a precondition no longer holds, nothing was moved: "
                    + "; ".join(str(c) for c in refused)
                )
            # the set is found by the video's name, so it is read first
            carried = _sidecars(src)
            moved += _park(src, dst)
        elif dst.exists():
            # an earlier run parked the video and stopped before its sidecars
            carried = _sidecars(src)
        else:
            raise FileNotFoundError(f"{src} is not there, and nothing is parked at {dst}")
        for sidecar in carried:
            if sidecar.exists():
                moved += _park(sidecar, park_target(parked, sidecar))
        return {"moved": len(moved), "sidecars": [str(p) for p in carried]}

    def parked(self, step: Step) -> bool:
        src, dst = Path(step.params["src"]), Path(step.params["dst"])
        return not src.exists() and dst.exists() and not any(
            p.exists() for p in _sidecars(src))

    # ------------------------------------------------------------- notify
    def notify(self, step: Step) -> Mapping[str, Any]:
        roots = self.roots()
        if not roots:
            raise RuntimeError(
                "the library folders could not be read, and a notification that "
                "might name one starts a full scan; the rows go at the next scan"
            )
        try:
            sent = notify_changed(
                self.client, [Path(p) for p in step.params["paths"]], roots=roots,
                kind=str(step.params.get("kind") or "Deleted"),
            )
        except NotifyRefused as exc:
            raise RuntimeError(str(exc)) from exc
        return {"sent": sent}


def _sidecars(video: Path) -> list[Path]:
    """What belongs to a video by the server's rules; the video need not be there."""
    if video.suffix.lower() not in VIDEO_SUFFIXES or not video.parent.is_dir():
        return []
    return [s.path for s in sidecars_of(video)]


def _park(src: Path, dst: Path) -> list[str]:
    """Move one file or one folder to its parking place; the files moved."""
    if src.is_dir():
        moved: list[str] = []
        tree = walk(src)
        files = [entry.path for entry in tree]
        if tree.skipped:
            raise RuntimeError(
                f"{src} holds something the walk does not enter: "
                + "; ".join(str(s) for s in tree.skipped[:3])
            )
        for path in files:
            moved += _park_file(path, dst / path.relative_to(src))
        _remove_empty(src)
        return moved
    return _park_file(src, dst)


def _park_file(src: Path, dst: Path) -> list[str]:
    if dst.exists():
        if identical(src, dst).same:
            # an earlier run copied it and stopped before the original went
            os.remove(src)
            return [str(src)]
        raise FileExistsError(f"{dst} exists and is not {src}; nothing is overwritten")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if same_device(src.parent, dst.parent):
        os.rename(src, dst)
        return [str(src)]
    report = verified_copy(src, dst, move=True, dry_run=False)
    if not report.ok:
        raise OSError("; ".join(report.problems))
    return [str(src)]


def _remove_empty(folder: Path) -> None:
    """Remove a folder whose files were all parked, deepest first."""
    for here, _dirs, _files in sorted(os.walk(folder), key=lambda t: -len(t[0])):
        os.rmdir(here)


def actions(client: Client, *, rules: Rules = DEFAULT_RULES) -> dict[str, Action]:
    """The two actions a leftovers plan names, bound to one server."""
    world = _Apply(client, rules)
    return {
        PARK: FunctionAction(world.park, world.parked),
        NOTIFY: FunctionAction(world.notify),
    }


#: Categories a sweep proposes; ``corrupt-unplayable`` comes from a check.
SWEPT: tuple[str, ...] = ("release-junk", "dead-release-folder", SAMPLE)

#: What an injected checker looks like, for tests and a quick run.
IntegrityCheck = Callable[[Path], IntegrityReport]
