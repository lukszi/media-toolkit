"""dubalign.decode -- one sequential read per source, two outputs from it.

Every measurement in this package works on raw float samples, not on a
container. Two dumps are needed: the full-rate interleaved signal, which is
what gets spliced, and a single-channel reduced copy, which is what gets
searched. Both come out of **one** invocation with two outputs.

That is not a micro-optimisation. Reading a feature-length file off mechanical
storage costs more than the entire measurement that follows it, and reading it
twice costs twice -- while a second pass also invites the two dumps to disagree
about where the stream begins. One decode, two files, one answer about the head.

The decode is sequential from the first packet, never a seek. Seeking into a
compressed stream lands where the decoder can resume, which is not where it was
asked to land, and the difference between those two points is exactly the kind
of tens-of-milliseconds error this package exists to measure.

The dumps are headerless float: no container to re-parse, no timestamps to
misread, and sample *n* is unambiguously sample *n*. What the container knew
about where the stream started is recorded by :mod:`dubalign.probe` and applied
deliberately, rather than surviving by accident.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from mkvkit.config import Config
from mkvkit.run import Runner, default_runner
from numpy.typing import NDArray

from .align import ANALYSIS_RATE, FULL_RATE, Signal
from .probe import AudioStream, Source, probe

__all__ = [
    "Decoded",
    "decode",
    "decode_command",
]

log = logging.getLogger(__name__)

#: What a float dump is written as. Four bytes a sample a channel.
SAMPLE_FORMAT = "f32le"
BYTES_PER_SAMPLE = 4


@dataclass(frozen=True)
class Decoded:
    """Two dumps of one track, and everything needed to read them back."""

    source: Path
    stream: AudioStream
    full_path: Path
    analysis_path: Path
    sample_rate: int
    channels: int
    analysis_rate: int

    @property
    def frames(self) -> int:
        """How many full-rate sample frames the dump holds."""
        per_frame = self.channels * BYTES_PER_SAMPLE
        return self.full_path.stat().st_size // per_frame

    @property
    def duration_s(self) -> float:
        return self.frames / self.sample_rate

    def full(self) -> NDArray[np.float32]:
        """The interleaved full-rate signal, mapped rather than read.

        A feature at 48 kHz in six channels is a gigabyte and a half. Nothing
        here needs all of it at once, so nothing here loads all of it at once.
        """
        mapped = np.memmap(self.full_path, dtype=np.float32, mode="r")
        return mapped.reshape(-1, self.channels)

    def channel(self, index: int) -> Signal:
        """One channel of the full-rate signal, as float64."""
        if not 0 <= index < self.channels:
            raise ValueError(f"channel {index} of {self.channels}")
        return np.asarray(self.full()[:, index], dtype=np.float64)

    def analysis(self) -> Signal:
        """The reduced single-channel copy: what every search runs on."""
        return np.asarray(
            np.fromfile(self.analysis_path, dtype=np.float32), dtype=np.float64
        )


def decode_command(
    source: Path | str,
    stream: AudioStream,
    full_path: Path | str,
    analysis_path: Path | str,
    *,
    sample_rate: int = FULL_RATE,
    channels: int | None = None,
    analysis_rate: int = ANALYSIS_RATE,
) -> list[str]:
    """The arguments for the one invocation that writes both dumps.

    Built as a function of its own so a test can read it without a program
    being installed -- which is where the mistakes in a command line are.
    """
    width = channels if channels is not None else stream.channels
    if width < 1:
        raise ValueError(f"{Path(source).name}: {stream.selector} reports no channels")
    return [
        "-nostdin", "-v", "error", "-i", str(source),
        # full rate, every channel: the material
        "-map", stream.selector, "-c:a", f"pcm_{SAMPLE_FORMAT}",
        "-ar", str(sample_rate), "-ac", str(width), "-f", SAMPLE_FORMAT,
        "-y", str(full_path),
        # reduced, one channel: the analysis copy
        "-map", stream.selector, "-c:a", f"pcm_{SAMPLE_FORMAT}",
        "-ar", str(analysis_rate), "-ac", "1", "-f", SAMPLE_FORMAT,
        "-y", str(analysis_path),
    ]


def decode(
    source: Path | str,
    *,
    out_dir: Path | str,
    audio_index: int = 0,
    name: str | None = None,
    sample_rate: int = FULL_RATE,
    channels: int | None = None,
    analysis_rate: int = ANALYSIS_RATE,
    probed: Source | None = None,
    reuse: bool = True,
    runner: Runner | None = None,
    config: Config | None = None,
) -> Decoded:
    """Decode one audio track of one file into a full and a reduced dump.

    ``reuse`` keeps dumps that are already there, because a decode is the
    expensive part of every job in this package and re-running a measurement is
    the normal way to work. Pass ``reuse=False`` when the source has changed.
    """
    target = Path(source)
    run = runner if runner is not None else default_runner(config)
    found = probed if probed is not None else probe(target, runner=run, config=config)
    stream = found.audio(audio_index)
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stem = name if name is not None else f"{target.stem}.a{audio_index}"
    width = channels if channels is not None else stream.channels
    full_path = directory / f"{stem}.{sample_rate // 1000}k{width}ch.{SAMPLE_FORMAT}"
    analysis_path = directory / f"{stem}.{analysis_rate // 1000}k1ch.{SAMPLE_FORMAT}"

    decoded = Decoded(
        source=target, stream=stream, full_path=full_path,
        analysis_path=analysis_path, sample_rate=sample_rate,
        channels=width, analysis_rate=analysis_rate,
    )
    if reuse and full_path.exists() and analysis_path.exists():
        log.info("reusing the decode of %s %s", target.name, stream.selector)
        return decoded

    log.info("decoding %s %s in one pass", target.name, stream.selector)
    run(
        "ffmpeg",
        decode_command(
            target, stream, full_path, analysis_path,
            sample_rate=sample_rate, channels=width, analysis_rate=analysis_rate,
        ),
    )
    return decoded
