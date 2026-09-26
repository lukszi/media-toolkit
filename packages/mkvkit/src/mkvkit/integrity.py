"""mkvkit.integrity -- is the payload actually there, and does it play?

Every other reader in this package reads headers. A header is a promise: the
container says it lasts forty minutes, the track list says there is video and
audio, the seek index points somewhere. None of it proves the bytes the
promise is about exist. A file whose download was preallocated and never
filled, or a copy that stopped half way, keeps its header intact -- and a
probe, a track comparison and a duration check all pass on it. A duplicate
pass that trusts those checks keeps the empty file and removes the one that
plays.

This module reads the payload, three ways, each cheaper than the next is
thorough:

**A sampled zero-fill read.** ``blocks`` evenly spaced blocks of
``block_size`` bytes, from the first byte to the last. Compressed media has
no long runs of zero bytes; a block that is nothing but zeros is space that
was reserved and never written. It costs a few dozen reads.

**A packet scan.** Every packet of every audio and video track is listed by
the demuxer -- a full read of the file, no decoding -- and each track's
timeline coverage is added up: how many seconds of the container's duration
the track actually has packets for, and how many bytes of the file are
payload at all. A track that covers a few seconds of a forty-minute
container is a file that is mostly holes, whatever its header says.

**A decode.** Optional and slowest: every audio and video track is decoded,
frame by frame, and each frame's duration is added up per track. What the
decoder complains about is reported, and so is how much of each track it
could actually turn into pictures and sound.

The answer is a :class:`IntegrityReport`. ``ok`` means every check that ran
found the payload present; ``evidence`` false means a check could not run
at all -- the file could not be read, or a program is missing -- and a
caller deciding whether a file may be relied on must treat that as a
refusal, never as a pass.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config
from .run import CommandFailed, Runner, default_runner
from .tools import ToolNotFound

__all__ = [
    "DEFAULT_BLOCKS",
    "DEFAULT_BLOCK_SIZE",
    "IntegrityReport",
    "Thresholds",
    "TrackCoverage",
    "ZeroSample",
    "check",
    "sample_zero_fill",
]

log = logging.getLogger(__name__)

#: How many blocks the zero-fill sample reads, evenly spaced over the file.
DEFAULT_BLOCKS = 64
#: How large each sampled block is. A run of this many zero bytes does not
#: occur in compressed audio or video.
DEFAULT_BLOCK_SIZE = 1 << 20

#: A packet or frame with no usable duration is taken to last until the next
#: one of its track, but never longer than this: a single stray packet far
#: past the end must not paint the whole gap as covered.
GAP_CAP_S = 1.0

#: The tracks whose coverage is measured. Subtitles are sparse by nature, and
#: a picture attached as cover art is one packet by design.
MEASURED_KINDS = ("video", "audio")


@dataclass(frozen=True)
class Thresholds:
    """Where "present" ends. Every value is a fraction between 0 and 1."""

    #: the largest share of sampled blocks that may be all zeros
    max_zero_fraction: float = 0.02
    #: the smallest share of the container's duration each audio and video
    #: track must have packets (and, when decoded, frames) for
    min_coverage: float = 0.90
    #: the smallest share of the file's bytes that must be packet payload
    min_payload_fraction: float = 0.50
    #: how many lines the decoder may complain before the file fails
    max_decode_errors: int = 0


@dataclass(frozen=True)
class ZeroSample:
    """What the sampled read found."""

    size: int
    block_size: int
    offsets: tuple[int, ...] = ()
    zero_offsets: tuple[int, ...] = ()

    @property
    def blocks(self) -> int:
        return len(self.offsets)

    @property
    def zero_blocks(self) -> int:
        return len(self.zero_offsets)

    @property
    def fraction(self) -> float:
        return self.zero_blocks / self.blocks if self.blocks else 0.0

    def __str__(self) -> str:
        return (
            f"{self.zero_blocks} of {self.blocks} sampled block(s) of "
            f"{self.block_size} bytes are all zeros ({self.fraction:.0%})"
        )


@dataclass(frozen=True)
class TrackCoverage:
    """One audio or video track: what the demuxer and the decoder found for it."""

    index: int
    kind: str
    codec: str | None = None
    packets: int = 0
    payload_bytes: int = 0
    covered_s: float = 0.0
    first_s: float | None = None
    last_s: float | None = None
    #: None when the file was not decoded
    frames: int | None = None
    decoded_s: float | None = None

    def share(self, duration_s: float | None, *, decoded: bool = False) -> float | None:
        seconds = self.decoded_s if decoded else self.covered_s
        if seconds is None or not duration_s:
            return None
        return seconds / duration_s

    def describe(self, duration_s: float | None) -> str:
        parts = [
            f"stream {self.index} {self.kind} {self.codec or '?'}: "
            f"{self.packets} packet(s), {self.covered_s:.1f} s of payload"
        ]
        share = self.share(duration_s)
        if share is not None:
            parts.append(f"({share:.0%} of the container)")
        if self.frames is not None and self.decoded_s is not None:
            parts.append(f"; decoded {self.frames} frame(s), {self.decoded_s:.1f} s")
        return " ".join(parts)


@dataclass(frozen=True)
class IntegrityReport:
    """Everything that was read, what it showed, and whether it is enough."""

    path: Path
    size: int | None = None
    container_duration_s: float | None = None
    zero: ZeroSample | None = None
    tracks: tuple[TrackCoverage, ...] = ()
    payload_bytes: int | None = None
    #: None when the file was not decoded; otherwise what the decoder said
    decode_errors: tuple[str, ...] | None = None
    problems: tuple[str, ...] = ()
    #: False when a check could not run at all: no answer, which is not a pass
    evidence: bool = True
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.evidence and not self.problems

    @property
    def decoded(self) -> bool:
        return self.decode_errors is not None

    def verdict(self) -> str:
        if not self.evidence:
            return "NO EVIDENCE"
        return "ok" if self.ok else "FAILED"

    def __str__(self) -> str:
        head = f"{self.path.name}: {self.verdict()}"
        facts: list[str] = []
        if self.size is not None:
            facts.append(f"{self.size} bytes")
        if self.container_duration_s is not None:
            facts.append(f"container {self.container_duration_s:.1f} s")
        if self.payload_bytes is not None and self.size:
            facts.append(f"payload {self.payload_bytes / self.size:.0%} of the file")
        lines = [head + (f" ({', '.join(facts)})" if facts else "")]
        if self.zero is not None:
            lines.append(f"  {self.zero}")
        lines += [f"  {t.describe(self.container_duration_s)}" for t in self.tracks]
        if self.decode_errors is not None:
            lines.append(f"  decoder: {len(self.decode_errors)} complaint(s)")
            lines += [f"    {e}" for e in self.decode_errors[:5]]
        else:
            lines.append("  not decoded")
        lines += [f"  PROBLEM: {p}" for p in self.problems]
        lines += [f"  note: {n}" for n in self.notes]
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        """A plain record, for a JSON report."""
        return {
            "path": str(self.path),
            "verdict": self.verdict(),
            "ok": self.ok,
            "evidence": self.evidence,
            "size": self.size,
            "container_duration_s": self.container_duration_s,
            "payload_bytes": self.payload_bytes,
            "zero_blocks": None if self.zero is None else self.zero.zero_blocks,
            "sampled_blocks": None if self.zero is None else self.zero.blocks,
            "tracks": [
                {
                    "index": t.index, "kind": t.kind, "codec": t.codec,
                    "packets": t.packets, "covered_s": round(t.covered_s, 3),
                    "frames": t.frames,
                    "decoded_s": None if t.decoded_s is None else round(t.decoded_s, 3),
                }
                for t in self.tracks
            ],
            "decode_errors": None if self.decode_errors is None
            else list(self.decode_errors),
            "problems": list(self.problems),
            "notes": list(self.notes),
        }


# ------------------------------------------------------------- zero-fill read
def sample_zero_fill(
    path: Path | str,
    *,
    blocks: int = DEFAULT_BLOCKS,
    block_size: int = DEFAULT_BLOCK_SIZE,
) -> ZeroSample:
    """Read ``blocks`` evenly spaced blocks and say which are all zeros.

    The first block starts at byte 0 and the last ends at the last byte, so
    both ends are always read. A file smaller than one block is read whole.
    Raises ``OSError`` when the file cannot be read.
    """
    if blocks < 1 or block_size < 1:
        raise ValueError("blocks and block_size are positive")
    target = Path(path)
    size = target.stat().st_size
    if size == 0:
        return ZeroSample(size=0, block_size=block_size)
    span = min(block_size, size)
    count = max(1, min(blocks, size // span))
    last = size - span
    offsets = tuple(
        sorted({0 if count == 1 else round(i * last / (count - 1)) for i in range(count)})
    )
    zeros: list[int] = []
    with target.open("rb", buffering=0) as handle:
        for offset in offsets:
            handle.seek(offset)
            block = handle.read(span)
            if block and not block.strip(b"\0"):
                zeros.append(offset)
    return ZeroSample(
        size=size, block_size=span, offsets=offsets, zero_offsets=tuple(zeros)
    )


# ---------------------------------------------------------------- the reading
def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


def _covered(spans: list[tuple[float, float | None]]) -> tuple[float, float | None, float | None]:
    """Seconds covered by (start, duration) spans, and the first and last instant."""
    if not spans:
        return 0.0, None, None
    spans.sort(key=lambda s: s[0])
    total = 0.0
    end: float | None = None
    for i, (start, duration) in enumerate(spans):
        if duration is None or duration <= 0:
            following = spans[i + 1][0] if i + 1 < len(spans) else start
            duration = min(max(following - start, 0.0), GAP_CAP_S)
        total += min(duration, GAP_CAP_S * 30)
        end = max(end if end is not None else start, start + duration)
    return total, spans[0][0], end


def _measured_streams(probed: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    out = []
    for stream in probed.get("streams") or []:
        if not isinstance(stream, Mapping):
            continue
        if stream.get("codec_type") not in MEASURED_KINDS:
            continue
        disposition = stream.get("disposition") or {}
        if isinstance(disposition, Mapping) and disposition.get("attached_pic"):
            continue
        out.append(stream)
    return out


_COMPACT = re.compile(r"([a-z_]+)=([^|]*)")


def _packet_scan(
    path: Path, streams: Sequence[int], run: Runner
) -> tuple[dict[int, list[tuple[float, float | None]]], dict[int, int]]:
    """Every packet's (start, duration) and the payload bytes, per stream."""
    result = run("ffprobe", [
        "-v", "error", "-of", "compact=p=0:nk=0",
        "-show_entries", "packet=stream_index,pts_time,dts_time,duration_time,size",
        str(path),
    ])
    wanted = set(streams)
    spans: dict[int, list[tuple[float, float | None]]] = {i: [] for i in streams}
    payload: dict[int, int] = dict.fromkeys(streams, 0)
    for line in result.stdout.splitlines():
        fields = dict(_COMPACT.findall(line))
        try:
            index = int(fields.get("stream_index", ""))
        except ValueError:
            continue
        if index not in wanted:
            continue
        start = _float(fields.get("pts_time"))
        if start is None:
            start = _float(fields.get("dts_time"))
        size = int(fields["size"]) if fields.get("size", "").isdigit() else 0
        payload[index] += size
        if start is not None:
            spans[index].append((start, _float(fields.get("duration_time"))))
    return spans, payload


_TIMEBASE = re.compile(r"^#tb (\d+): (\d+)/(\d+)")


def _decode(
    path: Path, streams: Sequence[int], run: Runner
) -> tuple[dict[int, list[tuple[float, float | None]]], tuple[str, ...]]:
    """Decode every measured track; each frame's (start, duration), per track."""
    args = ["-nostdin", "-v", "error", "-i", str(path)]
    for index in streams:
        args += ["-map", f"0:{index}"]
    args += ["-f", "framecrc", "-"]
    try:
        result = run("ffmpeg", args)
        stdout, stderr = result.stdout, result.stderr
        failed: list[str] = []
    except CommandFailed as exc:
        stdout, stderr = exc.result.stdout, exc.result.stderr
        failed = [f"the decoder exited {exc.result.returncode}"]
    timebase: dict[int, float] = {}
    spans: dict[int, list[tuple[float, float | None]]] = {i: [] for i in streams}
    for line in stdout.splitlines():
        found = _TIMEBASE.match(line)
        if found:
            out, num, den = (int(g) for g in found.groups())
            timebase[out] = num / den if den else 0.0
            continue
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            continue
        try:
            out, pts, duration = int(parts[0]), int(parts[2]), int(parts[3])
        except ValueError:
            continue
        if out >= len(streams):
            continue
        unit = timebase.get(out, 0.0)
        spans[streams[out]].append((pts * unit, duration * unit if duration > 0 else None))
    complaints = tuple(failed + [line.strip() for line in stderr.splitlines() if line.strip()])
    return spans, complaints


# ------------------------------------------------------------------ the check
def check(
    path: Path | str,
    *,
    decode: bool = True,
    blocks: int = DEFAULT_BLOCKS,
    block_size: int = DEFAULT_BLOCK_SIZE,
    thresholds: Thresholds | None = None,
    runner: Runner | None = None,
    config: Config | None = None,
) -> IntegrityReport:
    """Read the payload of one file and say whether it is there.

    Never raises for a file that is broken, missing or unreadable, or for a
    program that is not installed: each of those is a report, with
    ``evidence`` false when nothing could be measured.
    """
    limits = thresholds or Thresholds()
    target = Path(path)
    run = runner if runner is not None else default_runner(config)
    problems: list[str] = []
    notes: list[str] = []

    try:
        size = target.stat().st_size
        zero = sample_zero_fill(target, blocks=blocks, block_size=block_size)
    except OSError as exc:
        return IntegrityReport(
            path=target, evidence=False,
            problems=(f"the file cannot be read: {exc.strerror or exc}",),
        )
    if size == 0:
        return IntegrityReport(
            path=target, size=0, zero=zero, problems=("the file is empty",)
        )
    if zero.fraction > limits.max_zero_fraction:
        problems.append(
            f"{zero.zero_blocks} of {zero.blocks} sampled blocks are nothing but "
            "zero bytes: space that was reserved and never written"
        )

    try:
        probed_text = run("ffprobe", [
            "-v", "error", "-of", "json", "-show_streams", "-show_format", str(target),
        ]).stdout
        probed: dict[str, Any] = json.loads(probed_text or "{}")
    except (CommandFailed, ToolNotFound, json.JSONDecodeError, OSError) as exc:
        return IntegrityReport(
            path=target, size=size, zero=zero, evidence=False,
            problems=(*problems, f"the demuxer could not read the file: {_first_line(exc)}"),
        )

    fmt = probed.get("format") or {}
    duration = _float(fmt.get("duration"))
    measured = _measured_streams(probed)
    indices = [int(s.get("index") or 0) for s in measured]
    codecs = {int(s.get("index") or 0): s.get("codec_name") for s in measured}
    kinds = {int(s.get("index") or 0): str(s.get("codec_type")) for s in measured}
    if not measured:
        problems.append("the file has no audio or video track to measure")
    if not duration:
        problems.append("the container states no duration to measure against")

    try:
        spans, payload = _packet_scan(target, indices, run)
    except (CommandFailed, ToolNotFound, OSError) as exc:
        return IntegrityReport(
            path=target, size=size, container_duration_s=duration, zero=zero,
            evidence=False,
            problems=(*problems, f"the packets could not be listed: {_first_line(exc)}"),
        )

    decoded: dict[int, list[tuple[float, float | None]]] | None = None
    complaints: tuple[str, ...] | None = None
    if decode and indices:
        try:
            decoded, complaints = _decode(target, indices, run)
        except (ToolNotFound, OSError) as exc:
            return IntegrityReport(
                path=target, size=size, container_duration_s=duration, zero=zero,
                evidence=False,
                problems=(*problems, f"the file could not be decoded: {_first_line(exc)}"),
            )

    tracks: list[TrackCoverage] = []
    for index in indices:
        covered, first, last = _covered(spans[index])
        frames: int | None = None
        decoded_s: float | None = None
        if decoded is not None:
            frames = len(decoded[index])
            decoded_s, _, _ = _covered(decoded[index])
        track = TrackCoverage(
            index=index, kind=kinds[index], codec=codecs[index],
            packets=len(spans[index]), payload_bytes=payload[index],
            covered_s=covered, first_s=first, last_s=last,
            frames=frames, decoded_s=decoded_s,
        )
        tracks.append(track)
        share = track.share(duration)
        if share is not None and share < limits.min_coverage:
            problems.append(
                f"stream {index} ({track.kind}) has packets for {track.covered_s:.1f} s "
                f"of a {duration:.1f} s container ({share:.0%})"
            )
        decoded_share = track.share(duration, decoded=True)
        if decoded_share is not None and decoded_share < limits.min_coverage:
            problems.append(
                f"stream {index} ({track.kind}) decodes to {decoded_s:.1f} s "
                f"of a {duration:.1f} s container ({decoded_share:.0%})"
            )

    total_payload = sum(payload.values())
    if size and total_payload / size < limits.min_payload_fraction:
        problems.append(
            f"only {total_payload / size:.0%} of the file's bytes are audio or video "
            "payload"
        )
    if complaints is not None and len(complaints) > limits.max_decode_errors:
        problems.append(
            f"the decoder complained {len(complaints)} time(s)"
            + (f"; first: {complaints[0]}" if complaints else "")
        )
    if not decode:
        notes.append("not decoded: the payload was listed, not played")
    return IntegrityReport(
        path=target, size=size, container_duration_s=duration, zero=zero,
        tracks=tuple(tracks), payload_bytes=total_payload,
        decode_errors=complaints, problems=tuple(problems), notes=tuple(notes),
    )


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip() or type(exc).__name__
    return text.splitlines()[0]

