"""The generated fixtures are what they claim to be.

These run only where ffmpeg and ffprobe are installed, and they are the
foundation every later media test stands on: if the fixture does not have the
tracks, the languages, the marks and the delay written down here, then a test
that passes against it proves nothing.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures import (
    AUDIO_TRACKS,
    CHAPTER_NAMES,
    CHAPTER_TIMES_S,
    OFFSET_MS,
    PAL_RATIO,
    SUBTITLE_CUES,
    build,
    probe,
)

pytestmark = pytest.mark.needs_ffmpeg


def _streams(path: Path, kind: str) -> list[dict[str, Any]]:
    return [s for s in probe(path)["streams"] if s.get("codec_type") == kind]


def _duration_s(stream: dict[str, Any]) -> float:
    """Matroska reports a stream duration as a tag more often than as a field."""
    raw = stream.get("duration")
    if raw not in (None, "N/A"):
        return float(raw)
    tag = stream.get("tags", {}).get("DURATION") or stream["tags"]["DURATION-eng"]
    hours, minutes, seconds = tag.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def test_every_fixture_is_built(media_fixtures: dict[str, Path]) -> None:
    for name, path in media_fixtures.items():
        assert path.is_file(), name
        assert path.stat().st_size > 0, name


def test_the_multitrack_file_has_the_tracks_it_promises(
    media_fixtures: dict[str, Path],
) -> None:
    path = media_fixtures["tiny_multitrack.mkv"]
    assert len(_streams(path, "video")) == 1
    audio = _streams(path, "audio")
    assert len(audio) == len(AUDIO_TRACKS)
    languages = [s.get("tags", {}).get("language") for s in audio]
    assert languages == [track.language for track in AUDIO_TRACKS]
    named = [s.get("tags", {}).get("title") for s in audio]
    assert named[1] == AUDIO_TRACKS[1].name
    assert len(_streams(path, "subtitle")) == 1


def test_the_renamed_file_is_not_the_container_it_claims(
    media_fixtures: dict[str, Path],
) -> None:
    path = media_fixtures["not_really_mkv.mkv"]
    assert path.suffix == ".mkv"
    reported = str(probe(path)["format"]["format_name"])
    assert "matroska" not in reported


def test_the_offset_pair_carries_the_delay_it_was_given(
    media_fixtures: dict[str, Path],
) -> None:
    audio = _streams(media_fixtures["offset_pair.mka"], "audio")
    assert len(audio) == 2
    measured_ms = (_duration_s(audio[1]) - _duration_s(audio[0])) * 1000.0
    assert abs(measured_ms - OFFSET_MS) < 20.0, measured_ms


def test_the_drifting_pair_carries_all_three_defects(
    media_fixtures: dict[str, Path]
) -> None:
    """The one fixture that is not one known answer but three at once."""
    if "drift_pair.mka" not in media_fixtures:
        pytest.skip("the drifting pair needs the alignment package to be importable")
    from tests.synthetic import HEAD_LAG_S, RATE_AFTER, STEP_AT_S, STEP_DROP_S

    streams = _streams(media_fixtures["drift_pair.mka"], "audio")
    assert len(streams) == 2
    assert [s["tags"]["title"] for s in streams] == ["reference", "a different transfer"]
    reference, transfer = (_duration_s(s) for s in streams)
    assert reference == pytest.approx(120.0, abs=0.05)
    # the gap at the head, less the stretch that is missing, less the rate
    assert transfer == pytest.approx(
        HEAD_LAG_S + STEP_AT_S + (120.0 - STEP_AT_S - STEP_DROP_S) / RATE_AFTER, abs=0.05
    )


def test_the_chapter_grid_is_where_it_says(media_fixtures: dict[str, Path]) -> None:
    chapters = probe(media_fixtures["chapter_grid.mkv"])["chapters"]
    assert len(chapters) == len(CHAPTER_TIMES_S)
    starts = [float(c["start_time"]) for c in chapters]
    for measured, expected in zip(starts, CHAPTER_TIMES_S, strict=True):
        assert abs(measured - expected) < 0.005
    assert [c["tags"]["title"] for c in chapters] == list(CHAPTER_NAMES)


def test_the_rescaled_grid_is_the_same_grid_scaled(
    media_fixtures: dict[str, Path],
) -> None:
    chapters = probe(media_fixtures["chapter_grid_pal.mkv"])["chapters"]
    starts = [float(c["start_time"]) for c in chapters]
    for measured, expected in zip(starts, CHAPTER_TIMES_S, strict=True):
        assert abs(measured - expected * PAL_RATIO) < 0.005


def test_the_subtitle_file_has_the_cues_it_says(
    media_fixtures: dict[str, Path],
) -> None:
    text = media_fixtures["sample_cues.srt"].read_text(encoding="utf-8")
    assert text.count("-->") == len(SUBTITLE_CUES)
    for _start, _end, cue in SUBTITLE_CUES:
        assert cue in text


def test_building_twice_changes_nothing(media_fixtures: dict[str, Path]) -> None:
    """Generation is idempotent: an existing fixture is left exactly as it is."""
    before = {name: path.stat().st_mtime_ns for name, path in media_fixtures.items()}
    after = {name: path.stat().st_mtime_ns for name, path in build().items()}
    assert before == after


def test_the_test_gate_finds_programs_the_way_the_toolkit_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not a bare PATH lookup: a Windows install of the container tools is not on it.

    A gate that only looked at the PATH would skip every container-tool test
    on a machine where every command under test works.
    """
    from mkvkit.tools import clear_cache

    from tests.fixtures.make_fixtures import locate

    program = tmp_path / ("mkvmerge.exe" if os.name == "nt" else "mkvmerge")
    program.write_bytes(b"")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setenv("MKVKIT_MKVMERGE", str(program))
    monkeypatch.setattr("mkvkit.tools.tool_version", lambda _path: "stand-in")
    clear_cache()
    try:
        assert locate("mkvmerge") == str(program.resolve())
        monkeypatch.delenv("MKVKIT_MKVMERGE")
        monkeypatch.setattr("mkvkit.tools.search_locations", lambda *_a, **_k: [])
        clear_cache()
        assert locate("mkvmerge") is None
    finally:
        clear_cache()
