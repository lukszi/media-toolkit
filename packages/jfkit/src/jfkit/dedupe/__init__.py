"""jfkit.dedupe -- resolve copies of one film or episode into a keeper and parked copies.

A duplicate is resolved in one audited plan, never by hand:

* :mod:`.groups` finds the groups -- films by provider identifier, episodes
  by series, season and episode, never by a name, with segments of one
  episode kept apart;
* :mod:`.facts` reads what each copy's file holds;
* :mod:`.rules` decides, by the owner's policy, which copy may stand in for
  which and which one is kept -- or that both stay;
* :mod:`.resolver` probes every copy and reads the keeper's payload, one
  reader per disk;
* :mod:`.plan` turns the SAFE verdicts into steps: carry the watched state,
  park each other copy with its sidecars, park emptied release folders,
  notify the server.

``jfkit dedupe`` (:mod:`.cli`) is the command. The method, and why each rule
is there, is ``docs/methods/duplicate-resolution.md``.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

from .facts import Copy, Track, copy_from_probe, probe_copy, resolution_class
from .groups import SEGMENTS, Group, GroupScan, Member, find_groups, members_of, segment_of
from .plan import Planned, actions, build_plan, parked_path
from .resolver import Verdict, default_checker, resolve
from .rules import (
    BLOCKED,
    KEEP_BOTH,
    NOT_DUPLICATE,
    SAFE,
    Choice,
    Coverage,
    Rules,
    choose,
    coverage,
    rank,
)

__all__ = [
    "BLOCKED",
    "KEEP_BOTH",
    "NOT_DUPLICATE",
    "SAFE",
    "SEGMENTS",
    "Choice",
    "Copy",
    "Coverage",
    "Group",
    "GroupScan",
    "Member",
    "Planned",
    "Rules",
    "Track",
    "Verdict",
    "actions",
    "build_plan",
    "choose",
    "copy_from_probe",
    "coverage",
    "default_checker",
    "find_groups",
    "members_of",
    "parked_path",
    "probe_copy",
    "rank",
    "resolution_class",
    "resolve",
    "segment_of",
]
