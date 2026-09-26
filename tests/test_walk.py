"""The walker: links and junctions are reported and not entered.

The junction test builds a real NTFS junction, because the whole reason this
module exists is that the platform's own link check does not see one.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from mkvkit.cli import main
from mkvkit.walk import SkipReason, is_link_or_junction, link_kind, walk


def _tree(root: Path) -> None:
    (root / "Season 1").mkdir(parents=True)
    (root / "Season 1" / "a.txt").write_text("a", encoding="utf-8")
    (root / "Season 1" / "b.nfo").write_text("bb", encoding="utf-8")
    (root / "Extras").mkdir()
    (root / "Extras" / "c.txt").write_text("ccc", encoding="utf-8")
    (root / "top.txt").write_text("dddd", encoding="utf-8")


def _junction(target: Path, link: Path) -> None:
    try:
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    except (ImportError, OSError):
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                       check=True, capture_output=True)


def _relatives(tree: object) -> list[str]:
    return [str(entry.relative) for entry in tree]  # type: ignore[attr-defined]


def test_a_plain_tree_is_walked_in_name_order_with_sizes(tmp_path: Path) -> None:
    _tree(tmp_path)
    entries = list(walk(tmp_path))
    assert [str(e.relative) for e in entries] == [
        "top.txt", "Extras/c.txt", "Season 1/a.txt", "Season 1/b.nfo",
    ]
    sizes = {str(e.relative): e.size for e in entries}
    assert sizes["top.txt"] == 4 and sizes["Extras/c.txt"] == 3
    assert all(not e.is_dir for e in entries)


def test_directories_are_yielded_when_asked_for(tmp_path: Path) -> None:
    _tree(tmp_path)
    dirs = [str(e.relative) for e in walk(tmp_path, include_dirs=True) if e.is_dir]
    assert sorted(dirs) == ["Extras", "Season 1"]


def test_excludes_match_names_and_relative_paths_and_are_reported(tmp_path: Path) -> None:
    _tree(tmp_path)
    tree = walk(tmp_path, exclude=["Extras", "Season 1/*.nfo"])
    assert sorted(_relatives(tree)) == ["Season 1/a.txt", "top.txt"]
    reasons = {(s.path.name, s.reason) for s in tree.skipped}
    assert reasons == {("Extras", SkipReason.EXCLUDED), ("b.nfo", SkipReason.EXCLUDED)}


def test_an_excluded_path_is_not_entered(tmp_path: Path) -> None:
    _tree(tmp_path)
    tree = walk(tmp_path, exclude_paths=[tmp_path / "Season 1"])
    assert sorted(_relatives(tree)) == ["Extras/c.txt", "top.txt"]
    assert [s.reason for s in tree.skipped] == [SkipReason.EXCLUDED]


def test_suffixes_limit_the_files_and_not_the_folders(tmp_path: Path) -> None:
    _tree(tmp_path)
    assert _relatives(walk(tmp_path, suffixes=[".NFO"])) == ["Season 1/b.nfo"]


def test_sizes_can_be_left_out(tmp_path: Path) -> None:
    _tree(tmp_path)
    assert {e.size for e in walk(tmp_path, sizes=False)} == {None}


def test_a_root_that_is_not_a_folder_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(NotADirectoryError):
        list(walk(tmp_path / "missing"))


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are an NTFS feature")
def test_a_junction_is_not_entered_and_is_reported(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    _tree(elsewhere)
    root = tmp_path / "library"
    root.mkdir()
    (root / "own.txt").write_text("x", encoding="utf-8")
    _junction(elsewhere, root / "pointer")

    # the trap: the platform's own check does not see it
    assert not os.path.islink(root / "pointer")
    assert os.path.isdir(root / "pointer")
    assert link_kind(root / "pointer") is SkipReason.JUNCTION
    assert is_link_or_junction(root / "pointer")
    assert not is_link_or_junction(root / "own.txt")

    tree = walk(root)
    assert _relatives(tree) == ["own.txt"]
    assert [(s.path.name, s.reason) for s in tree.skipped] == [
        ("pointer", SkipReason.JUNCTION)
    ]

    followed = walk(root, follow_links=True)
    names = _relatives(followed)
    assert "pointer/top.txt" in names and "own.txt" in names
    assert next(e for e in followed if e.relative.name == "top.txt").link is None


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are an NTFS feature")
def test_a_junction_loop_is_walked_once_when_links_are_followed(tmp_path: Path) -> None:
    root = tmp_path / "library"
    (root / "inner").mkdir(parents=True)
    (root / "inner" / "one.txt").write_text("1", encoding="utf-8")
    _junction(root, root / "inner" / "back")
    tree = walk(root, follow_links=True)
    assert _relatives(tree) == ["inner/one.txt"]
    assert [s.reason for s in tree.skipped] == [SkipReason.LOOP]


def _symlink_or_skip(target: Path, link: Path, *, directory: bool) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError):
        pytest.skip("this account may not create symbolic links")


def test_a_directory_symlink_is_not_entered_and_is_reported(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    _tree(elsewhere)
    root = tmp_path / "library"
    root.mkdir()
    _symlink_or_skip(elsewhere, root / "pointer", directory=True)
    (root / "own.txt").write_text("x", encoding="utf-8")
    _symlink_or_skip(root / "own.txt", root / "alias.txt", directory=False)

    tree = walk(root)
    assert _relatives(tree) == ["own.txt"]
    assert sorted((s.path.name, s.reason) for s in tree.skipped) == [
        ("alias.txt", SkipReason.SYMLINK), ("pointer", SkipReason.SYMLINK),
    ]
    assert link_kind(root / "pointer") is SkipReason.SYMLINK


def test_a_symlink_loop_is_walked_once_when_links_are_followed(tmp_path: Path) -> None:
    root = tmp_path / "library"
    (root / "inner").mkdir(parents=True)
    (root / "inner" / "one.txt").write_text("1", encoding="utf-8")
    _symlink_or_skip(root, root / "inner" / "back", directory=True)
    tree = walk(root, follow_links=True)
    assert _relatives(tree) == ["inner/one.txt"]
    assert [s.reason for s in tree.skipped] == [SkipReason.LOOP]


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0,  # type: ignore[attr-defined,unused-ignore]
                    reason="needs POSIX permissions and an unprivileged account")
def test_a_folder_that_cannot_be_listed_is_reported_and_the_walk_goes_on(
    tmp_path: Path,
) -> None:
    _tree(tmp_path)
    locked = tmp_path / "Season 1"
    locked.chmod(0)
    try:
        tree = walk(tmp_path)
        assert sorted(_relatives(tree)) == ["Extras/c.txt", "top.txt"]
        assert [(s.path.name, s.reason) for s in tree.skipped] == [
            ("Season 1", SkipReason.ERROR)
        ]
    finally:
        locked.chmod(0o755)


def test_a_listing_error_is_reported_and_the_walk_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _tree(tmp_path)
    real = os.scandir

    def refusing(path: object) -> object:
        if str(path).endswith("Season 1"):
            raise PermissionError(13, "Permission denied", str(path))
        return real(path)  # type: ignore[call-overload]

    monkeypatch.setattr("mkvkit.walk.os.scandir", refusing)
    seen = []
    tree = walk(tmp_path, on_skip=seen.append)
    assert sorted(_relatives(tree)) == ["Extras/c.txt", "top.txt"]
    assert [(s.path.name, s.reason, s.detail) for s in tree.skipped] == [
        ("Season 1", SkipReason.ERROR, "Permission denied")
    ]
    assert seen == tree.skipped
    assert "Permission denied" in str(tree.skipped[0])


def test_the_walk_verb_prints_entries_and_what_it_skipped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    _tree(tmp_path)
    assert main(["walk", str(tmp_path), "--exclude", "Extras", "--json"]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    paths = sorted(Path(r["path"]).name for r in rows if "path" in r)
    assert paths == ["a.txt", "b.nfo", "top.txt"]
    assert [r["reason"] for r in rows if "skipped" in r] == ["excluded"]
