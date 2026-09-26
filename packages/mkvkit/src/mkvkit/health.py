"""mkvkit.health -- find the files in a library whose payload is not there.

A partial download keeps its header. The container says forty minutes, the
track list says video and audio, and the seek index is intact -- and most of
the file is bytes that were reserved and never written. Such a file sits in a
library for months looking healthy to everything that reads headers, until
somebody plays it, or until a duplicate pass keeps it and removes the copy
that played.

Reading every file whole would find them and would take days. This scan
finds them in two stages, the first cheap enough for a whole library:

**Stage 1, the sweep: every video file, a glance each.**

* a sampled zero-fill read -- ``blocks`` evenly spaced blocks, first byte to
  last; a block of nothing but zero bytes does not occur in compressed media;
* the container's own extent -- a Matroska segment, the top-level boxes of an
  MP4 or the chunks of an AVI say how many bytes the file should have, and a
  file shorter than that was cut off;
* the probe's durations -- the container's against each audio and video
  track's own, because a stray packet stamped far past the end, or a track
  that stops half way, shows as a disagreement nobody reads;
* size against duration and bitrate -- a file much smaller than its tracks'
  own statistics count, or one averaging implausibly little for its picture
  size over the duration it states.

A file that passes all four is OK. One that fails any is SUSPECT, with the
evidence. One that cannot be opened is UNREADABLE.

**Stage 2, the confirmation: only the suspects, read whole.**
:func:`mkvkit.integrity.check` -- every packet listed and every frame decoded
-- turns a SUSPECT into CORRUPT, or clears it to OK, or says UNREADABLE when
nothing could measure it.

**One reader per disk.** Both stages run through
:func:`mkvkit.lanes.map_by_device`: one worker per device, the devices side
by side, and a ``before_each`` hook where a gate can hold a lane while
somebody else is using its disk.

**Incremental and resumable.** Every answer is appended to a state file the
moment it is known, keyed by path, size and modification time and by the
settings it was measured with. A second run reads the file back and skips
every file that has not changed; a run that was interrupted resumes from the
last answer written.

**Read-only, always.** Nothing here moves, renames or deletes anything. What
it can do is print the manifest a person would release to have the corrupt
files parked.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import logging
import os
import re
import struct
import sys
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from . import integrity
from .config import Config
from .devices import Device
from .devices import device_of as volume_of
from .lanes import map_by_device
from .run import CommandFailed, Runner, default_runner
from .sidecars import VIDEO_SUFFIXES
from .walk import Skipped, walk

__all__ = [
    "AMBIGUOUS_SUFFIXES",
    "CORRUPT",
    "OK",
    "RULES",
    "SCAN_SUFFIXES",
    "SUSPECT",
    "UNREADABLE",
    "VERDICTS",
    "DeviceStats",
    "FileResult",
    "Progress",
    "ScanReport",
    "Settings",
    "StateFile",
    "Target",
    "confirm",
    "declared_end",
    "in_disc_structure",
    "iter_suspects",
    "manifest_rows",
    "not_a_stream",
    "plan",
    "register",
    "render_table",
    "sweep",
    "sweep_one",
    "write_json",
    "write_tsv",
]

log = logging.getLogger(__name__)

OK = "OK"
SUSPECT = "SUSPECT"
CORRUPT = "CORRUPT"
UNREADABLE = "UNREADABLE"
#: The verdicts, from best to worst.
VERDICTS: tuple[str, ...] = (OK, SUSPECT, CORRUPT, UNREADABLE)

#: The files a sweep reads: the server's video suffixes, less the ones that
#: are not a media container a demuxer opens (disc images, playlists, stub
#: files, split-archive parts).
SCAN_SUFFIXES = VIDEO_SUFFIXES - {
    ".001", ".asx", ".bin", ".ifo", ".img", ".iso", ".nrg", ".strm",
}

MIB = 1 << 20

#: The state file's schema. A record of another schema is ignored, which
#: costs a re-scan and never a wrong answer.
SCHEMA = 1

#: The version of the stage-1 rules. A rule change that can only clear a file
#: (never newly suspect one) bumps this, and a re-run reads again only the
#: suspects an older version answered for -- not the whole library.
RULES = 3

#: Folders a disc is copied into. The files in them are pieces of a disc --
#: menus, a few seconds of a logo, one cell of a title -- and their headers
#: describe the title they belong to, not the piece. Only the payload checks
#: (zero fill, extent) judge them.
DISC_FOLDERS = frozenset({"bdmv", "video_ts", "hvdvd_ts", "stream"})

#: A container shorter than this is a clip -- a sample, a trailer, a menu --
#: and its average bitrate says nothing about whether it is whole.
MIN_FLOOR_DURATION_S = 120.0

#: Suffixes the server counts as video that are also used for other things --
#: above all ``.ts``, which is a transport stream and a TypeScript source.
AMBIGUOUS_SUFFIXES = frozenset({".ts", ".tp", ".m2t", ".mts", ".m2ts"})


# ------------------------------------------------------------------ settings
@dataclass(frozen=True)
class Settings:
    """Everything a stage-1 answer depends on. Change any and files are read again."""

    #: evenly spaced blocks sampled per file
    blocks: int = 16
    block_size: int = MIB
    #: the largest share of sampled blocks that may be all zeros
    max_zero_fraction: float = integrity.Thresholds().max_zero_fraction
    #: a track may state a duration this far from the container's: whichever
    #: of the two is larger, seconds or a share of the container
    duration_tolerance_s: float = 30.0
    duration_tolerance: float = 0.05
    #: the file's size against what its tracks' own statistics count
    min_size_ratio: float = 0.85
    max_size_ratio: float = 1.6
    #: the most bits per second any file here plausibly averages
    max_bitrate: float = 250e6

    def fingerprint(self) -> str:
        """Twelve hex digits that change whenever any setting does."""
        text = json.dumps(asdict(self), sort_keys=True)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]

    def thresholds(self) -> integrity.Thresholds:
        return replace(integrity.Thresholds(), max_zero_fraction=self.max_zero_fraction)


#: The least a video stream of at least this many lines plausibly averages,
#: in bits per second, over the whole file. Far below what any encoder makes
#: of a real picture of that size, so only a file that is mostly missing, or
#: whose stated duration is wrong, falls under it.
BITRATE_FLOORS: tuple[tuple[int, float], ...] = (
    (2000, 1.0e6),
    (1000, 400e3),
    (700, 250e3),
    (0, 80e3),
)


# ------------------------------------------------------------------- results
@dataclass(frozen=True)
class FileResult:
    """One file's verdict, what it was measured with, and why."""

    path: str
    size: int | None
    mtime_ns: int | None
    device: Device
    verdict: str
    stage: int = 1
    evidence: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    zero_blocks: int | None = None
    sampled_blocks: int | None = None
    duration_s: float | None = None
    settings: str = ""
    scanned_at: str = ""
    seconds: float = 0.0
    bytes_read: int = 0
    #: False when the file only carries a video suffix: it is reported, not judged
    media: bool = True
    rules: int = RULES
    #: filled in from the server when asked; never stored in the state file
    item_id: str | None = None
    title: str | None = None

    @property
    def rank(self) -> int:
        return VERDICTS.index(self.verdict) if self.verdict in VERDICTS else len(VERDICTS)

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["evidence"] = list(self.evidence)
        out["notes"] = list(self.notes)
        return out

    def state_record(self) -> dict[str, Any]:
        record = self.as_dict()
        record.pop("item_id", None)
        record.pop("title", None)
        record["schema"] = SCHEMA
        return record

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> FileResult:
        known = {f for f in cls.__dataclass_fields__ if f not in ("item_id", "title")}
        values = {k: v for k, v in record.items() if k in known}
        values["evidence"] = tuple(values.get("evidence") or ())
        values["notes"] = tuple(values.get("notes") or ())
        values.setdefault("rules", 1)
        return cls(**values)


@dataclass(frozen=True)
class Target:
    """One file queued for a stage, with what it measured as when it was queued."""

    path: Path
    size: int
    mtime_ns: int
    device: Device
    #: the stage-1 answer a confirmation starts from
    previous: FileResult | None = None


# ---------------------------------------------------------------- state file
class StateFile:
    """Append-only JSON lines: the latest record per path wins.

    Each answer is written and flushed as soon as it is known, so a run that
    stops -- interrupted, out of time, or the machine gone -- loses at most the
    files that were being read at that moment. A torn last line is ignored.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._handle: TextIO | None = None

    @staticmethod
    def key(path: Path | str) -> str:
        text = str(path).replace("\\", "/")
        return text.casefold() if os.name == "nt" else text

    def load(self) -> dict[str, FileResult]:
        """The latest record for every path the file knows."""
        latest: dict[str, FileResult] = {}
        if not self.path.is_file():
            return latest
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    log.debug("ignoring a torn line in %s", self.path)
                    continue
                if not isinstance(record, dict) or record.get("schema") != SCHEMA:
                    continue
                try:
                    found = FileResult.from_record(record)
                except TypeError:
                    continue
                latest[self.key(found.path)] = found
        return latest

    def append(self, result: FileResult) -> None:
        line = json.dumps(result.state_record(), ensure_ascii=False)
        with self._lock:
            if self._handle is None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._handle = self.path.open("a", encoding="utf-8", newline="\n")
            self._handle.write(line + "\n")
            self._handle.flush()

    def close(self) -> None:
        with self._lock:
            if self._handle is not None:
                self._handle.close()
                self._handle = None

    def compact(self) -> None:
        """Rewrite the file with one line per path, atomically."""
        self.close()
        latest = self.load()
        if not latest:
            return
        temporary = self.path.with_name(self.path.name + ".compact")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            for result in latest.values():
                handle.write(json.dumps(result.state_record(), ensure_ascii=False) + "\n")
        os.replace(temporary, self.path)


# ------------------------------------------------------------------ progress
@dataclass
class DeviceStats:
    """One device's share of a stage: how much, how fast, how long is left."""

    device: Device
    total: int = 0
    total_bytes: int = 0
    done: int = 0
    done_bytes: int = 0
    read_bytes: int = 0
    unchanged: int = 0
    #: files the walk found on this device, when only a subset is scanned
    population: int = 0
    started: float | None = None
    finished: float | None = None
    busy_s: float = 0.0
    waiting: str | None = None

    def elapsed(self, now: float) -> float:
        if self.started is None:
            return 0.0
        return (self.finished or now) - self.started

    def rate(self, now: float) -> float:
        """Files per second of wall time on this lane."""
        spent = self.elapsed(now)
        return self.done / spent if spent > 0 else 0.0

    def eta_s(self, now: float) -> float | None:
        rate = self.rate(now)
        if rate <= 0:
            return None
        return (self.total - self.done) / rate

    def projected_s(self, now: float) -> float | None:
        """What every file the walk found on this device would take at this pace."""
        rate = self.rate(now)
        population = self.population or self.total
        return population / rate if rate > 0 else None

    def line(self, now: float) -> str:
        share = f"{self.done / self.total:.0%}" if self.total else "-"
        spent = self.elapsed(now)
        read = self.read_bytes / spent / MIB if spent > 0 else 0.0
        eta = self.eta_s(now)
        text = (
            f"{self.device}: {self.done}/{self.total} file(s) ({share}), "
            f"{self.rate(now):.2f} file(s)/s, read {read:.1f} MiB/s, "
            f"elapsed {_clock(spent)}"
        )
        if self.done < self.total:
            text += f", ETA {_clock(eta) if eta is not None else '?'}"
        if self.waiting:
            text += f" -- waiting: {self.waiting}"
        return text

    def as_dict(self, now: float) -> dict[str, Any]:
        spent = self.elapsed(now)
        return {
            "device": self.device,
            "files": self.total,
            "scanned": self.done,
            "unchanged": self.unchanged,
            "population": self.population or self.total,
            "bytes_in_files": self.done_bytes,
            "bytes_read": self.read_bytes,
            "elapsed_s": round(spent, 1),
            "files_per_s": round(self.rate(now), 3),
            "read_mib_per_s": round(self.read_bytes / spent / MIB, 2) if spent > 0 else 0.0,
            "projected_s": None if self.projected_s(now) is None
            else round(self.projected_s(now) or 0.0, 1),
        }


def _clock(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    whole = int(max(0.0, seconds))
    return f"{whole // 3600}:{whole % 3600 // 60:02d}:{whole % 60:02d}"


class Progress:
    """Per-device counters, and a line per device every ``every_s`` seconds."""

    def __init__(
        self,
        *,
        every_s: float = 30.0,
        out: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.devices: dict[Device, DeviceStats] = {}
        self.every_s = every_s
        self._out = out or (lambda line: print(line, file=sys.stderr, flush=True))
        self._clock = clock
        self._lock = threading.Lock()
        self._last = clock()

    def now(self) -> float:
        return self._clock()

    def plan(self, targets: Iterable[Target], *, unchanged: Mapping[Device, int] | None = None,
             population: Mapping[Device, int] | None = None) -> None:
        for target in targets:
            stats = self.devices.setdefault(target.device, DeviceStats(target.device))
            stats.total += 1
            stats.total_bytes += target.size
        for device, count in (unchanged or {}).items():
            self.devices.setdefault(device, DeviceStats(device)).unchanged += count
        for device, count in (population or {}).items():
            self.devices.setdefault(device, DeviceStats(device)).population = count

    def start(self, device: Device) -> None:
        with self._lock:
            stats = self.devices.setdefault(device, DeviceStats(device))
            if stats.started is None:
                stats.started = self._clock()
            stats.waiting = None

    def waiting(self, device: Device, why: str | None) -> None:
        with self._lock:
            self.devices.setdefault(device, DeviceStats(device)).waiting = why
        if why:
            self._out(f"{device}: waiting -- {why}")

    def done(self, device: Device, *, size: int, read: int, seconds: float) -> None:
        with self._lock:
            stats = self.devices.setdefault(device, DeviceStats(device))
            stats.done += 1
            stats.done_bytes += size
            stats.read_bytes += read
            stats.busy_s += seconds
            now = self._clock()
            if stats.done >= stats.total:
                stats.finished = now
            due = now - self._last >= self.every_s
            if due:
                self._last = now
        if due:
            self.report()

    def report(self) -> None:
        now = self._clock()
        with self._lock:
            lines = [stats.line(now) for _, stats in sorted(self.devices.items())]
        for line in lines:
            self._out(line)

    def summary(self) -> list[dict[str, Any]]:
        now = self._clock()
        with self._lock:
            return [stats.as_dict(now) for _, stats in sorted(self.devices.items())]


# ---------------------------------------------------------- container extent
_EBML = b"\x1a\x45\xdf\xa3"
_SEGMENT = b"\x18\x53\x80\x67"
_BOXES = {b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide", b"pnot", b"uuid", b"styp"}


def _vint(data: bytes, pos: int) -> tuple[int | None, int] | None:
    """A Matroska size at ``pos``: (value or None when unknown, its length)."""
    if pos >= len(data) or data[pos] == 0:
        return None
    first = data[pos]
    length = 9 - first.bit_length()
    if pos + length > len(data):
        return None
    value = first & ((1 << (8 - length)) - 1)
    for byte in data[pos + 1:pos + length]:
        value = (value << 8) | byte
    unknown = value == (1 << (7 * length)) - 1
    return (None if unknown else value), length


def declared_end(path: Path | str, *, max_boxes: int = 100_000) -> int | None:
    """How many bytes the container says the file has; None when it does not say.

    Matroska: the end of the segment its header opens (unless the segment's
    size is "unknown", as a live recording writes it). MP4 and QuickTime: the
    end of the last top-level box, walked box by box. AVI: the end of the
    last top-level chunk. A file shorter than this was cut off.
    """
    target = Path(path)
    size = target.stat().st_size
    with target.open("rb") as handle:
        head = handle.read(4096)
        if head.startswith(_EBML):
            return _matroska_end(head)
        if len(head) >= 8 and head[4:8] in _BOXES:
            return _chunked_end(handle, size, big_endian=True, max_items=max_boxes)
        if head.startswith(b"RIFF") and head[8:12] == b"AVI ":
            return _chunked_end(handle, size, big_endian=False, max_items=max_boxes)
    return None


def _matroska_end(head: bytes) -> int | None:
    found = _vint(head, 4)
    if found is None or found[0] is None:
        return None
    pos = 4 + found[1] + found[0]
    if head[pos:pos + 4] != _SEGMENT:
        return None
    found = _vint(head, pos + 4)
    if found is None or found[0] is None:
        return None
    return pos + 4 + found[1] + found[0]


def _chunked_end(handle: Any, size: int, *, big_endian: bool, max_items: int) -> int | None:
    """Walk top-level boxes (MP4) or chunks (RIFF) and return where the last one ends."""
    offset = 0
    for _ in range(max_items):
        if offset >= size:
            return offset
        handle.seek(offset)
        header = handle.read(16)
        if len(header) < 8:
            return offset + 8
        if big_endian:
            length = struct.unpack(">I", header[:4])[0]
            if length == 1:
                if len(header) < 16:
                    return offset + 16
                length = struct.unpack(">Q", header[8:16])[0]
            elif length == 0:
                return size
            if length < 8:
                return None
        else:
            length = 8 + struct.unpack("<I", header[4:8])[0]
            length += length & 1
        offset += length
    return None


# -------------------------------------------------------------------- stage 1
_SYNC = 0x47


def not_a_stream(path: Path | str, *, probe: int = 4096) -> str | None:
    """What a file with a transport-stream suffix is instead, or None if it is one.

    A transport stream carries a sync byte every 188 bytes (every 192 in the
    variant with a timestamp in front). A file whose start has neither pattern
    and reads as text is source code, a subtitle or a note that happens to
    share the suffix. A file that starts with zeros is not text: it stays a
    media file, and the sweep judges it.
    """
    with Path(path).open("rb") as handle:
        head = handle.read(probe)
    if not head:
        return None
    for stride, first in ((188, 0), (192, 4)):
        offsets = range(first, min(len(head), first + stride * 5), stride)
        if offsets and all(head[i] == _SYNC for i in offsets if i < len(head)):
            return None
    if b"\x00" in head:
        return None
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as exc:
        if exc.start < len(head) - 4:  # a character cut by the probe's end is fine
            return None
    return "text, with no transport-stream sync pattern"


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


_CLOCK = re.compile(r"^(\d+):(\d{1,2}):(\d{1,2}(?:\.\d+)?)$")


def _clock_value(value: Any) -> float | None:
    found = _CLOCK.match(str(value).strip()) if value is not None else None
    if not found:
        return None
    hours, minutes, seconds = found.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _tag(stream: Mapping[str, Any], name: str) -> Any:
    """A Matroska statistics tag, whatever language suffix it was written with."""
    tags = stream.get("tags") or {}
    if not isinstance(tags, Mapping):
        return None
    upper = name.upper()
    for key, value in tags.items():
        key_text = str(key).upper()
        if key_text == upper or key_text.startswith(upper + "-"):
            return value
    return None


def _measured(probed: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    out = []
    for stream in probed.get("streams") or []:
        if not isinstance(stream, Mapping):
            continue
        if stream.get("codec_type") not in ("video", "audio"):
            continue
        disposition = stream.get("disposition") or {}
        if isinstance(disposition, Mapping) and disposition.get("attached_pic"):
            continue
        out.append(stream)
    return out


def _stream_duration(stream: Mapping[str, Any]) -> float | None:
    stated = _number(stream.get("duration"))
    return stated if stated is not None else _clock_value(_tag(stream, "DURATION"))


def _stream_bitrate(stream: Mapping[str, Any]) -> float | None:
    stated = _number(stream.get("bit_rate"))
    return stated if stated is not None else _number(_tag(stream, "BPS"))


def in_disc_structure(path: Path | str) -> bool:
    """Whether a file sits inside a copied disc's folder structure."""
    return any(part.casefold() in DISC_FOLDERS for part in Path(path).parent.parts)


def probe_findings(
    probed: Mapping[str, Any], size: int, settings: Settings, *, disc: bool = False
) -> tuple[list[str], float | None]:
    """What the probe's durations and bitrates say against the file's size.

    ``disc`` marks a piece of a copied disc, whose headers describe the whole
    title: only a missing track or duration is reported for it.
    """
    findings: list[str] = []
    fmt = probed.get("format") or {}
    container = _number(fmt.get("duration")) if isinstance(fmt, Mapping) else None
    streams = _measured(probed)
    if not streams:
        findings.append("the probe found no audio or video track")
        return findings, container
    if not container or container <= 0:
        findings.append("the container states no duration")
        return findings, container
    if disc:
        return findings, container

    tolerance = max(settings.duration_tolerance_s, settings.duration_tolerance * container)
    stated = [(s, _stream_duration(s)) for s in streams]
    for stream, seconds in stated:
        if seconds is not None and abs(seconds - container) > tolerance:
            findings.append(
                f"stream {stream.get('index')} ({stream.get('codec_type')}) states "
                f"{seconds:.1f} s against a {container:.1f} s container"
            )

    counted = [_number(_tag(s, "NUMBER_OF_BYTES")) for s in streams]
    rates = [_stream_bitrate(s) for s in streams]
    expected: float | None = None
    source = ""
    if all(c is not None for c in counted):
        expected = sum(c or 0.0 for c in counted)
        source = "its tracks' statistics count"
    elif all(r is not None for r in rates):
        expected = sum(r or 0.0 for r in rates) * container / 8
        source = "its tracks' stated bitrates and the container's duration imply"
    if expected:
        ratio = size / expected
        if ratio < settings.min_size_ratio:
            findings.append(
                f"the file holds {ratio:.0%} of the {expected / MIB:,.0f} MiB {source}: "
                "cut off, or never finished"
            )
        elif ratio > settings.max_size_ratio:
            findings.append(
                f"the file is {ratio:.1f} times the {expected / MIB:,.0f} MiB {source}: "
                "space the tracks do not account for"
            )

    average = size * 8 / container
    heights = [int(_number(s.get("height")) or 0) for s in streams
               if s.get("codec_type") == "video"]
    if heights and container >= MIN_FLOOR_DURATION_S:
        tallest = max(heights)
        floor = next(rate for lines, rate in BITRATE_FLOORS if tallest >= lines)
        if average < floor:
            findings.append(
                f"the file averages {average / 1e3:,.0f} kbit/s over its stated "
                f"{container:.0f} s, implausibly little for {tallest}-line video"
            )
    if average > settings.max_bitrate:
        findings.append(
            f"the file averages {average / 1e6:,.0f} Mbit/s over its stated "
            f"{container:.1f} s: the stated duration is too short for its size"
        )
    return findings, container


def sweep_one(
    target: Target,
    settings: Settings,
    *,
    runner: Runner,
) -> FileResult:
    """Stage 1 for one file: the zero sample, the extent, the probe. Never raises
    for a broken file; a missing program raises, because no file can be judged."""
    started = time.monotonic()
    path = target.path
    base = FileResult(
        path=str(path), size=target.size, mtime_ns=target.mtime_ns, device=target.device,
        verdict=OK, settings=settings.fingerprint(),
        scanned_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    evidence: list[str] = []
    notes: list[str] = []
    try:
        stat = path.stat()
        size = stat.st_size
        zero = integrity.sample_zero_fill(
            path, blocks=settings.blocks, block_size=settings.block_size
        )
    except OSError as exc:
        return replace(
            base, verdict=UNREADABLE, seconds=time.monotonic() - started,
            evidence=(f"the file cannot be read: {exc.strerror or exc}",),
        )
    read = zero.blocks * zero.block_size
    base = replace(base, size=size, mtime_ns=stat.st_mtime_ns,
                   zero_blocks=zero.zero_blocks, sampled_blocks=zero.blocks)
    if size == 0:
        return replace(base, verdict=SUSPECT, evidence=("the file is empty",),
                       seconds=time.monotonic() - started)
    if path.suffix.lower() in AMBIGUOUS_SUFFIXES:
        what = not_a_stream(path)
        if what is not None:
            return replace(
                base, verdict=OK, media=False, seconds=time.monotonic() - started,
                bytes_read=read, notes=(f"not a media file: {what}; not judged",),
            )
    if zero.fraction > settings.max_zero_fraction:
        evidence.append(
            f"{zero.zero_blocks} of {zero.blocks} sampled blocks are nothing but zero "
            f"bytes ({zero.fraction:.0%}): space that was reserved and never written"
        )
    elif zero.zero_blocks:
        notes.append(str(zero))

    try:
        end = declared_end(path)
    except OSError as exc:
        end = None
        notes.append(f"the container's extent could not be read: {exc.strerror or exc}")
    if end is not None and end > size:
        evidence.append(
            f"the container declares {end:,} bytes and the file has {size:,} "
            f"({size / end:.0%}): cut off"
        )

    duration: float | None = None
    try:
        result = runner("ffprobe", [
            "-v", "error", "-of", "json", "-show_format", "-show_streams", str(path),
        ])
        probed = json.loads(result.stdout or "{}")
        complaints = [line.strip() for line in result.stderr.splitlines() if line.strip()]
        if complaints:
            notes.append(f"the demuxer said: {complaints[0]}"
                         + (f" (and {len(complaints) - 1} more)" if len(complaints) > 1 else ""))
        disc = in_disc_structure(path)
        found, duration = probe_findings(probed, size, settings, disc=disc)
        if disc:
            notes.append("a piece of a copied disc: judged by its payload only")
        evidence += found
    except CommandFailed as exc:
        first = (exc.result.stderr.strip() or exc.result.stdout.strip()).splitlines()
        evidence.append(
            "the demuxer could not read the header"
            + (f": {first[0]}" if first else "")
        )
    except (json.JSONDecodeError, OSError) as exc:
        evidence.append(f"the probe's answer could not be read: {exc}")

    return replace(
        base,
        verdict=SUSPECT if evidence else OK,
        evidence=tuple(evidence), notes=tuple(notes), duration_s=duration,
        seconds=time.monotonic() - started, bytes_read=read,
    )


# ------------------------------------------------------------------ planning
@dataclass
class ScanReport:
    """Everything a run found, and what it did not get to."""

    results: list[FileResult] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    not_scanned: list[str] = field(default_factory=list)
    devices: list[dict[str, Any]] = field(default_factory=list)
    confirmed: list[dict[str, Any]] = field(default_factory=list)

    def by_verdict(self, verdict: str) -> list[FileResult]:
        return [r for r in self.results if r.verdict == verdict]

    def counts(self) -> dict[str, int]:
        return {v: len(self.by_verdict(v)) for v in VERDICTS}

    def not_media(self) -> list[FileResult]:
        return [r for r in self.results if not r.media]


def plan(
    roots: Sequence[Path | str],
    *,
    state: Mapping[str, FileResult],
    settings: Settings,
    device_of: Callable[[Path | str], Device] = volume_of,
    exclude: Sequence[str] = (),
    exclude_paths: Sequence[Path | str] = (),
    suffixes: Iterable[str] = SCAN_SUFFIXES,
    rescan: bool = False,
    subset: int | None = None,
    retry_unreadable: bool = True,
) -> tuple[list[Target], list[FileResult], list[Skipped], dict[Device, int]]:
    """Walk the roots and split what was found into work and known answers.

    Returns the targets to read, the answers the state file already holds for
    files that have not changed, what the walk left out, and how many files
    the walk found per device. With ``subset``, at most that many targets per
    device, spread evenly over its files in walk order.
    """
    fingerprint = settings.fingerprint()
    wanted = frozenset(s.lower() for s in suffixes)
    targets: list[Target] = []
    known: list[FileResult] = []
    skipped: list[Skipped] = []
    seen: set[str] = set()
    for root in roots:
        walked = walk(root, exclude=exclude, exclude_paths=exclude_paths, suffixes=wanted)
        for entry in walked:
            if entry.is_dir:
                continue
            key = StateFile.key(entry.path)
            if key in seen:
                continue
            seen.add(key)
            try:
                stat = entry.path.stat()
            except OSError as exc:
                known.append(FileResult(
                    path=str(entry.path), size=entry.size, mtime_ns=None,
                    device=device_of(entry.path), verdict=UNREADABLE, settings=fingerprint,
                    evidence=(f"the file cannot be read: {exc.strerror or exc}",),
                ))
                continue
            device = device_of(entry.path)
            previous = state.get(key)
            if (
                not rescan and previous is not None
                and previous.size == stat.st_size and previous.mtime_ns == stat.st_mtime_ns
                and previous.settings == fingerprint
                and not (retry_unreadable and previous.verdict == UNREADABLE)
                and not (previous.verdict == SUSPECT and previous.stage == 1
                         and previous.rules < RULES)
            ):
                known.append(replace(previous, device=device))
                continue
            targets.append(Target(entry.path, stat.st_size, stat.st_mtime_ns, device))
        skipped += walked.skipped
    population: dict[Device, int] = {}
    for target in targets:
        population[target.device] = population.get(target.device, 0) + 1
    if subset is not None:
        targets = _spread(targets, subset)
    return targets, known, skipped, population


def _spread(targets: list[Target], per_device: int) -> list[Target]:
    """At most ``per_device`` targets per device, evenly spaced through each."""
    grouped: dict[Device, list[Target]] = {}
    for target in targets:
        grouped.setdefault(target.device, []).append(target)
    out: list[Target] = []
    for items in grouped.values():
        if len(items) <= per_device:
            out += items
            continue
        step = len(items) / per_device
        out += [items[int(i * step)] for i in range(per_device)]
    return out


# --------------------------------------------------------------------- stages
class OutOfTime(RuntimeError):
    """The run's time budget ran out before this file was started."""


def _lanes(
    targets: Sequence[Target],
    work: Callable[[Target], FileResult],
    *,
    progress: Progress,
    before_each: Callable[[Device, Target], None] | None,
    deadline: float | None,
    device_key: Callable[[Path | str], Device],
) -> tuple[list[FileResult], list[str]]:
    """Run one stage over the targets, one reader per device."""

    def gate(device: Device, target: Target) -> None:
        if deadline is not None and progress.now() >= deadline:
            raise OutOfTime(target.path)
        if before_each is not None:
            before_each(device, target)
        progress.start(device)

    def run(target: Target) -> FileResult:
        found = work(target)
        progress.done(target.device, size=target.size, read=found.bytes_read,
                      seconds=found.seconds)
        return found

    by_path = {StateFile.key(t.path): t.device for t in targets}
    outcomes = map_by_device(
        run, targets, path_of=lambda t: t.path,
        device_of=lambda p: by_path.get(StateFile.key(p)) or device_key(p),
        before_each=gate,
    )
    results: list[FileResult] = []
    not_scanned: list[str] = []
    for outcome in outcomes:
        if outcome.ok and outcome.result is not None:
            results.append(outcome.result)
        else:
            reason = outcome.error
            not_scanned.append(f"{outcome.item.path}: {_why(reason)}")
    return results, not_scanned


def _why(error: BaseException | None) -> str:
    if isinstance(error, OutOfTime):
        return "not started: the time budget ran out"
    text = str(error).strip() if error is not None else ""
    return (text.splitlines()[0] if text else type(error).__name__) or "unknown"


def sweep(
    targets: Sequence[Target],
    settings: Settings,
    *,
    state: StateFile | None = None,
    runner: Runner | None = None,
    config: Config | None = None,
    progress: Progress | None = None,
    before_each: Callable[[Device, Target], None] | None = None,
    deadline: float | None = None,
    check: Callable[[Target], FileResult] | None = None,
    device_of: Callable[[Path | str], Device] = volume_of,
) -> tuple[list[FileResult], list[str]]:
    """Stage 1 over every target: one reader per device, each answer saved as it comes."""
    run = runner or default_runner(config)
    measure = check or (lambda target: sweep_one(target, settings, runner=run))
    watch = progress or Progress(every_s=float("inf"), out=lambda _l: None)

    def work(target: Target) -> FileResult:
        found = measure(target)
        if state is not None:
            state.append(found)
        return found

    return _lanes(targets, work, progress=watch, before_each=before_each,
                  deadline=deadline, device_key=device_of)


def confirm(
    suspects: Sequence[FileResult],
    *,
    decode: bool = True,
    blocks: int = integrity.DEFAULT_BLOCKS,
    settings: Settings | None = None,
    state: StateFile | None = None,
    runner: Runner | None = None,
    config: Config | None = None,
    progress: Progress | None = None,
    before_each: Callable[[Device, Target], None] | None = None,
    deadline: float | None = None,
    check: Callable[..., integrity.IntegrityReport] | None = None,
    device_of: Callable[[Path | str], Device] = volume_of,
) -> tuple[list[FileResult], list[str]]:
    """Stage 2: read each suspect whole, one per device, and give it its verdict."""
    chosen = settings or Settings()
    measure = check or integrity.check
    watch = progress or Progress(every_s=float("inf"), out=lambda _l: None)
    targets = [
        Target(Path(s.path), s.size or 0, s.mtime_ns or 0, s.device, previous=s)
        for s in suspects
    ]

    def work(target: Target) -> FileResult:
        started = time.monotonic()
        before = target.previous
        if before is None:  # pragma: no cover - every target here is built with one
            raise ValueError(f"{target.path}: nothing to confirm")
        report = measure(
            target.path, decode=decode, blocks=max(blocks, chosen.blocks),
            block_size=chosen.block_size, thresholds=chosen.thresholds(),
            runner=runner, config=config,
        )
        if not report.evidence:
            # Opened but not demuxable is a file nothing can play; not opened
            # at all, or a program missing, is no answer.
            unplayable = report.size is not None and any(
                p.startswith("the demuxer could not read") for p in report.problems
            )
            verdict = CORRUPT if unplayable else UNREADABLE
        elif report.ok:
            verdict = OK
        else:
            verdict = CORRUPT
        evidence = [f"stage 1: {line}" for line in before.evidence]
        if verdict == OK:
            evidence.append(
                "stage 2: every track's packets cover the container"
                + (" and decode without complaint" if decode else "")
                + "; cleared"
            )
        else:
            evidence += [f"stage 2: {problem}" for problem in report.problems]
        found = replace(
            before, verdict=verdict, stage=2, evidence=tuple(evidence),
            notes=before.notes + report.notes,
            zero_blocks=None if report.zero is None else report.zero.zero_blocks,
            sampled_blocks=None if report.zero is None else report.zero.blocks,
            duration_s=report.container_duration_s or before.duration_s,
            scanned_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            seconds=time.monotonic() - started,
            bytes_read=report.size or 0,
        )
        if state is not None:
            state.append(found)
        return found

    return _lanes(targets, work, progress=watch, before_each=before_each,
                  deadline=deadline, device_key=device_of)


# -------------------------------------------------------------------- output
TSV_COLUMNS: tuple[str, ...] = (
    "verdict", "stage", "device", "path", "size", "zero_blocks", "sampled_blocks",
    "duration_s", "item_id", "title", "evidence", "notes",
)


def _ordered(results: Iterable[FileResult]) -> list[FileResult]:
    return sorted(results, key=lambda r: (-r.rank, r.device, r.path.casefold()))


def render_table(results: Iterable[FileResult], *, include_ok: bool = False) -> str:
    """A verdict per line and the evidence under it, worst first."""
    lines: list[str] = []
    for result in _ordered(results):
        if result.verdict == OK and not include_ok:
            continue
        size = f"{(result.size or 0) / MIB:,.0f} MiB"
        head = f"{result.verdict:<10} {result.device:<4} {size:>10}  {result.path}"
        lines.append(head)
        if result.title or result.item_id:
            lines.append(f"{'':<17}server: {result.title or '?'} [{result.item_id or '-'}]")
        lines += [f"{'':<17}- {line}" for line in result.evidence]
    return "\n".join(lines)


def write_json(path: Path | str, report: ScanReport, *, meta: Mapping[str, Any]) -> None:
    document = {
        **meta,
        "counts": report.counts(),
        "not_media": len(report.not_media()),
        "devices": report.devices,
        "confirmed": report.confirmed,
        "not_scanned": report.not_scanned,
        "skipped": [str(s) for s in report.skipped],
        "results": [r.as_dict() for r in _ordered(report.results)],
    }
    Path(path).write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def tsv_text(results: Iterable[FileResult], *, include_ok: bool = True) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter="\t", lineterminator="\n")
    writer.writerow(TSV_COLUMNS)
    for result in _ordered(results):
        if result.verdict == OK and not include_ok:
            continue
        row = result.as_dict()
        row["evidence"] = " | ".join(result.evidence)
        row["notes"] = " | ".join(result.notes)
        writer.writerow(["" if row.get(c) is None else row.get(c) for c in TSV_COLUMNS])
    return buffer.getvalue()


def write_tsv(path: Path | str, results: Iterable[FileResult], *,
              include_ok: bool = True) -> None:
    Path(path).write_text(tsv_text(results, include_ok=include_ok), encoding="utf-8")


#: The category a corrupt file is proposed under: ``jfkit delete``'s
#: ``corrupt-unplayable``, which reads the payload again before anything
#: moves and refuses while another catalogued copy exists. ``jfkit delete``
#: refuses a category nobody released, so the manifest is still for a person
#: to read and release, not for a pipeline to run. (mkvkit does not import
#: jfkit; a test keeps the two names the same.)
MANIFEST_CATEGORY = "corrupt-unplayable"


def manifest_rows(results: Iterable[FileResult]) -> list[dict[str, str]]:
    """The corrupt files as rows of a ``jfkit delete`` manifest."""
    return [
        {
            "item_id": result.item_id or "",
            "path": result.path,
            "category": MANIFEST_CATEGORY,
            "reason": "; ".join(
                [line for line in result.evidence if line.startswith("stage 2:")][:3]
            ),
        }
        for result in _ordered(results) if result.verdict == CORRUPT
    ]


def manifest_text(rows: Sequence[Mapping[str, str]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=["item_id", "path", "category", "reason"],
                            delimiter="\t", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


# ----------------------------------------------------------------------- CLI
def _device_map(pairs: Sequence[str]) -> Callable[[Path | str], Device]:
    """``VOLUME=DISK`` pairs: volumes that share a disk share a lane."""
    table: dict[str, str] = {}
    for pair in pairs:
        volume, sep, disk = pair.partition("=")
        if not sep or not volume or not disk:
            raise SystemExit(f"--same-disk takes VOLUME=DISK, not {pair!r}")
        table[volume.rstrip("\\/").upper() if os.name == "nt" else volume] = disk

    def device(path: Path | str) -> Device:
        volume = volume_of(path)
        return table.get(volume, volume)

    return device


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser(
        "health",
        help="find the files whose payload is not there: sweep every file, confirm suspects",
        description=(
            "Stage 1 samples every video file under the roots (zero-filled blocks, the "
            "container's extent, durations, size against bitrate); stage 2 reads each "
            "suspect whole. One reader per disk. Read-only: nothing is moved or deleted."
        ),
        epilog=(
            "exit status: 0 when every file is OK; 1 when any is SUSPECT, CORRUPT or "
            "UNREADABLE, or was not reached; 2 for a usage error or a missing program."
        ),
    )
    parser.add_argument("roots", nargs="+", type=Path, metavar="ROOT")
    parser.add_argument("--state", type=Path, metavar="PATH",
                        help="the state file that makes a re-run incremental "
                             "(default: health-state.jsonl in [paths].work)")
    parser.add_argument("--rescan", action="store_true",
                        help="read every file again, whatever the state file says")
    parser.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                        help="leave out entries whose name or relative path matches")
    parser.add_argument("--exclude-path", action="append", default=[], type=Path,
                        metavar="PATH", help="leave out this folder or file")
    parser.add_argument("--same-disk", action="append", default=[], metavar="VOLUME=DISK",
                        help="volumes that are partitions of one disk share one lane")
    sweep_group = parser.add_argument_group("stage 1")
    defaults = Settings()
    sweep_group.add_argument("--blocks", type=int, default=defaults.blocks, metavar="N",
                             help="blocks sampled per file (default: %(default)s)")
    sweep_group.add_argument("--block-kib", type=int, default=defaults.block_size // 1024,
                             metavar="KIB", help="size of each block (default: %(default)s)")
    sweep_group.add_argument("--max-zero-fraction", type=float,
                             default=defaults.max_zero_fraction, metavar="F")
    sweep_group.add_argument("--duration-tolerance", type=float,
                             default=defaults.duration_tolerance_s, metavar="S",
                             help="seconds a track's duration may differ from the "
                                  "container's (or 5%%, whichever is more)")
    sweep_group.add_argument("--subset", type=int, metavar="N",
                             help="read at most N files per disk, spread evenly, and "
                                  "project the full run's time")
    sweep_group.add_argument("--from-state", action="store_true",
                             help="read no file stage 1 has not answered for: report "
                                  "what the state file holds, and confirm its suspects")
    sweep_group.add_argument("--time-budget", type=float, metavar="MIN",
                             help="start no file after this many minutes; the rest "
                                  "wait for the next run")
    confirm_group = parser.add_argument_group("stage 2")
    confirm_group.add_argument("--confirm", type=int, metavar="N",
                               help="confirm at most N suspects (default: all)")
    confirm_group.add_argument("--confirm-only", type=Path, metavar="FILE",
                               help="confirm only the suspects listed here, one path per "
                                    "line, in that order within each disk")
    confirm_group.add_argument("--no-confirm", action="store_true",
                               help="stop after stage 1")
    confirm_group.add_argument("--no-decode", action="store_true",
                               help="list every packet but do not decode")
    server_group = parser.add_argument_group("the server (needs jfkit)")
    server_group.add_argument("--gate", choices=("auto", "local", "off"), default="auto",
                              help="hold a disk's lane while it is busy: auto asks the "
                                   "server too when one is configured, local only reads "
                                   "this machine (default: %(default)s)")
    server_group.add_argument("--gate-every", type=float, default=60.0, metavar="S",
                              help="look at a lane's gate again after this many seconds")
    server_group.add_argument("--gate-timeout", type=float, metavar="MIN",
                              help="give up on a disk that stays busy this long")
    server_group.add_argument("--lock", action="append", default=[], type=Path,
                              metavar="PATH",
                              help="a lock file whose existence holds every lane")
    server_group.add_argument("--titles", action="store_true",
                              help="name each file that is not OK by its server item")
    out_group = parser.add_argument_group("output")
    out_group.add_argument("--all", action="store_true",
                           help="list OK files in the table too")
    out_group.add_argument("--json", type=Path, metavar="PATH")
    out_group.add_argument("--tsv", type=Path, metavar="PATH")
    out_group.add_argument("--manifest", type=Path, metavar="PATH",
                           help="write the corrupt files as a deletion manifest")
    out_group.add_argument("--progress-every", type=float, default=30.0, metavar="S")
    parser.set_defaults(handler=_health)


def _server_side() -> Any:
    """The optional server half: the gate and the titles live in jfkit."""
    try:
        import importlib

        return importlib.import_module("jfkit.healthlink")
    except ImportError:
        return None


def _health(args: argparse.Namespace, config: Config) -> int:
    settings = Settings(
        blocks=args.blocks, block_size=max(1, args.block_kib) * 1024,
        max_zero_fraction=args.max_zero_fraction,
        duration_tolerance_s=args.duration_tolerance,
    )
    if args.blocks < 1:
        print("--blocks must be at least 1")
        return 2
    state_path = args.state or (Path(config.paths.work) / "health-state.jsonl")
    state = StateFile(state_path)
    device_key = _device_map(args.same_disk)
    progress = Progress(every_s=args.progress_every)
    runner = default_runner(config)
    runner("ffprobe", ["-version"])  # a missing program is a usage error, now

    link = _server_side()
    before_each: Callable[[Device, Target], None] | None = None
    if args.gate != "off":
        if link is None:
            print("the gate needs jfkit; install it, or pass --gate off")
            return 2
        before_each = link.lane_gate(
            config, server=args.gate == "auto", every_s=args.gate_every,
            timeout_s=None if args.gate_timeout is None else args.gate_timeout * 60,
            locks=args.lock, progress=progress,
        )
    if args.titles and link is None:
        print("--titles needs jfkit")
        return 2

    known = state.load()
    targets, unchanged, skipped, population = plan(
        args.roots, state=known, settings=settings, device_of=device_key,
        exclude=args.exclude, exclude_paths=args.exclude_path, rescan=args.rescan,
        subset=args.subset,
    )
    if args.from_state:
        # A suspect an older version of the rules answered for is looked at
        # again, cheaply: its answer is known, only the rules moved on.
        again = [
            t for t in targets
            if (old := known.get(StateFile.key(t.path))) is not None
            and old.verdict == SUSPECT and old.stage == 1 and old.rules < RULES
            and old.size == t.size and old.mtime_ns == t.mtime_ns
        ]
        print(f"stage 1: {len(again)} suspect(s) of older rules looked at again; "
              f"{len(targets) - len(again)} file(s) the state file has no answer for "
              "are left for another run", file=sys.stderr)
        targets = again
    counts: dict[Device, int] = {}
    for result in unchanged:
        counts[result.device] = counts.get(result.device, 0) + 1
    progress.plan(targets, unchanged=counts, population=population if args.subset else None)
    print(
        f"stage 1: {len(targets)} file(s) to read, {len(unchanged)} unchanged since the "
        f"last run, on {len({t.device for t in targets})} disk(s); state: {state_path}",
        file=sys.stderr,
    )
    deadline = (
        progress.now() + args.time_budget * 60 if args.time_budget is not None else None
    )
    report = ScanReport(skipped=skipped)
    try:
        fresh, missed = sweep(
            targets, settings, state=state, runner=runner, progress=progress,
            before_each=before_each, deadline=deadline, device_of=device_key,
        )
    finally:
        state.close()
    progress.report()
    report.devices = progress.summary()
    report.not_scanned += missed
    results = {StateFile.key(r.path): r for r in [*unchanged, *fresh]}

    suspects = [r for r in results.values() if r.verdict == SUSPECT and r.stage == 1]
    suspects.sort(key=lambda r: (-(r.zero_blocks or 0) / max(1, r.sampled_blocks or 1),
                                 r.path))
    if args.confirm_only is not None:
        order = {
            StateFile.key(line.strip()): n for n, line in enumerate(
                Path(args.confirm_only).read_text(encoding="utf-8").splitlines()
            ) if line.strip()
        }
        listed = [s for s in suspects if StateFile.key(s.path) in order]
        listed.sort(key=lambda s: order[StateFile.key(s.path)])
        if len(listed) < len(order):
            print(f"stage 2: {len(order) - len(listed)} listed path(s) are not stage-1 "
                  "suspects and are not confirmed", file=sys.stderr)
        suspects = listed
    if not args.no_confirm and suspects:
        chosen = suspects if args.confirm is None else suspects[:max(0, args.confirm)]
        print(f"stage 2: confirming {len(chosen)} of {len(suspects)} suspect(s)",
              file=sys.stderr)
        if not args.no_decode:
            runner("ffmpeg", ["-version"])  # the decode needs it; say so before reading
        second = Progress(every_s=args.progress_every)
        second.plan(Target(Path(s.path), s.size or 0, 0, s.device) for s in chosen)
        if before_each is not None and link is not None:
            before_each = link.lane_gate(
                config, server=args.gate == "auto", every_s=0.0,
                timeout_s=None if args.gate_timeout is None else args.gate_timeout * 60,
                locks=args.lock, progress=second,
            )
        try:
            confirmed, missed = confirm(
                chosen, decode=not args.no_decode, settings=settings, state=state,
                runner=runner, config=config, progress=second, before_each=before_each,
                deadline=deadline, device_of=device_key,
            )
        finally:
            state.close()
        second.report()
        report.confirmed = second.summary()
        report.not_scanned += missed
        for result in confirmed:
            results[StateFile.key(result.path)] = result
    report.results = list(results.values())

    if args.titles and link is not None:
        report.results = link.with_titles(config, report.results)

    table = render_table(report.results, include_ok=args.all)
    if table:
        print(table)
    for skip in report.skipped:
        print(f"  {skip}")
    for missed_line in report.not_scanned:
        print(f"  not reached: {missed_line}")
    counted = report.counts()
    print(" ".join(f"{v} {counted[v]}" for v in VERDICTS)
          + f" -- {len(report.results)} file(s), {len(report.not_scanned)} not reached"
          + (f", {len(report.not_media())} not media (counted OK)"
             if report.not_media() else ""))
    for device in report.devices:
        print(
            f"  {device['device']}: {device['scanned']} read in {device['elapsed_s']} s "
            f"({device['files_per_s']} file(s)/s, {device['read_mib_per_s']} MiB/s), "
            f"{device['unchanged']} unchanged"
            + (f"; all {device['population']} would take about "
               f"{_clock(device['projected_s'])}" if args.subset else "")
        )

    rows = manifest_rows(report.results)
    if rows:
        print("\ncorrupt files, as a manifest a person could release (nothing was moved):")
        print(manifest_text(rows), end="")
        if args.manifest:
            Path(args.manifest).write_text(manifest_text(rows), encoding="utf-8")
    meta = {
        "roots": [str(r) for r in args.roots],
        "settings": asdict(settings),
        "fingerprint": settings.fingerprint(),
        "state": str(state_path),
    }
    if args.json:
        write_json(args.json, report, meta=meta)
    if args.tsv:
        write_tsv(args.tsv, report.results, include_ok=True)
    healthy = counted[OK] == len(report.results) and not report.not_scanned
    return 0 if healthy else 1


def iter_suspects(results: Iterable[FileResult]) -> Iterator[FileResult]:
    """The results that are not OK, worst first."""
    for result in _ordered(results):
        if result.verdict != OK:
            yield result
