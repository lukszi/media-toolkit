"""Reading a container, building the decode command, and checking the dump.

The parsing and the command building are tested with a stand-in for the
program, so they run on a machine with nothing installed. The two tests that
decode a real file are marked, and use the generated fixtures.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from dubalign.check_raw import check_signal, edge_silence
from dubalign.decode import decode, decode_command
from dubalign.probe import (
    AudioStream,
    SourceError,
    first_packet_time,
    probe,
    reference_risk,
    source_from_json,
)
from mkvkit.run import Result

SR = 48_000

#: One file, three audio tracks, and every awkwardness worth parsing: a track
#: that starts late, a layout that differs from the channel count's default
#: name, a language in a tag and a title in another case, and a stream that is
#: not audio sitting in the middle of the numbering.
PROBED: dict[str, Any] = {
    "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "h264"},
        {
            "index": 1, "codec_type": "audio", "codec_name": "eac3", "channels": 6,
            "channel_layout": "5.1(side)", "sample_rate": "48000",
            "start_time": "0.030000", "duration": "1500.000000",
            "tags": {"language": "eng", "TITLE": "Original"},
        },
        {"index": 2, "codec_type": "subtitle", "codec_name": "subrip"},
        {
            "index": 3, "codec_type": "audio", "codec_name": "truehd", "channels": 8,
            "channel_layout": "7.1", "sample_rate": "48000",
            "tags": {"language": "deu"},
        },
        {
            "index": 4, "codec_type": "audio", "codec_name": "flac", "channels": 2,
            "channel_layout": "stereo", "sample_rate": "48000",
        },
    ],
    "format": {"duration": "1500.000000", "start_time": "0.000000"},
}


def runner_returning(payload: str) -> Any:
    def run(tool: str, args: Any, *, ok: Any = (0,)) -> Result:
        return Result(tool=tool, argv=(tool,), returncode=0, stdout=payload, stderr="")

    return run


# ------------------------------------------------------------------ the parse
def test_the_audio_streams_are_numbered_twice_and_both_numbers_are_kept() -> None:
    source = source_from_json(Path("/srv/media/movies/example.mkv"), PROBED)
    assert [s.index for s in source.streams] == [1, 3, 4]
    assert [s.audio_index for s in source.streams] == [0, 1, 2]
    assert source.audio(1).selector == "0:a:1"
    assert source.audio(1).index == 3


def test_a_track_that_starts_late_says_so() -> None:
    """The 30 ms a raw decode silently throws away."""
    stream = source_from_json(Path("example.mkv"), PROBED).audio(0)
    assert stream.start_time_s == pytest.approx(0.030)
    assert "starts +30.0 ms" in stream.describe()


def test_the_layout_is_read_rather_than_inferred_from_the_channel_count() -> None:
    stream = source_from_json(Path("example.mkv"), PROBED).audio(0)
    assert stream.channels == 6
    assert stream.layout == "5.1(side)"


def test_tags_are_read_whatever_case_they_arrive_in() -> None:
    stream = source_from_json(Path("example.mkv"), PROBED).audio(0)
    assert stream.language == "eng"
    assert stream.title == "Original"


def test_a_track_that_is_not_there_is_named_in_the_refusal() -> None:
    source = source_from_json(Path("example.mkv"), PROBED)
    with pytest.raises(SourceError, match="a:7"):
        source.audio(7)


def test_a_file_with_no_audio_at_all_is_not_a_crash() -> None:
    source = source_from_json(Path("silent.mkv"), {"streams": [], "format": {}})
    assert source.streams == ()
    with pytest.raises(SourceError, match="none"):
        source.audio(0)


def test_a_duration_that_is_not_a_number_reads_as_no_duration() -> None:
    source = source_from_json(Path("x.mkv"), {"format": {"duration": "N/A"}})
    assert source.duration_s is None


# ------------------------------------------------------------- the reference
def test_a_losslessly_packed_track_is_refused_as_a_reference() -> None:
    risky = source_from_json(Path("example.mkv"), PROBED).audio(1)
    message = reference_risk(risky)
    assert message is not None
    assert "sync point" in message


def test_an_ordinary_track_is_accepted_as_a_reference() -> None:
    assert reference_risk(source_from_json(Path("x.mkv"), PROBED).audio(0)) is None


# --------------------------------------------------------------- the command
def test_one_invocation_writes_both_dumps() -> None:
    stream = source_from_json(Path("x.mkv"), PROBED).audio(0)
    args = decode_command("in.mkv", stream, "full.f32le", "small.f32le")
    assert args.count("-i") == 1, "the source must be read exactly once"
    assert args.count("-map") == 2, "one read, two outputs"
    assert args.index("full.f32le") < args.index("small.f32le")


def test_the_command_never_seeks() -> None:
    """A seek lands where the decoder can resume, not where it was asked."""
    stream = source_from_json(Path("x.mkv"), PROBED).audio(0)
    assert "-ss" not in decode_command("in.mkv", stream, "a", "b")


def test_the_two_outputs_have_the_rates_and_widths_they_should() -> None:
    stream = source_from_json(Path("x.mkv"), PROBED).audio(0)
    args = decode_command(
        "in.mkv", stream, "full.f32le", "small.f32le",
        sample_rate=48_000, analysis_rate=16_000,
    )
    first, second = _outputs(args, "full.f32le", "small.f32le")
    assert first["-ar"] == "48000" and first["-ac"] == "6"
    assert second["-ar"] == "16000" and second["-ac"] == "1"


def _outputs(args: list[str], first_name: str, second_name: str) -> tuple[
    dict[str, str], dict[str, str]
]:
    """The switches belonging to each of the two outputs, by where they sit."""
    cut = args.index(first_name)
    end = args.index(second_name)
    return _switches(args[:cut]), _switches(args[cut:end])


def _switches(part: list[str]) -> dict[str, str]:
    return {part[i]: part[i + 1] for i in range(len(part) - 1) if part[i].startswith("-")}


def test_a_track_reporting_no_channels_is_refused() -> None:
    broken = AudioStream(index=1, audio_index=0, codec="eac3", channels=0,
                         layout="unknown", sample_rate=48_000)
    with pytest.raises(ValueError, match="no channels"):
        decode_command("in.mkv", broken, "a", "b")


def test_the_first_packet_time_is_read_from_the_packet() -> None:
    read = first_packet_time("x.mkv", 0, runner=runner_returning("0.030000\n0.062000\n"))
    assert read == pytest.approx(0.030)


def test_a_stream_with_no_readable_packet_is_an_error_not_a_zero() -> None:
    with pytest.raises(SourceError, match="no readable packet"):
        first_packet_time("x.mkv", 0, runner=runner_returning("\n"))


# ------------------------------------------------------------ dump reuse
def writing_runner(calls: list[list[str]], fill: bytes = b"x" * 64) -> Any:
    """Stands in for the decoder: writes each output it was asked for."""

    def run(tool: str, args: Any, *, ok: Any = (0,)) -> Result:
        argv = [str(a) for a in args]
        calls.append(argv)
        for i, arg in enumerate(argv):
            if arg == "-y":
                Path(argv[i + 1]).write_bytes(fill)
        return Result(tool=tool, argv=(tool,), returncode=0, stdout="", stderr="")

    return run


def fake_decode(source: Path, work: Path, calls: list[list[str]], **kwargs: Any) -> Any:
    probed = source_from_json(source, PROBED)
    kwargs.setdefault("name", "other")
    return decode(source, out_dir=work, probed=probed,
                  runner=writing_runner(calls), **kwargs)


def media(tmp_path: Path, name: str, content: bytes = b"container") -> Path:
    path = tmp_path / "media" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_the_same_source_reuses_its_dump(tmp_path: Path) -> None:
    source, calls = media(tmp_path, "donor.mkv"), []
    first = fake_decode(source, tmp_path / "work", calls)
    again = fake_decode(source, tmp_path / "work", calls)
    assert len(calls) == 1
    assert again.full_path == first.full_path


def test_a_different_source_under_the_same_name_gets_its_own_dump(tmp_path: Path) -> None:
    """The stale-dump trap: a second film in the same work directory."""
    calls: list[list[str]] = []
    first = fake_decode(media(tmp_path, "donor.mkv"), tmp_path / "work", calls)
    second = fake_decode(media(tmp_path, "result.mkv"), tmp_path / "work", calls)
    assert len(calls) == 2, "the second source was answered from the first one's dump"
    assert second.full_path != first.full_path
    assert second.analysis_path != first.analysis_path


def test_a_different_track_gets_its_own_dump(tmp_path: Path) -> None:
    source, calls = media(tmp_path, "donor.mkv"), []
    first = fake_decode(source, tmp_path / "work", calls, audio_index=0, channels=2)
    other = fake_decode(source, tmp_path / "work", calls, audio_index=2, channels=2)
    assert len(calls) == 2
    assert other.full_path != first.full_path


def test_a_changed_file_is_decoded_again(tmp_path: Path) -> None:
    import os

    source, calls = media(tmp_path, "donor.mkv"), []
    first = fake_decode(source, tmp_path / "work", calls)
    stamp = source.stat().st_mtime_ns
    os.utime(source, ns=(stamp + 10**9, stamp + 10**9))
    touched = fake_decode(source, tmp_path / "work", calls)
    assert len(calls) == 2, "a newer file was answered from the older dump"
    assert touched.full_path != first.full_path
    source.write_bytes(b"a re-muxed container, longer than before")
    os.utime(source, ns=(stamp + 10**9, stamp + 10**9))
    resized = fake_decode(source, tmp_path / "work", calls)
    assert len(calls) == 3, "a file of another size was answered from the old dump"
    assert resized.full_path not in (first.full_path, touched.full_path)


def test_a_dump_is_written_under_a_part_name_and_renamed(tmp_path: Path) -> None:
    source, calls = media(tmp_path, "donor.mkv"), []
    decoded = fake_decode(source, tmp_path / "work", calls)
    written = [argv[i + 1] for argv in calls for i, a in enumerate(argv) if a == "-y"]
    assert all(path.endswith(".part") for path in written)
    assert decoded.full_path.exists() and decoded.analysis_path.exists()
    assert list((tmp_path / "work").glob("*.part")) == []


def test_a_leftover_part_is_never_taken_for_a_dump(tmp_path: Path) -> None:
    source, calls = media(tmp_path, "donor.mkv"), []
    decoded = fake_decode(source, tmp_path / "work", calls)
    # As an interrupted run would leave it: no finished dump, a truncated part.
    decoded.full_path.unlink()
    decoded.analysis_path.unlink()
    part = decoded.full_path.with_name(decoded.full_path.name + ".part")
    part.write_bytes(b"ab")
    again = fake_decode(source, tmp_path / "work", calls)
    assert len(calls) == 2, "the truncated part was accepted"
    assert again.full_path.read_bytes() == b"x" * 64
    assert not part.exists()


def test_an_interrupted_decode_leaves_nothing_a_later_run_would_trust(
    tmp_path: Path,
) -> None:
    source = media(tmp_path, "donor.mkv")
    work = tmp_path / "work"

    def interrupted(tool: str, args: Any, *, ok: Any = (0,)) -> Result:
        argv = [str(a) for a in args]
        Path(argv[argv.index("-y") + 1]).write_bytes(b"z")
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        decode(source, out_dir=work, name="other",
               probed=source_from_json(source, PROBED), runner=interrupted)
    assert list(work.iterdir()) == []
    calls: list[list[str]] = []
    fake_decode(source, work, calls)
    assert len(calls) == 1


# ------------------------------------------------------------- the raw check
def noise(seconds: float, sr: int = SR, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return np.asarray(rng.standard_normal(int(seconds * sr)) * 0.2, dtype=np.float64)


def test_a_dump_that_matches_the_container_is_clean() -> None:
    check = check_signal(noise(2.0), SR, declared_duration_s=2.0)
    assert check.ok
    assert check.duration_s == pytest.approx(2.0)
    assert check.problems == ()


def test_a_dump_that_lost_its_head_is_a_problem_not_a_note() -> None:
    """The failure that measures perfectly and is wrong by what went missing."""
    check = check_signal(noise(2.0)[int(0.2 * SR):], SR, declared_duration_s=2.0)
    assert not check.ok
    assert "-200 ms" in check.problems[0]


def test_a_dump_a_few_milliseconds_out_is_a_note() -> None:
    check = check_signal(noise(2.0)[: -int(0.015 * SR)], SR, declared_duration_s=2.0)
    assert check.ok
    assert any("differs from the container" in n for n in check.notes)


def test_a_silent_dump_says_the_selector_probably_picked_the_wrong_track() -> None:
    check = check_signal(np.zeros(SR), SR)
    assert not check.ok
    assert "not the one intended" in check.problems[0]


def test_an_empty_dump_is_a_problem() -> None:
    assert not check_signal(np.zeros(0), SR).ok


def test_clipping_is_recorded_as_a_note() -> None:
    loud = np.concatenate([noise(0.5), np.ones(100)])
    assert any("clipping" in n for n in check_signal(loud, SR).notes)


def test_silence_at_the_ends_is_measured() -> None:
    body = noise(1.0)
    padded = np.concatenate([np.zeros(int(0.6 * SR)), body, np.zeros(int(0.8 * SR))])
    head, tail = edge_silence(padded, SR)
    assert head == pytest.approx(0.6, abs=0.02)
    assert tail == pytest.approx(0.8, abs=0.02)
    check = check_signal(padded, SR)
    assert any("head" in n for n in check.notes)


def test_a_signal_with_no_sound_in_it_reads_as_silent_throughout() -> None:
    head, tail = edge_silence(np.zeros(SR), SR)
    assert head == pytest.approx(1.0)
    assert tail == pytest.approx(1.0)


def test_the_description_names_the_problems() -> None:
    lines = check_signal(np.zeros(SR), SR).describe()
    assert any(line.startswith("PROBLEM:") for line in lines)


# ------------------------------------------------------------ against a file
@pytest.mark.needs_ffmpeg
def test_a_real_file_reads_back_the_tracks_it_was_built_with(
    media_fixtures: dict[str, Path]
) -> None:
    source = probe(media_fixtures["offset_pair.mka"])
    assert len(source.streams) == 2
    assert source.streams[0].sample_rate == SR
    assert source.duration_s is not None


@pytest.mark.needs_ffmpeg
def test_one_decode_gives_a_dump_the_container_agrees_with(
    media_fixtures: dict[str, Path], tmp_path: Path
) -> None:
    source_path = media_fixtures["offset_pair.mka"]
    decoded = decode(source_path, out_dir=tmp_path, audio_index=0)
    assert decoded.full_path.exists() and decoded.analysis_path.exists()
    check = check_signal(
        decoded.channel(0), decoded.sample_rate,
        declared_duration_s=decoded.stream.duration_s,
    )
    assert check.ok, check.describe()
    assert len(decoded.analysis()) == pytest.approx(
        decoded.duration_s * decoded.analysis_rate, rel=0.01
    )


@pytest.mark.needs_ffmpeg
def test_a_second_decode_reuses_the_dump(
    media_fixtures: dict[str, Path], tmp_path: Path
) -> None:
    source_path = media_fixtures["offset_pair.mka"]
    first = decode(source_path, out_dir=tmp_path, audio_index=0)
    stamp = first.full_path.stat().st_mtime_ns
    again = decode(source_path, out_dir=tmp_path, audio_index=0)
    assert again.full_path.stat().st_mtime_ns == stamp


@pytest.mark.needs_ffmpeg
def test_the_json_the_parser_is_tested_against_is_the_shape_the_program_prints(
    media_fixtures: dict[str, Path]
) -> None:
    """The hand-written structure above has to stay the real one."""
    from dubalign.probe import ffprobe_json

    real = ffprobe_json(media_fixtures["tiny_multitrack.mkv"])
    assert set(PROBED) <= set(real)
    audio = [s for s in real["streams"] if s.get("codec_type") == "audio"]
    for key in ("index", "codec_name", "channels", "channel_layout", "sample_rate"):
        assert key in audio[0], key
    assert "duration" in json.loads(json.dumps(real))["format"]
