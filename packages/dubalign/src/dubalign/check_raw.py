"""dubalign.check_raw -- look at a decode before believing anything measured on it.

A decode that lost its first packets measures beautifully. Every window
correlates, every confidence is high, and every answer is wrong by exactly the
amount that went missing -- which is the amount you were trying to measure. The
correlation cannot notice, because a shifted signal is still a signal.

So the dump is checked against what the container said before it is used:

* **length**, against the duration the container declared. A dump that is
  short by a hundred milliseconds is a dump that lost its head or its tail, and
  it is the single most common way a whole measurement goes quietly wrong;
* **head and tail silence**, reported rather than judged. Silence at the head
  is ordinary; silence at the head of one source and not the other is where the
  offset between them comes from, and it is worth seeing before rather than
  after;
* **level**, as peak and RMS. An all-silent dump means the wrong track was
  selected, and a dump that is clipping means the source was already clipping,
  which is worth knowing before anything is mixed into it.

Nothing here refuses to continue. It reports problems and notes, in the same
shape the rest of the toolkit uses: a problem is a reason to stop, a note is
something a person should see and a program should not act on.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

import numpy as np

from .align import Signal

__all__ = [
    "DEFAULT_TOLERANCE_S",
    "SILENCE_DBFS",
    "RawCheck",
    "check_signal",
    "edge_silence",
]

log = logging.getLogger(__name__)

#: Below this a block is treated as silence rather than as quiet material.
SILENCE_DBFS = -90.0
#: How far a dump may be from the declared duration before it is a problem.
#: One video frame at the slowest ordinary rate, rounded down.
DEFAULT_TOLERANCE_S = 0.040


@dataclass(frozen=True)
class RawCheck:
    """What the dump looks like, and whether it can be trusted."""

    frames: int
    sample_rate: int
    duration_s: float
    peak: float
    rms: float
    head_silence_s: float
    tail_silence_s: float
    declared_duration_s: float | None = None
    problems: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default=())

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def peak_dbfs(self) -> float:
        return 20.0 * math.log10(self.peak) if self.peak > 0 else float("-inf")

    def describe(self) -> list[str]:
        lines = [
            f"{self.frames} frames = {self.duration_s:.3f} s at {self.sample_rate} Hz",
            f"peak {self.peak_dbfs:+.1f} dBFS, silence {self.head_silence_s:.3f} s at "
            f"the head and {self.tail_silence_s:.3f} s at the tail",
        ]
        lines += [f"PROBLEM: {p}" for p in self.problems]
        lines += [f"note: {n}" for n in self.notes]
        return lines


def edge_silence(
    x: Signal, sample_rate: int, *, threshold_dbfs: float = SILENCE_DBFS,
    block_s: float = 0.020,
) -> tuple[float, float]:
    """How many seconds of silence sit at each end, to the nearest block."""
    block = max(1, int(block_s * sample_rate))
    frames = len(x) // block
    if frames == 0:
        return 0.0, 0.0
    blocks = x[: frames * block].reshape(frames, block)
    level = np.sqrt(np.mean(blocks * blocks, axis=1))
    loud = level > 10.0 ** (threshold_dbfs / 20.0)
    if not loud.any():
        return len(x) / sample_rate, len(x) / sample_rate
    first = int(np.argmax(loud))
    last = frames - 1 - int(np.argmax(loud[::-1]))
    return first * block / sample_rate, (frames - 1 - last) * block / sample_rate


def check_signal(
    x: Signal,
    sample_rate: int,
    *,
    declared_duration_s: float | None = None,
    tolerance_s: float = DEFAULT_TOLERANCE_S,
    threshold_dbfs: float = SILENCE_DBFS,
) -> RawCheck:
    """Measure a decoded signal against what the container promised."""
    problems: list[str] = []
    notes: list[str] = []
    frames = len(x)
    duration = frames / sample_rate if sample_rate else 0.0
    peak = float(np.abs(x).max()) if frames else 0.0
    rms = float(np.sqrt(np.mean(x * x))) if frames else 0.0
    head, tail = edge_silence(x, sample_rate, threshold_dbfs=threshold_dbfs)

    if frames == 0:
        problems.append("the decode is empty")
    elif peak == 0.0:
        problems.append(
            "the decode is silent from end to end, which usually means the "
            "selector picked a track that is not the one intended"
        )
    if declared_duration_s is not None and frames:
        difference = duration - declared_duration_s
        if abs(difference) > tolerance_s:
            problems.append(
                f"the decode is {difference * 1000:+.0f} ms from the declared "
                f"{declared_duration_s:.3f} s; a dump that lost packets still "
                "correlates perfectly and is wrong by exactly that much"
            )
        elif abs(difference) > tolerance_s / 4:
            notes.append(f"length differs from the container by {difference * 1000:+.1f} ms")
    if peak >= 1.0:
        notes.append(f"the source reaches {20 * math.log10(peak):+.2f} dBFS and is clipping")
    if head > 0.5:
        notes.append(
            f"{head:.3f} s of silence at the head, which is an offset between "
            "the two sources if the other one has none"
        )
    if tail > 0.5:
        notes.append(f"{tail:.3f} s of silence at the tail")

    return RawCheck(
        frames=frames, sample_rate=sample_rate, duration_s=duration, peak=peak,
        rms=rms, head_silence_s=head, tail_silence_s=tail,
        declared_duration_s=declared_duration_s,
        problems=tuple(problems), notes=tuple(notes),
    )
