"""Verification: the evidence, the declared expectation, and the verdict.

The comparison is a pure function over two pieces of evidence, so most of
this runs with no programs and no files -- including the cases that are hard
to produce on purpose, like a payload that changed when it should not have.
The last few read the generated fixtures.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import shutil
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from mkvkit.chapters.xml import Chapter, ChapterSet
from mkvkit.probe import probe_from_json
from mkvkit.run import Result
from mkvkit.tags import SimpleTag, Tag, TagSet, Targets
from mkvkit.verify import (
    Evidence,
    ExpectedDelta,
    HeaderOnly,
    StreamHash,
    TracksAppended,
    TracksDropped,
    collect,
    compare,
    frames_identical,
    reheader,
    stream_hashes,
)

SECOND = 1_000_000_000


def identified(
    *,
    languages: Sequence[str] = ("eng", "deu", "fra"),
    ietf: Sequence[str | None] = (),
    uids: Sequence[int] = (11, 22, 33),
    duration_ns: int = 5_000_000_000,
    title: str | None = None,
    chapters: int = 0,
) -> dict[str, Any]:
    tracks: list[dict[str, Any]] = [
        {
            "id": 0, "type": "video", "codec": "MPEG-4p2",
            "properties": {"codec_id": "V_MPEG4/ISO/ASP", "number": 1, "uid": 1,
                           "pixel_dimensions": "160x120", "enabled_track": True},
        }
    ]
    for position, language in enumerate(languages):
        properties: dict[str, Any] = {
            "codec_id": "A_FLAC", "number": position + 2, "uid": uids[position],
            "language": language, "audio_channels": 2,
            "audio_sampling_frequency": 48000, "enabled_track": True,
            "default_track": position == 0,
        }
        subtag = ietf[position] if position < len(ietf) else None
        if subtag:
            properties["language_ietf"] = subtag
        tracks.append(
            {"id": position + 1, "type": "audio", "codec": "FLAC",
             "properties": properties}
        )
    return {
        "container": {
            "type": "Matroska",
            "properties": {"duration": duration_ns, **({"title": title} if title else {})},
        },
        "tracks": tracks,
        "chapters": [{"num_entries": chapters}] if chapters else [],
    }


def probed(
    *,
    languages: Sequence[str] = ("eng", "deu", "fra"),
    audio_start_s: Sequence[float] = (0.0, 0.0, 0.0),
    duration_s: float = 5.0,
) -> dict[str, Any]:
    streams: list[dict[str, Any]] = [
        {"index": 0, "codec_type": "video", "codec_name": "mpeg4",
         "start_time": "0.000000", "duration": f"{duration_s:.6f}"}
    ]
    for position, language in enumerate(languages):
        streams.append(
            {
                "index": position + 1, "codec_type": "audio", "codec_name": "flac",
                "channels": 2, "start_time": f"{audio_start_s[position]:.6f}",
                "duration": f"{duration_s:.6f}", "tags": {"language": language},
            }
        )
    return {"streams": streams, "format": {"duration": f"{duration_s:.6f}",
                                           "size": "100000"}}


def evidence(
    *,
    identify: dict[str, Any] | None = None,
    probe_json: dict[str, Any] | None = None,
    digests: Sequence[str] = ("v-one", "a-one", "a-two", "a-three"),
    elements: Sequence[str] = ("Tracks", "Cues"),
    chapters: ChapterSet | None = None,
    tags: TagSet | None = None,
    name: str = "example.mkv",
) -> Evidence:
    found = probe_from_json(
        f"/srv/media/movies/{name}",
        identify if identify is not None else identified(),
        probe_json if probe_json is not None else probed(),
        elements=elements,
    )
    hashes = tuple(
        StreamHash(index, "v" if index == 0 else "a", "MD5", digest)
        for index, digest in enumerate(digests)
    )
    return Evidence(
        path=Path(f"/srv/media/movies/{name}"),
        probe=found,
        hashes=hashes,
        chapters=chapters if chapters is not None else ChapterSet(),
        tags=tags if tags is not None else TagSet(),
    )


# ------------------------------------------------------------------- the basics
def test_a_file_against_itself_passes() -> None:
    left = evidence()
    assert compare(left, evidence()).ok


def test_a_payload_that_changed_is_located_by_position() -> None:
    built = evidence(digests=("v-one", "a-one", "SOMETHING-ELSE", "a-three"))
    result = compare(evidence(), built)
    assert not result.ok
    assert "position 2" in result.problems[0]
    assert "original stream 2" in result.problems[0]


def test_a_video_payload_difference_says_what_to_check_next() -> None:
    """A container that re-states its parameter sets changes bytes, not pictures."""
    built = evidence(digests=("SOMETHING-ELSE", "a-one", "a-two", "a-three"))
    result = compare(evidence(), built)
    assert "frame hashes" in result.problems[0]


def test_the_number_of_streams_compared_is_reported() -> None:
    assert compare(evidence(), evidence()).hashed_streams == 4


# ------------------------------------------------------------ dropping a track
def dropped_built(**kwargs: Any) -> Evidence:
    return evidence(
        identify=identified(languages=("eng", "deu"), uids=(11, 22)),
        probe_json=probed(languages=("eng", "deu")),
        digests=("v-one", "a-one", "a-two"),
        **kwargs,
    )


def test_a_dropped_track_passes_when_it_was_declared() -> None:
    result = compare(evidence(), dropped_built(), TracksDropped(dropped=frozenset({3})))
    assert result.ok, result.problems
    assert result.hashed_streams == 3


def test_a_dropped_track_fails_when_it_was_not_declared() -> None:
    result = compare(evidence(), dropped_built())
    assert not result.ok


def test_dropping_the_wrong_track_is_caught_by_the_hashes() -> None:
    """The count is right, the content is not."""
    built = evidence(
        identify=identified(languages=("eng", "fra"), uids=(11, 33)),
        probe_json=probed(languages=("eng", "fra")),
        digests=("v-one", "a-one", "a-three"),
    )
    result = compare(evidence(), built, TracksDropped(dropped=frozenset({3})))
    assert not result.ok


def test_a_shorter_container_after_a_drop_is_a_note_when_the_hashes_matched() -> None:
    built = dropped_built()
    built = replace(
        built,
        probe=probe_from_json(
            built.path,
            identified(languages=("eng", "deu"), uids=(11, 22), duration_ns=2_000_000_000),
            probed(languages=("eng", "deu"), duration_s=2.0),
            elements=["Tracks", "Cues"],
        ),
    )
    result = compare(evidence(), built, TracksDropped(dropped=frozenset({3})))
    assert result.ok, result.problems
    assert any("shorter" in note for note in result.notes)


def test_a_longer_container_is_never_a_note() -> None:
    built = evidence(
        identify=identified(duration_ns=9_000_000_000),
        probe_json=probed(duration_s=9.0),
    )
    result = compare(evidence(), built)
    assert not result.ok
    assert any("duration changed" in p for p in result.problems)


def test_an_intended_default_move_is_allowed_only_when_declared() -> None:
    moved = identified()
    moved["tracks"][1]["properties"]["default_track"] = False
    moved["tracks"][2]["properties"]["default_track"] = True
    built = evidence(identify=moved)
    assert not compare(evidence(), built).ok
    assert compare(evidence(), built, TracksDropped(default_moved=True)).ok


def test_a_moved_default_that_left_two_defaults_is_refused() -> None:
    """Setting the new default without clearing the old one is half an edit."""
    both = identified()
    both["tracks"][2]["properties"]["default_track"] = True
    result = compare(evidence(), evidence(identify=both), TracksDropped(default_moved=True))
    assert not result.ok
    assert any("2 audio track(s) carry the default flag" in p for p in result.problems)


def test_a_moved_default_that_left_none_is_refused() -> None:
    none = identified()
    none["tracks"][1]["properties"]["default_track"] = False
    result = compare(evidence(), evidence(identify=none), TracksDropped(default_moved=True))
    assert not result.ok


# ------------------------------------------------------------------ appending
def test_appended_streams_are_allowed_when_declared() -> None:
    built = evidence(
        identify=identified(languages=("eng", "deu", "fra", "spa"),
                            uids=(11, 22, 33, 44)),
        probe_json=probed(languages=("eng", "deu", "fra", "spa"),
                          audio_start_s=(0.0, 0.0, 0.0, 0.0)),
        digests=("v-one", "a-one", "a-two", "a-three", "a-four"),
    )
    assert compare(evidence(), built, TracksAppended(count=1)).ok
    assert not compare(evidence(), built).ok


# ------------------------------------------------------------- the quiet notes
def test_a_modern_subtag_appearing_is_a_note() -> None:
    built = evidence(identify=identified(ietf=("en", "de", "fr")))
    result = compare(evidence(), built)
    assert result.ok, result.problems
    assert any("modern language subtag" in note for note in result.notes)


def test_two_different_modern_subtags_still_fail() -> None:
    original = evidence(identify=identified(ietf=("en-GB", None, None)))
    built = evidence(identify=identified(ietf=("en-US", None, None)))
    result = compare(original, built)
    assert not result.ok
    assert "two different modern language subtags" in result.problems[0]


def test_regenerated_identifiers_are_a_note() -> None:
    built = evidence(identify=identified(uids=(91, 92, 93)))
    result = compare(evidence(), built)
    assert result.ok, result.problems
    assert any("regenerated" in note for note in result.notes)


def test_a_language_a_player_would_read_differently_is_a_failure() -> None:
    built = evidence(
        identify=identified(languages=("eng", "deu", "spa"), uids=(11, 22, 33)),
        probe_json=probed(languages=("eng", "deu", "spa")),
    )
    result = compare(evidence(), built)
    assert not result.ok


# --------------------------------------------------------------- the other bars
def test_audio_that_moved_past_the_bar_is_a_failure_and_below_it_a_note() -> None:
    past = evidence(probe_json=probed(audio_start_s=(0.0, 0.1, 0.0)))
    result = compare(evidence(), past)
    assert not result.ok
    assert "100.0 ms" in result.problems[0]
    assert "40 ms bar" in result.problems[0]

    inside = evidence(probe_json=probed(audio_start_s=(0.0, 0.01, 0.0)))
    quiet = compare(evidence(), inside)
    assert quiet.ok
    assert any("audio start moved" in note for note in quiet.notes)


def test_a_missing_seek_index_is_a_failure() -> None:
    built = evidence(elements=("Tracks",))
    result = compare(evidence(), built)
    assert any("seek index" in p for p in result.problems)


def test_a_changed_container_title_is_a_failure() -> None:
    built = evidence(identify=identified(title="Something else"))
    assert not compare(evidence(), built).ok


# ------------------------------------------------------------------- chapters
def marks(names: Sequence[str | None], *, shift_ns: int = 0) -> ChapterSet:
    return ChapterSet(
        tuple(
            Chapter(index * 2 * SECOND + shift_ns, name)
            for index, name in enumerate(names)
        )
    )


def test_chapters_are_compared_by_count_time_and_name() -> None:
    original = evidence(chapters=marks(["One", "Two", "Three"]))
    assert compare(original, evidence(chapters=marks(["One", "Two", "Three"]))).ok

    fewer = compare(original, evidence(chapters=marks(["One", "Two"])))
    assert "3 chapter mark(s) became 2" in fewer.problems[0]

    renamed = compare(original, evidence(chapters=marks(["One", "Two", "Other"])))
    assert any("name" in p for p in renamed.problems)


def test_replaced_marks_are_held_to_the_document_not_to_the_old_marks() -> None:
    original = evidence(chapters=marks(["One", "Two", "Three"]))
    written = marks(["Alpha", "Beta"])
    delta = TracksDropped(replaced_chapters=written)
    assert compare(original, evidence(chapters=marks(["Alpha", "Beta"])), delta).ok
    wrong = compare(original, evidence(chapters=marks(["Alpha", "Other"])), delta)
    assert any("name" in p for p in wrong.problems)
    # Without the declaration the same file fails against the old marks.
    assert not compare(original, evidence(chapters=marks(["Alpha", "Beta"]))).ok


def test_a_mark_that_moved_by_less_than_a_millisecond_did_not_move() -> None:
    original = evidence(chapters=marks(["One", "Two"]))
    assert compare(original, evidence(chapters=marks(["One", "Two"], shift_ns=500_000))).ok
    moved = compare(original, evidence(chapters=marks(["One", "Two"], shift_ns=50_000_000)))
    assert not moved.ok


# ----------------------------------------------------------------------- tags
def tagged(value: str) -> TagSet:
    return TagSet(
        (
            Tag(targets=Targets(track_uids=(22,)),
                simples=(SimpleTag("LANGUAGE", value),)),
        )
    )


def test_a_tag_that_disappeared_is_a_failure_and_a_new_one_is_a_note() -> None:
    original = evidence(tags=tagged("deu"))
    lost = compare(original, evidence(tags=TagSet()))
    assert not lost.ok
    assert "are gone" in lost.problems[0]

    gained = compare(evidence(tags=TagSet()), original)
    assert gained.ok
    assert any("are new" in note for note in gained.notes)


# ----------------------------------------------------------------- header only
def test_a_header_edit_may_be_verified_without_re_reading_the_payloads() -> None:
    """The editor cannot touch a payload, so not measuring it is not a shortcut."""
    original = replace(evidence(), hashes=(), hashed=False)
    edited = replace(
        evidence(
            identify=identified(languages=("eng", "deu", "spa"), uids=(11, 22, 33)),
            probe_json=probed(languages=("eng", "deu", "spa")),
        ),
        hashes=(),
        hashed=False,
    )
    result = compare(original, edited, HeaderOnly())
    assert any("headers only" in note for note in result.notes)
    assert result.hashed_streams == 0
    assert result.ok, result.problems


def test_a_header_only_expectation_still_refuses_a_changed_codec() -> None:
    changed = identified()
    changed["tracks"][1]["properties"]["codec_id"] = "A_OPUS"
    result = compare(evidence(), evidence(identify=changed), HeaderOnly())
    assert not result.ok


def test_the_verdict_prints_as_one_readable_block() -> None:
    text = str(compare(evidence(), evidence(elements=("Tracks",))))
    assert text.startswith("FAIL")
    assert "seek index" in text
    assert str(compare(evidence(), evidence())).startswith("PASS")


# ------------------------------------------------------------------ the reading
class StandIn:
    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = answers
        self.calls: list[str] = []

    def __call__(
        self, tool: str, args: Sequence[str | Path], *, ok: Sequence[int] = (0,)
    ) -> Result:
        self.calls.append(tool)
        return Result(tool, tuple(str(a) for a in args), 0,
                      self.answers.get(tool, ""), "")


def test_the_hash_list_is_read_the_way_the_program_prints_it() -> None:
    runner = StandIn({"ffmpeg": "0,v,MD5=one\n1,a,MD5=two\n2,a,MD5=three\n"})
    hashes = stream_hashes("/srv/media/movies/example.mkv", runner=runner)
    assert [h.index for h in hashes] == [0, 1, 2]
    assert [h.type for h in hashes] == ["v", "a", "a"]
    assert hashes[1].value == "two"
    assert hashes[1].algorithm == "MD5"


def test_re_reading_the_headers_keeps_the_hashes_already_collected() -> None:
    import json

    before = evidence()
    runner = StandIn(
        {
            "mkvmerge": json.dumps(identified(languages=("eng", "deu", "spa"),
                                              uids=(11, 22, 33))),
            "ffprobe": json.dumps(probed(languages=("eng", "deu", "spa"))),
        }
    )
    after = reheader(before, runner=runner)
    assert after.hashes == before.hashes
    assert "ffmpeg" not in runner.calls
    assert after.probe.audio[2].language == "spa"


# ------------------------------------------------------------- against the files
@pytest.mark.needs_ffmpeg
@pytest.mark.needs_mkvtoolnix
def test_a_real_file_against_a_copy_of_itself_passes(
    media_fixtures: dict[str, Path], tmp_path: Path
) -> None:
    source = media_fixtures["tiny_multitrack.mkv"]
    copy = tmp_path / "example.mkv"
    shutil.copyfile(source, copy)
    result = compare(collect(source), collect(copy), ExpectedDelta())
    assert result.ok, result.problems
    assert result.hashed_streams == 5


@pytest.mark.needs_ffmpeg
@pytest.mark.needs_mkvtoolnix
def test_a_real_header_edit_verifies_without_re_hashing(
    media_fixtures: dict[str, Path], tmp_path: Path
) -> None:
    from mkvkit.propedit import TrackEdit, safe_propedit

    copy = tmp_path / "example.mkv"
    shutil.copyfile(media_fixtures["tiny_multitrack.mkv"], copy)
    before = collect(copy)
    uid = before.probe.audio[2].uid
    assert uid is not None
    assert safe_propedit(copy, [TrackEdit(uid, language="spa")], dry_run=False).ok
    after = reheader(before)
    assert after.hashes == before.hashes
    result = compare(before, after, HeaderOnly())
    assert result.ok, result.problems
    assert after.probe.audio[2].language == "spa"


@pytest.mark.needs_ffmpeg
@pytest.mark.needs_mkvtoolnix
def test_the_frame_hashes_of_a_file_and_its_copy_are_identical(
    media_fixtures: dict[str, Path], tmp_path: Path
) -> None:
    copy = tmp_path / "example.mkv"
    shutil.copyfile(media_fixtures["tiny_multitrack.mkv"], copy)
    assert frames_identical(media_fixtures["tiny_multitrack.mkv"], copy)


@pytest.mark.needs_ffmpeg
@pytest.mark.needs_mkvtoolnix
def test_evidence_from_a_file_with_chapters_carries_them(
    media_fixtures: dict[str, Path],
) -> None:
    found = collect(media_fixtures["chapter_grid.mkv"], hashes=False)
    assert len(found.chapters) == 12
    assert found.hashed is False
