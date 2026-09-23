"""Database and preview upkeep, against a database this file builds.

Never a real one. The schema here is three columns wide and reproduces only
the shape the operations need -- an identifier, a name and a path -- which is
enough to test every guard and tells nobody anything about any collection.

The tests worth reading are the two refusals: a destination that does not
exist stops the write before it starts, and a row count that does not match
the number the operation was told to expect stops it too. Both are the
difference between a fix and a statement somebody typed twice.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from jfkit.maintenance import (
    PREVIEW_SUFFIX,
    MaintenanceRefused,
    Reindex,
    RepointPaths,
    counts,
    integrity,
    read_only,
    restore_previews,
    run_operations,
    running_tasks,
    snapshot,
)
from jfkit.service import ManualServiceController

from tests.fake_server import Recorder, client_for, fake_server

OLD_ROOT = "/var/lib/old-location"
NEW_ROOT = "/var/lib/media-server"

ROWS = [
    ("00000000-0000-0000-0000-000000000101", "CollectionFolder", "Movies",
     f"{OLD_ROOT}/root/default/Movies"),
    ("00000000-0000-0000-0000-000000000102", "CollectionFolder", "Series",
     f"{OLD_ROOT}/root/default/Series"),
    ("00000000-0000-0000-0000-000000000103", "Movie", "The Quiet Harbour",
     "/srv/media/movies/The Quiet Harbour (1978)/the-quiet-harbour.mkv"),
]


@pytest.fixture
def database(tmp_path: Path) -> Path:
    """A database with the shape the operations need and nothing else in it."""
    path = tmp_path / "catalogue.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE BaseItems (Id TEXT PRIMARY KEY, Type TEXT, Name TEXT, Path TEXT)"
    )
    connection.execute(
        "CREATE TABLE UserData (Key TEXT, UserId TEXT, PlayCount INTEGER)"
    )
    connection.execute("CREATE INDEX idx_baseitems_path ON BaseItems (Path)")
    connection.executemany("INSERT INTO BaseItems VALUES (?, ?, ?, ?)", ROWS)
    connection.execute(
        "INSERT INTO UserData VALUES ('a', '00000000-0000-0000-0000-000000000001', 2)"
    )
    connection.commit()
    connection.close()
    return path


@pytest.fixture
def controller() -> ManualServiceController:
    """A controller that says yes, so the test is about the database, not the prompt."""
    return ManualServiceController(name="the stand-in", confirm=lambda _prompt: True)


@pytest.fixture
def server() -> Iterator[tuple[str, Recorder]]:
    with fake_server() as running:
        yield running


def always_there(_path: str) -> bool:
    return True


# ------------------------------------------------------------------ reading
def test_a_read_only_connection_refuses_to_write(database: Path) -> None:
    """The flag is in the connection, not in a comment.

    A statement in the wrong function fails here instead of succeeding
    against a database somebody else has open.
    """
    with read_only(database) as connection, pytest.raises(sqlite3.OperationalError):
        connection.execute("DELETE FROM BaseItems")


def test_a_copy_is_taken_without_touching_the_original(
    database: Path, tmp_path: Path
) -> None:
    out = snapshot(database, tmp_path / "copies" / "catalogue.db")
    assert out.is_file()
    assert counts(out) == counts(database)
    assert integrity(out) == ["ok"]


def test_a_copy_is_never_written_over(database: Path, tmp_path: Path) -> None:
    out = snapshot(database, tmp_path / "copy.db")
    with pytest.raises(MaintenanceRefused, match="not overwritten"):
        snapshot(database, out)


def test_counts_skip_a_table_that_is_not_there(database: Path) -> None:
    assert counts(database) == {"BaseItems": 3, "UserData": 1}
    assert counts(database, ["NoSuchTable"]) == {}


# --------------------------------------------------------------- operations
def test_the_dry_run_says_what_it_would_change_and_changes_nothing(
    database: Path, controller: ManualServiceController, tmp_path: Path
) -> None:
    report = run_operations(
        database,
        [RepointPaths(OLD_ROOT, NEW_ROOT, expect_rows=2, exists=always_there)],
        controller=controller,
        snapshot_dir=tmp_path / "copies",
    )
    assert not report.applied
    assert any(NEW_ROOT in line for line in report.description)
    with read_only(database) as connection:
        left = connection.execute(
            "SELECT COUNT(*) FROM BaseItems WHERE Path LIKE ?", (OLD_ROOT + "%",)
        ).fetchone()[0]
    assert left == 2


def test_applying_moves_exactly_the_rows_that_matched(
    database: Path, controller: ManualServiceController, tmp_path: Path
) -> None:
    report = run_operations(
        database,
        [RepointPaths(OLD_ROOT, NEW_ROOT, expect_rows=2, exists=always_there)],
        controller=controller,
        snapshot_dir=tmp_path / "copies",
        dry_run=False,
    )
    assert report.applied and report.ok, str(report)
    assert report.rows_touched == 2
    assert report.snapshot is not None and report.snapshot.is_file()
    with read_only(database) as connection:
        paths = [row[0] for row in connection.execute("SELECT Path FROM BaseItems")]
    assert sum(1 for p in paths if p.startswith(NEW_ROOT)) == 2
    assert sum(1 for p in paths if p.startswith(OLD_ROOT)) == 0
    assert "/srv/media/movies" in " ".join(paths), "the media rows were left alone"


def test_a_destination_that_does_not_exist_stops_the_write(
    database: Path, controller: ManualServiceController
) -> None:
    """The precondition, doing its job.

    Rewriting a path to somewhere that is not there produces a library that
    is listed and empty, which looks like data loss and is much harder to
    diagnose than a refusal.
    """
    with pytest.raises(MaintenanceRefused, match="do not exist"):
        run_operations(
            database,
            [RepointPaths(OLD_ROOT, NEW_ROOT, exists=lambda _p: False)],
            controller=controller,
            dry_run=False,
        )
    assert counts(database)["BaseItems"] == 3


def test_a_row_count_that_is_not_the_expected_one_stops_the_write(
    database: Path, controller: ManualServiceController
) -> None:
    with pytest.raises(MaintenanceRefused, match="were expected"):
        run_operations(
            database,
            [RepointPaths(OLD_ROOT, NEW_ROOT, expect_rows=5, exists=always_there)],
            controller=controller,
            dry_run=False,
        )


def test_an_underscore_in_a_prefix_is_not_a_wildcard(
    database: Path, controller: ManualServiceController, tmp_path: Path
) -> None:
    """The dialect treats it as one, and a path with one in it matches too much."""
    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO BaseItems VALUES ('00000000-0000-0000-0000-000000000104', "
        "'CollectionFolder', 'Other', '/var/lib/oldXlocation/root/default/Other')"
    )
    connection.commit()
    connection.close()

    report = run_operations(
        database,
        [RepointPaths("/var/lib/old_location", NEW_ROOT, exists=always_there)],
        controller=controller,
    )
    assert all("oldXlocation" not in line for line in report.description)


def test_rebuilding_the_indexes_leaves_the_rows_alone(
    database: Path, controller: ManualServiceController, tmp_path: Path
) -> None:
    before = counts(database)
    report = run_operations(
        database, [Reindex()], controller=controller,
        snapshot_dir=tmp_path / "copies", dry_run=False,
    )
    assert report.ok
    assert counts(database) == before
    assert integrity(database) == ["ok"]


def test_the_counts_are_compared_and_a_change_is_reported(
    database: Path, controller: ManualServiceController
) -> None:
    """An operation that removes rows says so, even if it thought it was fine."""

    class RemoveEverything:
        name = "remove every row, which nothing should ever do"

        def describe(self, connection: sqlite3.Connection) -> list[str]:
            return [self.name]

        def apply(self, connection: sqlite3.Connection) -> int:
            return connection.execute("DELETE FROM BaseItems").rowcount

    report = run_operations(
        database, [RemoveEverything()], controller=controller, dry_run=False
    )
    assert not report.ok
    assert "3 row(s) before, 0 after" in str(report)


def test_a_running_task_stops_the_pass_before_the_service_does(
    database: Path, controller: ManualServiceController, server: tuple[str, Recorder]
) -> None:
    url, recorder = server
    recorder.scheduled_tasks = [
        {"Id": "00000000-0000-0000-0000-000000000201", "Name": "Scan the library",
         "State": "Running"},
    ]
    with pytest.raises(MaintenanceRefused, match="half-finished"):
        run_operations(
            database, [Reindex()], controller=controller,
            client=client_for(url), dry_run=False,
        )


def test_an_idle_server_is_not_in_the_way(server: tuple[str, Recorder]) -> None:
    url, recorder = server
    recorder.scheduled_tasks = [
        {"Id": "00000000-0000-0000-0000-000000000201", "Name": "Scan the library",
         "State": "Idle"},
    ]
    assert running_tasks(client_for(url)) == []


# ---------------------------------------------------------------- the tiles
def _tiles(root: Path, item: str, names: list[str]) -> None:
    directory = root / f"{item}{PREVIEW_SUFFIX}"
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).write_bytes(b"tile")


def test_restoring_puts_back_only_what_is_missing(tmp_path: Path) -> None:
    """Additive, in both directions, and never a synchronisation.

    A directory that exists only where the media lives is newer than the
    copy and is left alone. A file present in both is left alone. Nothing is
    deleted anywhere.
    """
    backup, live = tmp_path / "copy", tmp_path / "live"
    _tiles(backup, "a", ["1.jpg", "2.jpg"])
    _tiles(backup, "b", ["1.jpg"])
    _tiles(live, "a", ["1.jpg"])
    _tiles(live, "c", ["1.jpg"])

    planned = restore_previews(backup, live)
    assert planned.files == 2          # a/2.jpg and b/1.jpg
    assert planned.skipped_present == 1
    assert planned.only_in_live == 1
    assert not (live / f"b{PREVIEW_SUFFIX}").exists()

    done = restore_previews(backup, live, dry_run=False)
    assert done.applied and done.files == 2
    assert (live / f"b{PREVIEW_SUFFIX}" / "1.jpg").is_file()
    assert (live / f"c{PREVIEW_SUFFIX}" / "1.jpg").is_file(), "nothing was removed"
    assert "restored 2 file(s)" in str(done)


def test_a_second_restore_has_nothing_left_to_do(tmp_path: Path) -> None:
    backup, live = tmp_path / "copy", tmp_path / "live"
    _tiles(backup, "a", ["1.jpg"])
    restore_previews(backup, live, dry_run=False)
    again = restore_previews(backup, live, dry_run=False)
    assert again.files == 0


def test_restoring_from_a_copy_that_is_not_there_does_nothing(tmp_path: Path) -> None:
    found = restore_previews(tmp_path / "missing", tmp_path / "live")
    assert found.files == 0
