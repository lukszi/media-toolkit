"""Evidence-first deletion: the allowlist, the preconditions, and the order.

Nothing here is deleted -- that is the module's whole point, and it is what
the tests check. A candidate that passes every precondition is *moved* to a
parking directory, and the catalogue row goes afterwards, in that order -- by
telling the server the path is gone, never by asking it to delete the item,
which on the real server takes the item's whole folder with it. The stand-in
models that, so a test that reached the item delete would lose files.

The three worth reading: a category nobody released is refused with its name
in the message; a manifest whose path has gone stale is refused rather than
guessed at; and the row is never removed before the file has arrived
somewhere, because a row removed first leaves a file nothing knows about.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from jfkit.safedelete import (
    CATEGORIES,
    Candidate,
    load_manifest,
    preconditions,
    safe_delete,
)
from jfkit.safedelete.evidence import (
    folder_contents,
    identical,
    media_free,
    remap_path,
    sha256_of,
)

from tests.fake_server import (
    ITEMS,
    SECOND_USER_ID,
    THIRD_USER_ID,
    USER_ID,
    Recorder,
    client_for,
    fake_server,
)

FIRST = ITEMS[0]["Id"]
SECOND = ITEMS[1]["Id"]


@pytest.fixture
def server() -> Iterator[tuple[str, Recorder]]:
    with fake_server() as running:
        yield running


@pytest.fixture
def tree(tmp_path: Path, server: tuple[str, Recorder]) -> Path:
    """Two files whose catalogue paths are the paths they are at."""
    _url, recorder = server
    root = tmp_path / "media"
    for item, name in ((recorder.items[0], "one.mkv"), (recorder.items[1], "two.mkv")):
        folder = root / Path(name).stem
        folder.mkdir(parents=True)
        path = folder / name
        path.write_bytes(b"the same bytes")
        item["Path"] = str(path)
    return root


def candidate(tree: Path, **over: object) -> Candidate:
    base = {
        "item_id": FIRST,
        "path": tree / "one" / "one.mkv",
        "category": "byte-identical-twin",
        "keeper": tree / "two" / "two.mkv",
    }
    base.update(over)
    return Candidate(**base)  # type: ignore[arg-type]


# ------------------------------------------------------------------ evidence
def test_two_files_of_different_sizes_are_not_hashed(tmp_path: Path) -> None:
    """A difference in size is an answer, and it is free."""
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_bytes(b"one")
    b.write_bytes(b"two bytes longer")
    found = identical(a, b)
    assert not found.same
    assert found.digest is None
    assert "sizes differ" in str(found)


def test_two_files_of_one_size_and_different_contents_are_different(
    tmp_path: Path
) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_bytes(b"aaa")
    b.write_bytes(b"bbb")
    assert not identical(a, b).same


def test_two_identical_files_say_so_and_carry_the_digest(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_bytes(b"the same bytes")
    b.write_bytes(b"the same bytes")
    found = identical(a, b)
    assert found.same
    assert found.digest == sha256_of(a)


def test_a_stale_path_is_remapped_and_the_rule_is_named(tmp_path: Path) -> None:
    """A candidate whose path has gone is usually a folder that was renamed."""
    (tmp_path / "new").mkdir()
    (tmp_path / "new" / "one.mkv").write_bytes(b"x")
    found = remap_path(tmp_path / "old" / "one.mkv", [("old", "new")])
    assert found.moved
    assert found.rule == "old -> new"


def test_a_path_that_is_there_is_not_remapped(tmp_path: Path) -> None:
    here = tmp_path / "one.mkv"
    here.write_bytes(b"x")
    assert remap_path(here, [("one", "two")]).found == here


def test_a_path_that_no_rule_finds_says_so(tmp_path: Path) -> None:
    assert remap_path(tmp_path / "gone.mkv", [("a", "b")]).found is None


def test_the_folder_walk_separates_what_belongs_from_what_does_not(
    tmp_path: Path
) -> None:
    folder = tmp_path / "The Quiet Harbour (1978)"
    folder.mkdir()
    media = folder / "the-quiet-harbour.mkv"
    media.write_bytes(b"x")
    (folder / "the-quiet-harbour.srt").write_text("1", encoding="utf-8")
    (folder / "blue-canyon.mkv").write_bytes(b"y")
    (folder / "notes.log").write_text("x", encoding="utf-8")

    found = folder_contents(media)
    assert media in found.belongs
    assert len(found.belongs) == 2
    assert [p.name for p in found.other_media] == ["blue-canyon.mkv"]
    assert [p.name for p in found.unrelated] == ["notes.log"]
    assert not found.safe_to_remove_folder
    assert any("different item" in note for note in found.notes)


def test_a_folder_with_no_media_in_it_is_recognisable(tmp_path: Path) -> None:
    folder = tmp_path / "leftovers"
    (folder / "meta").mkdir(parents=True)
    (folder / "meta" / "poster.jpg").write_bytes(b"x")
    assert media_free(folder)
    (folder / "film.mkv").write_bytes(b"x")
    assert not media_free(folder)


# -------------------------------------------------------------- the manifest
def test_a_manifest_reads_as_tab_separated_or_as_objects(tmp_path: Path) -> None:
    tsv = tmp_path / "manifest.tsv"
    tsv.write_text(
        "item_id\tpath\tcategory\treason\n"
        f"{FIRST}\t/srv/media/movies/one.mkv\tbyte-identical-twin\ta twin is kept\n",
        encoding="utf-8",
    )
    rows = load_manifest(tsv)
    assert len(rows) == 1 and rows[0].category == "byte-identical-twin"

    js = tmp_path / "manifest.json"
    js.write_text(
        f'[{{"item_id": "{FIRST}", "path": "/srv/media/movies/one.mkv", '
        '"category": "media-free-folder"}]',
        encoding="utf-8",
    )
    assert load_manifest(js)[0].category == "media-free-folder"


def test_a_manifest_missing_a_column_says_which(tmp_path: Path) -> None:
    bad = tmp_path / "manifest.tsv"
    bad.write_text("item_id\tpath\n1\t/srv/media/one.mkv\n", encoding="utf-8")
    with pytest.raises(ValueError, match="has no category"):
        load_manifest(bad)


# ---------------------------------------------------------- the preconditions
def test_a_category_nobody_released_is_refused_with_its_name(
    server: tuple[str, Recorder], tree: Path
) -> None:
    """The allowlist, which is where somebody's decision is written down."""
    url, _ = server
    checks, _ = preconditions(
        client_for(url), candidate(tree), allowed_categories=["media-free-folder"]
    )
    refused = [check for check in checks if not check.ok]
    assert refused
    assert "byte-identical-twin" in refused[0].detail
    assert str(refused[0]).startswith("FAIL: ")


def test_every_known_category_has_a_sentence_saying_what_it_means() -> None:
    assert set(CATEGORIES) >= {
        "byte-identical-twin", "media-free-folder", "rebuild-donor", "superseded-copy"
    }
    assert all(CATEGORIES.values())


def test_a_path_that_does_not_match_the_catalogue_is_refused(
    server: tuple[str, Recorder], tree: Path
) -> None:
    """A manifest is a plan, and a plan that was right yesterday is not evidence."""
    url, _ = server
    moved = candidate(tree, path=tree / "elsewhere" / "one.mkv")
    checks, _ = preconditions(
        client_for(url), moved, allowed_categories=["byte-identical-twin"]
    )
    names = {check.name for check in checks if not check.ok}
    assert "the catalogue's path is the manifest's path" in names


def test_somebody_elses_position_refuses_the_deletion(
    server: tuple[str, Recorder], tree: Path
) -> None:
    """Not the administrator's position: anybody's."""
    url, recorder = server
    recorder.user_data[(SECOND_USER_ID, FIRST)] = {
        "PlayCount": 1, "PlaybackPositionTicks": 900, "Played": False
    }
    checks, _ = preconditions(
        client_for(url), candidate(tree),
        allowed_categories=["byte-identical-twin"],
        users=[USER_ID, SECOND_USER_ID],
    )
    failed = [check for check in checks if not check.ok]
    assert any("position" in check.name for check in failed)


def test_a_twin_that_is_not_identical_refuses_the_deletion(
    server: tuple[str, Recorder], tree: Path
) -> None:
    url, _ = server
    (tree / "two" / "two.mkv").write_bytes(b"different bytes entirely")
    checks, _ = preconditions(
        client_for(url), candidate(tree), allowed_categories=["byte-identical-twin"]
    )
    assert any(not check.ok and "twin" in check.name for check in checks)


def test_a_category_that_needs_a_keeper_and_has_none_refuses(
    server: tuple[str, Recorder], tree: Path
) -> None:
    url, _ = server
    checks, _ = preconditions(
        client_for(url), candidate(tree, keeper=None),
        allowed_categories=["byte-identical-twin"],
    )
    assert any(not check.ok and "manifest names none" in check.detail
               for check in checks)


def test_a_superseded_copy_needs_the_kept_item_to_be_there(
    server: tuple[str, Recorder], tree: Path
) -> None:
    url, _ = server
    client = client_for(url)
    good = candidate(tree, category="superseded-copy", keeper_id=SECOND)
    checks, _ = preconditions(client, good, allowed_categories=["superseded-copy"])
    assert all(check.ok for check in checks), [str(c) for c in checks]

    bad = candidate(tree, category="superseded-copy", keeper_id=None)
    checks, _ = preconditions(client, bad, allowed_categories=["superseded-copy"])
    assert any(not check.ok for check in checks)


# ------------------------------------------------------------------- the run
def test_the_dry_run_checks_everything_and_changes_nothing(
    server: tuple[str, Recorder], tree: Path, tmp_path: Path
) -> None:
    url, recorder = server
    report = safe_delete(
        client_for(url), [candidate(tree)],
        allowed_categories=["byte-identical-twin"],
        parked=tmp_path / "parked",
        audit=tmp_path / "audit.log",
    )
    assert not report.applied
    assert len(report.allowed) == 1
    assert (tree / "one" / "one.mkv").is_file()
    assert recorder.deleted == []
    assert (tmp_path / "audit.log").is_file()
    assert "would be parked" in str(report)


def test_an_allowed_candidate_is_moved_and_then_the_row_goes(
    server: tuple[str, Recorder], tree: Path, tmp_path: Path
) -> None:
    """The order is the safety: the row goes after the file has arrived."""
    url, recorder = server
    parked = tmp_path / "parked"
    report = safe_delete(
        client_for(url, dry_run=False), [candidate(tree)],
        allowed_categories=["byte-identical-twin"],
        parked=parked, audit=tmp_path / "audit.log",
    )
    assert report.applied
    outcome = report.allowed[0]
    assert outcome.parked is not None and outcome.parked.is_file()
    assert not (tree / "one" / "one.mkv").exists()
    assert recorder.removed_by_scan == [FIRST]
    assert outcome.row_removed
    assert recorder.find(FIRST) is None
    assert outcome.bytes_freed == len(b"the same bytes")

    log_lines = (tmp_path / "audit.log").read_text(encoding="utf-8").splitlines()
    parked_at = next(i for i, line in enumerate(log_lines) if "parked" in line)
    removed_at = next(i for i, line in enumerate(log_lines) if "row removed" in line)
    assert parked_at < removed_at


def test_a_refused_candidate_is_left_entirely_alone(
    server: tuple[str, Recorder], tree: Path, tmp_path: Path
) -> None:
    url, recorder = server
    report = safe_delete(
        client_for(url, dry_run=False), [candidate(tree)],
        allowed_categories=["media-free-folder"],
        parked=tmp_path / "parked",
    )
    assert report.refused and not report.allowed
    assert (tree / "one" / "one.mkv").is_file()
    assert recorder.deleted == []


def test_nothing_is_parked_over_something_already_there(
    server: tuple[str, Recorder], tree: Path, tmp_path: Path
) -> None:
    """The copy somebody may still need is not overwritten by the next one."""
    url, recorder = server
    parked = tmp_path / "parked"
    already = parked / Path(*(tree / "one" / "one.mkv").parts[1:])
    already.parent.mkdir(parents=True)
    already.write_bytes(b"an earlier parked copy")

    report = safe_delete(
        client_for(url, dry_run=False), [candidate(tree)],
        allowed_categories=["byte-identical-twin"], parked=parked,
    )
    assert report.refused
    assert already.read_bytes() == b"an earlier parked copy"
    assert recorder.deleted == []


def test_the_rest_of_the_folder_is_copied_aside_first(
    server: tuple[str, Recorder], tree: Path, tmp_path: Path
) -> None:
    url, _ = server
    (tree / "one" / "one.srt").write_text("1\n", encoding="utf-8")
    report = safe_delete(
        client_for(url, dry_run=False), [candidate(tree)],
        allowed_categories=["byte-identical-twin"],
        parked=tmp_path / "parked", backup_folders=tmp_path / "folders",
    )
    backup = report.allowed[0].folder_backup
    assert backup is not None
    assert (backup / "one.srt").is_file()
    assert (tree / "one" / "one.srt").is_file(), "the copy is a copy"


def test_a_media_free_folder_is_moved_whole(
    server: tuple[str, Recorder], tree: Path, tmp_path: Path
) -> None:
    url, recorder = server
    leftovers = tree / "leftovers"
    leftovers.mkdir()
    (leftovers / "poster.jpg").write_bytes(b"x" * 10)
    recorder.items[0]["Path"] = str(leftovers)

    report = safe_delete(
        client_for(url, dry_run=False),
        [Candidate(item_id=FIRST, path=leftovers, category="media-free-folder")],
        allowed_categories=["media-free-folder"], parked=tmp_path / "parked",
    )
    assert report.allowed
    assert not leftovers.exists()
    assert report.allowed[0].parked is not None
    assert (report.allowed[0].parked / "poster.jpg").is_file()


def test_the_audit_records_the_checks_that_passed_too(
    server: tuple[str, Recorder], tree: Path, tmp_path: Path
) -> None:
    """"What happened to this file" is asked months later, by somebody else."""
    url, _ = server
    audit = tmp_path / "audit.log"
    safe_delete(
        client_for(url), [candidate(tree)],
        allowed_categories=["byte-identical-twin"],
        parked=tmp_path / "parked", audit=audit,
    )
    text = audit.read_text(encoding="utf-8")
    assert "ok: the item is in the catalogue" in text
    assert "run started" in text and "run finished" in text


def test_the_audit_is_appended_to_and_not_replaced(
    server: tuple[str, Recorder], tree: Path, tmp_path: Path
) -> None:
    url, _ = server
    audit = tmp_path / "audit.log"
    for _ in range(2):
        safe_delete(
            client_for(url), [candidate(tree)],
            allowed_categories=[], parked=tmp_path / "parked", audit=audit,
        )
    assert audit.read_text(encoding="utf-8").count("run started") == 2


# ------------------------------------------------ the row, and nothing else
def test_apply_never_asks_the_server_to_delete_an_item(
    server: tuple[str, Recorder], tree: Path, tmp_path: Path
) -> None:
    """The item delete removes the containing folder on the real server."""
    url, recorder = server
    safe_delete(
        client_for(url, dry_run=False), [candidate(tree)],
        allowed_categories=["byte-identical-twin"], parked=tmp_path / "parked",
    )
    assert not [r for r in recorder.requests if r[0] == "DELETE"]
    assert recorder.deleted == [] and recorder.folders_deleted == []


def test_everything_else_in_the_folder_survives_the_apply(
    server: tuple[str, Recorder], tree: Path, tmp_path: Path
) -> None:
    """Sidecars, artwork, and a different film that shares the folder."""
    url, _ = server
    folder = tree / "one"
    (folder / "one.srt").write_text("1\n", encoding="utf-8")
    (folder / "poster.jpg").write_bytes(b"artwork")
    (folder / "another-film.mkv").write_bytes(b"a different item")

    report = safe_delete(
        client_for(url, dry_run=False), [candidate(tree)],
        allowed_categories=["byte-identical-twin"], parked=tmp_path / "parked",
    )
    assert report.allowed and report.allowed[0].parked is not None
    assert (folder / "one.srt").read_text(encoding="utf-8") == "1\n"
    assert (folder / "poster.jpg").read_bytes() == b"artwork"
    assert (folder / "another-film.mkv").read_bytes() == b"a different item"
    assert (tree / "two" / "two.mkv").is_file(), "the twin is untouched"


def test_the_row_goes_through_a_deleted_path_notification(
    server: tuple[str, Recorder], tree: Path, tmp_path: Path
) -> None:
    url, recorder = server
    safe_delete(
        client_for(url, dry_run=False), [candidate(tree)],
        allowed_categories=["byte-identical-twin"], parked=tmp_path / "parked",
    )
    assert recorder.notifications == [
        {"Path": str(tree / "one" / "one.mkv"), "UpdateType": "Deleted"}
    ]
    assert recorder.removed_by_scan == [FIRST]
    assert recorder.find(SECOND) is not None, "the kept twin's row stays"


def test_the_stand_in_item_delete_really_takes_the_folder(
    server: tuple[str, Recorder], tree: Path
) -> None:
    """The model the tests above rely on: this is what the old call did."""
    url, recorder = server
    (tree / "one" / "one.srt").write_text("1\n", encoding="utf-8")
    client_for(url, dry_run=False).request("DELETE", f"/Items/{FIRST}")
    assert recorder.folders_deleted == [str(tree / "one")]
    assert not (tree / "one").exists()


def test_a_notification_does_not_drop_a_row_whose_file_is_still_there(
    server: tuple[str, Recorder], tree: Path
) -> None:
    url, recorder = server
    client_for(url, dry_run=False).post(
        "/Library/Media/Updated",
        {"Updates": [{"Path": str(tree / "one" / "one.mkv"), "UpdateType": "Deleted"}]},
    )
    assert recorder.removed_by_scan == []
    assert recorder.find(FIRST) is not None
    assert (tree / "one" / "one.mkv").is_file()


def test_a_library_root_is_never_named_in_the_notification(
    server: tuple[str, Recorder], tree: Path, tmp_path: Path
) -> None:
    """A folder candidate that is a library root would start a full scan."""
    url, recorder = server
    leftovers = tree / "leftovers"
    leftovers.mkdir()
    (leftovers / "poster.jpg").write_bytes(b"x")
    recorder.items[0]["Path"] = str(leftovers)
    recorder.virtual_folders = [{"Name": "Movies", "Locations": [str(leftovers)]}]

    report = safe_delete(
        client_for(url, dry_run=False),
        [Candidate(item_id=FIRST, path=leftovers, category="media-free-folder")],
        allowed_categories=["media-free-folder"], parked=tmp_path / "parked",
    )
    assert recorder.notifications == []
    assert not report.allowed[0].row_removed
    assert any("scheduled scan" in note for note in report.allowed[0].notes)


# ------------------------------------------------------ everybody's position
def test_naming_nobody_checks_every_user_the_server_has(
    server: tuple[str, Recorder], tree: Path
) -> None:
    url, recorder = server
    checks, _ = preconditions(
        client_for(url), candidate(tree), allowed_categories=["byte-identical-twin"]
    )
    play = next(c for c in checks if c.name == "nobody has a position in it")
    assert play.ok and play.detail == "3 user(s) checked"
    read = {route for _method, route in recorder.requests}
    for user in (USER_ID, SECOND_USER_ID, THIRD_USER_ID):
        assert f"/Users/{user}/Items/{FIRST}" in read


def test_a_position_nobody_named_still_refuses_the_apply(
    server: tuple[str, Recorder], tree: Path, tmp_path: Path
) -> None:
    """The operator named nobody; the third user is half-way through it."""
    url, recorder = server
    recorder.user_data[(THIRD_USER_ID, FIRST)] = {
        "PlayCount": 0, "PlaybackPositionTicks": 12_000, "Played": False
    }
    report = safe_delete(
        client_for(url, dry_run=False), [candidate(tree)],
        allowed_categories=["byte-identical-twin"], parked=tmp_path / "parked",
    )
    assert report.refused and not report.allowed
    assert "1 of 3 user(s) do" in str(report)
    assert (tree / "one" / "one.mkv").is_file()
    assert recorder.notifications == [] and recorder.find(FIRST) is not None


@pytest.mark.parametrize("broken", ["empty", "failing"])
def test_a_user_list_that_cannot_be_had_refuses_the_apply(
    server: tuple[str, Recorder], tree: Path, tmp_path: Path, broken: str
) -> None:
    url, recorder = server
    if broken == "empty":
        recorder.users = []
    else:
        recorder.users_status = 500
    report = safe_delete(
        client_for(url, dry_run=False), [candidate(tree)],
        allowed_categories=["byte-identical-twin"], parked=tmp_path / "parked",
        audit=tmp_path / "audit.log",
    )
    assert report.refused and not report.allowed
    refusal = next(c for c in report.refused[0].refusals
                   if c.name == "nobody has a position in it")
    assert "user" in refusal.detail
    assert (tree / "one" / "one.mkv").is_file()
    assert not (tmp_path / "parked").exists()
    assert recorder.notifications == []
    assert "play state cannot be checked" in (tmp_path / "audit.log").read_text(
        encoding="utf-8"
    )


# ------------------------------------------------ the keeper is not the candidate
def test_a_twin_that_is_the_candidate_itself_is_refused(
    server: tuple[str, Recorder], tree: Path, tmp_path: Path
) -> None:
    """Every byte of a file is identical to itself; that is not a twin."""
    url, _ = server
    same = tree / "one" / "one.mkv"
    report = safe_delete(
        client_for(url, dry_run=False), [candidate(tree, keeper=same)],
        allowed_categories=["byte-identical-twin"], parked=tmp_path / "parked",
    )
    assert report.refused and not report.allowed
    assert "the candidate itself" in str(report)
    assert same.is_file()


def test_a_twin_spelled_differently_but_the_same_file_is_refused(
    server: tuple[str, Recorder], tree: Path
) -> None:
    url, _ = server
    roundabout = tree / "two" / ".." / "one" / "one.mkv"
    checks, _ = preconditions(
        client_for(url), candidate(tree, keeper=roundabout),
        allowed_categories=["byte-identical-twin"],
    )
    assert any(not c.ok and "the candidate itself" in c.detail for c in checks)


@pytest.mark.parametrize("category", ["superseded-copy", "rebuild-donor"])
def test_a_kept_item_that_is_the_candidate_itself_is_refused(
    server: tuple[str, Recorder], tree: Path, tmp_path: Path, category: str
) -> None:
    url, _ = server
    report = safe_delete(
        client_for(url, dry_run=False),
        [candidate(tree, category=category, keeper=None, keeper_id=FIRST)],
        allowed_categories=[category], parked=tmp_path / "parked",
    )
    assert report.refused and not report.allowed
    assert "the candidate itself" in str(report)
    assert (tree / "one" / "one.mkv").is_file()


def test_a_kept_item_whose_file_is_the_candidates_is_refused(
    server: tuple[str, Recorder], tree: Path
) -> None:
    """Two rows, one file: keeping the other row keeps nothing."""
    url, recorder = server
    recorder.items[1]["Path"] = str(tree / "one" / "one.mkv")
    checks, _ = preconditions(
        client_for(url),
        candidate(tree, category="superseded-copy", keeper=None, keeper_id=SECOND),
        allowed_categories=["superseded-copy"],
    )
    assert any(not c.ok and "the candidate itself" in c.detail for c in checks)


def test_a_file_a_swap_parked_is_refused_every_time(
    server: tuple[str, Recorder], tree: Path, tmp_path: Path
) -> None:
    """The recipe's afterwards step: a parked original has no row of its own.

    The row it had belongs to the rebuild now at the live path, so the path
    check fails however the manifest names the kept item.
    """
    url, _ = server
    parked_original = tmp_path / "parked" / "one.mkv"
    parked_original.parent.mkdir(parents=True)
    parked_original.write_bytes(b"the original")
    for keeper_id in (FIRST, SECOND):
        report = safe_delete(
            client_for(url, dry_run=False),
            [Candidate(item_id=FIRST, path=parked_original, category="rebuild-donor",
                       keeper_id=keeper_id)],
            allowed_categories=["rebuild-donor"], parked=tmp_path / "again",
        )
        assert report.refused and not report.allowed
        assert "the catalogue's path is the manifest's path" in str(report)
    assert parked_original.read_bytes() == b"the original"
