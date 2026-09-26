"""Copying and moving a file, proved by reading the copy back.

A move between volumes is a copy and a delete, and only the delete is
irreversible. These tests pin the order that makes it safe: nothing is
overwritten, the copy is hashed from the destination side before it gets
its name, and the source goes only after that.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import os
from pathlib import Path

import pytest
from mkvkit import cli as mkvkit_cli
from mkvkit import transfer
from mkvkit.transfer import digest_of, verified_copy


def featurette(tmp_path: Path) -> Path:
    source = tmp_path / "one-volume" / "Northwind" / "Featurettes" / "Making Northwind.mkv"
    source.parent.mkdir(parents=True)
    source.write_bytes(os.urandom(3 * transfer.CHUNK // 2))
    os.utime(source, (1_000_000_000, 1_000_000_000))
    return source


def target_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "other-volume" / "Northwind" / "Featurettes"
    folder.mkdir(parents=True)
    return folder


def leftovers(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir() if ".part-" in p.name)


def test_the_dry_run_writes_nothing(tmp_path: Path) -> None:
    source = featurette(tmp_path)
    folder = target_folder(tmp_path)
    report = verified_copy(source, folder, move=True)
    assert report.ok and not report.applied
    assert list(folder.iterdir()) == [] and source.is_file()
    assert "would move" in str(report)


def test_a_copy_is_hashed_on_both_sides_and_keeps_its_time(tmp_path: Path) -> None:
    source = featurette(tmp_path)
    folder = target_folder(tmp_path)
    report = verified_copy(source, folder, dry_run=False)
    arrived = folder / source.name
    assert report.ok and report.verified, str(report)
    assert report.source_digest == digest_of(source) == digest_of(arrived)
    assert arrived.stat().st_mtime == pytest.approx(1_000_000_000)
    assert source.is_file()
    assert leftovers(folder) == []
    assert "cache" in " ".join(report.notes), "the read-back caveat is said"


def test_a_move_removes_the_source_only_after_the_copy_is_proved(tmp_path: Path) -> None:
    source = featurette(tmp_path)
    original = source.read_bytes()
    folder = target_folder(tmp_path)
    report = verified_copy(source, folder, move=True, dry_run=False)
    assert report.ok and report.source_removed
    assert not source.exists()
    assert (folder / source.name).read_bytes() == original


def test_an_existing_destination_is_never_replaced(tmp_path: Path) -> None:
    source = featurette(tmp_path)
    folder = target_folder(tmp_path)
    (folder / source.name).write_bytes(b"somebody else's file")
    report = verified_copy(source, folder, move=True, dry_run=False)
    assert not report.ok and "never replaced" in report.problems[0]
    assert (folder / source.name).read_bytes() == b"somebody else's file"
    assert source.is_file()


def test_a_copy_that_does_not_hash_like_the_source_is_discarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What arrived is not what was sent: nothing is named, nothing is removed."""
    source = featurette(tmp_path)
    folder = target_folder(tmp_path)
    monkeypatch.setattr(transfer, "digest_of", lambda path, algorithm="sha256": "0" * 64)
    report = verified_copy(source, folder, move=True, dry_run=False)
    assert not report.ok and "does not hash like the source" in report.problems[0]
    assert source.is_file()
    assert list(folder.iterdir()) == [], "no partial file and no final name"


def test_a_copy_that_fails_half_way_leaves_nothing_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = featurette(tmp_path)
    folder = target_folder(tmp_path)

    def full_disk(src: Path, partial: Path, algorithm: str) -> str:
        partial.write_bytes(b"half")
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(transfer, "_copy_hashing", full_disk)
    report = verified_copy(source, folder, move=True, dry_run=False)
    assert not report.ok and "the source is untouched" in report.problems[0]
    assert source.is_file() and list(folder.iterdir()) == []


def test_a_staging_folder_on_the_same_volume_is_renamed_from(tmp_path: Path) -> None:
    source = featurette(tmp_path)
    folder = target_folder(tmp_path)
    stage = tmp_path / "other-volume" / "staging"
    stage.mkdir()
    report = verified_copy(source, folder, stage=stage, dry_run=False)
    assert report.ok and (folder / source.name).is_file()
    assert list(stage.iterdir()) == []


def test_a_missing_source_or_folder_is_refused_before_anything_is_written(
    tmp_path: Path,
) -> None:
    folder = target_folder(tmp_path)
    assert not verified_copy(tmp_path / "gone.mkv", folder, dry_run=False).ok
    source = featurette(tmp_path)
    report = verified_copy(source, tmp_path / "nowhere" / "x.mkv", dry_run=False)
    assert not report.ok and "folder does not exist" in report.problems[0]


def test_the_command_exits_non_zero_on_a_refusal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = featurette(tmp_path)
    folder = target_folder(tmp_path)
    assert mkvkit_cli.main(["copy", str(source), str(folder), "--move", "--apply"]) == 0
    assert not source.exists()
    assert mkvkit_cli.main(["copy", str(folder / source.name), str(folder), "--apply"]) == 1
    assert "never replaced" in capsys.readouterr().out
