"""jfkit.safedelete -- delete only what a named category released, and prove each one.

A drawer of one-off scripts once deleted one specific set of files each. Each
was written, read, argued over and run once, and each hardcoded what it was
deleting. What is worth keeping is not any of those lists -- it is the shape
they all had, which is this:

**Nothing is deletable by default.** A candidate declares a category, and a
category has to be in the allowlist the caller passes in. "Released" is a
decision a person makes about a *kind* of thing -- byte-identical duplicates,
folders with no media in them, the donor file a rebuild was made from -- and
the allowlist is where that decision is written down. A candidate whose
category is not there is refused with its category named, not skipped
quietly.

**Every precondition is checked against the world, not against the manifest.**
The item is still in the catalogue; its path is the path the manifest claims;
the file is on disk at that path; nobody has a position in it; and whatever
the category requires -- a verified twin, an empty folder -- is true right
now. A manifest is a plan, and a plan that was right yesterday is a reason to
check, not a reason to proceed.

**Nothing is deleted. Things are moved.** The file goes to a parking
directory that keeps its layout, and it stays there until a person decides
otherwise. The catalogue row goes only after the move has succeeded, in that
order, because a row removed before a move that then fails leaves a file
nothing knows about. And the row is never removed by asking the server to
delete the item: on this server that call deletes the item's *containing
folder* from disk -- sidecars, artwork, extras and any other film that shares
it. The server is told instead that the path is gone, and its own scan drops
the row whose file is missing, which touches nothing on disk.

**Every step of every item is logged, including the ones that did nothing.**
The audit file is append-only and is the answer to "what happened to X",
which is a question that gets asked months later by somebody who was not
there.

**A leftover has no row, so it is checked against the whole catalogue.** A
tracker note, a folder of screenshots, a release folder whose video went
long ago, a video whose payload is gone: none of these is an item the
server can be asked about. The three leftover categories
(:mod:`.leftovers`) take no item identifier; instead the catalogue is read
once and a candidate passes only when nothing catalogued lives at its path
or below it. ``media-free-folder`` takes the same route when it is given no
identifier, which is what a release subfolder needs: its path is never any
item's path.

**A video goes with its sidecars.** Parking a video parks every file that
belongs to it by the server's own rules (:mod:`mkvkit.sidecars`) -- its
description file, its pictures, its preview tiles, its subtitles -- so no
orphan is left for a second, hand-made pass. A folder left with no video
in it is said so, because it is now a leftover of its own.

**The dry run is the default, and it is the useful one.** It runs every
precondition, reports every refusal with its reason and totals the bytes, and
changes nothing. A run that passes its dry run and then fails a precondition
has learned something real; that is the point.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import csv
import functools
import json
import logging
import os
import shutil
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mkvkit import integrity
from mkvkit.integrity import IntegrityReport
from mkvkit.sidecars import VIDEO_SUFFIXES, SidecarKind, SidecarSet, sidecars_of
from mkvkit.transfer import parked_relative
from mkvkit.walk import walk

from ..client import Client
from ..dto import every_user, fetch, user_data
from ..errors import ItemNotFound
from ..refresh import NotifyRefused, library_roots, notify_changed
from ..validation import warnings_for
from .catalogue import Catalogue
from .checks import Check
from .evidence import (
    FolderContents,
    Identity,
    folder_contents,
    identical,
    loose_tracks,
    media_free,
)
from .junk import DEFAULT_RULES, Rules
from .leftovers import (
    CORRUPT,
    DEAD_FOLDER,
    LEFTOVER_CATEGORIES,
    RELEASE_JUNK,
    SAMPLE,
    corrupt_checks,
    dead_folder_checks,
    release_junk_checks,
    sample_checks,
)

__all__ = [
    "CARRIED_STATE_CATEGORIES",
    "CATEGORIES",
    "KEPT_COPY_CATEGORIES",
    "LEFTOVER_CATEGORIES",
    "Candidate",
    "Check",
    "DeletionReport",
    "KeeperCheck",
    "Outcome",
    "default_keeper_check",
    "load_manifest",
    "parked_location",
    "parked_relative",
    "preconditions",
    "resolve_users",
    "safe_delete",
]

log = logging.getLogger(__name__)

#: The categories this module knows how to check. A category is a *reason* a
#: thing may be deleted, and each one names the extra evidence it needs. A
#: caller may pass categories of its own; these are the ones with a check.
CATEGORIES: Mapping[str, str] = {
    "byte-identical-twin": "another file with the same bytes is kept",
    "media-free-folder": "the folder holds no media file at all",
    "rebuild-donor": "the file a kept rebuild was made from, and the rebuild is there",
    "superseded-copy": "a kept item covers this one, named in the manifest",
    "resolved-duplicate": (
        "a copy of the same film the owner's rules keep instead, and every "
        "user's watched state is on it"
    ),
    RELEASE_JUNK: "a tracker note, shortcut, program, padding or screenshot the "
                  "rules name, with nothing catalogued at or below it",
    DEAD_FOLDER: "a release folder with no video left, only description files, "
                 "artwork, preview tiles and junk, and nothing catalogued in it",
    CORRUPT: "a video whose payload check failed, with evidence, and no other "
             "catalogued copy to keep",
    SAMPLE: "a release sample the rules name, which no catalogue row points at",
}


#: The categories that remove one copy because another is kept. Each one
#: reads the kept copy's payload before anything moves.
KEPT_COPY_CATEGORIES = frozenset({"rebuild-donor", "superseded-copy", "resolved-duplicate"})

#: The categories whose play state may be non-blank, because it was carried
#: onto the kept item first. Instead of "nobody has a position in it" they
#: check that the kept item holds everything this one records, per user.
CARRIED_STATE_CATEGORIES = frozenset({"resolved-duplicate"})

#: Reads one kept file and says whether its payload is there and plays.
KeeperCheck = Callable[[Path], IntegrityReport]


def default_keeper_check(*, decode: bool = True) -> KeeperCheck:
    """The full payload check, decode included unless asked otherwise."""
    return functools.partial(integrity.check, decode=decode)


@dataclass(frozen=True)
class Candidate:
    """One thing somebody proposes to delete, and why they think they may."""

    item_id: str
    path: Path
    category: str
    reason: str = ""
    #: the file that is being kept instead, where the category needs one
    keeper: Path | None = None
    #: the identifier of the kept item, where the category names one
    keeper_id: str | None = None
    #: corrupt-unplayable: the payload check already made, if one was
    integrity: IntegrityReport | None = None
    #: corrupt-unplayable: (size, modification time in ns) when it was measured
    measured: tuple[int, int] | None = None

    @property
    def size(self) -> int:
        """What removing this would free. A folder counts everything below it.

        The walk enters no link or junction: what is behind one is not freed.
        """
        try:
            if self.path.is_dir():
                return sum(entry.size or 0 for entry in walk(self.path))
            return self.path.stat().st_size
        except OSError:
            return 0

    @property
    def needs_item(self) -> bool:
        """Whether this candidate is checked through a catalogue row of its own."""
        if self.category in LEFTOVER_CATEGORIES or self.category == "media-free-folder":
            return bool(self.item_id)
        return True


@dataclass(frozen=True)
class Outcome:
    """What was decided about one candidate, and what was done about it."""

    candidate: Candidate
    checks: tuple[Check, ...] = ()
    parked: Path | None = None
    row_removed: bool = False
    folder_backup: Path | None = None
    bytes_freed: int = 0
    notes: tuple[str, ...] = ()

    @property
    def allowed(self) -> bool:
        return all(check.ok for check in self.checks) and bool(self.checks)

    @property
    def refusals(self) -> tuple[Check, ...]:
        return tuple(check for check in self.checks if not check.ok)

    def __str__(self) -> str:
        head = "would be parked" if self.allowed and not self.parked else (
            "parked" if self.parked else "refused"
        )
        lines = [f"{self.candidate.path.name} ({self.candidate.category}): {head}"]
        lines += [f"  {check}" for check in self.checks if not check.ok]
        if self.parked is not None:
            lines.append(f"  parked at {self.parked}")
        if self.folder_backup is not None:
            lines.append(f"  the rest of the folder was copied to {self.folder_backup}")
        lines += [f"  note: {note}" for note in self.notes]
        return "\n".join(lines)


@dataclass(frozen=True)
class DeletionReport:
    """Every candidate, every refusal, and what a run would free."""

    outcomes: tuple[Outcome, ...] = ()
    applied: bool = False
    audit: Path | None = None
    allowed_categories: tuple[str, ...] = ()

    @property
    def allowed(self) -> tuple[Outcome, ...]:
        return tuple(outcome for outcome in self.outcomes if outcome.allowed)

    @property
    def refused(self) -> tuple[Outcome, ...]:
        return tuple(outcome for outcome in self.outcomes if not outcome.allowed)

    @property
    def bytes_freed(self) -> int:
        # A candidate that has been moved no longer has a size where it was, so
        # the measured figure wins wherever there is one.
        return sum(
            outcome.bytes_freed or outcome.candidate.size for outcome in self.allowed
        )

    def __str__(self) -> str:
        head = "applied" if self.applied else "dry run, nothing moved"
        lines = [
            f"{len(self.outcomes)} candidate(s): {len(self.allowed)} would be "
            f"parked, {len(self.refused)} refused; "
            f"{self.bytes_freed / 2**30:.1f} GiB. {head}.",
            f"categories released: {', '.join(self.allowed_categories) or 'none'}",
        ]
        lines += ["  " + line for outcome in self.refused
                  for line in str(outcome).splitlines()]
        lines += [f"  {outcome.candidate.path.name}: note: {note}"
                  for outcome in self.allowed for note in outcome.notes]
        if self.audit is not None:
            lines.append(f"every step is in {self.audit}")
        return "\n".join(lines)


# ------------------------------------------------------------------ manifest
def load_manifest(path: Path | str) -> list[Candidate]:
    """Read a manifest. Tab-separated or a list of objects; nothing else.

    The columns are the fields of :class:`Candidate`. A manifest with a
    category column that nobody released is still read -- the refusal happens
    where it can be reported, not where it can be silently dropped. The
    leftover categories may leave ``item_id`` empty.
    """
    here = Path(path)
    text = here.read_text(encoding="utf-8")
    if here.suffix.lower() == ".json":
        rows: Sequence[Mapping[str, Any]] = json.loads(text)
    else:
        rows = list(csv.DictReader(text.splitlines(), delimiter="\t"))
    out: list[Candidate] = []
    for index, row in enumerate(rows):
        missing = sorted({"item_id", "path", "category"} - set(row))
        if missing:
            raise ValueError(f"{here}: row {index} has no {', '.join(missing)}")
        out.append(
            Candidate(
                item_id=str(row["item_id"]),
                path=Path(str(row["path"])),
                category=str(row["category"]),
                reason=str(row.get("reason") or ""),
                keeper=Path(str(row["keeper"])) if row.get("keeper") else None,
                keeper_id=str(row["keeper_id"]) if row.get("keeper_id") else None,
            )
        )
    return out


# ------------------------------------------------------------ preconditions
def preconditions(
    client: Client,
    candidate: Candidate,
    *,
    allowed_categories: Iterable[str],
    users: Sequence[str] = (),
    keeper_check: KeeperCheck | None = None,
    catalogue: Catalogue | None = None,
    rules: Rules = DEFAULT_RULES,
    integrity_check: KeeperCheck | None = None,
    roots: Sequence[str] | None = None,
) -> tuple[list[Check], FolderContents | None]:
    """Every check for one candidate, against the world as it is now.

    A leftover candidate with no item identifier is checked against
    ``catalogue`` -- read from the server when it is not given -- instead of
    a row of its own; ``rules`` say what is junk. ``integrity_check``
    measures a corrupt-unplayable candidate that carries no report yet
    (default: the full :func:`mkvkit.integrity.check`, decode included).
    ``roots`` are the library folders, read from the server when not given.

    ``keeper_check`` reads the payload of the copy that is kept, for every
    category that removes one copy because another stays; by default the
    full :func:`mkvkit.integrity.check`, decode included.

    ``users`` narrows the play-state check to the users named. Naming nobody
    means *everybody*: every user the server lists is checked, and a server
    that cannot list them, or lists none, fails the check rather than
    passing it with nobody consulted.
    """
    checks: list[Check] = []
    released = set(allowed_categories)

    checks.append(Check(
        "the category is one somebody released",
        candidate.category in released,
        f"{candidate.category!r}; released: {', '.join(sorted(released)) or 'none'}",
    ))

    if not candidate.needs_item:
        known = catalogue if catalogue is not None else Catalogue.fetch(client)
        if roots is None and candidate.category == DEAD_FOLDER:
            roots = library_roots(client) or []
        checks += _leftover_checks(candidate, known, rules, integrity_check, roots or ())
        return checks, None

    record: dict[str, Any] | None = None
    try:
        record = fetch(client, candidate.item_id)
        checks.append(Check("the item is in the catalogue", True))
    except (ItemNotFound, LookupError) as exc:
        checks.append(Check("the item is in the catalogue", False, str(exc)))

    if record is not None:
        catalogued = str(record.get("Path") or "")
        checks.append(Check(
            "the catalogue's path is the manifest's path",
            _same_path(catalogued, candidate.path),
            f"catalogue says {catalogued!r}",
        ))

    checks.append(Check(
        "it is on disk where the manifest says",
        candidate.path.exists(),
        str(candidate.path),
    ))

    if candidate.category in CARRIED_STATE_CATEGORIES:
        checks.append(_state_carried_check(client, candidate, users))
    else:
        play = _play_state_check(client, candidate.item_id, users)
        if candidate.category == CORRUPT and not play.ok and play.detail:
            # A file that does not play has nobody's position worth keeping in
            # it; the row's state is said, and it does not refuse.
            play = Check(play.name, True, f"reported, not a refusal: {play.detail}")
        checks.append(play)

    contents: FolderContents | None = None
    kept_file: Path | None = None
    if candidate.category == "byte-identical-twin":
        checks.append(_twin_check(candidate))
        kept_file = candidate.keeper
    elif candidate.category == "media-free-folder":
        checks.append(Check(
            "the folder holds no media",
            media_free(candidate.path),
            str(candidate.path),
        ))
        tracks = loose_tracks(candidate.path)
        checks.append(Check(
            "the folder holds no loose audio or subtitle track",
            not tracks,
            ", ".join(p.name for p in tracks[:5]) + (" ..." if len(tracks) > 5 else ""),
        ))
    elif candidate.category in KEPT_COPY_CATEGORIES:
        kept_check, kept_file = _keeper_check(client, candidate)
        checks.append(kept_check)
    elif candidate.category == CORRUPT:
        known = catalogue if catalogue is not None else Catalogue.fetch(client)
        checks += _leftover_checks(candidate, known, rules, integrity_check, ())[1:]

    if candidate.category in {"byte-identical-twin", *KEPT_COPY_CATEGORIES}:
        # Removing a copy because another is kept is only safe if the kept one
        # plays. Its header proves nothing: a file that was never filled keeps
        # a perfect one. So the payload of the kept file is read, and a check
        # that cannot run is a refusal, not a pass.
        if all(check.ok for check in checks):
            checks.append(_kept_copy_plays(kept_file, keeper_check))
        else:
            # Reading and decoding a kept file is the slow part of a run; for
            # a candidate another precondition already refuses it would only
            # add a second reason. Recorded as a refusal all the same.
            checks.append(Check(
                KEPT_COPY_PLAYS, False,
                "not measured: an earlier precondition already refuses this one",
            ))

    if candidate.path.is_file():
        # Reported rather than checked: another file in the folder does not stop
        # this one being removed, but it does stop the FOLDER being removed, and
        # that distinction is worth putting in front of a person rather than
        # deciding for them.
        contents = folder_contents(candidate.path)
    return checks, contents


def _leftover_checks(
    candidate: Candidate,
    catalogue: Catalogue,
    rules: Rules,
    integrity_check: KeeperCheck | None,
    roots: Sequence[str],
) -> list[Check]:
    """The checks of a leftover category, which read the catalogue, not a row."""
    if candidate.category == RELEASE_JUNK:
        return release_junk_checks(candidate.path, catalogue, rules=rules)
    if candidate.category == DEAD_FOLDER:
        return dead_folder_checks(candidate.path, catalogue, rules=rules, roots=roots)
    if candidate.category == SAMPLE:
        return sample_checks(candidate.path, catalogue, rules=rules)
    if candidate.category == CORRUPT:
        report = candidate.integrity
        if report is None and candidate.path.is_file():
            run = integrity_check or default_keeper_check()
            try:
                report = run(candidate.path)
            except Exception as exc:  # any failure to measure is no evidence
                report = IntegrityReport(
                    path=candidate.path, evidence=False,
                    problems=(f"the check could not run: {exc}",),
                )
        return corrupt_checks(
            candidate.path, catalogue, report, measured=candidate.measured,
        )
    # media-free-folder with no identifier: the subtree, not a row
    rows = catalogue.at_or_below(candidate.path)
    tracks = loose_tracks(candidate.path)
    return [
        Check("it is on disk", candidate.path.exists(), str(candidate.path)),
        Check(
            "nothing catalogued is at or below it", not rows,
            "; ".join(str(r.get("Path")) for r in rows[:3]),
        ),
        Check("the folder holds no media", media_free(candidate.path),
              str(candidate.path)),
        Check(
            "the folder holds no loose audio or subtitle track", not tracks,
            ", ".join(p.name for p in tracks[:5]) + (" ..." if len(tracks) > 5 else ""),
        ),
    ]


def _top_level(path: Path, roots: Sequence[str] | None) -> tuple[str, ...]:
    """A note when a folder candidate sits directly under a library folder.

    The server answers a vanished folder by refreshing its parent item, and
    the parent of a top-level folder is the whole library: every file in it
    is looked at again. That is not a reason to refuse, but it is a reason to
    choose when -- on a quiet disk, not in the middle of other work.
    """
    if not roots or not path.is_dir():
        return ()
    return tuple(warnings_for(roots, removes=[path]))


def _same_path(catalogued: str, manifest: Path) -> bool:
    if not catalogued:
        return False
    return Path(catalogued).as_posix().casefold() == manifest.as_posix().casefold()


def _same_file(one: Path, other: Path) -> bool:
    """Whether two paths name one file: by the filesystem where both exist.

    Where both are there the filesystem is asked, which sees through links,
    case and a relative spelling; where either is not, the spellings are
    compared, normalised, which is the most that can be known.
    """
    if one.exists() and other.exists():
        try:
            return os.path.samefile(one, other)
        except OSError:
            pass
    return _normalised(one) == _normalised(other)


def _normalised(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def resolve_users(client: Client, users: Sequence[str]) -> tuple[list[str], str]:
    """The users a play-state check reads: the ones named, or all of them.

    Returns the list and, where it could not be had, why. An empty list is a
    check that cannot pass: "0 user(s) checked" is not a pass, it is nobody
    having been asked.
    """
    if users:
        return list(users), ""
    try:
        found = every_user(client)
    except Exception as exc:  # any failure here is a refusal, with its reason
        return [], f"the user list could not be read: {exc}"
    if not found:
        return [], "the server listed no users, so nobody's play state was read"
    return found, ""


def _play_state_check(client: Client, item_id: str, users: Sequence[str]) -> Check:
    name = "nobody has a position in it"
    names, why_not = resolve_users(client, users)
    if not names:
        return Check(name, False, why_not)
    play = user_data(client, item_id, names)
    watched = {
        user: state for user, state in play.items()
        if state.get("PlayCount") or state.get("PlaybackPositionTicks")
        or state.get("Played") or state.get("IsFavorite")
    }
    return Check(
        name,
        not watched,
        f"{len(watched)} of {len(names)} user(s) do" if watched
        else f"{len(names)} user(s) checked",
    )


def _state_carried_check(client: Client, candidate: Candidate, users: Sequence[str]) -> Check:
    """Every user's state on this copy is held by the kept item too.

    A duplicate that somebody watched may go only once its history is on the
    copy that stays; the resolver writes it there first, and this reads both
    rows again, now, for every user.
    """
    from ..userdata import UserState, carries

    name = "every user's watched state is on the kept item"
    if not candidate.keeper_id:
        return Check(name, False, "no kept item is named to carry it")
    names, why_not = resolve_users(client, users)
    if not names:
        return Check(name, False, why_not)
    here = user_data(client, candidate.item_id, names)
    kept = user_data(client, candidate.keeper_id, names)
    short = [
        user for user in names
        if not carries(UserState.from_server(kept.get(user)),
                       UserState.from_server(here.get(user)))
    ]
    return Check(
        name,
        not short,
        f"{len(short)} of {len(names)} user(s) would lose state" if short
        else f"{len(names)} user(s) checked",
    )


def _twin_check(candidate: Candidate) -> Check:
    if candidate.keeper is None:
        return Check(
            "a twin is named and is identical", False,
            "this category needs a keeper, and the manifest names none",
        )
    if _same_file(candidate.path, candidate.keeper):
        return Check(
            "a twin is named and is identical", False,
            f"the twin named is the candidate itself: {candidate.keeper}",
        )
    found: Identity = identical(candidate.path, candidate.keeper)
    return Check("a twin is named and is identical", found.same, str(found))


def _keeper_check(client: Client, candidate: Candidate) -> tuple[Check, Path | None]:
    """Whether the kept item is there, and the file it keeps (None if unknown)."""
    if not candidate.keeper_id:
        return Check(
            "the kept item exists", False,
            "this category needs the identifier of what is kept instead",
        ), None
    if candidate.keeper_id == candidate.item_id:
        return Check(
            "the kept item exists", False,
            "the kept item named is the candidate itself",
        ), None
    if candidate.keeper is not None and _same_file(candidate.keeper, candidate.path):
        return Check(
            "the kept item exists", False,
            f"the keeper named is the candidate itself: {candidate.keeper}",
        ), None
    try:
        kept = fetch(client, candidate.keeper_id)
    except (ItemNotFound, LookupError) as exc:
        return Check("the kept item exists", False, str(exc)), None
    kept_path = Path(str(kept.get("Path") or ""))
    if kept.get("Path") and (
        _same_path(str(kept["Path"]), candidate.path)
        or _same_file(kept_path, candidate.path)
    ):
        return Check(
            "the kept item exists and its file is there", False,
            f"the kept item's file is the candidate itself: {kept_path}",
        ), None
    there = bool(kept.get("Path")) and kept_path.is_file()
    return Check(
        "the kept item exists and its file is there",
        there,
        f"kept: {kept.get('Name')} at {kept_path}",
    ), (kept_path if there else None)


KEPT_COPY_PLAYS = "the kept copy's payload is there and plays"


def _kept_copy_plays(kept: Path | None, keeper_check: KeeperCheck | None) -> Check:
    if kept is None:
        return Check(
            KEPT_COPY_PLAYS, False,
            "no kept file could be named, so there is no evidence it plays",
        )
    run = keeper_check or default_keeper_check()
    try:
        report = run(kept)
    except Exception as exc:  # any failure to measure is a refusal
        return Check(
            KEPT_COPY_PLAYS, False,
            f"no evidence: the check of {kept} could not run: {exc}",
        )
    if not report.evidence:
        return Check(
            KEPT_COPY_PLAYS, False,
            f"no evidence for {kept}: " + "; ".join(report.problems),
        )
    detail = (
        f"{kept}: " + ("; ".join(report.problems) if report.problems else
                       "every track covers the container"
                       + ("" if report.decoded else " (not decoded)"))
    )
    return Check(KEPT_COPY_PLAYS, report.ok, detail)


# ---------------------------------------------------------------- the run
def safe_delete(
    client: Client,
    candidates: Sequence[Candidate],
    *,
    allowed_categories: Iterable[str],
    parked: Path | str,
    users: Sequence[str] = (),
    audit: Path | str | None = None,
    backup_folders: Path | str | None = None,
    remove_rows: bool = True,
    keeper_check: KeeperCheck | None = None,
    catalogue: Catalogue | None = None,
    rules: Rules = DEFAULT_RULES,
    integrity_check: KeeperCheck | None = None,
) -> DeletionReport:
    """Check everything, then -- if the client is not in a dry run -- park it.

    The order inside one candidate is the only order that is safe: park the
    file, confirm it arrived, then let the row go. A row removed first leaves
    a file that nothing in the catalogue knows about, and those are found
    years later by accident.

    The row goes by telling the server the path was deleted, so that its own
    scan drops the row whose file is missing. ``remove_rows=False`` skips the
    notification and leaves the row for the next scheduled scan. Nothing here
    ever asks the server to delete an item: that call removes the item's
    whole containing folder from disk.

    ``users`` names whose play state is checked; naming nobody checks every
    user the server lists, once for the run.

    ``keeper_check`` reads the payload of every kept copy (default: the full
    :func:`mkvkit.integrity.check`). Each kept file is read once per run, however
    many candidates name it.

    The leftover categories are checked against ``catalogue``, read once for
    the run when it is not given; ``rules`` say what is junk, and
    ``integrity_check`` measures a corrupt-unplayable candidate.
    """
    released = tuple(sorted(set(allowed_categories)))
    audit_path = Path(audit) if audit is not None else None
    writer = _Audit(audit_path)
    writer.line(f"run started, categories released: {', '.join(released) or 'none'}")
    if not users:
        listed, why_not = resolve_users(client, users)
        writer.line(
            f"play state is checked for every user the server lists: {len(listed)}"
            if listed else f"play state cannot be checked: {why_not}"
        )
        # Empty when the list could not be had: each candidate then asks
        # again and fails its own check with the reason, in its own lines.
        users = listed

    measure = keeper_check or default_keeper_check()
    measured: dict[str, IntegrityReport] = {}

    def once(kept: Path) -> IntegrityReport:
        key = os.path.normcase(os.path.abspath(kept))
        if key not in measured:
            measured[key] = measure(kept)
            for line in str(measured[key]).splitlines():
                writer.line(f"kept copy: {line}")
        return measured[key]

    # Read once: a folder directly under one of these costs a whole-library
    # refresh when it goes, which is worth saying before it is scheduled.
    served_roots = library_roots(client)
    if catalogue is None and any(
        not c.needs_item or c.category == CORRUPT for c in candidates
    ):
        catalogue = Catalogue.fetch(client)
    outcomes: list[Outcome] = []
    for candidate in candidates:
        checks, contents = preconditions(
            client, candidate, allowed_categories=released, users=users,
            keeper_check=once, catalogue=catalogue, rules=rules,
            integrity_check=integrity_check, roots=served_roots or [],
        )
        notes = (tuple(contents.notes) if contents is not None else ()) + _top_level(
            candidate.path, served_roots
        )
        outcome = Outcome(candidate=candidate, checks=tuple(checks), notes=notes)
        for check in checks:
            writer.line(f"{candidate.item_id} {check}")
        for note in notes:
            writer.line(f"{candidate.item_id} note: {note}")

        if not outcome.allowed or client.dry_run:
            if not outcome.allowed:
                writer.line(
                    f"{candidate.item_id} REFUSED "
                    f"({len(outcome.refusals)} precondition(s))"
                )
            outcomes.append(outcome)
            continue

        outcomes.append(
            _park_and_remove(
                client, candidate, contents, Path(parked), tuple(checks),
                backup_folders=Path(backup_folders) if backup_folders else None,
                remove_row=remove_rows, writer=writer,
            )
        )

    writer.line("run finished")
    return DeletionReport(
        outcomes=tuple(outcomes),
        applied=not client.dry_run,
        audit=audit_path,
        allowed_categories=released,
    )


def _park_and_remove(
    client: Client,
    candidate: Candidate,
    contents: FolderContents | None,
    parked: Path,
    checks: tuple[Check, ...],
    *,
    backup_folders: Path | None,
    remove_row: bool,
    writer: _Audit,
) -> Outcome:
    size = candidate.size
    relative = parked_relative(candidate.path)
    destination = parked / relative
    if destination.exists():
        writer.line(f"{candidate.item_id} REFUSED something is already parked there")
        return Outcome(
            candidate=candidate,
            checks=(*checks,
                    Check("nothing is parked there already", False, str(destination))),
        )

    backup: Path | None = None
    if backup_folders is not None and contents is not None and contents.belongs:
        backup = backup_folders / relative.parent
        backup.mkdir(parents=True, exist_ok=True)
        for extra in contents.belongs:
            if extra != candidate.path and extra.is_file():
                shutil.copy2(extra, backup / extra.name)
        writer.line(f"{candidate.item_id} copied the rest of the folder to {backup}")

    # Read before the video moves: the set is found by the video's own name.
    carried: SidecarSet | None = (
        sidecars_of(candidate.path)
        if candidate.path.is_file()
        and candidate.path.suffix.lower() in VIDEO_SUFFIXES else None
    )
    label = candidate.item_id or "-"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(candidate.path), str(destination))
    writer.line(f"{label} parked {candidate.path} at {destination}")
    if not destination.exists():  # pragma: no cover - the move raises instead
        return Outcome(
            candidate=candidate,
            checks=(*checks, Check("it arrived in the parking directory", False)),
        )
    side_notes = _park_sidecars(
        carried, parked, label, writer,
        tracks_too=candidate.category == CORRUPT,
    )
    size += sum(_parked_size(parked, s.path) for s in (carried or ()))

    removed = False
    row_notes: list[str] = []
    if remove_row and candidate.item_id:
        removed, note = _let_the_row_go(client, candidate)
        writer.line(f"{label} {note}")
        if not removed:
            row_notes.append(note)
    return Outcome(
        candidate=candidate,
        checks=checks,
        parked=destination,
        row_removed=removed,
        folder_backup=backup,
        bytes_freed=size,
        notes=(
            *(contents.notes if contents is not None else ()),
            *side_notes,
            *row_notes,
            "parked, not deleted; removing it for good is a separate decision",
        ),
    )


def parked_location(parked: Path | str, path: Path | str) -> Path:
    """Where a path lands in the parking directory, drive letter kept
    (:func:`mkvkit.transfer.parked_relative`)."""
    return Path(parked) / parked_relative(path)


def _parked_size(parked: Path, original: Path) -> int:
    here = parked_location(parked, original)
    try:
        if here.is_dir():
            return sum(entry.size or 0 for entry in walk(here))
        return here.stat().st_size if here.exists() else 0
    except OSError:
        return 0


def _park_sidecars(
    carried: SidecarSet | None, parked: Path, label: str, writer: _Audit,
    *, tracks_too: bool,
) -> list[str]:
    """Park every file that belongs to a parked video, and say what is left.

    Description files, pictures, preview tiles and the rest mean nothing
    without their video and go with it. A subtitle or an external audio
    track may be the only copy of a translation the kept copy lacks, so it
    goes only when no copy is kept (``tracks_too``, corrupt-unplayable);
    otherwise it stays and is said. A sidecar already gone is skipped; one
    whose place in the parking directory is taken is left where it is and
    said, never overwritten.
    """
    if carried is None:
        return []
    notes: list[str] = []
    for sidecar in carried:
        source = sidecar.path
        if not source.exists():
            continue
        if sidecar.kind in (SidecarKind.SUBTITLE, SidecarKind.AUDIO) and not tracks_too:
            notes.append(
                f"{sidecar.kind.value} track left in place, it may be the only copy: "
                f"{source}"
            )
            writer.line(f"{label} sidecar KEPT ({sidecar.kind.value}) {source}")
            continue
        target = parked_location(parked, source)
        if target.exists():
            notes.append(f"sidecar left in place, its parking place is taken: {source}")
            writer.line(f"{label} sidecar LEFT {source}: {target} exists")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        writer.line(f"{label} parked sidecar ({sidecar.kind.value}) {source} at {target}")
    folder = carried.video.parent
    try:
        with os.scandir(folder) as listing:
            left = [
                entry.name for entry in listing
                if entry.is_file(follow_symlinks=False)
                and os.path.splitext(entry.name)[1].lower() in VIDEO_SUFFIXES
            ]
    except OSError:
        left = ["?"]
    if not left:
        notes.append(
            f"{folder} holds no video of its own now: 'jfkit leftovers sweep' "
            "lists it as a dead-release-folder candidate when nothing else is in it"
        )
        writer.line(f"{label} note: {folder} holds no video of its own now")
    return notes


def _let_the_row_go(client: Client, candidate: Candidate) -> tuple[bool, str]:
    """Tell the server the path is gone, and see whether the row went with it.

    Never ``DELETE /Items/{id}``: on this server that removes the item's
    containing folder from disk, sidecars and neighbours included. A
    path notification makes the server look at the path, find nothing there,
    and drop the row itself -- and a scan deletes no files.
    """
    roots = library_roots(client)
    if roots is None:
        return False, (
            "row left for a scheduled scan: the library folders could not be read, "
            "and a notification that might name one starts a full scan"
        )
    try:
        notify_changed(client, [candidate.path], roots=roots, kind="Deleted")
    except NotifyRefused as exc:
        return False, f"row left for a scheduled scan: {exc}"
    try:
        fetch(client, candidate.item_id)
    except (ItemNotFound, LookupError):
        return True, "row removed by the server's own scan after a path notification"
    return False, (
        "the server was told the path is gone; the row goes when its scan gets "
        "there, and nothing on disk is touched by that"
    )


@dataclass
class _Audit:
    """Append-only, one line per step, including the steps that did nothing."""

    path: Path | None = None
    lines: list[str] = field(default_factory=list)

    def line(self, text: str) -> None:
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        entry = f"{stamp}\t{text}"
        self.lines.append(entry)
        log.info("%s", text)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(entry + "\n")
