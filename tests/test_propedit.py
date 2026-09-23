"""The in-place editor: what it refuses, what it writes, what it proves afterwards.

The first half drives the module with a stand-in for the programs, so the
refusals, the command and the comparison are tested in milliseconds. The
second half edits real copies of the generated fixtures, which is the only way
to find out what the editor actually does to a file.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import json
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from mkvkit import tags as tags_module
from mkvkit.chapters.xml import Chapter, ChapterSet
from mkvkit.probe import probe
from mkvkit.propedit import TrackEdit, safe_propedit
from mkvkit.run import Result

SECOND = 1_000_000_000
FIRST_UID = 2222
SECOND_UID = 3333


def identified(
    *,
    container: str = "Matroska",
    language: str = "eng",
    tag_language: str | None = None,
    name: str | None = None,
    default: bool = True,
    chapters: int = 0,
) -> dict[str, Any]:
    first: dict[str, Any] = {
        "codec_id": "A_FLAC", "number": 1, "uid": FIRST_UID, "language": language,
        "default_track": default, "enabled_track": True, "audio_channels": 2,
    }
    if tag_language:
        first["tag_language"] = tag_language
    if name:
        first["track_name"] = name
    return {
        "container": {"type": container, "properties": {"duration": 5_000_000_000}},
        "tracks": [
            {"id": 0, "type": "audio", "codec": "FLAC", "properties": first},
            {
                "id": 1, "type": "audio", "codec": "FLAC",
                "properties": {
                    "codec_id": "A_FLAC", "number": 2, "uid": SECOND_UID,
                    "language": "deu", "enabled_track": True, "audio_channels": 2,
                },
            },
        ],
        "chapters": [{"num_entries": chapters}] if chapters else [],
    }


def probed() -> dict[str, Any]:
    return {
        "streams": [
            {"index": 0, "codec_type": "audio", "codec_name": "flac", "channels": 2},
            {"index": 1, "codec_type": "audio", "codec_name": "flac", "channels": 2},
        ],
        "format": {"duration": "5.000000", "size": "1024"},
    }


class StandIn:
    """Answers for each program, in the order the module asks for them."""

    def __init__(
        self,
        identify: Sequence[dict[str, Any]],
        *,
        extract: Sequence[str] = ("", ""),
        returncode: int = 0,
    ) -> None:
        self.identify = [json.dumps(answer) for answer in identify]
        self.extract = list(extract)
        self.returncode = returncode
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def __call__(
        self, tool: str, args: Sequence[str | Path], *, ok: Sequence[int] = (0,)
    ) -> Result:
        argv = tuple(str(a) for a in args)
        self.calls.append((tool, argv))
        if tool == "mkvmerge":
            answer = self.identify[0] if len(self.identify) == 1 else self.identify.pop(0)
            return Result(tool, argv, 0, answer, "")
        if tool == "ffprobe":
            return Result(tool, argv, 0, json.dumps(probed()), "")
        if tool == "mkvextract":
            return Result(tool, argv, 0, self.extract.pop(0) if self.extract else "", "")
        return Result(tool, argv, self.returncode, "", "warning: something minor")

    @property
    def edits(self) -> list[tuple[str, ...]]:
        return [argv for tool, argv in self.calls if tool == "mkvpropedit"]


def target(tmp_path: Path) -> Path:
    path = tmp_path / "example.mkv"
    path.write_bytes(b"not read: the programs are stood in for")
    return path


# ------------------------------------------------------------------- refusals
def test_a_file_that_is_not_really_matroska_is_refused(tmp_path: Path) -> None:
    runner = StandIn([identified(container="QuickTime/MP4")])
    result = safe_propedit(
        target(tmp_path), [TrackEdit(FIRST_UID, language="deu")], runner=runner
    )
    assert not result.ok
    assert "rebuilt" in result.problems[0]
    assert runner.edits == []


def test_an_identifier_no_track_carries_is_refused(tmp_path: Path) -> None:
    runner = StandIn([identified()])
    result = safe_propedit(target(tmp_path), [TrackEdit(999, language="deu")],
                           runner=runner)
    assert "no track in this file carries the identifier 999" in result.problems[0]
    assert runner.edits == []


def test_the_same_track_twice_in_one_call_is_refused(tmp_path: Path) -> None:
    runner = StandIn([identified()])
    result = safe_propedit(
        target(tmp_path),
        [TrackEdit(FIRST_UID, language="deu"), TrackEdit(FIRST_UID, forced=True)],
        runner=runner,
    )
    assert any("edited twice" in p for p in result.problems)


def test_an_empty_call_is_refused_rather_than_run(tmp_path: Path) -> None:
    runner = StandIn([identified()])
    result = safe_propedit(target(tmp_path), [], runner=runner)
    assert "nothing to do" in result.problems[0]


def test_a_language_a_tag_would_overrule_is_refused(tmp_path: Path) -> None:
    """The edit would succeed, and the file would still read the old way."""
    runner = StandIn([identified(language="eng", tag_language="fra")])
    result = safe_propedit(
        target(tmp_path), [TrackEdit(FIRST_UID, language="deu")], runner=runner
    )
    assert not result.ok
    assert "would go on overruling" in result.problems[0]
    assert runner.edits == []


def test_the_same_edit_is_allowed_when_the_tag_is_rewritten_with_it(
    tmp_path: Path,
) -> None:
    runner = StandIn([identified(language="eng", tag_language="fra")])
    tags = tags_module.TagSet(
        (
            tags_module.Tag(
                targets=tags_module.Targets(track_uids=(FIRST_UID,)),
                simples=(tags_module.SimpleTag("LANGUAGE", "deu"),),
            ),
        )
    )
    result = safe_propedit(
        target(tmp_path), [TrackEdit(FIRST_UID, language="deu")], tags=tags,
        runner=runner,
    )
    assert result.ok
    assert result.command


# --------------------------------------------------------------------- dry run
def test_a_dry_run_writes_nothing_and_shows_the_command(tmp_path: Path) -> None:
    runner = StandIn([identified()])
    result = safe_propedit(
        target(tmp_path), [TrackEdit(SECOND_UID, default=True, forced=False)],
        runner=runner,
    )
    assert result.applied is False
    assert runner.edits == []
    assert "--edit" in result.command
    assert f"track:={SECOND_UID}" in result.command
    assert "flag-default=1" in result.command
    assert "flag-forced=0" in result.command
    assert result.notes == ("dry run: nothing was written",)


def test_the_dry_run_still_produces_the_rollback(tmp_path: Path) -> None:
    """It is captured from the file's state, so it cannot be made afterwards."""
    runner = StandIn([identified(language="eng", name="A name", default=True)])
    result = safe_propedit(
        target(tmp_path),
        [TrackEdit(FIRST_UID, language="deu", name="Another", default=False)],
        runner=runner,
    )
    assert result.rollback is not None
    rows = result.rollback.to_tsv().splitlines()
    assert f"{FIRST_UID}\tlanguage\teng" in "\n".join(rows)
    assert f"{FIRST_UID}\tname\tA name" in "\n".join(rows)
    assert f"{FIRST_UID}\tflag-default\t1" in "\n".join(rows)


def test_every_change_goes_into_one_invocation(tmp_path: Path) -> None:
    """Each call bumps the modification time, and that bump is expensive."""
    runner = StandIn([identified()])
    result = safe_propedit(
        target(tmp_path),
        [TrackEdit(FIRST_UID, language="deu"), TrackEdit(SECOND_UID, forced=True)],
        title="An example title",
        chapters=ChapterSet((Chapter(0, "A name"), Chapter(SECOND))),
        runner=runner,
    )
    assert result.command.count("--edit") == 3  # the file, and two tracks
    assert "--chapters" in result.command


# ------------------------------------------------------------------ comparison
def test_an_applied_edit_is_checked_against_the_file_afterwards(
    tmp_path: Path,
) -> None:
    runner = StandIn([identified(language="eng"), identified(language="deu")])
    result = safe_propedit(
        target(tmp_path), [TrackEdit(FIRST_UID, language="deu")],
        dry_run=False, runner=runner,
    )
    assert result.applied
    assert result.ok, result.problems
    assert len(runner.edits) == 1


def test_an_edit_that_did_not_take_is_a_problem(tmp_path: Path) -> None:
    """The editor exits zero on plenty of things that changed nothing."""
    runner = StandIn([identified(language="eng"), identified(language="eng")])
    result = safe_propedit(
        target(tmp_path), [TrackEdit(FIRST_UID, language="deu")],
        dry_run=False, runner=runner,
    )
    assert not result.ok
    assert "was set to 'deu' and reads 'eng'" in result.problems[0]


def test_a_change_nobody_asked_for_is_a_problem(tmp_path: Path) -> None:
    before = identified(language="eng", default=True)
    after = identified(language="deu", default=False)
    runner = StandIn([before, after])
    result = safe_propedit(
        target(tmp_path), [TrackEdit(FIRST_UID, language="deu")],
        dry_run=False, runner=runner,
    )
    assert any("without being asked to" in p for p in result.problems)


def test_chapters_that_move_on_their_own_are_a_problem(tmp_path: Path) -> None:
    before = identified(language="eng", chapters=16)
    after = identified(language="deu", chapters=0)
    runner = StandIn([before, after])
    result = safe_propedit(
        target(tmp_path), [TrackEdit(FIRST_UID, language="deu")],
        dry_run=False, runner=runner,
    )
    assert any("chapters changed without being asked" in p for p in result.problems)


def test_a_track_that_vanished_is_a_problem(tmp_path: Path) -> None:
    after = identified(language="deu")
    after["tracks"] = after["tracks"][:1]
    runner = StandIn([identified(language="eng"), after])
    result = safe_propedit(
        target(tmp_path), [TrackEdit(FIRST_UID, language="deu")],
        dry_run=False, runner=runner,
    )
    assert "the track count changed: 2 -> 1" in result.problems[0]


def test_a_warning_from_the_editor_is_a_note_and_not_a_failure(
    tmp_path: Path,
) -> None:
    runner = StandIn(
        [identified(language="eng"), identified(language="deu")], returncode=1
    )
    result = safe_propedit(
        target(tmp_path), [TrackEdit(FIRST_UID, language="deu")],
        dry_run=False, runner=runner,
    )
    assert result.ok
    assert any("warned" in note for note in result.notes)


# -------------------------------------------------------------- against the files
@pytest.mark.needs_ffmpeg
@pytest.mark.needs_mkvtoolnix
def test_a_real_language_edit_changes_that_and_nothing_else(
    media_fixtures: dict[str, Path], tmp_path: Path
) -> None:
    path = tmp_path / "example.mkv"
    shutil.copyfile(media_fixtures["tiny_multitrack.mkv"], path)
    before = probe(path)
    uid = before.audio[2].uid
    assert uid is not None
    result = safe_propedit(path, [TrackEdit(uid, language="spa")], dry_run=False)
    assert result.ok, result.problems
    after = probe(path)
    assert after.track_by_uid(uid) is not None
    assert after.track_by_uid(uid).language == "spa"  # type: ignore[union-attr]
    assert [t.language for t in after.audio][:2] == ["eng", "deu"]
    assert after.has_cues is True


@pytest.mark.needs_ffmpeg
@pytest.mark.needs_mkvtoolnix
def test_a_real_flag_edit_moves_the_default(
    media_fixtures: dict[str, Path], tmp_path: Path
) -> None:
    path = tmp_path / "example.mkv"
    shutil.copyfile(media_fixtures["tiny_multitrack.mkv"], path)
    before = probe(path)
    first, second = before.audio[0], before.audio[1]
    assert first.uid is not None and second.uid is not None
    result = safe_propedit(
        path,
        [TrackEdit(first.uid, default=False), TrackEdit(second.uid, default=True)],
        dry_run=False,
    )
    assert result.ok, result.problems
    after = probe(path)
    assert [t.default for t in after.audio][:2] == [False, True]


@pytest.mark.needs_ffmpeg
@pytest.mark.needs_mkvtoolnix
def test_writing_chapters_replaces_them_and_leaves_the_tracks_alone(
    media_fixtures: dict[str, Path], tmp_path: Path
) -> None:
    path = tmp_path / "example.mkv"
    shutil.copyfile(media_fixtures["chapter_grid.mkv"], path)
    before = probe(path)
    marks = ChapterSet(
        (Chapter(0, "The harbour at dawn"), Chapter(2 * SECOND, "Out on the water"))
    )
    result = safe_propedit(path, chapters=marks, dry_run=False)
    assert result.ok, result.problems
    after = probe(path)
    assert before.chapter_count == 12
    assert after.chapter_count == 2
    assert [t.language for t in after.tracks] == [t.language for t in before.tracks]


@pytest.mark.needs_ffmpeg
@pytest.mark.needs_mkvtoolnix
def test_a_real_tag_write_survives_the_editor_s_normalisation(
    media_fixtures: dict[str, Path], tmp_path: Path
) -> None:
    """The comparison has to pass on a file the editor has rewritten its own way."""
    path = tmp_path / "example.mkv"
    shutil.copyfile(media_fixtures["tiny_multitrack.mkv"], path)
    existing = tags_module.read_tags(path)
    merged = tags_module.merge(
        existing, [tags_module.provenance("chapter names", "an example source")]
    )
    result = safe_propedit(path, tags=merged, dry_run=False)
    assert result.ok, result.problems
    written = tags_module.read_tags(path)
    assert ("", "CHAPTER_NAMES_SOURCE", "an example source") in written.triples


@pytest.mark.needs_ffmpeg
@pytest.mark.needs_mkvtoolnix
def test_the_renamed_container_is_refused_against_the_real_file(
    media_fixtures: dict[str, Path], tmp_path: Path
) -> None:
    path = tmp_path / "example.mkv"
    shutil.copyfile(media_fixtures["not_really_mkv.mkv"], path)
    found = probe(path)
    result = safe_propedit(path, [TrackEdit(1, language="deu")], dry_run=False)
    assert not result.ok
    assert not found.container.is_matroska


@pytest.mark.needs_ffmpeg
@pytest.mark.needs_mkvtoolnix
def test_the_fixture_with_a_contradicting_tag_refuses_a_header_only_edit(
    media_fixtures: dict[str, Path], tmp_path: Path
) -> None:
    path = tmp_path / "example.mkv"
    shutil.copyfile(media_fixtures["tag_override.mkv"], path)
    found = probe(path)
    uid = found.audio[0].uid
    assert uid is not None
    result = safe_propedit(path, [TrackEdit(uid, language="deu")], dry_run=False)
    assert not result.ok
    assert "overruling" in result.problems[0]


@pytest.mark.needs_ffmpeg
@pytest.mark.needs_mkvtoolnix
def test_both_halves_of_a_language_change_in_one_call(
    media_fixtures: dict[str, Path], tmp_path: Path
) -> None:
    """The header and the tag, together, or the file still reads the old way."""
    path = tmp_path / "example.mkv"
    shutil.copyfile(media_fixtures["tag_override.mkv"], path)
    found = probe(path)
    uid = found.audio[0].uid
    assert uid is not None
    fixed = tags_module.set_track_language(tags_module.read_tags(path), uid, "spa")
    result = safe_propedit(
        path, [TrackEdit(uid, language="spa")], tags=fixed, dry_run=False
    )
    assert result.ok, result.problems
    after = probe(path)
    track = after.track_by_uid(uid)
    assert track is not None
    assert track.language == "spa"
    assert track.tag_language == "spa"
    assert after.language_disagreements == ()
