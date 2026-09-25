"""jfkit.maintenance -- database and preview-image upkeep, with the guards kept.

Ported from a folder of one-off scripts into one module that uses
the standard library's own database driver rather than a separate program, so
there is nothing to find on the path and nothing to quote through a shell.

The guards are the module. The statements are four lines.

**Never read the live database.** It is open, in write-ahead mode, with a
connection pool in front of it. A second reader is how a database ends up
with a corrupt index and a support thread. Everything here reads a snapshot,
and the snapshot is made with the one statement that is safe against a live
database -- a read-only connection copying itself out.

**Never write it with the service up.** A write goes through
:func:`run_operations`, which insists on a controller, stops the service,
only then takes the snapshot and the counts, and starts the service again
afterwards whether the operation worked or not.

**No statement that was not written down.** A fix is a named operation with a
precondition, a description and a count of the rows it would touch, and the
dry run prints all three. There is no route in this module for a statement
somebody types once -- that is what a database console is for, and the
difference between the two is whether anybody can say afterwards what ran.

**Preview images are restored additively, never synchronised.** A change of
an item's type deletes the generated preview tiles for it and for every item
beneath it, and the queued jobs that were going to use them fail. The
mitigation is worth more than the warning: keep a copy, and put back only what
is missing. :func:`restore_previews` never overwrites a file and never deletes
one, on either side.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import logging
import shutil
import sqlite3
import tempfile
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePath
from typing import Protocol

from .client import Client
from .service import ServiceController, stopped

__all__ = [
    "PREVIEW_SUFFIX",
    "MaintenanceRefused",
    "MaintenanceReport",
    "Operation",
    "Reindex",
    "RepointPaths",
    "RestoreReport",
    "counts",
    "integrity",
    "read_only",
    "restore_previews",
    "run_operations",
    "running_tasks",
    "snapshot",
]

log = logging.getLogger(__name__)

#: The directory suffix generated preview tiles live in, beside the media.
PREVIEW_SUFFIX = ".trickplay"

#: Tables whose row count is compared before and after every write. A
#: maintenance pass that changes one of these has done something nobody asked
#: for, and finding that out from the counts is cheap.
COUNTED_TABLES: tuple[str, ...] = ("BaseItems", "UserData")


class MaintenanceRefused(RuntimeError):
    """A precondition was not met, so nothing was done."""


class Operation(Protocol):
    """One named fix: what it is, what it needs, and what it would touch.

    The name is a read-only property rather than an attribute so a frozen
    value type satisfies it, which is what every operation here is.
    """

    @property
    def name(self) -> str: ...

    def describe(self, connection: sqlite3.Connection) -> list[str]: ...

    def apply(self, connection: sqlite3.Connection) -> int: ...


@dataclass(frozen=True)
class MaintenanceReport:
    """What ran, against what copy, and whether the counts survived it."""

    database: Path
    snapshot: Path | None
    applied: bool
    operations: tuple[str, ...] = ()
    description: tuple[str, ...] = ()
    rows_touched: int = 0
    counts_before: Mapping[str, int] = field(default_factory=dict)
    counts_after: Mapping[str, int] = field(default_factory=dict)
    problems: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems

    def __str__(self) -> str:
        head = "applied" if self.applied else "dry run, nothing written"
        lines = [f"{self.database.name}: {head}"]
        if self.snapshot is not None:
            lines.append(f"  copy taken first: {self.snapshot}")
        lines += [f"  {line}" for line in self.description]
        if self.applied:
            lines.append(f"  rows touched: {self.rows_touched}")
            for table in sorted(set(self.counts_before) | set(self.counts_after)):
                before = self.counts_before.get(table)
                after = self.counts_after.get(table)
                mark = "" if before == after else "   <- CHANGED"
                lines.append(f"  {table}: {before} -> {after}{mark}")
        lines += [f"  problem: {p}" for p in self.problems]
        return "\n".join(lines)


@dataclass(frozen=True)
class RestoreReport:
    """What was put back, and what was deliberately left alone."""

    directories: int = 0
    files: int = 0
    bytes_copied: int = 0
    skipped_present: int = 0
    only_in_live: int = 0
    applied: bool = False

    def __str__(self) -> str:
        head = "restored" if self.applied else "would restore"
        return (
            f"{head} {self.files} file(s) in {self.directories} directory(ies), "
            f"{self.bytes_copied / 2**20:.1f} MiB; {self.skipped_present} already "
            f"present and left alone, {self.only_in_live} present only where they "
            "belong and not in the copy"
        )


# ------------------------------------------------------------------ reading
@contextmanager
def read_only(database: Path | str) -> Iterator[sqlite3.Connection]:
    """A connection that cannot write, whatever the caller does with it.

    The read-only flag is in the connection string rather than in a promise:
    the driver refuses a write itself, so a statement in the wrong function
    fails instead of succeeding against a database the server has open.
    """
    uri = f"file:{PurePath(str(database)).as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        yield connection


def snapshot(database: Path | str, out: Path | str) -> Path:
    """Copy a live database out, safely, and return where it went.

    This is the only correct way to get a usable copy of a database that is
    open: a read-only connection writes a consistent copy of itself. Copying
    the file with the filesystem catches it mid-transaction and copies the
    write-ahead log separately or not at all.
    """
    target = Path(out)
    if target.exists():
        raise MaintenanceRefused(f"{target} already exists; it is not overwritten")
    target.parent.mkdir(parents=True, exist_ok=True)
    with read_only(database) as connection:
        connection.execute("VACUUM INTO ?", (str(target),))
    log.info("copied %s to %s (%d bytes)", database, target, target.stat().st_size)
    return target


def integrity(database: Path | str) -> list[str]:
    """What the database says about itself. ``["ok"]`` is the good answer."""
    with read_only(database) as connection:
        return [row[0] for row in connection.execute("PRAGMA integrity_check")]


def counts(database: Path | str, tables: Sequence[str] = COUNTED_TABLES) -> dict[str, int]:
    """Row counts for the tables worth watching, skipping any that are absent."""
    out: dict[str, int] = {}
    with read_only(database) as connection:
        present = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        for table in tables:
            if table in present:
                out[table] = int(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )
    return out


def running_tasks(client: Client) -> list[str]:
    """Scheduled tasks the server says are running right now.

    A maintenance pass that stops the service under a running task is how a
    half-finished scan becomes a set of rows nobody can explain.
    """
    found = client.get("/ScheduledTasks")
    rows = found if isinstance(found, list) else []
    return [
        str(task.get("Name") or task.get("Key") or task.get("Id"))
        for task in rows
        if str(task.get("State") or "").lower() == "running"
    ]


# --------------------------------------------------------------- operations
@dataclass(frozen=True)
class RepointPaths:
    """Rewrite stored paths after the server's data directory moved.

    The documented fix for the stale-root problem, as a named operation: the
    library records keep absolute paths, the list route matches them by exact
    comparison, and after a move nothing matches. The rows are found by
    prefix, every new path is checked to exist on disk before anything is
    written, and the number of rows is asserted rather than assumed.
    """

    old_root: str
    new_root: str
    table: str = "BaseItems"
    expect_rows: int | None = None
    #: how a destination is checked to exist; injected in tests so no test
    #: needs a directory tree to prove the refusal works
    exists: Callable[[str], bool] | None = None

    name: str = "repoint paths after a data-directory move"

    def _rows(self, connection: sqlite3.Connection) -> list[tuple[str, str, str]]:
        cursor = connection.execute(
            f"SELECT Id, Name, Path FROM {self.table} WHERE Path LIKE ? ESCAPE '\\'",
            (_like_prefix(self.old_root),),
        )
        return [(str(a), str(b), str(c)) for a, b, c in cursor]

    def _moved(self, path: str) -> str:
        return self.new_root + path[len(self.old_root):]

    def describe(self, connection: sqlite3.Connection) -> list[str]:
        rows = self._rows(connection)
        lines = [f"{self.name}: {len(rows)} row(s) in {self.table}"]
        for _id, name, path in rows:
            lines.append(f"  {name}: {path} -> {self._moved(path)}")
        return lines

    def apply(self, connection: sqlite3.Connection) -> int:
        rows = self._rows(connection)
        if self.expect_rows is not None and len(rows) != self.expect_rows:
            raise MaintenanceRefused(
                f"{self.name}: {len(rows)} row(s) match, and {self.expect_rows} were "
                "expected. Nothing was written."
            )
        here = self.exists or (lambda p: Path(p).is_dir())
        missing = [self._moved(path) for _i, _n, path in rows if not here(self._moved(path))]
        if missing:
            raise MaintenanceRefused(
                f"{self.name}: {len(missing)} destination(s) do not exist, starting "
                f"with {missing[0]}. Nothing was written."
            )
        touched = 0
        for item_id, _name, path in rows:
            cursor = connection.execute(
                f"UPDATE {self.table} SET Path = ? WHERE Id = ? AND Path = ?",
                (self._moved(path), item_id, path),
            )
            if cursor.rowcount != 1:
                raise MaintenanceRefused(
                    f"{self.name}: updating {item_id} touched {cursor.rowcount} rows"
                )
            touched += cursor.rowcount
        left = connection.execute(
            f"SELECT COUNT(*) FROM {self.table} WHERE Path LIKE ? ESCAPE '\\'",
            (_like_prefix(self.old_root),),
        ).fetchone()[0]
        if left:
            raise MaintenanceRefused(f"{self.name}: {left} row(s) still carry the old root")
        return touched


@dataclass(frozen=True)
class Reindex:
    """Rebuild the indexes. The fix for a database that reads slowly and wrongly.

    It rewrites every index from the table data, so it repairs an index that
    has gone out of step with its table -- which is what a second writer, or a
    reader that was not supposed to be there, leaves behind.
    """

    name: str = "rebuild every index"

    def describe(self, connection: sqlite3.Connection) -> list[str]:
        indexes = connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'index'"
        ).fetchone()[0]
        return [f"{self.name}: {indexes} index(es)"]

    def apply(self, connection: sqlite3.Connection) -> int:
        connection.execute("REINDEX")
        return 0


def _like_prefix(prefix: str) -> str:
    """A pattern that matches this prefix literally, wildcards and all.

    An underscore is a single-character wildcard in this dialect, and a path
    with one in it quietly matches more rows than it should.
    """
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "%"


# ---------------------------------------------------------------- the pass
def run_operations(
    database: Path | str,
    operations: Iterable[Operation],
    *,
    controller: ServiceController,
    snapshot_dir: Path | str | None = None,
    dry_run: bool = True,
    client: Client | None = None,
) -> MaintenanceReport:
    """Describe, then -- if told twice -- stop, copy, count, apply, count, restart.

    The dry run reads a copy and never the original, so it is safe to run
    while everything is up; that is the whole point of having one. Without
    ``snapshot_dir`` the copy is made in a temporary directory and removed
    afterwards; the only thing that touches the live file is the read-only
    connection that writes the copy.

    On an applied run the copy is taken *after* the service has stopped, so
    the rollback artefact and the before-counts are the state that is about
    to be rewritten, not a state from a moment earlier. The after-counts are
    taken before the restart, for the same reason.

    The copy is not optional on an applied run. ``snapshot_dir`` may be left
    out while describing, and an applied run without one is refused: these
    operations rewrite rows in place in a file nothing else has a copy of, and
    a rollback artefact that the caller had to remember to ask for is not a
    rollback artefact.
    """
    path = Path(database)
    # Materialised once: the operations are walked twice, once to describe
    # and once to apply, and a generator walked twice is empty the second
    # time -- which applied nothing and reported ok.
    ops = list(operations)
    if client is not None:
        busy = running_tasks(client)
        if busy:
            raise MaintenanceRefused(
                "the server is running " + ", ".join(busy)
                + "; stopping it now leaves that half-finished"
            )

    if not dry_run and snapshot_dir is None:
        raise MaintenanceRefused(
            "an applied run takes a copy of the database first and needs "
            "somewhere to put it: pass snapshot_dir. Nothing was written."
        )

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    copy_name = f"{path.stem}-{stamp}{path.suffix}"

    if dry_run:
        # The description is read from a copy, never from the file the
        # server has open. With a directory named the copy is kept there;
        # without one it goes to a temporary directory and is removed.
        kept: Path | None = None
        if snapshot_dir is not None:
            kept = snapshot(path, Path(snapshot_dir) / copy_name)
            names, description = _describe(kept, ops)
        else:
            with tempfile.TemporaryDirectory(prefix="jfkit-describe-") as scratch:
                names, description = _describe(
                    snapshot(path, Path(scratch) / copy_name), ops
                )
        return MaintenanceReport(
            database=path, snapshot=kept, applied=False,
            operations=tuple(names), description=tuple(description),
        )

    assert snapshot_dir is not None  # refused above
    touched = 0
    with stopped(controller):
        # Stop first, then copy and count. A copy taken while the service
        # is still up is a copy of a database that can change before the
        # write: the rollback would not be the state that was rewritten, and
        # the before-counts would be compared against a moving file.
        copy = snapshot(path, Path(snapshot_dir) / copy_name)
        before = counts(copy)
        names, description = _describe(copy, ops)
        with closing(sqlite3.connect(str(path))) as connection:
            for operation in ops:
                touched += operation.apply(connection)
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        # Counted before the restart, while nothing else has the file open.
        after = counts(path)

    problems = [
        f"{table}: {before.get(table)} row(s) before, {after.get(table)} after"
        for table in sorted(set(before) | set(after))
        if before.get(table) != after.get(table)
    ]
    return MaintenanceReport(
        database=path, snapshot=copy, applied=True, operations=tuple(names),
        description=tuple(description), rows_touched=touched,
        counts_before=before, counts_after=after, problems=tuple(problems),
    )


def _describe(
    copy: Path, operations: Sequence[Operation]
) -> tuple[list[str], list[str]]:
    """Every operation's name and description, read from a copy."""
    names: list[str] = []
    description: list[str] = []
    with read_only(copy) as connection:
        for operation in operations:
            names.append(operation.name)
            description += operation.describe(connection)
    return names, description


# ------------------------------------------------------------- the tiles
def _preview_directories(base: Path) -> dict[str, dict[str, int]]:
    """Every preview directory below ``base``, as relative path -> file -> size."""
    out: dict[str, dict[str, int]] = {}
    if not base.is_dir():
        return out
    for directory in base.rglob(f"*{PREVIEW_SUFFIX}"):
        if not directory.is_dir():
            continue
        relative = directory.relative_to(base).as_posix()
        out[relative] = {
            inner.relative_to(directory).as_posix(): inner.stat().st_size
            for inner in directory.rglob("*")
            if inner.is_file()
        }
    return out


def restore_previews(
    backup: Path | str, live: Path | str, *, dry_run: bool = True
) -> RestoreReport:
    """Put back preview tiles that are missing, and touch nothing else.

    Additive in both directions. A directory present where the media lives and
    absent from the copy is left alone and counted; a file present in both is
    left alone and counted. Nothing is deleted on either side, and nothing is
    overwritten, because the copy is older by definition and "newer is right"
    is an assumption this has no way to check.
    """
    source_root, live_root = Path(backup), Path(live)
    source = _preview_directories(source_root)
    present = _preview_directories(live_root)

    directories = 0
    files = 0
    copied_bytes = 0
    skipped = 0
    for relative, contents in sorted(source.items()):
        here = present.get(relative)
        wanted = contents if here is None else {
            name: size for name, size in contents.items() if name not in here
        }
        if here is not None:
            skipped += len(set(contents) & set(here))
        if not wanted:
            continue
        directories += 1
        files += len(wanted)
        copied_bytes += sum(wanted.values())
        if dry_run:
            continue
        for name in wanted:
            src = source_root / relative / name
            dst = live_root / relative / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    return RestoreReport(
        directories=directories, files=files, bytes_copied=copied_bytes,
        skipped_present=skipped, only_in_live=len(set(present) - set(source)),
        applied=not dry_run,
    )
