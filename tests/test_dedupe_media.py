"""Duplicate resolution on real files: what the probe reads, and the zero-filled keeper.

The copies are built here from test patterns and tones by the encoders built
into ffmpeg, a few seconds each, so the track layouts are exactly the ones
the rules are about: a lossless track, a commentary, a forced subtitle, a
language only one copy has. Nothing is committed; nothing is real.

The test that matters most is the last one. The copy the rules prefer is
turned into the file that costs a film: its header and index
intact, everything between them zeros. Every header-level reader calls it
healthy. The payload check does not, and the group is blocked with the copy
that plays still in place.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

from pathlib import Path

import pytest
from jfkit.dedupe import BLOCKED, SAFE, Rules, find_groups, probe_copy, resolve
from mkvkit.config import DedupePolicy

from tests.fixtures.make_fixtures import _ffmpeg
from tests.test_integrity import zero_most_of

pytestmark = pytest.mark.needs_ffmpeg

RULES = Rules(keep_languages=("eng", "deu"))
SECONDS = 6


def _srt(path: Path) -> Path:
    path.write_text("1\n00:00:00,500 --> 00:00:01,500\nFirst cue\n", encoding="utf-8")
    return path


def _tone(frequency: int) -> list[str]:
    return ["-f", "lavfi", "-i",
            f"sine=frequency={frequency}:duration={SECONDS}:sample_rate=48000"]


def _video() -> list[str]:
    return ["-f", "lavfi", "-i", f"testsrc=size=320x240:rate=25:duration={SECONDS}"]


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """The better copy, with everything, and a lesser one, in two release folders."""
    from tests.fixtures import ffmpeg_missing

    if ffmpeg_missing():
        pytest.skip(f"not installed: {ffmpeg_missing()}")
    root = tmp_path_factory.mktemp("dedupe") / "movies"
    better = root / "The Quiet Harbour (1978)" / "The Quiet Harbour (1978).mkv"
    lesser = root / "The Quiet Harbour (1978) x264" / "The Quiet Harbour (1978).mkv"
    better.parent.mkdir(parents=True)
    lesser.parent.mkdir(parents=True)
    subs = _srt(root / "cues.srt")
    _ffmpeg(
        *_video(), *_tone(440), *_tone(660), *_tone(880),
        "-i", str(subs), "-i", str(subs),
        "-map", "0:v", "-map", "1:a", "-map", "2:a", "-map", "3:a",
        "-map", "4:s", "-map", "5:s",
        "-c:v", "mpeg4", "-q:v", "2",
        "-c:a:0", "ac3", "-ac:a:0", "6", "-c:a:1", "flac", "-ac:a:1", "2",
        "-c:a:2", "ac3", "-ac:a:2", "2", "-c:s", "srt",
        "-metadata:s:a:0", "language=eng", "-metadata:s:a:1", "language=deu",
        "-metadata:s:a:2", "language=eng", "-metadata:s:a:2", "title=Commentary",
        "-metadata:s:s:0", "language=eng", "-metadata:s:s:1", "language=eng",
        "-disposition:s:1", "forced",
        str(better),
    )
    _ffmpeg(
        *_video(), *_tone(440), "-i", str(subs), "-i", str(subs),
        "-map", "0:v", "-map", "1:a", "-map", "2:s", "-map", "3:s",
        "-c:v", "mpeg4", "-q:v", "8", "-c:a", "ac3", "-ac", "2", "-c:s", "srt",
        "-metadata:s:a:0", "language=eng", "-metadata:s:s:0", "language=eng",
        "-metadata:s:s:1", "language=fra",
        str(lesser),
    )
    _srt(lesser.with_name("The Quiet Harbour (1978).ita.srt"))
    return {"better": better, "lesser": lesser, "root": root}


def _groups(*paths: Path) -> tuple:  # type: ignore[type-arg]
    rows = [
        {"Id": f"00000000-0000-0000-0000-{n:012d}", "Name": "The Quiet Harbour",
         "Type": "Movie", "Path": str(path), "ProductionYear": 1978,
         "ProviderIds": {"Tmdb": "1001"}}
        for n, path in enumerate(paths, start=1)
    ]
    return find_groups(rows).groups


def test_the_probe_reads_every_track_the_rules_care_about(built: dict[str, Path]) -> None:
    (group,) = _groups(built["better"], built["lesser"])
    by_path = {Path(m.path): m for m in group.members}
    better = probe_copy(by_path[built["better"]], DedupePolicy())
    assert (better.width, better.height) == (320, 240)
    assert better.duration_s == pytest.approx(SECONDS, abs=0.5)
    described = [t.describe() for t in better.audio]
    assert described == ["eng ac3 6 ch", "deu flac 2 ch lossless",
                         "eng ac3 2 ch commentary"]
    assert [(t.language, t.forced) for t in better.subtitles] == [("eng", False),
                                                                  ("eng", True)]
    assert better.lossless and better.channels == 6

    lesser = probe_copy(by_path[built["lesser"]], DedupePolicy())
    assert lesser.origin == "re-encode"
    assert [(t.language, t.external) for t in lesser.subtitles] == [
        ("eng", False), ("fra", False), ("ita", True)]


def test_the_better_copy_is_kept_after_its_payload_is_decoded(
    built: dict[str, Path]
) -> None:
    (verdict,) = resolve(_groups(built["better"], built["lesser"]), RULES)
    assert verdict.verdict == SAFE, verdict.reasons
    assert verdict.keeper is not None and verdict.keeper.path == built["better"]
    assert verdict.integrity is not None and verdict.integrity.decoded
    assert any("subtitles in fra, ita" in note for note in verdict.notes)


def test_a_zero_filled_keeper_blocks_the_group(
    built: dict[str, Path], tmp_path: Path
) -> None:
    """Perfect headers, a body of zeros: kept on its headers, it costs the film."""
    folder = tmp_path / "movies" / "The Quiet Harbour (1978) remux"
    folder.mkdir(parents=True)
    hollow = zero_most_of(built["better"], folder / "The Quiet Harbour (1978).mkv",
                          keep_head=8 << 10, keep_tail=4 << 10)
    (verdict,) = resolve(_groups(hollow, built["lesser"]), RULES)
    assert verdict.verdict == BLOCKED, verdict.reasons
    assert verdict.keeper is not None and verdict.keeper.path == hollow, (
        "the rules prefer it on its headers; only the payload check stops it"
    )
    assert verdict.integrity is not None and not verdict.integrity.ok
    assert built["lesser"].is_file()
