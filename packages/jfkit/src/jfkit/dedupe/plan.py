"""The resolution as one plan: carry the watched state, park, tidy, notify.

Every SAFE group becomes steps of one :class:`mkvkit.steps.Plan`, in this
order across all groups:

1. **carry** (``userdata.write``) -- every user's watched state on every
   copy (played, play count, resume point, last-played date, favourite) is
   merged (:func:`jfkit.userdata.merge`) and written onto the keeper, where
   the keeper does not already hold it. Nothing is parked before this.
2. **park** (``dedupe.park``) -- each loser, through the safe-delete
   machinery (:func:`jfkit.safedelete.safe_delete`, category
   ``resolved-duplicate``). It re-checks everything at that moment: the row
   and the path, that every user's state is on the keeper now, that the
   keeper's file is the size it was when planned, and the keeper's payload
   (``policy.dedupe.apply_keeper_check``). It moves the file; it never asks
   the server to delete anything.
3. **sidecars** (``dedupe.park-sidecar``) -- the loser's own files
   (:func:`mkvkit.sidecars.sidecars_of`): its description, artwork,
   preview tiles and external subtitles, moved beside it.
4. **folders** (``dedupe.park-folder``) -- a release folder the parks leave
   with no video in it, parked whole, after checking again that it holds no
   media and no loose track. A folder that is a library folder, or not
   inside one, is never a candidate.
5. **notify** (``dedupe.notify``) -- one notification naming exactly the
   parked paths, never a library folder, so the server's own scan drops
   the rows whose files are gone.

Parking keeps the layout: a file at ``<volume>/a/b/c`` goes to
``<parked>/a/b/c``. A relative ``parked`` is taken on each file's own
volume, so a park is a rename and never a copy across disks.

Every step checks its own preconditions when it runs and knows whether its
effect is in place, so a run that stops half way is resumed by running the
same plan with the same audit.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mkvkit.integrity import IntegrityReport
from mkvkit.sidecars import VIDEO_SUFFIXES, sidecars_of
from mkvkit.steps import Action, FunctionAction, Plan, Step
from mkvkit.walk import walk

from .. import userdata
from ..client import Client
from ..query import playstate
from ..query import users as list_users
from ..refresh import library_roots, notify_changed
from ..safedelete import Candidate, parked_relative, safe_delete
from ..safedelete.evidence import loose_tracks, media_free
from ..validation import warnings_for
from .resolver import Verdict, default_checker
from .rules import SAFE

__all__ = [
    "CATEGORY",
    "NOTIFY",
    "PARK",
    "PARK_FOLDER",
    "PARK_SIDECAR",
    "Planned",
    "actions",
    "build_plan",
    "parked_path",
]

log = logging.getLogger(__name__)

#: The safe-delete category a loser is parked under.
CATEGORY = "resolved-duplicate"

PARK = "dedupe.park"
PARK_SIDECAR = "dedupe.park-sidecar"
PARK_FOLDER = "dedupe.park-folder"
NOTIFY = "dedupe.notify"

#: Why a group of one row's media sources is not planned.
VERSIONS = (
    "these copies are versions of one catalogue row, not rows of their own: the "
    "row's watched state and the file it plays are the server's to reconcile, so "
    "parking one version is left for a person"
)

#: The verb a plan is saved under.
VERB = "jfkit dedupe"


def parked_path(path: Path | str, parked: Path | str) -> Path:
    """Where ``path`` goes: its layout below ``parked``, drive letter kept
    (:func:`jfkit.safedelete.parked_relative`), on its own volume if relative."""
    here = Path(path)
    return _parked_root(here, parked) / parked_relative(here)


def _parked_root(path: Path, parked: Path | str) -> Path:
    root = Path(parked)
    return root if root.is_absolute() else Path(path.anchor) / root


def _key(path: Path | str) -> str:
    return str(path).replace("\\", "/").casefold().rstrip("/")


def _inside(path: Path | str, folder: Path | str) -> bool:
    here, there = _key(path), _key(folder)
    return here == there or here.startswith(there + "/")


# -------------------------------------------------------------------- carry
def _carry_steps(
    client: Client, verdicts: Sequence[Verdict], workers: int | None
) -> tuple[list[Step], list[str], dict[str, str]]:
    """The writes that put every copy's watched state on its keeper.

    Returns the steps, notes, and the groups whose state could not be read
    in full, by key: those are left out of the plan, because a copy whose
    history nobody could see must not be parked.
    """
    ids = [m.item_id for v in verdicts for m in v.group.members]
    if not ids:
        return [], [], {}
    names = list_users(client)
    report = playstate(client, ids, user_ids=list(names), workers=workers)
    if report.errors:
        problems = [f"user {u}: {len(b)} item(s) unread: {e}" for u, b, e in report.errors]
        raise RuntimeError(
            "watched state could not be read for every copy, so it cannot be "
            "carried: " + "; ".join(problems[:5])
        )
    missing = set(report.missing)
    rows = {(str(r["ItemId"]), str(r["UserId"])): r for r in report.rows}
    steps: list[Step] = []
    notes: list[str] = []
    unread: dict[str, str] = {}
    for verdict in verdicts:
        keeper = verdict.keeper
        assert keeper is not None
        lost = [m.file_name for m in verdict.group.members if m.item_id in missing]
        if lost:
            unread[verdict.group.key] = (
                "the watched state of " + ", ".join(lost) + " could not be read, so it "
                "cannot be carried; nothing in this group is parked"
            )
            continue
        kept_id = keeper.member.item_id
        for user, name in names.items():
            states = [userdata.UserState.from_server(rows.get((m.item_id, user)))
                      for m in verdict.group.members]
            now = userdata.UserState.from_server(rows.get((kept_id, user)))
            target = userdata.merge(states)
            if target.same_as(now) or target.blank:
                continue
            steps.append(Step(
                id=f"carry:{len(steps) + 1:04d}", action=userdata.WRITE,
                params={"item": kept_id, "user": user, "body": target.body(),
                        "was": now.to_json()},
                summary=f"carry onto {keeper.member.file_name} for {name or user}: "
                        f"{now} -> {target}",
            ))
    if steps:
        notes.append(f"{len(steps)} watched-state row(s) carried onto keepers first")
    return steps, notes, unread


# --------------------------------------------------------------------- park
def _folder_candidates(
    verdicts: Sequence[Verdict], parking: set[str], roots: Sequence[str] | None
) -> tuple[list[Path], list[str]]:
    notes: list[str] = []
    if roots is None:
        return [], ["the library folders could not be read, so no release folder "
                    "is parked"]
    keepers = [v.keeper.member.path for v in verdicts if v.keeper is not None]
    seen: set[str] = set()
    out: list[Path] = []
    for verdict in verdicts:
        for loser in verdict.losers:
            folder = Path(loser.member.path).parent
            if _key(folder) in seen:
                continue
            seen.add(_key(folder))
            if any(_inside(root, folder) for root in roots):
                continue  # a library folder, or one that holds one
            if not any(_inside(folder, root) for root in roots):
                continue  # outside every library: not ours to tidy
            if any(_inside(kept, folder) for kept in keepers):
                continue
            if not folder.is_dir():
                continue
            tree = walk(folder, suffixes=VIDEO_SUFFIXES, sizes=False)
            left = [e.path for e in tree if _key(e.path) not in parking]
            if left:
                continue
            if tree.skipped:
                notes.append(f"{folder} is left: something in it could not be looked at")
                continue
            out.append(folder)
            notes += warnings_for(roots, removes=[folder])
    return out, notes


@dataclass(frozen=True)
class Planned:
    """The plan, and the SAFE groups that could not be planned, with why."""

    plan: Plan
    unplanned: Mapping[str, str] = field(default_factory=dict)


def build_plan(
    client: Client,
    verdicts: Sequence[Verdict],
    *,
    parked: Path | str,
    workers: int | None = None,
) -> Planned:
    """The steps that resolve every SAFE verdict; the others contribute nothing."""
    safe = [v for v in verdicts if v.verdict == SAFE and v.keeper is not None]
    versions = {v.group.key: VERSIONS for v in safe
                if any(m.parent_id for m in v.group.members)}
    safe = [v for v in safe if v.group.key not in versions]
    steps, notes, unplanned = _carry_steps(client, safe, workers)
    unplanned = {**versions, **unplanned}
    safe = [v for v in safe if v.group.key not in unplanned]
    parks: list[Step] = []
    sidecars: list[Step] = []
    notify: list[str] = []
    parking: set[str] = set()
    freed = 0
    for verdict in safe:
        keeper = verdict.keeper
        assert keeper is not None
        for loser in verdict.losers:
            path = Path(loser.member.path)
            dest = parked_path(path, parked)
            parking.add(_key(path))
            freed += loser.size
            parks.append(Step(
                id=f"park:{len(parks) + 1:04d}", action=PARK,
                params={
                    "item_id": loser.member.item_id, "path": str(path),
                    "keeper_id": keeper.member.item_id,
                    "keeper_path": keeper.member.path, "keeper_size": keeper.size,
                    "parked": str(_parked_root(path, parked)), "dest": str(dest),
                    "group": verdict.group.key,
                },
                summary=f"park {path} (kept: {keeper.member.path})",
            ))
            notify.append(str(path))
            for sidecar in sidecars_of(path).sidecars:
                parking.add(_key(sidecar.path))
                sidecars.append(Step(
                    id=f"sidecar:{len(sidecars) + 1:04d}", action=PARK_SIDECAR,
                    params={"video": str(path), "src": str(sidecar.path),
                            "dst": str(parked_path(sidecar.path, parked))},
                    summary=f"park {sidecar.path} with its video",
                ))
    roots = library_roots(client) if parks else []
    folders, folder_notes = _folder_candidates(safe, parking, roots)
    notes += folder_notes
    folder_steps = [
        Step(id=f"folder:{index:04d}", action=PARK_FOLDER,
             params={"folder": str(folder), "dst": str(parked_path(folder, parked))},
             summary=f"park the release folder {folder}, left with no video")
        for index, folder in enumerate(folders, start=1)
    ]
    notify += [str(folder) for folder in folders]
    tail = [Step(id="notify", action=NOTIFY, params={"paths": notify},
                 summary=f"tell the server {len(notify)} path(s) are gone")] if notify else []
    notes.insert(0, f"{len(safe)} group(s) resolved, {len(parks)} copy(ies) parked, "
                    f"{freed / 2**30:.1f} GiB")
    plan = Plan(verb=VERB, steps=(*steps, *parks, *sidecars, *folder_steps, *tail),
                notes=tuple(notes))
    return Planned(plan=plan, unplanned=unplanned)


# ------------------------------------------------------------------ actions
def _move(src: Path, dst: Path) -> None:
    if dst.exists():
        raise FileExistsError(f"something is already parked at {dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))


def _merge_folder(src: Path, dst: Path) -> None:
    """Move a folder's contents into a parked folder that already exists."""
    if not dst.exists():
        _move(src, dst)
        return
    for entry in sorted(src.iterdir()):
        target = dst / entry.name
        if target.is_dir() and entry.is_dir():
            _merge_folder(entry, target)
        else:
            _move(entry, target)
    src.rmdir()


def actions(
    client: Client,
    *,
    keeper_check: Callable[[Path], IntegrityReport] | None = None,
    users: Sequence[str] = (),
) -> dict[str, Action]:
    """Every action a dedupe plan names, bound to one writing client."""
    check = keeper_check or default_checker("quick")

    def park(step: Step) -> Mapping[str, Any]:
        p = step.params
        kept = Path(p["keeper_path"])
        size = kept.stat().st_size
        if size != int(p["keeper_size"]):
            raise RuntimeError(
                f"the kept file {kept} is {size} bytes, not the {p['keeper_size']} it "
                "was when this plan was made; plan again"
            )
        report = safe_delete(
            client,
            [Candidate(item_id=p["item_id"], path=Path(p["path"]), category=CATEGORY,
                       reason=f"a copy of {p['group']}", keeper=kept,
                       keeper_id=p["keeper_id"])],
            allowed_categories=[CATEGORY], parked=p["parked"], users=users,
            remove_rows=False, keeper_check=check,
        )
        outcome = report.outcomes[0]
        if outcome.parked is None:
            raise RuntimeError("refused: " + "; ".join(str(c) for c in outcome.refusals))
        return {"parked": str(outcome.parked),
                "checks": [str(c) for c in outcome.checks]}

    def parked(step: Step) -> bool:
        return not Path(step.params["path"]).exists() and Path(step.params["dest"]).exists()

    def sidecar(step: Step) -> Mapping[str, Any]:
        video = Path(step.params["video"])
        if video.exists():
            raise RuntimeError(f"its video {video} was not parked, so it stays")
        src, dst = Path(step.params["src"]), Path(step.params["dst"])
        _move(src, dst)
        return {"src": str(src), "dst": str(dst)}

    def moved(step: Step) -> bool:
        return not Path(step.params["src"]).exists() and Path(step.params["dst"]).exists()

    def folder(step: Step) -> Mapping[str, Any]:
        here, dst = Path(step.params["folder"]), Path(step.params["dst"])
        if not here.exists():
            return {"already": "the folder is not there"}
        if not media_free(here):
            return {"left": "the folder holds media now, or something not looked at"}
        tracks = loose_tracks(here)
        if tracks:
            return {"left": f"the folder holds {len(tracks)} loose track(s)",
                    "tracks": [str(t) for t in tracks[:10]]}
        _merge_folder(here, dst)
        return {"folder": str(here), "dst": str(dst)}

    def notify(step: Step) -> Mapping[str, Any]:
        roots = library_roots(client)
        if roots is None:
            raise RuntimeError("the library folders could not be read; a notification "
                               "that might name one is not sent")
        sent = notify_changed(client, step.params["paths"], roots=roots, kind="Deleted")
        return {"sent": len(sent)}

    return {
        **userdata.actions(client),
        PARK: FunctionAction(park, parked),
        PARK_SIDECAR: FunctionAction(sidecar, moved),
        PARK_FOLDER: FunctionAction(folder, lambda s: not Path(s.params["folder"]).exists()),
        NOTIFY: FunctionAction(notify),
    }
