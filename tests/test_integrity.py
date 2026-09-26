"""Is the payload there: the check that reads the file, not its headers.

The case these tests exist for: a file whose header is intact and whose body
was never written. Every header-level reader calls it healthy -- the right
container, the right tracks, the right duration, a seek index -- and a
duplicate pass that trusts them keeps the empty copy and removes the one that
plays. The fixtures make exactly that file: a real, playable one, copied, with
everything but its first and last few kilobytes overwritten with zeros.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Sequence
from pathlib import Path

import pytest
from mkvkit import cli as mkvkit_cli
from mkvkit.integrity import Thresholds, check, sample_zero_fill
from mkvkit.probe import probe_from_json
from mkvkit.run import CommandFailed, Result
from mkvkit.tools import ToolNotFound

from tests.fixtures.make_fixtures import _ffmpeg

#: small blocks, because the fixtures are small
BLOCK = 16 << 10


def zero_most_of(source: Path, target: Path, *, keep_head: int = 32 << 10,
                 keep_tail: int = 16 << 10) -> Path:
    """A copy whose header and index survive and whose body is zeros."""
    data = bytearray(source.read_bytes())
    end = len(data) - keep_tail
    data[keep_head:end] = bytes(end - keep_head)
    target.write_bytes(bytes(data))
    return target


# ------------------------------------------------------------ zero-fill read
def test_a_file_of_real_bytes_has_no_zero_blocks(tmp_path: Path) -> None:
    path = tmp_path / "payload.bin"
    path.write_bytes(os.urandom(40 * BLOCK))
    found = sample_zero_fill(path, blocks=16, block_size=BLOCK)
    assert found.blocks == 16 and found.zero_blocks == 0
    assert found.offsets[0] == 0
    assert found.offsets[-1] == 40 * BLOCK - BLOCK, "the last block ends at the last byte"


def test_a_file_that_was_reserved_and_never_written_is_mostly_zero_blocks(
    tmp_path: Path
) -> None:
    path = tmp_path / "reserved.bin"
    path.write_bytes(os.urandom(BLOCK) + bytes(38 * BLOCK) + os.urandom(BLOCK))
    found = sample_zero_fill(path, blocks=16, block_size=BLOCK)
    assert found.zero_blocks == 14
    assert found.fraction == pytest.approx(14 / 16)


def test_a_file_smaller_than_a_block_is_read_whole(tmp_path: Path) -> None:
    path = tmp_path / "small.bin"
    path.write_bytes(bytes(100))
    found = sample_zero_fill(path, blocks=16, block_size=BLOCK)
    assert found.blocks == 1 and found.zero_blocks == 1 and found.block_size == 100


# ------------------------------------------------------ no evidence is no pass
def test_a_missing_file_is_no_evidence_and_not_ok(tmp_path: Path) -> None:
    report = check(tmp_path / "gone.mkv")
    assert not report.evidence and not report.ok
    assert "NO EVIDENCE" in str(report)


def test_a_missing_program_is_no_evidence_and_not_ok(tmp_path: Path) -> None:
    path = tmp_path / "file.mkv"
    path.write_bytes(os.urandom(4 * BLOCK))

    def absent(tool: str, args: Sequence[str | Path], *, ok: Sequence[int] = (0,)) -> Result:
        raise ToolNotFound(tool, [tool])

    report = check(path, block_size=BLOCK, runner=absent)
    assert not report.evidence and not report.ok
    assert report.verdict() == "NO EVIDENCE"


def test_an_empty_file_fails(tmp_path: Path) -> None:
    path = tmp_path / "empty.mkv"
    path.write_bytes(b"")
    report = check(path)
    assert not report.ok and "empty" in report.problems[0]


# ------------------------------------------------ the measurement, recorded
PROBED = {
    "format": {"duration": "600.0", "size": str(64 * BLOCK)},
    "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "h264"},
        {"index": 1, "codec_type": "audio", "codec_name": "aac"},
        {"index": 2, "codec_type": "subtitle", "codec_name": "subrip"},
        {"index": 3, "codec_type": "video", "codec_name": "mjpeg",
         "disposition": {"attached_pic": 1}},
    ],
}


def recorded(packets: str, frames: str = "", complaints: str = "",
             decode_exit: int = 0):  # type: ignore[no-untyped-def]
    """A runner that answers the way the two programs would."""
    def run(tool: str, args: Sequence[str | Path], *, ok: Sequence[int] = (0,)) -> Result:
        argv = tuple(str(a) for a in args)
        if tool == "ffprobe" and "-show_streams" in argv:
            return Result(tool, argv, 0, json.dumps(PROBED), "")
        if tool == "ffprobe":
            return Result(tool, argv, 0, packets, "")
        result = Result(tool, argv, decode_exit, frames, complaints)
        if decode_exit not in ok:
            raise CommandFailed(result)
        return result
    return run


def packet_lines(stream: int, count: int, step: float, size: int) -> str:
    return "\n".join(
        f"stream_index={stream}|pts_time={i * step:.6f}|dts_time={i * step:.6f}"
        f"|duration_time={step:.6f}|size={size}"
        for i in range(count)
    )


def frame_lines(out: int, count: int, step_ticks: int) -> str:
    return "\n".join(
        f"{out}, {i * step_ticks}, {i * step_ticks}, {step_ticks}, 100, 0x00000000"
        for i in range(count)
    )


def test_every_track_covering_the_container_passes(tmp_path: Path) -> None:
    path = tmp_path / "whole.mkv"
    path.write_bytes(os.urandom(64 * BLOCK))
    packets = packet_lines(0, 15000, 0.04, 60) + "\n" + packet_lines(1, 28125, 0.021333, 10)
    frames = (
        "#tb 0: 1/1000\n#tb 1: 1/48000\n"
        + frame_lines(0, 15000, 40) + "\n" + frame_lines(1, 28125, 1024)
    )
    report = check(path, block_size=BLOCK, runner=recorded(packets, frames))
    assert report.ok, str(report)
    assert [t.index for t in report.tracks] == [0, 1], "no subtitle, no cover art"
    assert report.tracks[0].decoded_s == pytest.approx(600.0)


def test_a_track_with_packets_for_a_few_seconds_of_a_long_container_fails(
    tmp_path: Path
) -> None:
    """The header says ten minutes; the video has packets for two seconds."""
    path = tmp_path / "holes.mkv"
    path.write_bytes(os.urandom(64 * BLOCK))
    packets = (
        packet_lines(0, 50, 0.04, 20000)
        # one stray packet far past the end must not count as covering the gap
        + "\nstream_index=0|pts_time=599.0|dts_time=599.0|duration_time=N/A|size=304\n"
        + packet_lines(1, 28125, 0.021333, 25)
    )
    report = check(path, decode=False, block_size=BLOCK, runner=recorded(packets))
    assert not report.ok
    assert any("stream 0 (video) has packets for" in p for p in report.problems)
    assert report.tracks[0].covered_s < 5.0


def test_what_the_decoder_complains_about_fails_the_file(tmp_path: Path) -> None:
    path = tmp_path / "damaged.mkv"
    path.write_bytes(os.urandom(64 * BLOCK))
    packets = packet_lines(0, 15000, 0.04, 60) + "\n" + packet_lines(1, 28125, 0.021333, 10)
    frames = (
        "#tb 0: 1/1000\n#tb 1: 1/48000\n"
        + frame_lines(0, 15000, 40) + "\n" + frame_lines(1, 28125, 1024)
    )
    report = check(path, block_size=BLOCK, runner=recorded(
        packets, frames, complaints="[h264] error while decoding MB 3 7\n",
    ))
    assert not report.ok
    assert any("decoder complained 1" in p for p in report.problems)
    lenient = check(path, block_size=BLOCK, thresholds=Thresholds(max_decode_errors=1),
                    runner=recorded(packets, frames,
                                    complaints="[h264] error while decoding MB 3 7\n"))
    assert lenient.ok


def test_a_decoder_that_gives_up_is_a_failure_not_a_crash(tmp_path: Path) -> None:
    path = tmp_path / "unplayable.mkv"
    path.write_bytes(os.urandom(64 * BLOCK))
    packets = packet_lines(0, 15000, 0.04, 60) + "\n" + packet_lines(1, 28125, 0.021333, 10)
    report = check(path, block_size=BLOCK, runner=recorded(
        packets, "", complaints="Invalid data found\n", decode_exit=1,
    ))
    assert report.evidence and not report.ok
    assert any("decoder complained" in p for p in report.problems)


# ---------------------------------------------------------- on real media
@pytest.mark.needs_ffmpeg
def test_a_playable_file_passes_and_its_zeroed_copy_does_not(
    media_fixtures: dict[str, Path], tmp_path: Path
) -> None:
    source = media_fixtures["tiny_multitrack.mkv"]
    whole = tmp_path / "Harbour Lights - S01E01.mkv"
    shutil.copyfile(source, whole)
    broken = zero_most_of(source, tmp_path / "Harbour Lights - S01E01 (1080p).mkv")

    assert check(whole, block_size=BLOCK).ok
    report = check(broken, block_size=BLOCK)
    assert report.evidence and not report.ok
    assert report.zero is not None and report.zero.fraction > 0.5
    assert any("sampled blocks" in p for p in report.problems)
    assert any("has packets for" in p for p in report.problems)


@pytest.mark.needs_ffmpeg
def test_the_zeroed_copy_keeps_headers_a_probe_calls_healthy(
    media_fixtures: dict[str, Path], tmp_path: Path
) -> None:
    """The premise: every header-level answer is unchanged by the damage."""
    from mkvkit.probe import ffprobe_json

    source = media_fixtures["tiny_multitrack.mp4"]
    broken = zero_most_of(source, tmp_path / "The Quiet Harbour (1978).mp4",
                          keep_head=4 << 10, keep_tail=8 << 10)
    before = ffprobe_json(source, chapters=False)
    after = ffprobe_json(broken, chapters=False)
    assert before["format"]["duration"] == after["format"]["duration"]
    assert len(before["streams"]) == len(after["streams"])
    assert not check(broken, block_size=4 << 10).ok


@pytest.mark.needs_ffmpeg
def test_the_command_exits_non_zero_for_a_broken_file_and_writes_its_report(
    media_fixtures: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = media_fixtures["tiny_multitrack.mkv"]
    broken = zero_most_of(source, tmp_path / "broken.mkv")
    report = tmp_path / "integrity.jsonl"
    assert mkvkit_cli.main(["integrity", str(source), "--block-mib", "0.015625"]) == 0
    assert mkvkit_cli.main([
        "integrity", str(broken), "--quick", "--block-mib", "0.015625",
        "--json", str(report),
    ]) == 1
    rows = [json.loads(line) for line in report.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["verdict"] == "FAILED" and rows[0]["zero_blocks"] > 0
    assert "FAILED" in capsys.readouterr().out


@pytest.mark.needs_ffmpeg
def test_probe_names_a_zero_filled_file_a_problem(
    media_fixtures: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = media_fixtures["drift_pair.mka"]
    broken = zero_most_of(source, tmp_path / "broken.mka",
                          keep_head=1 << 20, keep_tail=1 << 20)
    assert mkvkit_cli.main(["probe", "--no-elements", str(broken)]) == 1
    assert "run `mkvkit integrity`" in capsys.readouterr().out


@pytest.mark.needs_ffmpeg
def test_probe_warns_when_a_track_is_far_shorter_than_the_container(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Twelve seconds of picture and four of sound, in one twelve-second file."""
    uneven = tmp_path / "Northwind - S01E02.mkv"
    _ffmpeg(
        "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=10:duration=12",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=8000:duration=4",
        "-c:v", "mpeg4", "-c:a", "flac", str(uneven),
    )
    assert mkvkit_cli.main(["probe", "--no-elements", str(uneven)]) == 0
    printed = capsys.readouterr().out
    assert "durations disagree with the container" in printed
    assert "stream 1 (audio) states 4.0 s" in printed
    assert not check(uneven, block_size=BLOCK).ok


def test_a_track_duration_is_read_from_its_statistics_tag() -> None:
    found = probe_from_json(
        "episode.mkv", {"container": {"type": "Matroska"}},
        {
            "format": {"duration": "2528.6"},
            "streams": [
                {"index": 0, "codec_type": "video", "tags": {"DURATION": "00:42:08.600000000"}},
                {"index": 1, "codec_type": "audio", "tags": {"DURATION-eng": "00:20:55.000000000"}},
            ],
        },
        size=1,
    )
    assert found.streams[0].stated_duration_s == pytest.approx(2528.6)
    assert found.streams[1].stated_duration_s == pytest.approx(1255.0)
