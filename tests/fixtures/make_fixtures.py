"""tests.fixtures.make_fixtures -- every media fixture, generated, never committed.

    python -m tests.fixtures build

No test touches a real library. Each fixture is built from test patterns and
tones by ffmpeg into ``tests/_fixtures/``, which is not committed: this
repository contains no binary fixture at all, only this generator. Generation
takes seconds and is deterministic -- the noise source is seeded, and the
encoders used are the ones built into ffmpeg itself, so nothing here depends
on which external encoder libraries a build happens to carry.

Every fixture exists to make one known answer checkable:

``tiny_multitrack.mkv``
    one video track and three audio tracks at three frequencies, tagged with
    three different languages, one of them named, plus a text subtitle track.
    Track tables, language handling and header edits are all checked against
    a file whose answer is written down here.

``tiny_multitrack.mp4``
    the same content in the other container, for the cross-container paths.

``not_really_mkv.mkv``
    that same file, renamed. The header editor silently does nothing to a
    container that is not Matroska while the track lister happily parses it,
    so anything that writes headers must refuse this file rather than report
    success.

``offset_pair.mka``
    seeded noise, twice: once as recorded and once delayed by a known amount.
    The measurement pipeline must recover exactly that delay, and must give
    the same answer measured at two different points in the timeline. Noise
    rather than a tone on purpose: a tone correlates with itself one period
    later, so a sine cannot tell a lag of 250 ms from one of 252 ms.

``chapter_grid.mkv`` / ``chapter_grid_pal.mkv``
    the same twelve marks at known times, and the same grid rescaled by the
    ratio between the two frame rates a transfer may have been made at. Grid
    matching has to work in both directions.

``sample_cues.srt``
    a text subtitle file with known cue times, muxed into the multitrack
    fixture and also usable on its own.

``drift_pair.mka``
    two tracks that are the same programme in two transfers: one is the
    reference, the other carries a leading gap, a stretch that is missing
    entirely, and a rate difference after it. Every defect an alignment has to
    tell apart, in one file, with the answers written down in
    ``tests/synthetic.py`` -- which is also where the two signals come from, so
    the file and the in-memory pair the unit tests use are the same pair.

``tag_override.mkv``
    the multitrack file with one tag element added: a language tag on the
    first audio track claiming a language its own header does not. That tag
    is what a demuxer reports, so this fixture is the difference between a
    language edit that works and one that only looks as if it did. It is the
    one fixture that needs the container tools as well, because nothing in
    the encoder writes that element; where they are absent it is not built
    and the tests that want it skip.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "AUDIO_TRACKS",
    "CHAPTER_NAMES",
    "CHAPTER_STEP_S",
    "CHAPTER_TIMES_S",
    "DRIFT_SAMPLE_RATE",
    "DURATION_S",
    "FIXTURE_DIR",
    "GRID_DURATION_S",
    "OFFSET_MS",
    "OPTIONAL_NAMES",
    "PAL_RATIO",
    "SAMPLE_RATE",
    "SUBTITLE_CUES",
    "TAG_OVERRIDE_LANGUAGE",
    "build",
    "ffmpeg_missing",
    "main",
    "mkvtoolnix_missing",
    "probe",
]

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "_fixtures"

# --------------------------------------------------------------- known answers
DURATION_S = 5.0
FRAME_RATE = 10
FRAME_SIZE = "160x120"
SAMPLE_RATE = 48000


@dataclass(frozen=True)
class AudioTrack:
    frequency: int
    language: str
    name: str | None = None


#: index -> what that audio track must report. Three languages, one named.
AUDIO_TRACKS: tuple[AudioTrack, ...] = (
    AudioTrack(440, "eng"),
    AudioTrack(880, "deu", "Second opinion"),
    AudioTrack(1320, "fra"),
)

#: The delay between the two tracks of the offset pair, in milliseconds.
OFFSET_MS = 250.0

#: Twelve marks, evenly spaced, in a file that runs past the last of them.
CHAPTER_STEP_S = 2.0
CHAPTER_COUNT = 12
GRID_DURATION_S = 26.0
CHAPTER_TIMES_S: tuple[float, ...] = tuple(
    round(i * CHAPTER_STEP_S, 3) for i in range(CHAPTER_COUNT)
)
CHAPTER_NAMES: tuple[str, ...] = tuple(f"Mark {i + 1:02d}" for i in range(CHAPTER_COUNT))

#: The ratio between a 25-frame transfer and a 24-frame one, exactly.
PAL_RATIO = 25025 / 24000

#: start, end, text -- times in seconds.
SUBTITLE_CUES: tuple[tuple[float, float, str], ...] = (
    (0.5, 1.5, "First cue"),
    (2.0, 3.0, "Second cue"),
    (3.5, 4.5, "Third cue"),
)

#: The language the added tag claims, against the first audio track's header.
TAG_OVERRIDE_LANGUAGE = "fra"

#: The rate the drifting pair is written at. The rate difference in it is a
#: whole number of samples at this rate (48024 against 48000), so the file can
#: be rebuilt by a program as well as in memory and the two agree exactly.
DRIFT_SAMPLE_RATE = 48000

NAMES = (
    "sample_cues.srt",
    "tiny_multitrack.mkv",
    "tiny_multitrack.mp4",
    "not_really_mkv.mkv",
    "offset_pair.mka",
    "chapter_grid.mkv",
    "chapter_grid_pal.mkv",
)

#: Built only where something beyond the two media programs is available:
#: ``tag_override.mkv`` needs the container tools, and ``drift_pair.mka`` needs
#: the alignment package itself, because the two signals in it are the ones its
#: own tests measure. Both are skipped rather than faked where they cannot be
#: built, and the tests that want them skip with them.
OPTIONAL_NAMES = ("tag_override.mkv", "drift_pair.mka")


# -------------------------------------------------------------------- plumbing
def ffmpeg_missing() -> str | None:
    """None when both programs can be found, otherwise what is missing."""
    absent = [name for name in ("ffmpeg", "ffprobe") if locate(name) is None]
    return ", ".join(absent) if absent else None


def _alignment_available() -> bool:
    """Whether the package whose answers the drifting pair carries is importable."""
    try:
        import dubalign.pal  # noqa: F401
    except ImportError:
        return False
    return True


def mkvtoolnix_missing() -> str | None:
    """None when the container tools can be found, otherwise what is missing."""
    absent = [
        name for name in ("mkvmerge", "mkvpropedit", "mkvextract")
        if locate(name) is None
    ]
    return ", ".join(absent) if absent else None


def locate(program: str) -> str | None:
    """Where a program is, found the way the toolkit itself finds it.

    Not a bare PATH lookup: the toolkit also searches the platform's default
    install directories, and a Windows installer of the container tools does
    not put them on the PATH. A test gate that looked only at the PATH would
    skip tests on a machine where every command under test works.
    """
    from mkvkit.tools import ToolNotFound, find_tool

    try:
        return str(find_tool(program))
    except ToolNotFound:
        return None


def _run(program: str, args: Sequence[str]) -> str:
    executable = locate(program)
    if executable is None:
        raise RuntimeError(f"{program} was not found")
    completed = subprocess.run(
        [executable, *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )
    if completed.returncode != 0:
        tail = "\n".join(completed.stderr.strip().splitlines()[-12:])
        raise RuntimeError(f"{program} exited {completed.returncode}:\n{tail}")
    return completed.stdout


def _ffmpeg(*args: str) -> None:
    _run("ffmpeg", ["-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args])


def probe(path: Path) -> dict[str, Any]:
    """Streams, chapters and format of a file, as the plain structure ffprobe prints."""
    out = _run(
        "ffprobe",
        [
            "-hide_banner", "-loglevel", "error", "-of", "json",
            "-show_streams", "-show_chapters", "-show_format", str(path),
        ],
    )
    result: dict[str, Any] = json.loads(out)
    return result


def _timestamp(seconds: float) -> str:
    whole = int(seconds)
    milliseconds = round((seconds - whole) * 1000)
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"


# -------------------------------------------------------------------- builders
def _subtitles(path: Path) -> None:
    blocks = []
    for index, (start, end, text) in enumerate(SUBTITLE_CUES, start=1):
        blocks.append(f"{index}\n{_timestamp(start)} --> {_timestamp(end)}\n{text}\n")
    path.write_text("\n".join(blocks), encoding="utf-8", newline="\n")


def _multitrack_mkv(path: Path, subtitles: Path) -> None:
    args: list[str] = ["-f", "lavfi", "-i",
                       f"testsrc2=size={FRAME_SIZE}:rate={FRAME_RATE}:duration={DURATION_S}"]
    for track in AUDIO_TRACKS:
        args += ["-f", "lavfi", "-i",
                 f"sine=frequency={track.frequency}:duration={DURATION_S}"
                 f":sample_rate={SAMPLE_RATE}"]
    args += ["-i", str(subtitles), "-map", "0:v"]
    for index in range(len(AUDIO_TRACKS)):
        args += ["-map", f"{index + 1}:a"]
    args += ["-map", f"{len(AUDIO_TRACKS) + 1}:s"]
    # mpeg4 and flac are built into ffmpeg itself: no external encoder library
    # has to be present for the suite to run.
    args += ["-c:v", "mpeg4", "-c:a", "flac", "-c:s", "srt"]
    for index, track in enumerate(AUDIO_TRACKS):
        args += [f"-metadata:s:a:{index}", f"language={track.language}"]
        if track.name:
            args += [f"-metadata:s:a:{index}", f"title={track.name}"]
    args += ["-metadata:s:s:0", "language=eng", str(path)]
    _ffmpeg(*args)


def _multitrack_mp4(path: Path) -> None:
    _ffmpeg(
        "-f", "lavfi", "-i",
        f"testsrc2=size={FRAME_SIZE}:rate={FRAME_RATE}:duration={DURATION_S}",
        "-f", "lavfi", "-i",
        f"sine=frequency={AUDIO_TRACKS[0].frequency}:duration={DURATION_S}"
        f":sample_rate={SAMPLE_RATE}",
        "-map", "0:v", "-map", "1:a",
        "-c:v", "mpeg4", "-c:a", "aac",
        "-metadata:s:a:0", f"language={AUDIO_TRACKS[0].language}",
        str(path),
    )


def _offset_pair(path: Path) -> None:
    delay = int(OFFSET_MS)
    _ffmpeg(
        "-f", "lavfi", "-i",
        f"anoisesrc=color=white:seed=20260923:duration={DURATION_S}"
        f":sample_rate={SAMPLE_RATE}",
        "-filter_complex",
        f"[0:a]asplit=2[plain][shifted];[shifted]adelay={delay}:all=1[delayed]",
        "-map", "[plain]", "-map", "[delayed]",
        "-c:a", "flac",
        "-metadata:s:a:0", "title=reference",
        "-metadata:s:a:1", f"title=delayed by {delay} ms",
        str(path),
    )


def _drift_pair(path: Path, work: Path) -> None:
    """Two transfers of one programme, differing in every way that matters.

    The two signals are built by ``tests.synthetic``, which is also what the
    unit tests measure, and written into one container through raw samples.
    That is deliberate: the file fixture and the in-memory pair are the same
    pair, so an answer that holds for one holds for the other and there is only
    one answer key to keep right.

    It is the one fixture that needs the alignment package to be importable,
    which is why it is optional: a checkout with nothing installed can still
    build everything else.
    """
    import numpy as np

    from tests.synthetic import synthetic_pair

    pair = synthetic_pair(sr=DRIFT_SAMPLE_RATE)
    raw = []
    for name, signal in (("reference", pair.reference), ("dub", pair.other)):
        target = work / f"drift_{name}.f32le"
        np.asarray(signal, dtype=np.float32).tofile(target)
        raw.append(target)
    args: list[str] = []
    for target in raw:
        args += [
            "-f", "f32le", "-ar", str(DRIFT_SAMPLE_RATE), "-ac", "1", "-i", str(target)
        ]
    args += [
        "-map", "0:a", "-map", "1:a", "-c:a", "flac",
        "-metadata:s:a:0", "title=reference",
        "-metadata:s:a:1", "title=a different transfer",
        str(path),
    ]
    _ffmpeg(*args)
    for target in raw:
        target.unlink(missing_ok=True)


def _chapter_metadata(path: Path, scale: float) -> None:
    lines = [";FFMETADATA1"]
    for index, start in enumerate(CHAPTER_TIMES_S):
        end = (
            CHAPTER_TIMES_S[index + 1]
            if index + 1 < len(CHAPTER_TIMES_S)
            else GRID_DURATION_S
        )
        lines += [
            "",
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={round(start * scale * 1000)}",
            f"END={round(end * scale * 1000)}",
            f"title={CHAPTER_NAMES[index]}",
        ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def _chapter_grid(path: Path, metadata: Path, scale: float) -> None:
    duration = round(GRID_DURATION_S * scale, 3)
    _ffmpeg(
        "-f", "lavfi", "-i",
        f"testsrc2=size={FRAME_SIZE}:rate={FRAME_RATE}:duration={duration}",
        "-f", "lavfi", "-i",
        f"sine=frequency={AUDIO_TRACKS[0].frequency}:duration={duration}"
        f":sample_rate={SAMPLE_RATE}",
        "-f", "ffmetadata", "-i", str(metadata),
        "-map", "0:v", "-map", "1:a", "-map_metadata", "2",
        "-c:v", "mpeg4", "-c:a", "flac",
        "-metadata:s:a:0", f"language={AUDIO_TRACKS[0].language}",
        str(path),
    )


def _tag_override(path: Path, source: Path, work: Path) -> None:
    """A copy of the multitrack file, plus a language tag that contradicts a header.

    The tag is merged into whatever the file already carries rather than
    replacing it, because writing tags replaces the whole element and a
    fixture built by throwing the existing ones away would not look like a
    file anybody actually has.
    """
    shutil.copyfile(source, path)
    identified = json.loads(_run("mkvmerge", ["-J", str(path)]))
    audio = [t for t in identified["tracks"] if t["type"] == "audio"]
    uid = audio[0]["properties"]["uid"]
    existing = _run("mkvextract", [str(path), "tags"]).lstrip("﻿").strip()
    added = "\n".join(
        [
            "  <Tag>",
            f"    <Targets><TrackUID>{uid}</TrackUID></Targets>",
            "    <Simple>",
            "      <Name>LANGUAGE</Name>",
            f"      <String>{TAG_OVERRIDE_LANGUAGE}</String>",
            "    </Simple>",
            "  </Tag>",
            "",
        ]
    )
    if "</Tags>" in existing:
        document = existing.replace("</Tags>", added + "</Tags>")
    else:
        document = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE Tags SYSTEM "matroskatags.dtd">\n'
            "<Tags>\n" + added + "</Tags>\n"
        )
    document_path = work / "tag_override.tags.xml"
    document_path.write_text(document, encoding="utf-8", newline="\n")
    _run("mkvpropedit", [str(path), "--tags", f"all:{document_path}"])


# ----------------------------------------------------------------------- build
def build(out_dir: Path | None = None, *, force: bool = False) -> dict[str, Path]:
    """Generate anything missing and return every fixture by name."""
    missing = ffmpeg_missing()
    if missing:
        raise RuntimeError(f"cannot build fixtures: {missing} not on the PATH")
    root = FIXTURE_DIR if out_dir is None else Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    built = {name: root / name for name in NAMES}
    if force:
        for path in built.values():
            path.unlink(missing_ok=True)

    if not built["sample_cues.srt"].exists():
        _subtitles(built["sample_cues.srt"])
    if not built["tiny_multitrack.mkv"].exists():
        _multitrack_mkv(built["tiny_multitrack.mkv"], built["sample_cues.srt"])
    if not built["tiny_multitrack.mp4"].exists():
        _multitrack_mp4(built["tiny_multitrack.mp4"])
    if not built["not_really_mkv.mkv"].exists():
        # the same bytes under the wrong extension: the container-type guard
        shutil.copyfile(built["tiny_multitrack.mp4"], built["not_really_mkv.mkv"])
    if not built["offset_pair.mka"].exists():
        _offset_pair(built["offset_pair.mka"])

    for name, scale in (("chapter_grid.mkv", 1.0), ("chapter_grid_pal.mkv", PAL_RATIO)):
        if not built[name].exists():
            metadata = root / f"{Path(name).stem}.ffmeta"
            _chapter_metadata(metadata, scale)
            _chapter_grid(built[name], metadata, scale)
    drift = root / "drift_pair.mka"
    if force:
        drift.unlink(missing_ok=True)
    if drift.exists():
        built["drift_pair.mka"] = drift
    elif _alignment_available():
        _drift_pair(drift, root)
        built["drift_pair.mka"] = drift
    if mkvtoolnix_missing() is None:
        optional = root / "tag_override.mkv"
        if force:
            optional.unlink(missing_ok=True)
        if not optional.exists():
            _tag_override(optional, built["tiny_multitrack.mkv"], root)
        built["tag_override.mkv"] = optional
    return built


def clean(out_dir: Path | None = None) -> int:
    root = FIXTURE_DIR if out_dir is None else Path(out_dir)
    removed = 0
    if root.is_dir():
        for path in sorted(root.iterdir()):
            if path.is_file():
                path.unlink()
                removed += 1
    return removed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.fixtures")
    parser.add_argument("action", choices=("build", "clean", "list"), nargs="?",
                        default="build")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    if args.action == "list":
        for name in (*NAMES, *OPTIONAL_NAMES):
            print(name)
        return 0
    if args.action == "clean":
        print(f"removed {clean(args.out)} file(s)")
        return 0
    missing = ffmpeg_missing()
    if missing:
        print(f"cannot build fixtures: {missing} not on the PATH", file=sys.stderr)
        return 1
    for name, path in build(args.out, force=args.force).items():
        print(f"{name:<24} {path.stat().st_size:>9} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
