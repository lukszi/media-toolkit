"""dubalign.densemap -- the offset across the whole timeline, not at one point.

The question "how far apart are these two tracks" has no single answer, and
assuming it does is the mistake this module exists to prevent. Two transfers of
one programme can differ by a constant offset, by several constant offsets with
steps between them, by a slowly sliding rate difference, or by all three at
once. A measurement taken at one point cannot tell those apart, and it will
report a confident number for every one of them.

So the offset is measured **edge to edge**: a window of the reference is
searched against the *whole* of the other source, every couple of seconds, and
what comes back is a curve. A flat stretch is a constant offset. A vertical
move is a cut. A slope is a rate difference. All three are visible in the
picture and none of them is visible in a number.

Two properties make it affordable. The search runs on the onset envelope, at a
thousandth of the sample count, so a window can be searched against two hours
rather than against a guess. And the long side is transformed once and reused
for every window, so the cost of the whole map is one transform plus a
multiply per window.

Every point carries its own confidence. Music under dialogue, a passage of
silence, or a stretch that exists in one source and not the other all produce a
lag, and the confidence is how the next stage knows not to believe it.
:meth:`LagSeries.confident` is the filter everything downstream starts with.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import logging
import statistics
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

import numpy as np

from .align import (
    ANALYSIS_RATE,
    ENVELOPE_RATE,
    MIN_R,
    MIN_RATIO,
    Signal,
    best_with_ratio,
    ncc_full,
    onset_env,
)

__all__ = [
    "ENVELOPE_MIN_R",
    "ENVELOPE_MIN_RATIO",
    "GUARD_S",
    "LagPoint",
    "LagSeries",
    "dense_map",
]

log = logging.getLogger(__name__)

#: How far from the peak the second peak has to be, in seconds of lag. Wide
#: enough to clear the envelope's main lobe, narrow enough that a genuine
#: second alignment half a second away still counts against the first.
GUARD_S = 0.5

#: The bar for an envelope measurement, which is *not* the bar for a waveform
#: one and is much lower. Two transfers that were encoded separately, and
#: possibly resampled, share their onsets but not their sample values: a
#: correlation of 0.12 between their envelopes is an ordinary good match, where
#: 0.12 between their waveforms would be nothing at all. What separates a match
#: from a coincidence here is the ratio, not the height -- which is the whole
#: argument for having a second number. The height is kept only as a floor
#: against a window with no onsets in it.
ENVELOPE_MIN_R = 0.08
ENVELOPE_MIN_RATIO = MIN_RATIO


@dataclass(frozen=True)
class LagPoint:
    """One measurement: at ``t`` on the reference, the other source is ``lag_s`` away.

    ``t`` is the **centre** of the window the measurement was taken over, not
    where the window started. A correlation over a window reports the average
    offset across it, and on a stretch that is sliding, the average belongs in
    the middle. Labelling it at the start instead biases every point by half a
    window times the slope -- a quarter of a millisecond for a one-second
    window at half a millisecond per second, which is small until it is used to
    place a join and the join lands where the bias put it.
    """

    t: float
    lag_s: float
    r: float
    ratio: float

    @property
    def lag_ms(self) -> float:
        return self.lag_s * 1000.0

    def confident(self, *, min_r: float = MIN_R, min_ratio: float = MIN_RATIO) -> bool:
        return self.r >= min_r and self.ratio >= min_ratio


@dataclass(frozen=True)
class LagSeries:
    """A curve of offsets over a timeline, with how it was measured."""

    points: tuple[LagPoint, ...]
    window_s: float
    step_s: float
    #: What the lags are relative to, where they were measured around a base.
    base_s: float | None = None
    #: The bar that belongs to how this series was measured. Carried on the
    #: series rather than applied by whoever reads it, because an envelope
    #: measurement and a waveform measurement are believable at very different
    #: correlations and only the producer knows which this is.
    min_r: float = MIN_R
    min_ratio: float = MIN_RATIO

    def __len__(self) -> int:
        return len(self.points)

    def __iter__(self) -> Iterator[LagPoint]:
        return iter(self.points)

    def __getitem__(self, index: int) -> LagPoint:
        return self.points[index]

    def times(self) -> Signal:
        return np.asarray([p.t for p in self.points], dtype=np.float64)

    def lags(self) -> Signal:
        return np.asarray([p.lag_s for p in self.points], dtype=np.float64)

    def confident(
        self, *, min_r: float | None = None, min_ratio: float | None = None
    ) -> LagSeries:
        """The same series with only the points worth believing.

        Nothing downstream should ever run on the unfiltered series. A window
        of silence, or of music that repeats, produces a lag like any other and
        it is not a measurement.
        """
        bar_r = self.min_r if min_r is None else min_r
        bar_ratio = self.min_ratio if min_ratio is None else min_ratio
        kept = tuple(
            p for p in self.points if p.confident(min_r=bar_r, min_ratio=bar_ratio)
        )
        return LagSeries(
            points=kept, window_s=self.window_s, step_s=self.step_s,
            base_s=self.base_s, min_r=bar_r, min_ratio=bar_ratio,
        )

    def median_lag_s(self) -> float:
        if not self.points:
            raise ValueError("an empty series has no median")
        return statistics.median(p.lag_s for p in self.points)

    def spread_ms(self) -> float:
        """How far apart the extremes are. Flat means one constant offset."""
        if not self.points:
            return 0.0
        lags = self.lags()
        return float((lags.max() - lags.min()) * 1000.0)

    def summary(self) -> str:
        if not self.points:
            return "no confident points"
        lags = self.lags()
        return (
            f"{len(self.points)} points, lag median {self.median_lag_s() * 1000:+.1f} ms, "
            f"{lags.min() * 1000:+.1f} to {lags.max() * 1000:+.1f}, "
            f"spread {self.spread_ms():.1f} ms, "
            f"r median {statistics.median(p.r for p in self.points):.3f}"
        )


def dense_map(
    reference: Signal,
    other: Signal,
    *,
    sr: int = ANALYSIS_RATE,
    window_s: float = 10.0,
    step_s: float = 2.0,
    guard_s: float = GUARD_S,
    limit: Sequence[float] | None = None,
) -> LagSeries:
    """Search each window of the reference against the whole of the other source.

    ``limit`` is ``(t0, t1)`` on the reference timeline, for when only part of
    the programme is of interest. Without it the map runs edge to edge, which
    is the point.

    The lag is signed so that the same content sits at ``t + lag`` in the other
    source: a positive lag means the other source is late.
    """
    env_a = onset_env(reference, hop=sr // ENVELOPE_RATE)
    env_b = onset_env(other, hop=sr // ENVELOPE_RATE)
    width = int(window_s * ENVELOPE_RATE)
    if len(env_a) < width or len(env_b) < width:
        raise ValueError(
            f"a {window_s:.1f} s window does not fit: the two sources are "
            f"{len(env_a) / ENVELOPE_RATE:.1f} s and {len(env_b) / ENVELOPE_RATE:.1f} s"
        )
    # One transform of the long side, reused for every window. This is what
    # makes an edge-to-edge search affordable rather than notional.
    n_fft = 1 << (len(env_b) + width - 1).bit_length()
    b_fft = np.fft.rfft(env_b, n_fft)
    guard = max(1, int(guard_s * ENVELOPE_RATE))

    start = 0.0 if limit is None else float(limit[0])
    end = len(env_a) / ENVELOPE_RATE if limit is None else float(limit[1])
    points: list[LagPoint] = []
    t = start
    while t + window_s <= end:
        index = round(t * ENVELOPE_RATE)
        centre = t + window_s / 2.0
        window = env_a[index : index + width]
        # A window with no onsets in it has no alignment; say so rather than
        # reporting the argmax of a flat correlation.
        if len(window) < width or window.std() < 1e-9:
            points.append(LagPoint(t=centre, lag_s=float("nan"), r=0.0, ratio=0.0))
            t += step_s
            continue
        peak = best_with_ratio(ncc_full(window, env_b, b_fft=b_fft, n_fft=n_fft), guard)
        points.append(
            LagPoint(
                t=centre, lag_s=peak.index / ENVELOPE_RATE - t, r=peak.r,
                ratio=peak.ratio,
            )
        )
        t += step_s
    series = LagSeries(
        points=tuple(points), window_s=window_s, step_s=step_s,
        min_r=ENVELOPE_MIN_R, min_ratio=ENVELOPE_MIN_RATIO,
    )
    log.info("dense map: %s", series.confident().summary())
    return series
