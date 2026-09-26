"""Read every copy, apply the rules, and prove the keeper plays.

Three passes over the groups, each one only over what the last one left:

1. **Probe** every member of every candidate group -- a header read, cheap,
   but a read of the disk all the same, so it runs through
   :func:`mkvkit.lanes.map_by_device`: one reader per physical disk, the
   disks side by side. A member that is not on disk, or that the probe
   program cannot read, blocks its group.
2. **Choose** with :func:`jfkit.dedupe.rules.choose`.
3. **Read the keeper's payload** with :func:`mkvkit.integrity.check`, again
   one reader per disk. This is mandatory. The headers of a file whose
   payload was never written are perfect, so a keeper picked on its headers
   alone can be a file of zeros -- and a resolver that trusts it parks the
   only copy that plays. A keeper whose check fails, or cannot run, blocks
   the group: nothing in it is parked, and the report says why.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mkvkit import integrity
from mkvkit.devices import Device, device_of
from mkvkit.integrity import IntegrityReport
from mkvkit.lanes import map_by_device

from .facts import Copy, Prober, probe_copy
from .groups import Group
from .rules import BLOCKED, NOT_DUPLICATE, SAFE, Rules, choose

__all__ = [
    "Checker",
    "Verdict",
    "default_checker",
    "resolve",
]

log = logging.getLogger(__name__)

#: Reads one kept file's payload.
Checker = Callable[[Path], IntegrityReport]

#: Called on a disk's reader before each read; raising refuses that read.
BeforeEach = Callable[[Device, Any], None]


def default_checker(mode: str = "full") -> Checker:
    """The payload check: ``full`` decodes every track, ``quick`` lists every packet.

    Looked up when called, so a test that replaces :func:`mkvkit.integrity.check`
    replaces it here too.
    """
    def run(path: Path) -> IntegrityReport:
        return integrity.check(path, decode=mode == "full")
    return run


@dataclass(frozen=True)
class Verdict:
    """One group's answer: the verdict, the copies, and every reason."""

    group: Group
    verdict: str
    keeper: Copy | None = None
    losers: tuple[Copy, ...] = ()
    copies: tuple[Copy, ...] = ()
    reasons: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    integrity: IntegrityReport | None = None

    @property
    def title(self) -> str:
        return self.group.title

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.group.key,
            "kind": self.group.kind,
            "class": self.group.cls or "copies",
            "title": self.title,
            "verdict": self.verdict,
            "keeper": None if self.keeper is None else self.keeper.member.path,
            "keeper_id": None if self.keeper is None else self.keeper.member.item_id,
            "losers": [c.member.path for c in self.losers],
            "loser_ids": [c.member.item_id for c in self.losers],
            "members": [
                {"item_id": m.item_id, "path": m.path, "segment": m.segment}
                for m in self.group.members
            ],
            "copies": [c.as_dict() for c in self.copies],
            "reasons": list(self.reasons),
            "notes": list(self.notes),
            "integrity": None if self.integrity is None else self.integrity.as_dict(),
        }


def resolve(
    groups: Sequence[Group],
    rules: Rules,
    *,
    prober: Prober | None = None,
    checker: Checker | None = None,
    device_of: Callable[[Path | str], Device] = device_of,
    before_each: BeforeEach | None = None,
) -> list[Verdict]:
    """A verdict for every group, in the order given."""
    probe = prober or (lambda member: probe_copy(member, rules.dedupe))
    check = checker or default_checker(rules.dedupe.keeper_check)

    members = [m for g in groups if g.not_duplicate is None for m in g.members]
    probed = map_by_device(
        probe, members, path_of=lambda m: m.path, device_of=device_of,
        before_each=before_each,
    )
    copies: dict[int, Copy] = {}
    failed: dict[int, str] = {}
    for outcome in probed:
        key = id(outcome.item)
        if outcome.ok and outcome.result is not None:
            copies[key] = outcome.result
        else:
            failed[key] = _why(outcome.error)

    staged: list[tuple[Group, Verdict]] = []
    for group in groups:
        if group.not_duplicate is not None:
            staged.append((group, Verdict(group, NOT_DUPLICATE,
                                          reasons=(group.not_duplicate,))))
            continue
        unread = [(m, failed[id(m)]) for m in group.members if id(m) in failed]
        read = tuple(copies[id(m)] for m in group.members if id(m) in copies)
        if unread:
            staged.append((group, Verdict(group, BLOCKED, copies=read, reasons=tuple(
                f"{m.file_name} could not be read: {why}" for m, why in unread
            ))))
            continue
        choice = choose(read, rules)
        staged.append((group, Verdict(
            group, choice.verdict, keeper=choice.keeper, losers=choice.losers,
            copies=read, reasons=choice.reasons, notes=choice.notes,
        )))

    keepers = [v.keeper for _g, v in staged if v.verdict == SAFE and v.keeper is not None]
    checked = map_by_device(
        lambda copy: check(copy.path), keepers, path_of=lambda c: c.member.path,
        device_of=device_of, before_each=before_each,
    )
    reports: dict[int, IntegrityReport | str] = {}
    for done in checked:
        if done.ok and done.result is not None:
            reports[id(done.item)] = done.result
        else:
            reports[id(done.item)] = _why(done.error)

    out: list[Verdict] = []
    for _group, verdict in staged:
        if verdict.verdict != SAFE or verdict.keeper is None:
            out.append(verdict)
            continue
        found = reports.get(id(verdict.keeper), "the keeper was never checked")
        out.append(_with_integrity(verdict, found))
    return out


def _why(error: BaseException | None) -> str:
    if error is None:
        return "no answer"
    text = str(error).strip() or type(error).__name__
    return text.splitlines()[0]


def _with_integrity(verdict: Verdict, found: IntegrityReport | str) -> Verdict:
    keeper = verdict.keeper
    assert keeper is not None
    if isinstance(found, str):
        return Verdict(
            verdict.group, BLOCKED, keeper=keeper, losers=verdict.losers,
            copies=verdict.copies, notes=verdict.notes,
            reasons=(*verdict.reasons,
                     f"the keeper's payload could not be checked: {found}; nothing "
                     "is parked on a keeper nobody has seen play"),
        )
    if not found.ok:
        what = "could not be checked" if not found.evidence else "failed its payload check"
        return Verdict(
            verdict.group, BLOCKED, keeper=keeper, losers=verdict.losers,
            copies=verdict.copies, notes=verdict.notes, integrity=found,
            reasons=(*verdict.reasons,
                     f"the keeper {keeper.member.file_name} {what}: "
                     + "; ".join(found.problems)
                     + " -- another copy may be the only one that plays; nothing "
                       "is parked"),
        )
    how = "decoded" if found.decoded else "listed, not decoded"
    return Verdict(
        verdict.group, SAFE, keeper=keeper, losers=verdict.losers, copies=verdict.copies,
        reasons=verdict.reasons, integrity=found,
        notes=(*verdict.notes, f"the keeper's payload is there ({how})"),
    )

