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
otherwise. The catalogue row is removed only after the move has succeeded,
in that order, because a row removed before a move that then fails leaves a
file nothing knows about.

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
import shutil
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..client import Client
from ..dto import fetch, user_data
from ..errors import ItemNotFound
from .evidence import FolderContents, Identity, folder_contents, identical, media_free

__all__ = [
    "CATEGORIES",
    "Candidate",
    "Check",
    "DeletionReport",
    "Outcome",
    "load_manifest",
    "preconditions",
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
    """Every check for one candidate, against the world as it is now."""
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

    play = user_data(client, candidate.item_id, users) if users else {}
    watched = {
        user: state for user, state in play.items()
        if state.get("PlayCount") or state.get("PlaybackPositionTicks")
        or state.get("Played") or state.get("IsFavorite")
    }
    checks.append(Check(
        "nobody has a position in it",
        not watched,
        f"{len(watched)} user(s) do" if watched else f"{len(play)} user(s) checked",
    ))

    contents: FolderContents | None = None
    if candidate.category == "byte-identical-twin":
        checks.append(_twin_check(candidate))
    elif candidate.category == "media-free-folder":
        checks.append(Check(
            "the folder holds no media",
            media_free(candidate.path),
            str(candidate.path),
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


def _twin_check(candidate: Candidate) -> Check:
    if candidate.keeper is None:
        return Check(
            "a twin is named and is identical", False,
            "this category needs a keeper, and the manifest names none",
        )
    found: Identity = identical(candidate.path, candidate.keeper)
    return Check("a twin is named and is identical", found.same, str(found))


def _keeper_check(client: Client, candidate: Candidate) -> Check:
    if not candidate.keeper_id:
        return Check(
            "the kept item exists", False,
            "this category needs the identifier of what is kept instead",
        )
    try:
        kept = fetch(client, candidate.keeper_id)
    except (ItemNotFound, LookupError) as exc:
        return Check("the kept item exists", False, str(exc))
    kept_path = Path(str(kept.get("Path") or ""))
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
    file, confirm it arrived, then remove the row. A row removed first leaves
    a file that nothing in the catalogue knows about, and those are found
    years later by accident.
    """
    released = tuple(sorted(set(allowed_categories)))
    audit_path = Path(audit) if audit is not None else None
    writer = _Audit(audit_path)
    writer.line(f"run started, categories released: {', '.join(released) or 'none'}")

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
    if remove_row:
        client.request("DELETE", f"/Items/{candidate.item_id}")
        removed = True
        writer.line(f"{candidate.item_id} row removed")
    return Outcome(
        candidate=candidate,
        checks=checks,
        parked=destination,
        row_removed=removed,
        folder_backup=backup,
        bytes_freed=size,
        notes=(
            *(contents.notes if contents is not None else ()),
            "parked, not deleted; removing it for good is a separate decision",
        ),
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
