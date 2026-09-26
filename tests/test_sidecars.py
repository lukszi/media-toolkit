"""The sidecar set of a video: what a rename or a move has to carry with it.

Every video name here is one of the invented cast's example filenames; the
sidecars are built from their stems.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mkvkit.cli import main
from mkvkit.sidecars import (
    FolderListing,
    SidecarKind,
    planned_renames,
    rename_target,
    sidecars_in_folder,
    sidecars_of,
)

EPISODE = "Northwind - S01E03 - The Quiet Harbour.mkv"
STEM = "Northwind - S01E03 - The Quiet Harbour"
SHORT = "Harbour.Lights.S01E02.mkv"
LONG = "Harbour.Lights.S01E02.German.DL.mkv"


def _touch(folder: Path, *names: str) -> None:
    for name in names:
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")


def _kinds(found: object) -> dict[str, tuple[SidecarKind, bool]]:
    return {
        s.path.name: (s.kind, s.read_by_server)
        for s in found  # type: ignore[attr-defined]
    }


def test_every_kind_of_sidecar_is_found_and_classified(tmp_path: Path) -> None:
    _touch(
        tmp_path, EPISODE,
        f"{STEM}.nfo", f"{STEM}-thumb.jpg", f"{STEM}.png", f"{STEM}-fanart2.jpg",
        f"{STEM}.eng.srt", f"{STEM}.deu.forced.ass", f"{STEM}-eng.srt",
        f"{STEM}.deu.mka", f"{STEM}.chapters.xml", f"{STEM}.txt", f"{STEM}.ttml",
        f"{STEM}.idx",
        "unrelated.txt", "folder.jpg",
    )
    (tmp_path / f"{STEM}.trickplay" / "320 - 10x10").mkdir(parents=True)
    (tmp_path / "Northwind - S01E03 - The Quiet Harbour.backup").mkdir()

    found = sidecars_of(tmp_path / EPISODE)
    assert found.stem == STEM
    assert _kinds(found) == {
        f"{STEM}.nfo": (SidecarKind.NFO, True),
        f"{STEM}-thumb.jpg": (SidecarKind.IMAGE, True),
        f"{STEM}.png": (SidecarKind.IMAGE, True),
        f"{STEM}-fanart2.jpg": (SidecarKind.IMAGE, True),
        f"{STEM}.eng.srt": (SidecarKind.SUBTITLE, True),
        f"{STEM}.deu.forced.ass": (SidecarKind.SUBTITLE, True),
        # the server's only flag delimiter is the dot
        f"{STEM}-eng.srt": (SidecarKind.SUBTITLE, False),
        f"{STEM}.idx": (SidecarKind.SUBTITLE, True),
        f"{STEM}.deu.mka": (SidecarKind.AUDIO, True),
        f"{STEM}.chapters.xml": (SidecarKind.CHAPTERS, False),
        f"{STEM}.txt": (SidecarKind.OTHER, False),
        f"{STEM}.ttml": (SidecarKind.OTHER, False),
        f"{STEM}.trickplay": (SidecarKind.TRICKPLAY, True),
    }
    trick = found.of_kind(SidecarKind.TRICKPLAY)[0]
    assert trick.is_dir and trick.tail == ".trickplay"


def test_the_match_ignores_case_and_needs_a_delimiter(tmp_path: Path) -> None:
    _touch(tmp_path, EPISODE, f"{STEM.upper()}.NFO", f"{STEM}x.nfo", f"{STEM} .nfo")
    assert [s.path.name for s in sidecars_of(tmp_path / EPISODE)] == [
        f"{STEM.upper()}.NFO"
    ]


def test_a_file_belongs_to_the_video_with_the_longest_stem(tmp_path: Path) -> None:
    _touch(
        tmp_path, SHORT, LONG,
        "Harbour.Lights.S01E02.nfo",
        "Harbour.Lights.S01E02.German.DL.srt",
        "Harbour.Lights.S01E02.German.DL.nfo",
        "Harbour.Lights.S01E02.eng.srt",
    )
    short = sidecars_of(tmp_path / SHORT)
    assert sorted(s.path.name for s in short) == [
        "Harbour.Lights.S01E02.eng.srt", "Harbour.Lights.S01E02.nfo",
    ]
    long = sidecars_of(tmp_path / LONG)
    assert sorted(s.path.name for s in long) == [
        "Harbour.Lights.S01E02.German.DL.nfo", "Harbour.Lights.S01E02.German.DL.srt",
    ]
    # the other video is never a sidecar
    assert all(s.path.name != LONG for s in short)


def test_a_picture_in_the_metadata_folder_is_claimed_and_moves_with_it(
    tmp_path: Path,
) -> None:
    _touch(tmp_path, EPISODE, f"metadata/{STEM}.jpg", "metadata/other.jpg")
    found = sidecars_of(tmp_path / EPISODE)
    (picture,) = found.sidecars
    assert picture.in_metadata_folder and picture.kind is SidecarKind.IMAGE
    target = tmp_path / "moved" / "Northwind.S01E03E04.mkv"
    assert dict(found.renames(target))[picture.path] == (
        tmp_path / "moved" / "metadata" / "Northwind.S01E03E04.jpg"
    )


def test_planned_renames_carry_the_video_first_and_every_tail(tmp_path: Path) -> None:
    _touch(tmp_path, EPISODE, f"{STEM}.nfo", f"{STEM}.eng.srt")
    (tmp_path / f"{STEM}.trickplay").mkdir()
    new = tmp_path / "Northwind.S01E03E04.mkv"
    pairs = planned_renames(tmp_path / EPISODE, new)
    assert pairs[0] == (tmp_path / EPISODE, new)
    assert {b.name for _a, b in pairs[1:]} == {
        "Northwind.S01E03E04.nfo", "Northwind.S01E03E04.eng.srt",
        "Northwind.S01E03E04.trickplay",
    }


def test_a_set_can_be_read_after_the_video_itself_has_gone(tmp_path: Path) -> None:
    _touch(tmp_path, f"{STEM}.nfo")
    assert [s.path.name for s in sidecars_of(tmp_path / EPISODE)] == [f"{STEM}.nfo"]


def test_one_listing_serves_every_video_in_a_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _touch(tmp_path, SHORT, LONG, "Harbour.Lights.S01E02.nfo", "leftover.txt")
    listing = FolderListing.read(tmp_path)

    def no_listing(_path: object) -> object:
        raise AssertionError("the folder was listed again")

    monkeypatch.setattr("mkvkit.sidecars.os.scandir", no_listing)
    assert len(sidecars_of(tmp_path / SHORT, listing=listing)) == 1
    assert len(sidecars_of(tmp_path / LONG, listing=listing)) == 0


def test_a_folder_reports_the_files_that_belong_to_no_video(tmp_path: Path) -> None:
    _touch(tmp_path, SHORT, "Harbour.Lights.S01E02.nfo", "leftover.txt",
           "Harbour.Lights.S01E09.nfo")
    found = sidecars_in_folder(tmp_path)
    assert list(found.sets) == [tmp_path / SHORT]
    assert sorted(p.name for p in found.unclaimed) == [
        "Harbour.Lights.S01E09.nfo", "leftover.txt",
    ]


def test_rename_target_refuses_a_file_that_is_not_the_stems() -> None:
    folder = Path("/srv/media/series")
    assert rename_target(folder / f"{STEM}.eng.srt", old_stem=STEM,
                         new_stem="Northwind.S01E03E04") == (
        folder / "Northwind.S01E03E04.eng.srt"
    )
    with pytest.raises(ValueError, match="does not belong"):
        rename_target(folder / "other.srt", old_stem=STEM, new_stem="x")


def test_the_sidecars_verb_lists_sets_and_leftovers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    _touch(tmp_path, EPISODE, f"{STEM}.nfo", f"{STEM}.ttml", "leftover.txt")
    assert main(["sidecars", str(tmp_path), "--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    (only,) = document["sets"]
    assert Path(only["video"]).name == EPISODE
    assert {(Path(s["path"]).name, s["kind"]) for s in only["sidecars"]} == {
        (f"{STEM}.nfo", "nfo"), (f"{STEM}.ttml", "other"),
    }
    assert [Path(p).name for p in document["unclaimed"]] == ["leftover.txt"]

    assert main(["sidecars", str(tmp_path / EPISODE)]) == 0
    text = capsys.readouterr().out
    assert "not read by the server" in text and "nfo" in text
