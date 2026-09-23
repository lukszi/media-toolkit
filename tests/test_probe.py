"""Reading a file with both programs, and the disagreement that matters.

The parsing tests use recorded output and need nothing installed. The three
at the end read the generated fixtures and are marked accordingly.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from mkvkit.probe import (
    MediaProbe,
    ProbeError,
    container_mismatch,
    describe,
    identify_json,
    probe,
    probe_from_json,
)
from mkvkit.run import Result

# --------------------------------------------------------------- recorded output
# A three-track file: the muxer's view. The language of the second track is
# written in the bibliographic spelling, which is what this program reports
# and which is NOT what the other one reports for the same track.
IDENTIFIED: dict[str, Any] = {
    "container": {
        "type": "Matroska",
        "recognized": True,
        "supported": True,
        "properties": {"duration": 5_000_000_000, "title": "An example title"},
    },
    "tracks": [
        {
            "id": 0,
            "type": "video",
            "codec": "MPEG-4p2",
            "properties": {
                "codec_id": "V_MPEG4/ISO/ASP",
                "number": 1,
                "uid": 1111,
                "default_track": True,
                "enabled_track": True,
                "pixel_dimensions": "160x120",
                "display_dimensions": "160x120",
            },
        },
        {
            "id": 1,
            "type": "audio",
            "codec": "FLAC",
            "properties": {
                "codec_id": "A_FLAC",
                "number": 2,
                "uid": 2222,
                "language": "eng",
                "default_track": True,
                "enabled_track": True,
                "audio_channels": 2,
                "audio_sampling_frequency": 48000,
            },
        },
        {
            "id": 2,
            "type": "audio",
            "codec": "FLAC",
            "properties": {
                "codec_id": "A_FLAC",
                "number": 3,
                "uid": 3333,
                "language": "fre",
                "tag_language": "deu",
                "track_name": "Second opinion",
                "forced_track": True,
                "enabled_track": True,
                "audio_channels": 2,
                "audio_sampling_frequency": 48000,
            },
        },
    ],
    "attachments": [
        {"file_name": "cover.png", "size": 1024, "content_type": "image/png",
         "properties": {"uid": 4444}},
    ],
    "chapters": [{"num_entries": 12}],
    "global_tags": [{"num_entries": 1}],
    "track_tags": [{"track_id": 2, "num_entries": 2}],
}

# The same file, as the decoding library resolves it. Track 2's language comes
# from a tag element -- which is why the key is upper case and the value
# disagrees with the header above.
PROBED: dict[str, Any] = {
    "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "mpeg4",
         "start_time": "0.000000", "duration": "5.000000",
         "disposition": {"default": 1, "forced": 0}},
        {"index": 1, "codec_type": "audio", "codec_name": "flac", "channels": 2,
         "start_time": "0.000000", "duration": "5.000000",
         "tags": {"language": "eng"}, "disposition": {"default": 1}},
        {"index": 2, "codec_type": "audio", "codec_name": "flac", "channels": 2,
         "start_time": "0.000000", "duration": "5.000000",
         "tags": {"LANGUAGE": "deu", "title": "Second opinion"},
         "disposition": {"default": 0, "forced": 1}},
    ],
    "chapters": [
        {"id": 0, "start_time": "0.000000", "end_time": "2.000000",
         "tags": {"title": "Mark 01"}},
        {"id": 1, "start_time": "2.000000", "end_time": "4.000000",
         "tags": {"title": "Mark 02"}},
    ],
    "format": {"duration": "5.000000", "size": "123456", "format_name": "matroska,webm"},
}


def sample(path: str = "/srv/media/movies/example/example.mkv") -> MediaProbe:
    return probe_from_json(path, IDENTIFIED, PROBED, elements=["Tracks", "Cues"])


# ---------------------------------------------------------------------- parsing
def test_the_track_table_is_read_as_written() -> None:
    found = sample()
    assert [t.id for t in found.tracks] == [0, 1, 2]
    assert len(found.audio) == 2
    assert found.video[0].pixel_dimensions == "160x120"
    assert found.audio[1].name == "Second opinion"
    assert found.audio[1].forced is True
    assert found.audio[0].default is True
    assert found.audio[0].channels == 2
    assert found.track_by_uid(3333) is found.audio[1]


def test_a_language_is_canonicalised_but_the_spelling_is_kept() -> None:
    """The two programs spell some codes differently; a report wants both."""
    track = sample().audio[1]
    assert track.language_raw == "fre"
    assert track.language == "fra"


def test_the_container_and_its_counts_are_read() -> None:
    found = sample()
    assert found.container.is_matroska
    assert found.container.duration_s == pytest.approx(5.0)
    assert found.container.title == "An example title"
    assert found.chapter_count == 12
    assert found.edition_count == 1
    assert found.tag_count == 3
    assert found.attachments[0].name == "cover.png"
    assert found.size == 123456


def test_a_tag_sourced_language_is_recognised_by_the_case_of_its_key() -> None:
    """Upper case means the demuxer took it from a tag, which beats the header."""
    stream = sample().stream(2)
    assert stream is not None
    assert stream.language_from_tag is True
    assert stream.language == "deu"
    assert sample().stream(1) is not None
    other = sample().stream(1)
    assert other is not None and other.language_from_tag is False


def test_a_header_and_a_tag_that_disagree_are_reported() -> None:
    disagreements = sample().language_disagreements
    assert [d.track_id for d in disagreements] == [2]
    found = disagreements[0]
    assert (found.header, found.effective) == ("fra", "deu")
    assert found.source == "a tag element"
    assert "the header says fra" in str(found)


def test_the_tag_value_is_read_from_the_identification_output_alone() -> None:
    """The muxer names the tag's language in a property of its own."""
    found = probe_from_json("/srv/media/movies/example.mkv", IDENTIFIED)
    track = found.track(2)
    assert track is not None
    assert (track.language, track.tag_language) == ("fra", "deu")
    assert track.effective_language == "deu"
    assert [d.track_id for d in found.language_disagreements] == [2]


def test_a_track_with_no_tag_reads_as_its_header_says() -> None:
    track = sample().track(1)
    assert track is not None
    assert track.tag_language is None
    assert track.effective_language == "eng"


def test_the_audio_ordinal_counts_audio_tracks_only() -> None:
    """The header editor selects by this ordinal, not by the identifier."""
    found = sample()
    assert found.audio_ordinal(1) == 1
    assert found.audio_ordinal(2) == 2
    assert found.audio_ordinal(0) is None


def test_chapters_are_read_as_a_player_sees_them() -> None:
    marks = sample().chapters
    assert [m.title for m in marks] == ["Mark 01", "Mark 02"]
    assert marks[1].start_s == pytest.approx(2.0)


def test_nothing_in_the_answer_is_required() -> None:
    """A container nobody recognises still has to produce an answer."""
    found = probe_from_json("/srv/media/movies/nothing.dat", {}, {}, size=0)
    assert found.tracks == ()
    assert found.container.type is None
    assert found.chapter_count == 0
    assert found.has_cues is None


def test_the_seek_index_is_only_claimed_when_it_was_looked_for() -> None:
    assert sample().has_cues is True
    assert probe_from_json("/srv/media/movies/x.mkv", IDENTIFIED).has_cues is None


# ----------------------------------------------------------- the container guard
def test_a_file_named_for_matroska_that_is_not_is_refused_with_a_reason() -> None:
    identified = {"container": {"type": "QuickTime/MP4", "properties": {}}}
    found = probe_from_json("/srv/media/movies/example.mkv", identified, size=1)
    message = container_mismatch(found)
    assert message is not None
    assert "QuickTime/MP4" in message
    assert "rebuilt" in message


def test_a_real_matroska_file_passes_the_guard() -> None:
    assert container_mismatch(sample()) is None


def test_another_container_under_its_own_name_is_not_a_mismatch() -> None:
    identified = {"container": {"type": "QuickTime/MP4", "properties": {}}}
    found = probe_from_json("/srv/media/movies/example.mp4", identified, size=1)
    assert container_mismatch(found) is None


# ------------------------------------------------------------------- the running
class RecordingRunner:
    """A runner that answers from a table and remembers what it was asked."""

    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, tuple[str, ...], tuple[int, ...]]] = []

    def __call__(
        self, tool: str, args: Sequence[str | Path], *, ok: Sequence[int] = (0,)
    ) -> Result:
        self.calls.append((tool, tuple(str(a) for a in args), tuple(ok)))
        return Result(tool, ("stand-in", *(str(a) for a in args)), 0,
                      self.answers.get(tool, ""), "")


def test_a_warning_exit_code_is_accepted_from_the_identifier() -> None:
    """Exit code 1 means warnings, and a warning is routine."""
    runner = RecordingRunner({"mkvmerge": json.dumps(IDENTIFIED)})
    identify_json("/srv/media/movies/example.mkv", runner=runner)
    assert runner.calls[0][2] == (0, 1)


def test_output_that_is_not_usable_says_so_with_the_path() -> None:
    runner = RecordingRunner({"mkvmerge": "not JSON at all"})
    with pytest.raises(ProbeError, match=r"example.mkv"):
        identify_json("/srv/media/movies/example.mkv", runner=runner)


def test_both_programs_are_called_once_each() -> None:
    runner = RecordingRunner(
        {"mkvmerge": json.dumps(IDENTIFIED), "ffprobe": json.dumps(PROBED)}
    )
    found = probe("/srv/media/movies/example.mkv", runner=runner, elements=False)
    assert [call[0] for call in runner.calls] == ["mkvmerge", "ffprobe"]
    assert "-show_chapters" in runner.calls[1][1]
    assert len(found.tracks) == 3


def test_the_description_names_what_a_reader_needs() -> None:
    lines = "\n".join(describe(sample()))
    assert "Matroska" in lines
    assert "12 chapter(s)" in lines
    assert "seek index: yes" in lines
    assert "the header says fra" in lines


# ------------------------------------------------------------- against the files
@pytest.mark.needs_ffmpeg
@pytest.mark.needs_mkvtoolnix
def test_the_multitrack_fixture_reads_as_it_was_built(
    media_fixtures: dict[str, Path],
) -> None:
    found = probe(media_fixtures["tiny_multitrack.mkv"])
    assert found.container.is_matroska
    assert [t.language for t in found.audio] == ["eng", "deu", "fra"]
    assert found.subtitles and found.video
    assert found.has_cues is True
    assert container_mismatch(found) is None
    assert found.language_disagreements == ()


@pytest.mark.needs_ffmpeg
@pytest.mark.needs_mkvtoolnix
def test_the_renamed_fixture_is_caught_by_the_guard(
    media_fixtures: dict[str, Path],
) -> None:
    found = probe(media_fixtures["not_really_mkv.mkv"])
    assert not found.container.is_matroska
    assert container_mismatch(found) is not None


@pytest.mark.needs_ffmpeg
@pytest.mark.needs_mkvtoolnix
def test_the_chapter_fixture_reports_its_marks(media_fixtures: dict[str, Path]) -> None:
    found = probe(media_fixtures["chapter_grid.mkv"])
    assert found.chapter_count == 12
    assert found.edition_count == 1
    assert len(found.chapters) == 12
