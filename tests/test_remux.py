"""The rebuild: which tracks live, which die, and what the command looks like.

The planner is a pure function over a track table, so every rule it enforces
is tested directly -- including the ones that exist to stop a rebuild rather
than to shape one. The last two run the real muxer over a fixture.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from mkvkit.chapters.xml import Chapter, ChapterSet
from mkvkit.probe import probe_from_json
from mkvkit.remux import RemuxPolicy, build, command, plan
from mkvkit.run import CommandFailed, Result

SECOND = 1_000_000_000
SOURCE = "/srv/media/movies/example.mkv"
STAGING = "/srv/staging/example.mkv"

POLICY = RemuxPolicy(
    keep_languages=frozenset({"eng", "deu"}),
    droppable_languages=frozenset({"rus", "spa", "ita"}),
    default_audio="eng",
)


def identified(
    tracks: Sequence[tuple[str | None, str | None]],
    *,
    ietf: bool = False,
    default: int = 0,
    container: str = "Matroska",
) -> dict[str, Any]:
    """One video track, then one audio track per (language, name) pair."""
    out: list[dict[str, Any]] = [
        {"id": 0, "type": "video", "codec": "MPEG-4p2",
         "properties": {"codec_id": "V_MPEG4/ISO/ASP", "uid": 1, "number": 1}}
    ]
    for position, (language, name) in enumerate(tracks):
        properties: dict[str, Any] = {
            "codec_id": "A_FLAC", "uid": 10 + position, "number": position + 2,
            "audio_channels": 2, "default_track": position == default,
        }
        if language:
            properties["language"] = language
        if name:
            properties["track_name"] = name
        if ietf and language:
            properties["language_ietf"] = language[:2]
        out.append({"id": position + 1, "type": "audio", "codec": "FLAC",
                    "properties": properties})
    return {"container": {"type": container,
                          "properties": {"duration": 5_400_000_000_000}},
            "tracks": out}


def probed(tracks: Sequence[tuple[str | None, str | None]]) -> dict[str, Any]:
    streams: list[dict[str, Any]] = [
        {"index": 0, "codec_type": "video", "codec_name": "mpeg4"}
    ]
    for position, (language, _name) in enumerate(tracks):
        streams.append(
            {"index": position + 1, "codec_type": "audio", "codec_name": "flac",
             "tags": {"language": language} if language else {}}
        )
    return {"streams": streams, "format": {"duration": "5400.0", "size": "1000"}}


def probe_of(
    tracks: Sequence[tuple[str | None, str | None]], **kwargs: Any
) -> Any:
    return probe_from_json(
        SOURCE, identified(tracks, **kwargs), probed(tracks), elements=["Cues"]
    )


# ------------------------------------------------------------------ the rules
def test_a_droppable_dub_is_dropped_and_said_so() -> None:
    found = probe_of([("eng", None), ("deu", None), ("rus", None)])
    result = plan(found, POLICY, output=STAGING, original_language="eng")
    assert result.ok
    assert result.drop_audio == (3,)
    assert result.keep_audio == (1, 2)
    assert "rus is droppable" in str(result)


def test_nothing_is_dropped_that_is_not_on_the_list() -> None:
    found = probe_of([("eng", None), ("jpn", None)])
    result = plan(found, POLICY, output=STAGING, original_language="eng")
    assert result.drop_audio == ()
    assert "not on the droppable list" in str(result)


def test_the_original_language_is_never_dropped() -> None:
    """Even when the policy says the language is droppable."""
    found = probe_of([("eng", None), ("rus", None)])
    result = plan(found, POLICY, output=STAGING, original_language="rus")
    assert result.drop_audio == ()
    assert "it is the original language (rus)" in str(result)


def test_an_untagged_track_is_kept() -> None:
    """An unknown language is not an empty one."""
    found = probe_of([("eng", None), (None, None)])
    result = plan(found, POLICY, output=STAGING, original_language="eng")
    assert result.drop_audio == ()
    assert "no language is claimed" in str(result)


def test_a_commentary_track_is_kept_even_in_a_droppable_language() -> None:
    found = probe_of([("eng", None), ("spa", "Director commentary")])
    result = plan(found, POLICY, output=STAGING, original_language="eng")
    assert result.drop_audio == ()
    assert "commentary" in str(result)


def test_nothing_is_dropped_when_the_original_language_is_unknown() -> None:
    found = probe_of([("eng", None), ("rus", None)])
    result = plan(found, POLICY, output=STAGING, original_language=None)
    assert result.drop_audio == ()
    assert any("original language is not known" in note for note in result.notes)


def test_a_plan_that_would_empty_the_file_is_refused() -> None:
    everything = RemuxPolicy(droppable_languages=frozenset({"eng", "rus"}))
    found = probe_of([("eng", None), ("rus", None)])
    result = plan(found, everything, output=STAGING, original_language="jpn")
    assert not result.ok
    assert "every audio track would be dropped" in result.problems[0]


def test_a_plan_that_keeps_nothing_recognisable_is_refused() -> None:
    """The guard that catches a plan built from the wrong original language."""
    policy = RemuxPolicy(
        keep_languages=frozenset({"eng"}), droppable_languages=frozenset({"deu"})
    )
    found = probe_of([("deu", None), ("jpn", None)])
    result = plan(found, policy, output=STAGING, original_language="fra")
    assert not result.ok
    assert "policy language or the original language" in result.problems[0]


def test_writing_over_the_input_is_refused() -> None:
    found = probe_of([("eng", None), ("rus", None)])
    result = plan(found, POLICY, output=SOURCE, original_language="eng")
    assert not result.ok
    assert "the output is the input" in result.problems[0]


def test_a_rebuild_that_changes_nothing_is_not_worthwhile() -> None:
    found = probe_of([("eng", None), ("deu", None)])
    result = plan(found, POLICY, output=STAGING, original_language="eng")
    assert result.ok
    assert not result.worthwhile


def test_the_plan_declares_the_difference_the_verification_will_expect() -> None:
    found = probe_of([("eng", None), ("rus", None)])
    result = plan(found, POLICY, output=STAGING, original_language="eng")
    delta = result.expected_delta()
    assert delta.dropped == frozenset({2})


def test_the_default_moves_only_when_it_has_to() -> None:
    already = probe_of([("eng", None), ("deu", None)], default=0)
    assert plan(already, POLICY, output=STAGING, original_language="eng").set_default is None

    elsewhere = probe_of([("deu", None), ("eng", None)], default=0)
    moved = plan(elsewhere, POLICY, output=STAGING, original_language="eng")
    assert moved.set_default == 2


def test_a_policy_is_read_from_the_configuration() -> None:
    from mkvkit.config import Config, PolicyConfig

    config = Config(policy=PolicyConfig(keep_languages=("en", "ger"),
                                        droppable_languages=("rus",),
                                        default_audio="en"))
    policy = RemuxPolicy.from_config(config)
    assert policy.keep_languages == frozenset({"eng", "deu"})
    assert policy.droppable_languages == frozenset({"rus"})
    assert policy.default_audio == "eng"


def test_an_empty_policy_drops_nothing() -> None:
    """The default has to be the one that does not touch anybody's files."""
    found = probe_of([("eng", None), ("rus", None)])
    result = plan(found, RemuxPolicy(), output=STAGING, original_language="eng")
    assert result.drop_audio == ()


# ---------------------------------------------------------------- the command
def test_the_command_keeps_the_tracks_by_identifier() -> None:
    found = probe_of([("eng", None), ("deu", None), ("rus", None)])
    argv = command(plan(found, POLICY, output=STAGING, original_language="eng"))
    assert argv[:2] == ["-o", str(Path(STAGING))]
    assert "--audio-tracks" in argv
    assert argv[argv.index("--audio-tracks") + 1] == "1,2"
    assert argv[-1] == str(Path(SOURCE))


def test_a_chapter_document_is_always_paired_with_suppressing_the_old_marks() -> None:
    """Otherwise the document is added beside the file's own marks, not instead."""
    found = probe_of([("eng", None), ("rus", None)])
    marks = ChapterSet((Chapter(0, "The harbour at dawn"), Chapter(600 * SECOND)))
    argv = command(
        plan(found, POLICY, output=STAGING, original_language="eng", chapters=marks)
    )
    assert "--chapters" in argv
    assert "--no-chapters" in argv
    assert argv.index("--no-chapters") < argv.index(str(Path(SOURCE)))


def test_the_source_s_subtag_convention_is_kept() -> None:
    without = probe_of([("eng", None), ("rus", None)])
    assert "--disable-language-ietf" in command(
        plan(without, POLICY, output=STAGING, original_language="eng")
    )
    with_subtags = probe_of([("eng", None), ("rus", None)], ietf=True)
    assert "--disable-language-ietf" not in command(
        plan(with_subtags, POLICY, output=STAGING, original_language="eng")
    )


def test_a_moved_default_is_in_the_command() -> None:
    found = probe_of([("deu", None), ("eng", None), ("rus", None)], default=0)
    argv = command(plan(found, POLICY, output=STAGING, original_language="deu"))
    flags = [argv[i + 1] for i, a in enumerate(argv) if a == "--default-track-flag"]
    assert "2:1" in flags


def test_moving_the_default_clears_it_on_every_other_kept_track() -> None:
    """Otherwise the old default keeps its flag and the file has two."""
    found = probe_of([("deu", None), ("eng", None), ("fra", None)], default=0)
    moved = plan(found, POLICY, output=STAGING, original_language="deu")
    assert moved.set_default == 2
    argv = command(moved)
    flags = [argv[i + 1] for i, a in enumerate(argv) if a == "--default-track-flag"]
    assert sorted(flags) == ["1:0", "2:1", "3:0"]


def test_a_plan_that_moves_the_default_asks_for_exactly_one() -> None:
    found = probe_of([("deu", None), ("eng", None)], default=0)
    delta = plan(found, POLICY, output=STAGING, original_language="deu").expected_delta()
    assert delta.default_moved
    assert delta.one_default_audio


def test_a_chapter_document_that_fails_its_own_check_blocks_the_plan() -> None:
    found = probe_of([("eng", None), ("rus", None)])
    past_the_end = ChapterSet((Chapter(0), Chapter(9_000 * SECOND)))
    result = plan(
        found, POLICY, output=STAGING, original_language="eng", chapters=past_the_end
    )
    assert not result.ok
    assert "past-the-end" in result.problems[0]


# ------------------------------------------------------------------ the build
class StandIn:
    def __init__(self, *, writes: Path | None = None, returncode: int = 0) -> None:
        self.writes = writes
        self.returncode = returncode
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self, tool: str, args: Sequence[str | Path], *, ok: Sequence[int] = (0,)
    ) -> Result:
        argv = tuple(str(a) for a in args)
        self.calls.append(argv)
        if self.writes is not None:
            self.writes.write_bytes(b"a rebuilt file")
        return Result(tool, argv, self.returncode, "", "warning: a minor thing")


def test_a_dry_run_builds_nothing(tmp_path: Path) -> None:
    found = probe_of([("eng", None), ("rus", None)])
    output = tmp_path / "example.mkv"
    runner = StandIn()
    result = build(plan(found, POLICY, output=output, original_language="eng"),
                   runner=runner)
    assert not result.applied
    assert runner.calls == []
    assert not output.exists()
    assert result.command


def test_a_build_writes_a_part_file_and_renames_it(tmp_path: Path) -> None:
    """An interrupted run must not leave something that looks finished."""
    output = tmp_path / "example.mkv"
    part = tmp_path / "example.mkv.part"
    runner = StandIn(writes=part)
    found = probe_of([("eng", None), ("rus", None)])
    result = build(
        plan(found, POLICY, output=output, original_language="eng"),
        dry_run=False, runner=runner,
    )
    assert result.applied
    assert output.is_file()
    assert not part.exists()
    assert str(part) in runner.calls[0]


def test_a_build_that_produced_nothing_is_a_problem(tmp_path: Path) -> None:
    output = tmp_path / "example.mkv"
    found = probe_of([("eng", None), ("rus", None)])
    result = build(
        plan(found, POLICY, output=output, original_language="eng"),
        dry_run=False, runner=StandIn(),
    )
    assert not result.ok
    assert "produced nothing" in result.problems[0]


def test_a_blocked_plan_is_never_run(tmp_path: Path) -> None:
    runner = StandIn()
    found = probe_of([("eng", None), ("rus", None)])
    blocked = plan(found, POLICY, output=SOURCE, original_language="eng")
    result = build(blocked, dry_run=False, runner=runner)
    assert not result.ok
    assert runner.calls == []


@pytest.mark.parametrize("occupant", ["example.mkv", "example.mkv.part"])
def test_a_file_already_in_staging_is_refused_not_replaced(
    tmp_path: Path, occupant: str
) -> None:
    output = tmp_path / "example.mkv"
    there = tmp_path / occupant
    there.write_bytes(b"an earlier rebuild somebody may still need")
    runner = StandIn(writes=tmp_path / "example.mkv.part")
    found = probe_of([("eng", None), ("rus", None)])
    result = build(
        plan(found, POLICY, output=output, original_language="eng"),
        dry_run=False, runner=runner,
    )
    assert not result.ok
    assert "already exists" in result.problems[0]
    assert runner.calls == []
    assert there.read_bytes() == b"an earlier rebuild somebody may still need"


def test_a_chapter_document_already_in_staging_is_refused(tmp_path: Path) -> None:
    output = tmp_path / "example.mkv"
    document = tmp_path / "example.mkv.chapters.xml"
    document.write_text("somebody's own document", encoding="utf-8")
    runner = StandIn()
    found = probe_of([("eng", None), ("rus", None)])
    marks = ChapterSet((Chapter(0, "The harbour at dawn"), Chapter(600 * SECOND)))
    result = build(
        plan(found, POLICY, output=output, original_language="eng", chapters=marks),
        dry_run=False, runner=runner,
    )
    assert not result.ok
    assert runner.calls == []
    assert document.read_text(encoding="utf-8") == "somebody's own document"


class Failing(StandIn):
    """Writes half a part-file and then fails, the way an interrupted mux does."""

    def __call__(
        self, tool: str, args: Sequence[str | Path], *, ok: Sequence[int] = (0,)
    ) -> Result:
        super().__call__(tool, args, ok=ok)
        raise CommandFailed(Result(tool, tuple(str(a) for a in args), 2, "", "error"))


def test_a_failed_build_leaves_no_part_file_and_no_chapter_document(
    tmp_path: Path,
) -> None:
    output = tmp_path / "example.mkv"
    part = tmp_path / "example.mkv.part"
    found = probe_of([("eng", None), ("rus", None)])
    marks = ChapterSet((Chapter(0, "The harbour at dawn"), Chapter(600 * SECOND)))
    with pytest.raises(CommandFailed):
        build(
            plan(found, POLICY, output=output, original_language="eng", chapters=marks),
            dry_run=False, runner=Failing(writes=part),
        )
    assert sorted(p.name for p in tmp_path.iterdir()) == []


def test_a_warning_with_no_text_is_still_a_note(tmp_path: Path) -> None:
    output = tmp_path / "example.mkv"
    part = tmp_path / "example.mkv.part"

    class Quiet(StandIn):
        def __call__(
            self, tool: str, args: Sequence[str | Path], *, ok: Sequence[int] = (0,)
        ) -> Result:
            part.write_bytes(b"a rebuilt file")
            return Result(tool, tuple(str(a) for a in args), 1, "", "")

    found = probe_of([("eng", None), ("rus", None)])
    result = build(
        plan(found, POLICY, output=output, original_language="eng"),
        dry_run=False, runner=Quiet(),
    )
    assert result.applied
    assert any("gave no message" in note for note in result.notes)


# ------------------------------------------------------------- against the files
@pytest.mark.needs_ffmpeg
@pytest.mark.needs_mkvtoolnix
def test_a_real_rebuild_drops_the_track_and_verifies_against_the_plan(
    media_fixtures: dict[str, Path], tmp_path: Path
) -> None:
    from mkvkit.probe import probe
    from mkvkit.verify import collect, compare

    source = media_fixtures["tiny_multitrack.mkv"]
    found = probe(source)
    policy = RemuxPolicy(
        keep_languages=frozenset({"eng"}), droppable_languages=frozenset({"fra"})
    )
    staged = tmp_path / source.name
    remux = plan(found, policy, output=staged, original_language="eng")
    assert remux.ok, remux.problems
    assert remux.worthwhile
    result = build(remux, dry_run=False)
    assert result.ok, result.problems
    assert staged.is_file()

    comparison = compare(collect(source), collect(staged), remux.expected_delta())
    assert comparison.ok, comparison.problems
    assert comparison.hashed_streams == 4


@pytest.mark.needs_ffmpeg
@pytest.mark.needs_mkvtoolnix
def test_a_real_rebuild_with_chapters_does_not_end_up_with_two_sets(
    media_fixtures: dict[str, Path], tmp_path: Path
) -> None:
    from mkvkit.probe import probe

    source = media_fixtures["chapter_grid.mkv"]
    found = probe(source)
    assert found.chapter_count == 12
    marks = ChapterSet(
        (Chapter(0, "The harbour at dawn"), Chapter(2 * SECOND, "Out on the water"))
    )
    staged = tmp_path / source.name
    remux = plan(
        found, RemuxPolicy(), output=staged, original_language="eng", chapters=marks
    )
    assert remux.ok, remux.problems
    assert build(remux, dry_run=False).ok
    rebuilt = probe(staged)
    assert rebuilt.chapter_count == 2
    assert rebuilt.edition_count == 1


@pytest.mark.needs_ffmpeg
@pytest.mark.needs_mkvtoolnix
def test_a_real_rebuild_that_moves_the_default_leaves_exactly_one(
    media_fixtures: dict[str, Path], tmp_path: Path
) -> None:
    from mkvkit.probe import probe
    from mkvkit.verify import collect, compare

    source = media_fixtures["tiny_multitrack.mkv"]
    found = probe(source)
    assert found.audio[0].default is True
    policy = RemuxPolicy(
        keep_languages=frozenset({"eng", "deu"}),
        droppable_languages=frozenset({"fra"}),
        default_audio="deu",
    )
    staged = tmp_path / source.name
    remux = plan(found, policy, output=staged, original_language="eng")
    assert remux.set_default is not None
    assert build(remux, dry_run=False).ok
    rebuilt = probe(staged)
    assert [t.effective_language for t in rebuilt.audio if t.default] == ["deu"]
    comparison = compare(collect(source), collect(staged), remux.expected_delta())
    assert comparison.ok, comparison.problems
