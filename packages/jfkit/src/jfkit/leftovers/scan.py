"""jfkit.leftovers.scan -- walk the library folders and sort what is left over.

One walk per library folder (:func:`mkvkit.walk.walk`: no link or junction is
ever entered, excludes are part of the walk, and everything left out is
reported). Every file is classified by :mod:`jfkit.safedelete.junk`, and
every folder is judged by what is below it:

* a folder with **no video below it, nothing catalogued at or below it and
  nothing unseen below it** is *media-free*. What it holds decides what it
  is: nothing but junk makes it ``release-junk`` whole (a screenshot folder,
  a padding folder, an empty folder); description files, artwork and preview
  tiles make it a ``dead-release-folder`` -- unless the folder it sits in
  has a video of its own, whose pictures they may be; anything else
  (a subtitle, a soundtrack, a document, a file no rule knows) makes it a
  folder that is *kept* and reported, and the walk looks inside it for
  smaller leftovers;
* a junk file that is not inside a folder already found is ``release-junk``
  on its own; a release sample is a ``sample``; a file no rule knows is
  UNSURE and only reported.

The scan decides nothing about deletion. It proposes; the preconditions of
:mod:`jfkit.safedelete` decide, against the world, and the plan moves.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any

from mkvkit.devices import Device, device_of
from mkvkit.walk import Skipped, SkipReason, walk

from ..safedelete.catalogue import Catalogue, path_key
from ..safedelete.junk import DEFAULT_RULES, Kind, Rules, Verdict, classify, is_protected_folder
from ..safedelete.leftovers import (
    DEAD_FOLDER,
    RELEASE_JUNK,
    SAMPLE,
    Evidence,
    release_evidence,
)

__all__ = [
    "Finding",
    "Kept",
    "Scan",
    "scan",
    "scan_root",
]

log = logging.getLogger(__name__)

#: What a gate answers for a device: None to go ahead, or why not.
DeviceGate = Callable[[Device], str | None]


@dataclass(frozen=True)
class Finding:
    """One proposed leftover: a file or a folder, its category and why."""

    category: str
    path: Path
    is_dir: bool
    reason: str
    size: int = 0
    files: int = 1
    evidence: Evidence | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "category": self.category, "path": str(self.path), "is_dir": self.is_dir,
            "reason": self.reason, "size": self.size, "files": self.files,
            "evidence": None if self.evidence is None else self.evidence.as_dict(),
        }


@dataclass(frozen=True)
class Kept:
    """Something found and not proposed: an UNSURE file or a folder kept for its content."""

    kind: str
    path: Path
    reason: str
    size: int = 0
    evidence: Evidence | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "path": str(self.path), "reason": self.reason,
            "size": self.size,
            "evidence": None if self.evidence is None else self.evidence.as_dict(),
        }


@dataclass
class Scan:
    """Everything one sweep found, and everything it did not look at."""

    roots: list[Path] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    kept: list[Kept] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    #: the key of every file the walk saw, for the missing-content report
    seen: set[str] = field(default_factory=set)
    #: devices a gate held, and why; their roots were not walked
    held: dict[str, str] = field(default_factory=dict)
    #: the folders excluded by path, whether or not the walk met them
    excluded: set[str] = field(default_factory=set)

    def merge(self, other: Scan) -> None:
        self.roots += other.roots
        self.findings += other.findings
        self.kept += other.kept
        self.skipped += other.skipped
        for key, value in other.counts.items():
            self.counts[key] = self.counts.get(key, 0) + value
        self.seen |= other.seen
        self.held.update(other.held)
        self.excluded |= other.excluded

    def of(self, category: str) -> list[Finding]:
        return [f for f in self.findings if f.category == category]

    @property
    def unsure(self) -> list[Kept]:
        return [k for k in self.kept if k.kind == "unsure"]

    @property
    def kept_folders(self) -> list[Kept]:
        return [k for k in self.kept if k.kind != "unsure"]

    def unseen(self, path: Path | str) -> bool:
        """Whether a path lies in something the walk did not enter."""
        key = path_key(path)
        return any(
            key == other or key.startswith(other + "/")
            for other in [*(path_key(s.path) for s in self.skipped), *self.excluded]
        )


# ------------------------------------------------------------------- walk
@dataclass
class _Node:
    path: Path
    files: list[tuple[Path, Verdict, int]] = field(default_factory=list)
    children: list[Path] = field(default_factory=list)
    video: bool = False
    unseen: bool = False


def scan_root(
    root: Path | str,
    catalogue: Catalogue,
    *,
    rules: Rules = DEFAULT_RULES,
    exclude: Sequence[str] = (),
    exclude_paths: Sequence[str | os.PathLike[str]] = (),
) -> Scan:
    """Walk one library folder and sort everything below it."""
    top = Path(root)
    tree = walk(top, exclude=exclude, exclude_paths=exclude_paths, include_dirs=True)
    nodes: dict[Path, _Node] = {top: _Node(top)}
    out = Scan(roots=[top], excluded={path_key(p) for p in exclude_paths})
    for entry in tree:
        parent = entry.path.parent
        if entry.is_dir:
            # a row may stand for a folder (a disc laid out as files)
            out.seen.add(path_key(entry.path))
            nodes[entry.path] = _Node(entry.path)
            nodes.setdefault(parent, _Node(parent)).children.append(entry.path)
            continue
        verdict = classify(PurePath(top.name) / entry.relative, rules=rules,
                           size=entry.size)
        out.counts[verdict.kind.value] = out.counts.get(verdict.kind.value, 0) + 1
        out.seen.add(path_key(entry.path))
        nodes.setdefault(parent, _Node(parent)).files.append(
            (entry.path, verdict, entry.size or 0))
        if verdict.is_video:
            _mark(nodes, parent, top, "video")
    out.skipped = list(tree.skipped)
    for skipped in tree.skipped:
        _mark(nodes, skipped.path.parent, top, "unseen")
        if skipped.reason is not SkipReason.EXCLUDED:
            log.info("%s", skipped)

    covered: set[Path] = set()
    for child in sorted(nodes[top].children):
        _visit(child, nodes, catalogue, rules, out, covered, inside_protected=False,
               root=True)
    for node in nodes.values():
        if node.path in covered:
            continue
        for path, verdict, size in node.files:
            if verdict.kind is Kind.JUNK:
                out.findings.append(Finding(RELEASE_JUNK, path, False, verdict.reason, size))
            elif verdict.kind is Kind.SAMPLE:
                out.findings.append(Finding(SAMPLE, path, False, verdict.reason, size))
            elif verdict.kind is Kind.UNSURE:
                out.kept.append(Kept("unsure", path, verdict.reason, size))
    out.findings.sort(key=lambda f: (f.category, str(f.path)))
    out.kept.sort(key=lambda k: (k.kind, str(k.path)))
    return out


def _mark(nodes: dict[Path, _Node], start: Path, top: Path, what: str) -> None:
    here = start
    while True:
        node = nodes.setdefault(here, _Node(here))
        if getattr(node, what):
            return
        setattr(node, what, True)
        if here == top or here.parent == here:
            return
        here = here.parent


def _subtree(path: Path, nodes: dict[Path, _Node]) -> list[tuple[Path, Verdict, int]]:
    out: list[tuple[Path, Verdict, int]] = []
    stack = [path]
    while stack:
        node = nodes[stack.pop()]
        out += node.files
        stack += node.children
    return out


def _mark_covered(path: Path, nodes: dict[Path, _Node], covered: set[Path]) -> None:
    stack = [path]
    while stack:
        here = stack.pop()
        covered.add(here)
        stack += nodes[here].children


_LEFTOVER_REASONS = frozenset({
    "a description file", "artwork the server reads", "preview tiles the server built",
})


def _visit(
    path: Path,
    nodes: dict[Path, _Node],
    catalogue: Catalogue,
    rules: Rules,
    out: Scan,
    covered: set[Path],
    *,
    inside_protected: bool,
    root: bool = False,
) -> None:
    node = nodes[path]
    protected = inside_protected or is_protected_folder(path.name)
    free = not (node.video or node.unseen or catalogue.at_or_below(path))
    if protected or not free:
        for child in sorted(node.children):
            _visit(child, nodes, catalogue, rules, out, covered, inside_protected=protected)
        return

    contents = _subtree(path, nodes)
    size = sum(s for _p, _v, s in contents)
    kinds = {v.kind for _p, v, _s in contents}
    if kinds <= {Kind.JUNK, Kind.IGNORED}:
        reason = (
            "an empty folder" if not contents
            else "nothing but release junk: " + ", ".join(sorted(
                {v.reason for _p, v, _s in contents if v.kind is Kind.JUNK}
            ) or ["operating-system files"])
        )
        out.findings.append(Finding(RELEASE_JUNK, path, True, reason, size, len(contents)))
        _mark_covered(path, nodes, covered)
        return

    files = [p for p, _v, _s in contents]
    leftover_only = all(
        v.kind in (Kind.JUNK, Kind.IGNORED)
        or (v.kind is Kind.PROTECTED and v.reason in _LEFTOVER_REASONS)
        for _p, v, _s in contents
    )
    has_release_files = any(
        v.kind is Kind.PROTECTED and v.reason in _LEFTOVER_REASONS for _p, v, _s in contents
    )
    # In a library folder itself every video is an item of its own, and no
    # subfolder's pictures are its; one level down they may be.
    beside = nodes.get(path.parent)
    live_beside = beside is not None and not root and any(
        v.is_video for _p, v, _s in beside.files)
    # Evidence is about a release; a subfolder of a live one (its subtitles,
    # its soundtrack, its cover scans) is not a release of its own.
    evidence = (
        release_evidence(path, catalogue, files=files)
        if has_release_files and not live_beside else None
    )
    if leftover_only and not live_beside:
        what = sorted({v.reason for _p, v, _s in contents if v.kind is not Kind.IGNORED})
        out.findings.append(Finding(
            DEAD_FOLDER, path, True, "no video left; holds " + ", ".join(what),
            size, len(contents), evidence,
        ))
        _mark_covered(path, nodes, covered)
        return
    if leftover_only:
        reason = "no video of its own; it sits beside a video its pictures may belong to"
    else:
        held = sorted({
            v.reason for _p, v, _s in contents
            if v.kind in (Kind.PROTECTED, Kind.UNSURE)
            and not (v.kind is Kind.PROTECTED and v.reason in _LEFTOVER_REASONS)
        })
        reason = "no video, and kept for what it holds: " + ", ".join(held)
    out.kept.append(Kept("kept-folder", path, reason, size, evidence))
    for child in sorted(node.children):
        _visit(child, nodes, catalogue, rules, out, covered, inside_protected=False)


# ------------------------------------------------------------------ roots
def scan(
    roots: Iterable[Path | str],
    catalogue: Catalogue,
    *,
    rules: Rules = DEFAULT_RULES,
    exclude: Sequence[str] = (),
    exclude_paths: Sequence[str | os.PathLike[str]] = (),
    gate: DeviceGate | None = None,
    device_of: Callable[[Path | str], Device] = device_of,
) -> Scan:
    """Walk every library folder, one device after the other.

    The folders are grouped by device and each device is walked whole
    before the next starts. ``gate`` is asked once per device before its
    walk; a reason back means the device is held and its folders are not
    walked, which the result says.
    """
    order: dict[Device, list[Path]] = {}
    for root in roots:
        order.setdefault(device_of(root), []).append(Path(root))
    out = Scan()
    for device, folders in order.items():
        held = gate(device) if gate is not None else None
        if held:
            out.held[str(device)] = held
            log.warning("%s held: %s", device, held)
            continue
        for folder in folders:
            if not folder.is_dir():
                out.held[str(folder)] = "not a folder, or not reachable"
                continue
            log.info("walking %s", folder)
            out.merge(scan_root(
                folder, catalogue, rules=rules, exclude=exclude,
                exclude_paths=exclude_paths,
            ))
    return out
