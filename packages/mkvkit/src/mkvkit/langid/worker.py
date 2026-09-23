"""mkvkit.langid.worker -- sample a track and ask a detector what it hears.

The shape of the job, in one paragraph: a language model is expensive to load
and cheap to run, a hard disk is the opposite. So the model is loaded once and
fed from a queue, the reading is done by one thread per physical device, and
the windows a track contributes are chosen by where the speech is rather than
on a fixed grid. Nothing here transcribes: only the detector's language
probabilities are used, which is roughly thirty times cheaper.

Four things in this module were learned the expensive way.

**A per-output option binds to the output it precedes.** ``-t`` placed between
the input and the first output caps that output only; every further output
decodes to the end of the file. With five windows and three tracks that is the
difference between seconds and a full read of the file, and it is silent --
the data is right, the run is just inexplicably slow.

**An unindexed container turns a seek into a full read.** Matroska without
Cues, AVI without an index, fragmented MP4 with its index at the end: there
every ``-ss`` parses from byte zero, so N windows cost N reads. The cure is to
notice (the first window is slow) and switch to one sequential decode, slicing
the windows out of the decoded audio. At 16 kHz mono that is about 115 MB per
hour of runtime per track, which is a cheap trade against reading a 30 GB file
five times.

**Windows must avoid the opening minutes.** Logos, distributor stings and
music score; a window there reports the language of a silence.

**The result log is append-only and keyed.** These runs take hours and will be
interrupted -- by a reboot, a full disk, or somebody wanting the disk back.
A run that cannot resume is a run that gets abandoned half-way.

Everything that touches audio goes through :class:`Extractor` and
:class:`Detector`, so the whole module is testable with neither an external
program nor a model. That is not politeness: the parts most likely to be
subtly wrong are the window arithmetic and the bookkeeping, and those need no
audio at all.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import array
import json
import logging
import os
import subprocess
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, TypeAlias, runtime_checkable

from ..errors import require_module
from ..langcodes import canonical

__all__ = [
    "DEFAULT_OFFSETS",
    "RETRY_OFFSETS",
    "SAMPLE_RATE",
    "Detector",
    "Extractor",
    "FfmpegExtractor",
    "Pcm",
    "ResultLog",
    "SpeechGate",
    "TrackEvidence",
    "WhisperDetector",
    "Window",
    "WindowResult",
    "scan_track",
    "window_starts",
]

log = logging.getLogger(__name__)

#: What the detectors want, and small enough that a whole film's audio fits in
#: memory as a last resort.
SAMPLE_RATE = 16_000

#: Signed 16-bit mono samples. An :mod:`array` rather than a third-party array
#: type keeps the core of this package standard-library only; the detector that
#: needs a numeric library converts at its own boundary.
Pcm: TypeAlias = "array.array[int]"

#: Where in the runtime the windows are taken, as fractions. Spread wide, never
#: at the very start or the very end.
DEFAULT_OFFSETS: tuple[float, ...] = (0.15, 0.35, 0.55, 0.75, 0.90)
#: Interleaved with the first set, for the second look at a track whose first
#: sample was all music.
RETRY_OFFSETS: tuple[float, ...] = (0.25, 0.45, 0.65, 0.85, 0.10)

#: Never start a window inside the opening titles of a feature-length item.
OPENING_SKIP_S = 90.0
#: Below this runtime the opening-title rule would consume the whole item.
SHORT_ITEM_S = 600.0
#: A window shorter than this is not enough signal to ask about.
MIN_USABLE_S = 3.0
#: If the first window takes longer than this, the container has no usable
#: index and the whole file is redone as one sequential pass.
SLOW_SEEK_S = 25.0


# ----------------------------------------------------------------- data shapes
@dataclass(frozen=True)
class Window:
    """Decoded audio for one sampling window."""

    start_s: float
    pcm: Pcm
    sample_rate: int = SAMPLE_RATE

    @property
    def seconds(self) -> float:
        return len(self.pcm) / self.sample_rate


@dataclass(frozen=True)
class WindowResult:
    """What one window said, kept whole.

    The full probability vector is kept, not the winner: the aggregation is
    the interesting part of this pipeline and it cannot be redone from an
    argmax. Storing the vector is what lets the ladder be re-run, re-tuned and
    tested years later against evidence that was expensive to collect.
    """

    start_s: float
    probabilities: Mapping[str, float]
    speech_s: float | None = None
    counted: bool = True

    @property
    def winner(self) -> str | None:
        if not self.probabilities:
            return None
        return max(self.probabilities, key=lambda k: self.probabilities[k])

    def to_record(self, *, top: int = 5) -> dict[str, Any]:
        ranked = sorted(self.probabilities.items(), key=lambda kv: -kv[1])[:top]
        return {
            "start_s": round(self.start_s, 2),
            "speech_s": None if self.speech_s is None else round(self.speech_s, 1),
            "counted": self.counted,
            "top": [[code, round(float(p), 6)] for code, p in ranked],
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> WindowResult:
        top = record.get("top") or []
        probabilities = {str(code): float(p) for code, p in top}
        speech = record.get("speech_s")
        return cls(
            start_s=float(record.get("start_s", 0.0)),
            probabilities=probabilities,
            speech_s=None if speech is None else float(speech),
            counted=bool(record.get("counted", True)),
        )


@dataclass(frozen=True)
class TrackEvidence:
    """Every window of one audio track, plus how the reading went."""

    path: str
    stream_index: int
    model: str
    windows: tuple[WindowResult, ...] = ()
    existing_tag: str | None = None
    runtime_s: float | None = None
    note: str | None = None
    error: str | None = None
    io_s: float = 0.0
    detect_s: float = 0.0

    @property
    def counted(self) -> tuple[WindowResult, ...]:
        return tuple(w for w in self.windows if w.counted)

    @property
    def no_speech(self) -> bool:
        """Every window was music, silence or too short to count."""
        return bool(self.windows) and not self.counted

    @property
    def key(self) -> tuple[str, int, str]:
        return (self.path, self.stream_index, self.model)

    def to_record(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "stream_index": self.stream_index,
            "model": self.model,
            "existing_tag": self.existing_tag,
            "runtime_s": self.runtime_s,
            "note": self.note,
            "error": self.error,
            "io_s": round(self.io_s, 2),
            "detect_s": round(self.detect_s, 2),
            "windows": [w.to_record() for w in self.windows],
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> TrackEvidence:
        windows = record.get("windows") or []
        runtime = record.get("runtime_s")
        return cls(
            path=str(record["path"]),
            stream_index=int(record["stream_index"]),
            model=str(record.get("model", "")),
            windows=tuple(
                WindowResult.from_record(w) for w in windows
            ),
            existing_tag=(
                None if record.get("existing_tag") is None else str(record["existing_tag"])
            ),
            runtime_s=None if runtime is None else float(runtime),
            note=None if record.get("note") is None else str(record["note"]),
            error=None if record.get("error") is None else str(record["error"]),
            io_s=float(record.get("io_s", 0.0)),
            detect_s=float(record.get("detect_s", 0.0)),
        )


# -------------------------------------------------------------------- protocols
@runtime_checkable
class Detector(Protocol):
    """Anything that turns audio into a probability per language.

    ``family`` exists because the ladder's fourth stage needs an *independent*
    opinion, and two checkpoints of the same architecture agreeing is not
    independence. Two detectors with the same ``family`` may not be used to
    corroborate each other.
    """

    name: str
    family: str

    def detect(self, pcm: Pcm, sample_rate: int) -> Mapping[str, float]:
        """Return ``{ISO 639-2/T code: probability}``, summing to about one."""


@runtime_checkable
class SpeechGate(Protocol):
    """Voice-activity detection, for deciding whether a window counts."""

    def speech_seconds(self, pcm: Pcm, sample_rate: int) -> float | None:
        """Seconds of speech in this audio, or ``None`` if it cannot tell."""


class Extractor(Protocol):
    """Decodes windows of one audio track. The only thing that reads media."""

    def windows(
        self, path: str | os.PathLike[str], audio_ord: int,
        starts: Sequence[float], window_s: float,
    ) -> tuple[list[Window], str | None]:
        """Return the windows it could get, and a note about how it got them."""


# ------------------------------------------------------------------ pure arithmetic
def window_starts(
    runtime_s: float | None,
    count: int,
    window_s: float,
    offsets: Sequence[float] = DEFAULT_OFFSETS,
) -> list[float]:
    """Absolute start times for ``count`` windows, avoiding the opening titles.

    Clamped so the last window still fits inside the item, and so a short item
    does not lose every window to the opening-title rule.

        >>> window_starts(3600.0, 3, 20.0)
        [540.0, 1260.0, 1980.0]
        >>> window_starts(120.0, 3, 20.0)
        [18.0, 42.0, 66.0]
    """
    if count <= 0:
        return []
    runtime = runtime_s if runtime_s and runtime_s > 0 else 1800.0
    floor = OPENING_SKIP_S if runtime > SHORT_ITEM_S else min(30.0, runtime * 0.1)
    ceiling = max(floor, runtime - window_s - 5.0)
    chosen = list(offsets[:count]) or [0.5]
    return [round(min(max(runtime * f, floor), ceiling), 1) for f in chosen]


def max_windows_for(runtime_s: float | None, window_s: float) -> int:
    """How many non-overlapping windows the item could possibly supply.

    The settle rules need this: demanding five windows from a ninety-second
    clip is demanding the impossible, and the honest response is to raise the
    confidence bar instead of leaving the track forever unsettled.
    """
    if not runtime_s or runtime_s <= 0 or window_s <= 0:
        return 0
    usable = max(0.0, runtime_s - OPENING_SKIP_S if runtime_s > SHORT_ITEM_S else runtime_s)
    return int(usable // window_s)


# ---------------------------------------------------------------------- scanning
def scan_track(
    path: str | os.PathLike[str],
    stream_index: int,
    audio_ord: int,
    detector: Detector,
    *,
    extractor: Extractor,
    speech: SpeechGate | None = None,
    count: int = 5,
    window_s: float = 20.0,
    offsets: Sequence[float] = DEFAULT_OFFSETS,
    runtime_s: float | None = None,
    existing_tag: str | None = None,
    min_speech_s: float = 3.0,
) -> TrackEvidence:
    """Sample one audio track and record what the detector said, per window.

    No verdict is reached here. This function produces evidence; deciding what
    it means is :mod:`mkvkit.langid.ladder`'s job, and keeping the two apart is
    what makes the decision re-runnable against evidence that cost hours to
    collect.
    """
    starts = window_starts(runtime_s, count, window_s, offsets)
    t0 = time.monotonic()
    try:
        decoded, note = extractor.windows(path, audio_ord, starts, window_s)
        error: str | None = None
    except Exception as exc:
        decoded, note, error = [], None, f"{type(exc).__name__}: {exc}"
    io_s = time.monotonic() - t0

    t1 = time.monotonic()
    results: list[WindowResult] = []
    for window in decoded:
        if window.seconds < MIN_USABLE_S:
            continue
        speech_s = None if speech is None else speech.speech_seconds(
            window.pcm, window.sample_rate
        )
        counted = speech_s is None or speech_s >= min_speech_s
        probabilities = _canonical_vector(
            detector.detect(window.pcm, window.sample_rate)
        )
        results.append(
            WindowResult(
                start_s=window.start_s, probabilities=probabilities,
                speech_s=speech_s, counted=counted,
            )
        )
    return TrackEvidence(
        path=str(path), stream_index=stream_index, model=detector.name,
        windows=tuple(results), existing_tag=existing_tag, runtime_s=runtime_s,
        note=note, error=error or (None if results else "no usable audio"),
        io_s=io_s, detect_s=time.monotonic() - t1,
    )


def _canonical_vector(vector: Mapping[str, float]) -> dict[str, float]:
    """Canonicalise the detector's codes, summing any that collapse together.

    A detector that reports Bokmal and Nynorsk separately is reporting
    Norwegian twice; adding the two is right, and taking the larger would
    under-state a language that is simply spelled two ways.
    """
    out: dict[str, float] = {}
    for code, probability in vector.items():
        key = canonical(code) or code.strip().lower()
        out[key] = out.get(key, 0.0) + float(probability)
    return out


# ------------------------------------------------------------------- result log
@dataclass
class ResultLog:
    """An append-only JSONL of :class:`TrackEvidence`, keyed for resume.

    Append-only on purpose. A run that rewrites its output file loses
    everything when it is killed in the middle of the rewrite, and these runs
    are killed in the middle often enough that it is a design constraint and
    not a worry. The key includes the model, so re-running with a different
    model adds evidence instead of replacing it.
    """

    path: Path
    _keys: set[tuple[str, int, str]] = field(default_factory=set, repr=False)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self._keys = {record.key for record in self.read()}

    def read(self) -> Iterator[TrackEvidence]:
        """Every record, in the order it was written. Bad lines are skipped.

        A truncated final line is the normal way one of these files ends: the
        process died mid-write. Skipping it is right; refusing to read the file
        because of it would throw away the hours before the crash.
        """
        if not self.path.is_file():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield TrackEvidence.from_record(json.loads(line))
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    log.warning("%s:%d is not a usable record, skipping", self.path, number)

    def latest(self) -> dict[tuple[str, int, str], TrackEvidence]:
        """The last record for each key: a later scan supersedes an earlier one."""
        out: dict[tuple[str, int, str], TrackEvidence] = {}
        for record in self.read():
            out[record.key] = record
        return out

    def done(self, key: tuple[str, int, str]) -> bool:
        return key in self._keys

    def append(self, evidence: TrackEvidence) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(evidence.to_record(), ensure_ascii=False) + "\n")
            handle.flush()
        self._keys.add(evidence.key)

    def remaining(
        self, keys: Iterable[tuple[str, int, str]]
    ) -> list[tuple[str, int, str]]:
        return [key for key in keys if key not in self._keys]


# ---------------------------------------------------------------- ffmpeg reading
def _pcm_from_bytes(raw: bytes) -> Pcm:
    if len(raw) % 2:  # a short read at end of file
        raw = raw[:-1]
    samples = array.array("h")
    samples.frombytes(raw)
    if _BIG_ENDIAN:  # pragma: no cover - no big-endian machine in CI
        samples.byteswap()
    return samples


_BIG_ENDIAN = array.array("h", b"\x01\x00")[0] != 1


@dataclass
class FfmpegExtractor:
    """The real reader: one decode per window, and a fallback for bad indexes.

    Every invocation decodes to a pipe or to scratch; no media file is ever
    opened for writing, and nothing is written next to the source.
    """

    ffmpeg: str | os.PathLike[str] = "ffmpeg"
    scratch: Path | None = None
    timeout_s: float = 180.0
    sequential_timeout_s: float = 3600.0
    slow_seek_s: float = SLOW_SEEK_S
    runner: Callable[[Sequence[str], float], bytes] | None = None

    def windows(
        self, path: str | os.PathLike[str], audio_ord: int,
        starts: Sequence[float], window_s: float,
    ) -> tuple[list[Window], str | None]:
        collected: list[Window] = []
        for position, start in enumerate(starts):
            began = time.monotonic()
            raw = self._run(self._seek_command(path, audio_ord, start, window_s),
                            self.timeout_s)
            elapsed = time.monotonic() - began
            pcm = _pcm_from_bytes(raw)
            if len(pcm) >= MIN_USABLE_S * SAMPLE_RATE:
                collected.append(Window(start_s=start, pcm=pcm))
            if position == 0 and len(starts) > 1 and elapsed > self.slow_seek_s:
                log.info(
                    "%s: first window took %.1fs, treating the container as unindexed",
                    path, elapsed,
                )
                return self._sequential(path, audio_ord, starts, window_s)
        return collected, None

    def _sequential(
        self, path: str | os.PathLike[str], audio_ord: int,
        starts: Sequence[float], window_s: float,
    ) -> tuple[list[Window], str | None]:
        """One linear decode of the whole track, sliced in memory."""
        raw = self._run(self._whole_command(path, audio_ord), self.sequential_timeout_s)
        whole = _pcm_from_bytes(raw)
        need = int(window_s * SAMPLE_RATE)
        out: list[Window] = []
        for start in starts:
            offset = int(start * SAMPLE_RATE)
            chunk = whole[offset:offset + need]
            if len(chunk) >= MIN_USABLE_S * SAMPLE_RATE:
                out.append(Window(start_s=start, pcm=chunk))
        return out, "one sequential decode (container has no usable index)"

    def _common(self, window_s: float | None) -> list[str]:
        # -t belongs with the OUTPUT it caps. Between the input and the first
        # output it binds to that output alone, and every later output decodes
        # to the end of the file.
        out = ["-vn", "-sn", "-dn", "-ac", "1", "-ar", str(SAMPLE_RATE),
               "-c:a", "pcm_s16le", "-f", "s16le"]
        return ["-t", f"{window_s:g}", *out] if window_s is not None else out

    def _seek_command(
        self, path: str | os.PathLike[str], audio_ord: int,
        start: float, window_s: float,
    ) -> list[str]:
        return [
            str(self.ffmpeg), "-hide_banner", "-nostdin", "-loglevel", "error",
            "-ss", f"{start:.2f}", "-i", str(path),
            "-map", f"0:a:{audio_ord}", *self._common(window_s), "pipe:1",
        ]

    def _whole_command(
        self, path: str | os.PathLike[str], audio_ord: int
    ) -> list[str]:
        return [
            str(self.ffmpeg), "-hide_banner", "-nostdin", "-loglevel", "error",
            "-i", str(path), "-map", f"0:a:{audio_ord}",
            *self._common(None), "pipe:1",
        ]

    def _run(self, command: Sequence[str], timeout_s: float) -> bytes:
        if self.runner is not None:
            return self.runner(command, timeout_s)
        completed = subprocess.run(
            list(command), capture_output=True, timeout=timeout_s, check=False,
        )
        if completed.returncode != 0 and not completed.stdout:
            detail = completed.stderr.decode("utf-8", "replace").strip()[:300]
            raise RuntimeError(detail or f"ffmpeg exited {completed.returncode}")
        return completed.stdout


# ------------------------------------------------------------------- the detector
class WhisperDetector:
    """Language probabilities from a Whisper-family model, and nothing else.

    Only the language head is used: never a transcription, which costs an order
    of magnitude more for an answer this stage does not need. The model is
    loaded once; construct one of these and keep it.

    The model files are never vendored and never fetched by this package on
    your behalf beyond the library's own cache: which model, where its cache
    lives and which device it runs on are configuration.
    """

    family = "whisper"

    def __init__(
        self,
        model: str = "large-v3",
        *,
        device: str = "auto",
        compute_type: str | None = None,
        download_root: str | os.PathLike[str] | None = None,
        local_files_only: bool = False,
        vad_threshold: float = 0.5,
    ) -> None:
        whisper = require_module("faster_whisper", extra="langid")
        resolved_device = device
        if device == "auto":
            resolved_device = "cuda" if self._cuda_device_count() > 0 else "cpu"
        compute = compute_type or ("float16" if resolved_device == "cuda" else "int8")
        self.name = model
        self.device = resolved_device
        began = time.monotonic()
        self._model = whisper.WhisperModel(
            model, device=resolved_device, compute_type=compute,
            **({"download_root": str(download_root)} if download_root else {}),
            local_files_only=local_files_only,
        )
        self.load_s = time.monotonic() - began
        log.info("loaded %s on %s (%s) in %.1fs", model, resolved_device, compute,
                 self.load_s)
        self._vad_threshold = vad_threshold

    @staticmethod
    def _cuda_device_count() -> int:
        try:
            ctranslate2 = require_module("ctranslate2", extra="langid")
            return int(ctranslate2.get_cuda_device_count())
        except Exception:
            return 0

    def _to_float(self, pcm: Pcm, sample_rate: int) -> object:
        numpy = require_module("numpy", extra="langid")
        if sample_rate != SAMPLE_RATE:
            raise ValueError(
                f"this detector wants {SAMPLE_RATE} Hz mono audio, got {sample_rate}"
            )
        # The model always looks at a fixed-length mel, so pad rather than let
        # the library decide what a short buffer means.
        audio = numpy.frombuffer(pcm.tobytes(), dtype="<i2").astype("float32") / 32768.0
        need = SAMPLE_RATE * 30
        if audio.size < need:
            audio = numpy.pad(audio, (0, need - audio.size))
        return audio[:need]

    def detect(self, pcm: Pcm, sample_rate: int) -> Mapping[str, float]:
        _language, _probability, vector = self._model.detect_language(
            self._to_float(pcm, sample_rate)
        )
        return {str(code): float(p) for code, p in vector}

    def speech_seconds(self, pcm: Pcm, sample_rate: int) -> float | None:
        try:
            vad = require_module("faster_whisper.vad", extra="langid")
            options = vad.VadOptions(
                threshold=self._vad_threshold,
                min_speech_duration_ms=250,
                min_silence_duration_ms=500,
            )
            segments = vad.get_speech_timestamps(
                self._to_float(pcm, sample_rate), options, sampling_rate=sample_rate
            )
        except Exception:
            return None
        total = sum(int(s["end"]) - int(s["start"]) for s in segments)
        return total / sample_rate
