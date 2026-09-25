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
from mkvkit.propedit import TrackEdit, default_rollback_dir, safe_propedit
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


# -------------------------------------------------------------------- rollback
class Watching(StandIn):
    """Records, at the moment the editor runs, what the rollback directory holds."""

    def __init__(self, *args: Any, directory: Path, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.directory = directory
        self.present_at_edit: list[str] = []

    def __call__(
        self, tool: str, args: Sequence[str | Path], *, ok: Sequence[int] = (0,)
    ) -> Result:
        if tool == "mkvpropedit":
            self.present_at_edit = sorted(p.name for p in self.directory.iterdir())
        return super().__call__(tool, args, ok=ok)


def test_an_applied_edit_writes_its_rollback_before_it_edits(tmp_path: Path) -> None:
    directory = tmp_path / "rollback"
    runner = Watching(
        [identified(language="eng", name="A name"), identified(language="deu", name="A name")],
        extract=["<Chapters><EditionEntry/></Chapters>"],
        directory=directory,
    )
    result = safe_propedit(
        target(tmp_path), [TrackEdit(FIRST_UID, language="deu")],
        chapters=ChapterSet((Chapter(0, "A name"), Chapter(SECOND))),
        dry_run=False, runner=runner, rollback_dir=directory,
    )
    assert result.applied
    assert any(name.endswith(".rollback.tsv") for name in runner.present_at_edit)
    assert any(name.endswith(".chapters.xml") for name in runner.present_at_edit)
    tsv = next(directory.glob("*.rollback.tsv")).read_text(encoding="utf-8")
    assert f"{FIRST_UID}\tlanguage\teng" in tsv
    assert any("rollback written to" in note for note in result.notes)


def test_an_edit_whose_rollback_cannot_be_written_does_not_happen(
    tmp_path: Path,
) -> None:
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("a file where the directory would go", encoding="utf-8")
    runner = StandIn([identified(language="eng")])
    with pytest.raises(OSError):
        safe_propedit(
            target(tmp_path), [TrackEdit(FIRST_UID, language="deu")],
            dry_run=False, runner=runner, rollback_dir=blocked,
        )
    assert runner.edits == []


def test_a_rollback_is_never_written_over_an_earlier_one(tmp_path: Path) -> None:
    import datetime as dt

    from mkvkit.propedit import Rollback

    rollback = Rollback(tmp_path / "example.mkv", rows=((FIRST_UID, "name", "Old"),))
    moment = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC)
    first = rollback.write(tmp_path / "rb", now=moment)
    before = first.tsv.read_bytes()
    with pytest.raises(FileExistsError):
        rollback.write(tmp_path / "rb", now=moment)
    assert first.tsv.read_bytes() == before


def test_the_restore_command_deletes_what_the_file_did_not_have(tmp_path: Path) -> None:
    from mkvkit.propedit import Rollback

    rollback = Rollback(
        tmp_path / "example.mkv",
        rows=((FIRST_UID, "name", ""), (FIRST_UID, "language", "eng")),
        chapters_xml="",
    )
    argv = rollback.restore_command()
    assert argv[1:] == [
        "--edit", f"track:={FIRST_UID}", "--delete", "name",
        "--edit", f"track:={FIRST_UID}", "--set", "language=eng",
        "--chapters", "",
    ]


def test_the_default_rollback_location_is_under_the_work_directory() -> None:
    from mkvkit.config import Config, PathsConfig

    config = Config(paths=PathsConfig(work=Path("/srv/work")))
    assert default_rollback_dir(config) == Path("/srv/work") / "rollback"


def test_a_tag_document_that_would_lose_elements_is_refused_before_writing(
    tmp_path: Path,
) -> None:
    runner = StandIn([identified()])
    odd = tags_module.TagSet(unkept=("Simple/Something",))
    result = safe_propedit(
        target(tmp_path), tags=odd, dry_run=False, runner=runner,
        rollback_dir=tmp_path / "rb",
    )
    assert not result.ok
    assert "Simple/Something" in result.problems[0]
    assert runner.edits == []
    assert not (tmp_path / "rb").exists()


def test_a_chapter_document_that_would_lose_structure_is_refused(tmp_path: Path) -> None:
    runner = StandIn([identified()])
    odd = ChapterSet((Chapter(0),), unkept=("mark 1 has 1 mark(s) nested under it",))
    result = safe_propedit(
        target(tmp_path), chapters=odd, dry_run=False, runner=runner,
        rollback_dir=tmp_path / "rb",
    )
    assert not result.ok
    assert runner.edits == []


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


@pytest.mark.needs_ffmpeg
@pytest.mark.needs_mkvtoolnix
def test_a_written_rollback_puts_the_real_file_back(
    media_fixtures: dict[str, Path], tmp_path: Path
) -> None:
    """Edit a track, the marks and the tags; then apply the artefact and compare."""
    from mkvkit.chapters.xml import read_chapters
    from mkvkit.run import default_runner

    path = tmp_path / "example.mkv"
    shutil.copyfile(media_fixtures["chapter_grid.mkv"], path)
    before = probe(path)
    before_marks = read_chapters(path)
    before_tags = tags_module.read_tags(path)
    track = before.audio[0]
    assert track.uid is not None
    merged = tags_module.merge(
        before_tags, [tags_module.provenance("chapter names", "an example source")]
    )
    directory = tmp_path / "rollback"
    result = safe_propedit(
        path, [TrackEdit(track.uid, language="spa", name="Another name")],
        chapters=ChapterSet((Chapter(0, "The harbour at dawn"), Chapter(2 * SECOND))),
        tags=merged, dry_run=False, rollback_dir=directory,
    )
    assert result.ok, result.problems
    assert result.rollback is not None
    assert probe(path).chapter_count == 2

    files = sorted(directory.iterdir())
    tsv = next(p for p in files if p.name.endswith(".rollback.tsv"))
    chapters = next(p for p in files if p.name.endswith(".chapters.xml"))
    tags = next((p for p in files if p.name.endswith(".tags.xml")), None)
    from mkvkit.propedit import RollbackFiles

    restore = result.rollback.restore_command(RollbackFiles(tsv, chapters, tags))
    default_runner()("mkvpropedit", restore, ok=(0, 1))

    after = probe(path)
    restored = after.track_by_uid(track.uid)
    assert restored is not None
    assert (restored.language, restored.name) == (track.language, track.name)
    # The editor gives an edition without an identifier one of its own on
    # write; the marks themselves -- times, names, flags -- are what came back.
    def marks(found: ChapterSet) -> list[tuple[object, ...]]:
        return [(c.start_ns, c.end_ns, c.name, c.hidden, c.enabled) for c in found]

    assert marks(read_chapters(path)) == marks(before_marks)
    assert tags_module.read_tags(path).triples == before_tags.triples
