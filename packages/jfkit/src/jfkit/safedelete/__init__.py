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

**The dry run is the default, and it is the useful one.** It runs every
precondition, reports every refusal with its reason and totals the bytes, and
changes nothing. A run that passes its dry run and then fails a precondition
has learned something real; that is the point.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import csv
import json
import logging
import os
import shutil
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..client import Client
from ..dto import every_user, fetch, user_data
from ..errors import ItemNotFound
from ..refresh import NotifyRefused, library_roots, notify_changed
from .evidence import (
    FolderContents,
    Identity,
    folder_contents,
    identical,
    loose_tracks,
    media_free,
)

__all__ = [
    "CATEGORIES",
    "Candidate",
    "Check",
    "DeletionReport",
    "Outcome",
    "load_manifest",
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
}


@dataclass(frozen=True)
class Check:
    """One precondition, its answer, and enough detail to argue with."""

    name: str
    ok: bool
    detail: str = ""

    def __str__(self) -> str:
        return f"{'ok' if self.ok else 'FAIL'}: {self.name}" + (
            f" -- {self.detail}" if self.detail else ""
        )


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

    @property
    def size(self) -> int:
        """What removing this would free. A folder counts everything below it."""
        try:
            if self.path.is_dir():
                return sum(
                    entry.stat().st_size
                    for entry in self.path.rglob("*") if entry.is_file()
                )
            return self.path.stat().st_size
        except OSError:
            return 0


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
        if self.audit is not None:
            lines.append(f"every step is in {self.audit}")
        return "\n".join(lines)


# ------------------------------------------------------------------ manifest
def load_manifest(path: Path | str) -> list[Candidate]:
    """Read a manifest. Tab-separated or a list of objects; nothing else.

    The columns are the fields of :class:`Candidate`. A manifest with a
    category column that nobody released is still read -- the refusal happens
    where it can be reported, not where it can be silently dropped.
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
) -> tuple[list[Check], FolderContents | None]:
    """Every check for one candidate, against the world as it is now.

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

    checks.append(_play_state_check(client, candidate.item_id, users))

    contents: FolderContents | None = None
    if candidate.category == "byte-identical-twin":
        checks.append(_twin_check(candidate))
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
    elif candidate.category in {"rebuild-donor", "superseded-copy"}:
        checks.append(_keeper_check(client, candidate))

    if candidate.path.is_file():
        # Reported rather than checked: another file in the folder does not stop
        # this one being removed, but it does stop the FOLDER being removed, and
        # that distinction is worth putting in front of a person rather than
        # deciding for them.
        contents = folder_contents(candidate.path)
    return checks, contents


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


def _keeper_check(client: Client, candidate: Candidate) -> Check:
    if not candidate.keeper_id:
        return Check(
            "the kept item exists", False,
            "this category needs the identifier of what is kept instead",
        )
    if candidate.keeper_id == candidate.item_id:
        return Check(
            "the kept item exists", False,
            "the kept item named is the candidate itself",
        )
    if candidate.keeper is not None and _same_file(candidate.keeper, candidate.path):
        return Check(
            "the kept item exists", False,
            f"the keeper named is the candidate itself: {candidate.keeper}",
        )
    try:
        kept = fetch(client, candidate.keeper_id)
    except (ItemNotFound, LookupError) as exc:
        return Check("the kept item exists", False, str(exc))
    kept_path = Path(str(kept.get("Path") or ""))
    if kept.get("Path") and (
        _same_path(str(kept["Path"]), candidate.path)
        or _same_file(kept_path, candidate.path)
    ):
        return Check(
            "the kept item exists and its file is there", False,
            f"the kept item's file is the candidate itself: {kept_path}",
        )
    return Check(
        "the kept item exists and its file is there",
        bool(kept.get("Path")) and kept_path.is_file(),
        f"kept: {kept.get('Name')} at {kept_path}",
    )


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

    outcomes: list[Outcome] = []
    for candidate in candidates:
        checks, contents = preconditions(
            client, candidate, allowed_categories=released, users=users
        )
        notes = tuple(contents.notes) if contents is not None else ()
        outcome = Outcome(candidate=candidate, checks=tuple(checks), notes=notes)
        for check in checks:
            writer.line(f"{candidate.item_id} {check}")

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
    relative = (
        Path(*candidate.path.parts[1:]) if candidate.path.is_absolute()
        else candidate.path
    )
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

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(candidate.path), str(destination))
    writer.line(f"{candidate.item_id} parked {candidate.path} at {destination}")
    if not destination.exists():  # pragma: no cover - the move raises instead
        return Outcome(
            candidate=candidate,
            checks=(*checks, Check("it arrived in the parking directory", False)),
        )

    removed = False
    row_notes: list[str] = []
    if remove_row:
        removed, note = _let_the_row_go(client, candidate)
        writer.line(f"{candidate.item_id} {note}")
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
            *row_notes,
            "parked, not deleted; removing it for good is a separate decision",
        ),
    )


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
