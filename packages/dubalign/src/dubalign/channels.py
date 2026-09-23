"""dubalign.channels -- prove the two sources agree on channel order.

Two files can both report six channels, both name the same layout, and still
not be the same arrangement of them. Mixing one into the other on that
assumption puts the dialogue somewhere it is not supposed to be, and the result
measures perfectly: the alignment is right, the level is right, and the centre
channel is in the surrounds.

So the assumption is checked rather than made. Correlate every channel of one
source against every channel of the other over the same stretch of programme.
A clean diagonal means the interleave matches. Anything else means it does not.

What "clean" looks like is worth stating, because the off-diagonal terms are
never zero: the two front channels of any stereo mix share most of their
content, and so do the two surrounds. On a pair that agrees, the diagonal is
high and an off-diagonal term can still be substantial -- ordinary stereo
correlation, not a mapping error. The test is that each channel's best match is
itself, with a margin, and not that the off-diagonal terms are small.

Two channels are reported and not used as references anywhere in this package:
the low-frequency channel and the surrounds. A dub's low-frequency and surround
content was mixed separately from the original's, so correlating there compares
two things that were never meant to match.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

__all__ = [
    "SIX_CHANNEL_NAMES",
    "ChannelCheck",
    "channel_matrix",
    "compare_channels",
    "rms_dbfs",
]

log = logging.getLogger(__name__)

#: The usual order of a six-channel interleave. Used for reporting only:
#: nothing here assumes a file follows it, which is the whole point.
SIX_CHANNEL_NAMES = ("FL", "FR", "FC", "LFE", "SL", "SR")

#: How much better a channel's match with itself has to be than its best match
#: with any other, before the interleave is called agreed.
MIN_DIAGONAL = 0.50
MIN_MARGIN = 0.15


def rms_dbfs(x: NDArray[np.floating]) -> float:
    """The level of a block, in decibels below full scale."""
    block = np.asarray(x, dtype=np.float64)
    if block.size == 0:
        return float("-inf")
    rms = float(np.sqrt(np.mean(block * block)))
    return 20.0 * math.log10(rms) if rms > 0 else float("-inf")


def channel_matrix(
    a: NDArray[np.floating], b: NDArray[np.floating]
) -> NDArray[np.float64]:
    """Correlation of every channel of ``a`` against every channel of ``b``.

    Both blocks are ``(frames, channels)`` and must already be aligned: this
    answers "are these the same channels in the same order", not "how far apart
    are they", and the two questions have to be asked in that order.
    """
    left = np.asarray(a, dtype=np.float64)
    right = np.asarray(b, dtype=np.float64)
    if left.ndim != 2 or right.ndim != 2:
        raise ValueError("both blocks have to be (frames, channels)")
    length = min(len(left), len(right))
    if length < 2:
        raise ValueError("there is not enough overlap to correlate")
    left = left[:length] - left[:length].mean(axis=0)
    right = right[:length] - right[:length].mean(axis=0)
    norms = np.outer(
        np.maximum(np.linalg.norm(left, axis=0), 1e-12),
        np.maximum(np.linalg.norm(right, axis=0), 1e-12),
    )
    return np.asarray((left.T @ right) / norms, dtype=np.float64)


@dataclass(frozen=True)
class ChannelCheck:
    """Whether the two sources carry their channels in the same order."""

    matrix: NDArray[np.float64]
    diagonal: tuple[float, ...]
    best_off_diagonal: tuple[float, ...]
    problems: tuple[str, ...]

    @property
    def agreed(self) -> bool:
        return not self.problems

    def describe(self, names: tuple[str, ...] = SIX_CHANNEL_NAMES) -> list[str]:
        width = self.matrix.shape[0]
        labels = [names[i] if i < len(names) else str(i) for i in range(width)]
        lines = ["      " + "".join(f"{label:>8}" for label in labels)]
        for index, label in enumerate(labels):
            lines.append(
                f"  {label:<4}" + "".join(f"{v:8.3f}" for v in self.matrix[index])
            )
        lines += [f"PROBLEM: {p}" for p in self.problems]
        return lines


def compare_channels(
    a: NDArray[np.floating],
    b: NDArray[np.floating],
    *,
    min_diagonal: float = MIN_DIAGONAL,
    min_margin: float = MIN_MARGIN,
) -> ChannelCheck:
    """Check that each channel's best match in the other source is itself."""
    matrix = channel_matrix(a, b)
    width = matrix.shape[0]
    problems: list[str] = []
    diagonal: list[float] = []
    best_off: list[float] = []
    for index in range(width):
        own = float(matrix[index, index]) if index < matrix.shape[1] else float("nan")
        row = matrix[index].copy()
        if index < len(row):
            row[index] = -2.0
        other = float(row.max()) if len(row) else -2.0
        diagonal.append(own)
        best_off.append(other)
        if not np.isfinite(own) or own < min_diagonal:
            problems.append(
                f"channel {index} matches itself at {own:.3f}, which is not a match"
            )
        elif own - other < min_margin:
            problems.append(
                f"channel {index} matches channel {int(np.argmax(matrix[index]))} "
                f"about as well as itself ({other:.3f} against {own:.3f}); the two "
                "sources do not carry their channels in the same order"
            )
    check = ChannelCheck(
        matrix=matrix, diagonal=tuple(diagonal),
        best_off_diagonal=tuple(best_off), problems=tuple(problems),
    )
    for line in check.describe():
        log.debug("%s", line)
    return check
