"""dubalign.verify_pcm -- measure the built result against the reference.

The discipline, in four rules, each of which was learned by having got it
wrong first.

**Measure the whole programme, not the joins.** The obvious check is to listen
at each seam, and it is nearly worthless: the seams are the places already
thought about hardest. What goes wrong is elsewhere -- a stretch where the
model was fitted through a step, a plateau that was averaged away, a drift that
was corrected with the wrong sign. So the scan runs edge to edge at a fixed
spacing and reports every point.

**Every point has to pass, not the average.** A median of zero with a few
windows far outside the bar is a failure with a good average. The report
lists the points outside the bar and there is no summary statistic that can
hide them.

**Forty milliseconds, and the number is an argument.** That is roughly one
frame of picture, which is about where a sync error stops being something a
listener feels and starts being something they see. It is a house bar, not a
law of perception, and it is written down where it can be changed.

**Measure inside the finished thing.** Verifying the array that was built,
rather than the file that was written from it, checks the arithmetic and
misses the encoder. An encoder that adds a delay of its own -- and at least one
common one adds exactly 256 samples -- turns a perfect build into a file that
is five milliseconds late everywhere, and only a measurement of the file can
see it.

A window with nothing in it is skipped and counted rather than scored. Silence
correlates with silence at every lag, and counting that as a pass is how a scan
reports a clean result for a track that is not there at all -- which is why the
count of skipped windows is in the report and not hidden.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from .align import FULL_RATE, best_with_ratio, ncc_full
from .lag48k import PREFERRED_CHANNELS

__all__ = [
    "BAR_MS",
    "VerifyPoint",
    "VerifyReport",
    "verify_against",
]

log = logging.getLogger(__name__)

#: The house bar: about one frame of picture. An argument everywhere it is used.
BAR_MS = 40.0
#: Below this a window is treated as having nothing in it to measure.
SILENCE_PEAK = 1e-5
#: How well a window has to correlate before its lag means anything.
MIN_R = 0.70


@dataclass(frozen=True)
class VerifyPoint:
    """One measured instant of the finished result."""

    t: float
    lag_ms: float
    r: float
    ratio: float
    channel: int

    def within(self, bar_ms: float = BAR_MS, min_r: float = MIN_R) -> bool:
        return abs(self.lag_ms) < bar_ms and self.r >= min_r

    def describe(self) -> str:
        return (
            f"t={self.t:8.1f} s  lag {self.lag_ms:+8.3f} ms  r={self.r:.3f}  "
            f"peak/second={self.ratio:.2f}  channel {self.channel}"
        )


@dataclass(frozen=True)
class VerifyReport:
    """Every point, the bar they were held to, and what did not clear it."""

    points: tuple[VerifyPoint, ...]
    bar_ms: float
    min_r: float
    skipped_silent: int
    window_s: float
    step_s: float

    @property
    def outside(self) -> tuple[VerifyPoint, ...]:
        return tuple(p for p in self.points if not p.within(self.bar_ms, self.min_r))

    @property
    def passed(self) -> bool:
        """Every measured window inside the bar. Not most of them."""
        return bool(self.points) and not self.outside

    @property
    def worst_ms(self) -> float:
        return max((abs(p.lag_ms) for p in self.points), default=float("nan"))

    def describe(self) -> list[str]:
        if not self.points:
            return ["nothing was measured: every window was silent or out of range"]
        lags = [p.lag_ms for p in self.points]
        correlations = [p.r for p in self.points]
        lines = [
            f"{len(self.points)} window(s) of {self.window_s:.1f} s every "
            f"{self.step_s:.1f} s, {self.skipped_silent} skipped as silent",
            f"lag: median {statistics.median(lags):+.3f} ms, "
            f"{min(lags):+.3f} to {max(lags):+.3f}, worst {self.worst_ms:.3f} ms",
            f"correlation: median {statistics.median(correlations):.3f}, "
            f"lowest {min(correlations):.3f}",
            f"bar: {self.bar_ms:.0f} ms at every point, correlation at least {self.min_r:.2f}",
        ]
        if self.passed:
            lines.append(f"PASS: {len(self.points)} of {len(self.points)} inside the bar")
        else:
            lines.append(f"FAIL: {len(self.outside)} window(s) outside the bar")
            lines += [f"  {p.describe()}" for p in self.outside[:20]]
        return lines


def _as_block(x: NDArray[np.floating]) -> NDArray[np.float64]:
    block = np.asarray(x, dtype=np.float64)
    return block[:, None] if block.ndim == 1 else block


def verify_against(
    reference: NDArray[np.floating],
    built: NDArray[np.floating],
    *,
    sample_rate: int = FULL_RATE,
    window_s: float = 4.0,
    step_s: float = 5.0,
    search_s: float = 0.100,
    bar_ms: float = BAR_MS,
    min_r: float = MIN_R,
    preferred: tuple[int, ...] = PREFERRED_CHANNELS,
) -> VerifyReport:
    """Scan the whole runtime and report the offset at every point.

    ``built`` is expected to be on the reference's timeline, so every answer
    should be zero. ``search_s`` bounds how far a window is allowed to be
    wrong: wide enough to see a real failure, narrow enough that a window of
    music cannot find a plausible alignment a second away and report it.
    """
    left = _as_block(reference)
    right = _as_block(built)
    width = min(left.shape[1], right.shape[1])
    length = int(window_s * sample_rate)
    reach = int(search_s * sample_rate)
    end = min(len(left), len(right)) / sample_rate

    points: list[VerifyPoint] = []
    silent = 0
    t = 0.0
    while t + window_s + search_s <= end:
        index = round(t * sample_rate)
        channel = next(
            (
                c for c in (*preferred, *range(width))
                if c < width
                and float(np.abs(left[index : index + length, c]).max()) > SILENCE_PEAK
            ),
            None,
        )
        if channel is None:
            silent += 1
            t += step_s
            continue
        start = max(0, index - reach)
        span = length + (index - start) + reach
        window = left[index : index + length, channel]
        against = right[start : start + span, channel]
        if len(window) < length or len(against) < length + 1:
            t += step_s
            continue
        peak = best_with_ratio(ncc_full(window, against), max(1, int(0.002 * sample_rate)))
        points.append(
            VerifyPoint(
                t=t,
                lag_ms=(start + peak.index - index) / sample_rate * 1000.0,
                r=peak.r,
                ratio=peak.ratio,
                channel=channel,
            )
        )
        t += step_s

    report = VerifyReport(
        points=tuple(points), bar_ms=bar_ms, min_r=min_r, skipped_silent=silent,
        window_s=window_s, step_s=step_s,
    )
    for line in report.describe():
        log.info("%s", line)
    return report
