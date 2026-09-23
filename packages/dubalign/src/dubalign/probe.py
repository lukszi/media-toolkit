"""dubalign.probe -- what a container says about its audio, read explicitly.

Three things get assumed about a file and all three are wrong often enough to
have cost this method a rebuild each.

**Stream order is not stream numbering.** The selector that picks "the second
audio track" counts audio tracks; the index a report prints counts every
stream in the file. Mixing them up picks the wrong track and then measures it
very accurately.

**A layout is not a channel order.** Two files can both say six channels and
both say the same layout name and still be a remix of one another. The layout
is read here so that :mod:`dubalign.channels` can check it rather than trust
it.

**A stream does not have to start at zero.** A track whose first packet sits
30 ms into the container is playing 30 ms late relative to a track that starts
at zero, and a decode to raw samples throws that away -- the first sample of
the dump is the first sample of the *stream*, wherever that was. The start
time is read here, carried on the stream, and applied by
:func:`dubalign.epk_align.absolute_lag`, because a measurement that ignores it
is confidently wrong by exactly that much and looks perfect.

One more thing is recorded rather than assumed: not every codec is safe to
correlate against. A losslessly-packed format is decoded from the first major
sync point the decoder happens to find, so the same track opened two different
ways loses a different amount off its head. It is fine as material and unfit
as a reference, and :func:`reference_risk` says so before a measurement rather
than after one.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mkvkit.config import Config
from mkvkit.run import Runner, default_runner

__all__ = [
    "RISKY_REFERENCE_CODECS",
    "AudioStream",
    "Source",
    "SourceError",
    "ffprobe_json",
    "first_packet_time",
    "probe",
    "reference_risk",
    "source_from_json",
]

log = logging.getLogger(__name__)

#: Codecs whose decoded head depends on how the stream was opened. Usable as
#: material, unusable as the thing everything else is measured against.
RISKY_REFERENCE_CODECS = frozenset({"truehd", "mlp"})


class SourceError(ValueError):
    """The file does not carry what the measurement was asked to read."""


@dataclass(frozen=True)
class AudioStream:
    """One audio track, with both of its numbers and both of its clocks."""

    #: Position among every stream in the file: what a report prints.
    index: int
    #: Position among the audio streams: what a selector takes.
    audio_index: int
    codec: str
    channels: int
    layout: str
    sample_rate: int
    language: str | None = None
    title: str | None = None
    #: Where this stream's first packet sits in the container, in seconds.
    start_time_s: float = 0.0
    duration_s: float | None = None

    @property
    def selector(self) -> str:
        """How to name this stream to the decoder."""
        return f"0:a:{self.audio_index}"

    def describe(self) -> str:
        parts = [
            f"a:{self.audio_index} (stream {self.index})",
            self.codec,
            f"{self.channels}ch {self.layout}",
            f"{self.sample_rate} Hz",
        ]
        if self.language:
            parts.append(self.language)
        if self.title:
            parts.append(repr(self.title))
        if self.start_time_s:
            parts.append(f"starts {self.start_time_s * 1000:+.1f} ms")
        return "  ".join(parts)


@dataclass(frozen=True)
class Source:
    """A file, its duration, and every audio track in it."""

    path: Path
    duration_s: float | None
    start_time_s: float
    streams: tuple[AudioStream, ...]

    def audio(self, audio_index: int = 0) -> AudioStream:
        for stream in self.streams:
            if stream.audio_index == audio_index:
                return stream
        raise SourceError(
            f"{self.path.name} has no audio track a:{audio_index} "
            f"({len(self.streams)} audio track(s): "
            f"{', '.join(str(s.audio_index) for s in self.streams) or 'none'})"
        )

    def describe(self) -> list[str]:
        head = f"{self.path.name}"
        if self.duration_s is not None:
            head += f"  {self.duration_s:.3f} s"
        return [head, *(f"  {s.describe()}" for s in self.streams)]


def reference_risk(stream: AudioStream) -> str | None:
    """Why this track should not be the one everything is measured against.

    ``None`` when there is no reason. The check is cheap, the failure it
    prevents is not: a reference whose head moves gives three different answers
    for one pair and every one of them looks confident.
    """
    if stream.codec.lower() in RISKY_REFERENCE_CODECS:
        return (
            f"{stream.codec} is decoded from the first major sync point, so how "
            "much of its head is lost depends on how the stream was opened; "
            "measure against a frame-synchronous track instead"
        )
    return None


# ------------------------------------------------------------------- the calls
def ffprobe_json(
    path: Path | str, *, runner: Runner | None = None, config: Config | None = None
) -> dict[str, Any]:
    """Streams and format, as the plain structure the program prints."""
    run = runner if runner is not None else default_runner(config)
    result = run(
        "ffprobe",
        ["-v", "error", "-of", "json", "-show_streams", "-show_format", str(path)],
    )
    parsed: dict[str, Any] = json.loads(result.stdout or "{}")
    return parsed


def probe(
    path: Path | str, *, runner: Runner | None = None, config: Config | None = None
) -> Source:
    """Read one file's audio streams."""
    target = Path(path)
    return source_from_json(target, ffprobe_json(target, runner=runner, config=config))


def source_from_json(path: Path | str, probed: Mapping[str, Any]) -> Source:
    """Build the typed answer from the program's output.

    Separated from the running so the parsing -- where the interesting mistakes
    live -- is testable with nothing installed.
    """
    target = Path(path)
    streams: list[AudioStream] = []
    audio_index = 0
    for raw in _sequence(probed, "streams"):
        if raw.get("codec_type") != "audio":
            continue
        tags = raw.get("tags") or {}
        lowered = {str(k).lower(): v for k, v in tags.items()}
        streams.append(
            AudioStream(
                index=int(raw.get("index", len(streams))),
                audio_index=audio_index,
                codec=str(raw.get("codec_name") or "unknown"),
                channels=int(raw.get("channels") or 0),
                layout=str(raw.get("channel_layout") or "unknown"),
                sample_rate=int(raw.get("sample_rate") or 0),
                language=_text(lowered.get("language")),
                title=_text(lowered.get("title")),
                start_time_s=_number(raw.get("start_time")) or 0.0,
                duration_s=_number(raw.get("duration")),
            )
        )
        audio_index += 1
    container = probed.get("format") or {}
    return Source(
        path=target,
        duration_s=_number(container.get("duration")),
        start_time_s=_number(container.get("start_time")) or 0.0,
        streams=tuple(streams),
    )


def first_packet_time(
    path: Path | str,
    audio_index: int = 0,
    *,
    runner: Runner | None = None,
    config: Config | None = None,
) -> float:
    """When this stream's first packet actually plays, from the packet itself.

    The stream header's start time is what the container claims; this is what
    is in it. They usually agree. When they do not, this one is the one a
    decode to raw samples silently discards.
    """
    run = runner if runner is not None else default_runner(config)
    result = run(
        "ffprobe",
        [
            "-v", "error", "-select_streams", f"a:{audio_index}", "-show_packets",
            "-show_entries", "packet=pts_time", "-of", "csv=p=0", str(path),
        ],
    )
    for line in (result.stdout or "").splitlines():
        value = _number(line.split(",")[0].strip())
        if value is not None:
            return value
    raise SourceError(f"{Path(path).name}: a:{audio_index} has no readable packet time")


# -------------------------------------------------------------------- plumbing
def _sequence(source: Mapping[str, Any], key: str) -> Sequence[Mapping[str, Any]]:
    value = source.get(key)
    if not isinstance(value, list):
        return ()
    return [item for item in value if isinstance(item, dict)]


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number  # a NaN duration is no duration


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
