"""dubalign.changepoints -- find the seams instead of typing them in.

This is the piece the original work did not have. The measurement said where
the offset jumped; *finding* the jumps was done by reading a printout, and the
answers ended up as constants in a source file. Constants found by eye have
three problems: they cannot be checked, they cannot be re-derived when the
measurement improves, and they are quietly wrong. A join found by eye can sit
seconds away from the jump it was meant to render -- which leaves the entire
step as a sync error for that long.

So the boundaries are detected, and then refined.

**Detection** treats the lag curve as piecewise linear -- deliberately linear
and not piecewise constant, because a stretch with a rate difference in it is a
slope, and a detector that can only fit flat lines chops every slope into a
staircase of imaginary jumps. The segmentation is exact rather than greedy:
optimal partitioning over every possible split, with a penalty per boundary, so
the answer does not depend on the order things were tried in. It costs the
square of the number of measured points, which for a feature measured every few
seconds is nothing.

**Refinement** is a different question and gets a different tool. Detection
resolves a jump only to the spacing of the measurements -- a few seconds. The
two correlations in :mod:`dubalign.boundary` then locate it to a few tens of
milliseconds, by asking which of the two offsets is the right one at each
moment.

**The penalty is the one knob**, and it is reported rather than hidden: too
low and every wobble is a jump, too high and a real step is absorbed into a
slope. The default scales with the noise the series itself shows, so it adapts
to how well the material measured rather than to how long it is.

The optional numerical extra is used here for an outlier-robust pre-filter,
and there is a plain fallback for it. The detector itself needs nothing but
arrays -- it is not the sort of thing that should depend on an install.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import itertools
import logging
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from .align import ANALYSIS_RATE, Signal
from .boundary import crossing
from .densemap import LagSeries
from .drift import drift

__all__ = [
    "Changepoint",
    "Segment",
    "Segmentation",
    "find_changepoints",
    "refine_changepoints",
]

log = logging.getLogger(__name__)

#: How many measured points a segment has to hold. Three is the minimum for a
#: line to mean anything; below that every pair of points is a perfect fit and
#: the detector finds a boundary everywhere.
MIN_SEGMENT_POINTS = 3
#: How large a step has to be before it is worth a seam. Below this the join
#: costs more than the error it removes -- a crossfade is a real edit and a
#: two-millisecond correction is not audible.
MIN_STEP_MS = 5.0
#: The penalty's scale, in units of the noise the series shows. Raising it
#: finds fewer boundaries.
PENALTY_SCALE = 3.0


@dataclass(frozen=True)
class Segment:
    """One stretch over which the offset is one straight line."""

    t0: float
    t1: float
    slope: float
    intercept: float
    n: int
    residual_rms_ms: float

    def lag_at(self, t: float) -> float:
        return self.slope * t + self.intercept

    @property
    def rate_ratio(self) -> float:
        """The read ratio a plan uses for this stretch: ``1 + slope``."""
        return 1.0 + self.slope

    @property
    def drifts(self) -> bool:
        """Whether this stretch needs resampling as well as an offset."""
        return abs(self.slope) * (self.t1 - self.t0) * 1000.0 > MIN_STEP_MS

    def describe(self) -> str:
        return (
            f"{self.t0:8.2f} - {self.t1:8.2f} s  lag "
            f"{self.lag_at(self.t0) * 1000:+8.1f} to {self.lag_at(self.t1) * 1000:+8.1f} ms"
            f"  slope {self.slope * 1000:+.3f} ms/s  n={self.n}"
            f"  residual {self.residual_rms_ms:.2f} ms"
        )


@dataclass(frozen=True)
class Changepoint:
    """Where the offset jumps, by how much, and how well the instant is known."""

    t: float
    bracket: tuple[float, float]
    step_s: float
    lag_before: float
    lag_after: float
    #: How fast the offset is sliding on each side. Carried because a stretch
    #: that drifts has no single offset, and locating the jump means following
    #: the drift rather than picking one number out of the middle of it.
    slope_before: float = 0.0
    slope_after: float = 0.0
    refined: bool = False
    margin: float = 0.0

    def lag_before_at(self, t: float) -> float:
        return self.lag_before + self.slope_before * (t - self.t)

    def lag_after_at(self, t: float) -> float:
        return self.lag_after + self.slope_after * (t - self.t)

    @property
    def step_ms(self) -> float:
        return self.step_s * 1000.0

    @property
    def uncertainty_s(self) -> float:
        return (self.bracket[1] - self.bracket[0]) / 2.0

    def describe(self) -> str:
        how = "refined" if self.refined else "from the measurement spacing"
        return (
            f"{self.t:9.3f} s  step {self.step_ms:+8.1f} ms  "
            f"(+/- {self.uncertainty_s * 1000:.0f} ms, {how})"
        )


@dataclass(frozen=True)
class Segmentation:
    """The whole model: straight stretches, and the jumps between them."""

    segments: tuple[Segment, ...]
    changepoints: tuple[Changepoint, ...]
    penalty: float
    noise_ms: float

    def lag_at(self, t: float) -> float:
        """The modelled offset at any instant, from whichever segment owns it."""
        if not self.segments:
            raise ValueError("an empty segmentation has no offset")
        for segment in self.segments:
            if t <= segment.t1:
                return segment.lag_at(t)
        return self.segments[-1].lag_at(t)

    def describe(self) -> list[str]:
        lines = [
            f"{len(self.segments)} segment(s), {len(self.changepoints)} changepoint(s); "
            f"measurement noise {self.noise_ms:.2f} ms, penalty {self.penalty:.3g}"
        ]
        lines += [f"  {s.describe()}" for s in self.segments]
        lines += [f"  jump at {c.describe()}" for c in self.changepoints]
        return lines


# --------------------------------------------------------------------- pieces
def _median_filter(y: Signal, width: int) -> Signal:
    """Knock out single wild points. Uses the extra where it is installed."""
    if width < 3 or len(y) < width:
        return y
    try:
        from scipy.ndimage import median_filter
    except ImportError:
        half = width // 2
        padded = np.pad(y, half, mode="edge")
        windows = np.lib.stride_tricks.sliding_window_view(padded, width)
        return np.asarray(np.median(windows, axis=1), dtype=np.float64)
    return np.asarray(median_filter(y, size=width, mode="nearest"), dtype=np.float64)


def _noise_estimate(y: Signal) -> float:
    """How much the curve wobbles from point to point, robustly.

    Successive differences rather than a residual, so a genuine slope does not
    read as noise; a median absolute deviation rather than a standard one, so
    one real jump does not inflate it. The 0.6745 turns the median absolute
    deviation into the standard deviation it corresponds to for normal data,
    and the square root of two undoes the differencing.
    """
    if len(y) < 3:
        return 0.0
    differences = np.abs(np.diff(y))
    return float(np.median(differences) / 0.6745 / np.sqrt(2.0))


def _line_cost(t: Signal, y: Signal) -> float:
    """Residual sum of squares of the best straight line through a stretch."""
    n = len(t)
    if n < 2:
        return 0.0
    mean_t = t.mean()
    mean_y = y.mean()
    dt = t - mean_t
    denominator = float(np.dot(dt, dt))
    slope = float(np.dot(dt, y - mean_y) / denominator) if denominator > 1e-18 else 0.0
    residual = y - (slope * dt + mean_y)
    return float(np.dot(residual, residual))


def _fit_segment(t: Signal, y: Signal) -> tuple[float, float, float]:
    design = np.vstack([t, np.ones_like(t)]).T
    (slope, intercept), *_ = np.linalg.lstsq(design, y, rcond=None)
    residual = y - (slope * t + intercept)
    rms = float(np.sqrt(np.mean(residual * residual)))
    return float(slope), float(intercept), rms


def _partition(t: Signal, y: Signal, penalty: float, min_size: int) -> list[int]:
    """Optimal partitioning: the best split of the whole curve, not a greedy one.

    ``best[j]`` is the cost of the best segmentation of the first ``j`` points.
    Every possible previous boundary is considered, so the result does not
    depend on the order candidates were tried in -- which is the difference
    between a detector that gives one answer and one that gives a different
    answer when a point is added at the end.
    """
    n = len(t)
    best = np.full(n + 1, np.inf)
    best[0] = -penalty
    previous = np.zeros(n + 1, dtype=np.int64)
    for j in range(min_size, n + 1):
        for i in range(0, j - min_size + 1):
            if not np.isfinite(best[i]):
                continue
            candidate = best[i] + _line_cost(t[i:j], y[i:j]) + penalty
            if candidate < best[j]:
                best[j] = candidate
                previous[j] = i
    cuts: list[int] = []
    at = n
    while at > 0:
        cuts.append(at)
        at = int(previous[at])
    return sorted(cuts)


def _segments_from(t: Signal, y: Signal, cuts: list[int]) -> list[Segment]:
    """Fit one straight line to each stretch the cuts mark out."""
    segments: list[Segment] = []
    start = 0
    for end in cuts:
        if end - start >= 2:
            slope, intercept, rms = _fit_segment(t[start:end], y[start:end])
            segments.append(
                Segment(
                    t0=float(t[start]), t1=float(t[end - 1]), slope=slope,
                    intercept=intercept, n=end - start, residual_rms_ms=rms * 1000.0,
                )
            )
        start = end
    return segments


def _steps_between(segments: list[Segment]) -> list[float]:
    """How far the offset jumps at each boundary, read at the midpoint.

    Both models are evaluated at the same instant, so the step is the distance
    between them there rather than between two fits extrapolated apart.
    """
    steps: list[float] = []
    for left, right in itertools.pairwise(segments):
        middle = (left.t1 + right.t0) / 2.0
        steps.append(right.lag_at(middle) - left.lag_at(middle))
    return steps


def find_changepoints(
    series: LagSeries,
    *,
    penalty: float | None = None,
    penalty_scale: float = PENALTY_SCALE,
    min_size: int = MIN_SEGMENT_POINTS,
    min_step_ms: float = MIN_STEP_MS,
    smooth: int = 3,
) -> Segmentation:
    """Break a lag curve into straight stretches and report the jumps between them.

    ``penalty`` is the cost of admitting one more boundary. Left out, it is
    derived from the noise the series itself shows, so a well-measured curve
    admits smaller steps and a noisy one does not -- which is the behaviour
    wanted, and the opposite of a constant chosen once on one programme.

    The series should already be filtered to its confident points. A boundary
    fitted through a window of silence is a boundary in the silence, not in the
    programme.
    """
    points = tuple(p for p in series.points if not np.isnan(p.lag_s))
    if len(points) < 2 * min_size:
        return Segmentation(segments=(), changepoints=(), penalty=0.0, noise_ms=0.0)
    t = np.asarray([p.t for p in points], dtype=np.float64)
    y = np.asarray([p.lag_s for p in points], dtype=np.float64)
    filtered = _median_filter(y, smooth)
    # The noise is taken from the raw curve and the fit from the filtered one.
    # Taking both from the filtered curve reads the smoothing as a measurement
    # that went better than it did, and a penalty set from that lets the
    # detector split a flat stretch into a dozen imaginary ones.
    noise = _noise_estimate(y)
    chosen = (
        penalty
        if penalty is not None
        else penalty_scale * max(noise, 1e-6) ** 2 * np.log(len(t))
    )

    # Each cut is where one segment ends and the next begins; the last is the
    # end of the curve. A boundary whose step is too small to be worth a seam
    # is not reported *and not kept*: the two stretches either side are merged
    # and refitted, so the number of segments is always one more than the
    # number of jumps. A model that says otherwise cannot be built.
    cuts = _partition(t, filtered, float(chosen), min_size)
    while True:
        segments = _segments_from(t, filtered, cuts)
        steps = _steps_between(segments)
        small = [
            (abs(step), index) for index, step in enumerate(steps)
            if abs(step) * 1000.0 < min_step_ms
        ]
        if not small or len(cuts) < 2:
            break
        cuts.pop(min(small)[1])

    changes = [
        Changepoint(
            t=(left.t1 + right.t0) / 2.0,
            bracket=(left.t1, right.t0),
            step_s=step,
            lag_before=left.lag_at((left.t1 + right.t0) / 2.0),
            lag_after=right.lag_at((left.t1 + right.t0) / 2.0),
            slope_before=left.slope,
            slope_after=right.slope,
        )
        for (left, right), step in zip(
            itertools.pairwise(segments), _steps_between(segments), strict=True
        )
    ]

    found = Segmentation(
        segments=tuple(segments), changepoints=tuple(changes),
        penalty=float(chosen), noise_ms=noise * 1000.0,
    )
    for line in found.describe():
        log.info("%s", line)
    return found


def _correction(
    reference: Signal,
    other: Signal,
    model: Callable[[float], float],
    t0: float,
    t1: float,
    sr: int,
    *,
    search_s: float = 0.050,
) -> float | None:
    """How far the model is out over a stretch entirely on one side of a jump.

    A correction rather than an offset, so the drift stays in the model and
    only the constant error in it is measured.
    """
    if t1 - t0 < 1.0:
        return None
    measured = drift(
        reference, other, model, sr=sr, window_s=1.0, step_s=0.5,
        search_s=search_s, limit=(t0, t1),
    ).confident()
    if len(measured) < 2:
        return None
    return float(np.median([p.lag_s - model(p.t) for p in measured]))


def refine_changepoints(
    found: Segmentation,
    reference: Signal,
    other: Signal,
    *,
    sr: int = ANALYSIS_RATE,
    pad_s: float = 1.0,
    window_s: float = 0.200,
    step_s: float = 0.005,
) -> Segmentation:
    """Pin each jump down to a few tens of milliseconds with two correlations.

    Detection gets the jump to within the spacing of the measurements, which is
    seconds. This gets it to about the length of one correlation window. The
    difference matters: a join placed a second from its jump leaves the whole
    step uncorrected for that second, and a second of a hundred-millisecond
    error is heard.
    """
    refined: list[Changepoint] = []
    for change in found.changepoints:
        t0 = max(0.0, change.bracket[0] - pad_s)
        t1 = change.bracket[1] + pad_s
        # Re-measure the two offsets on material that is unambiguously on one
        # side of the jump. The model's values are extrapolations, and at this
        # resolution an extrapolation that is two milliseconds out correlates
        # at nothing -- which would make the crossing look like noise rather
        # than like a cut. This is the step that makes the refinement work.
        before_model = change.lag_before_at
        after_model = change.lag_after_at
        before = _correction(reference, other, before_model, t0, change.bracket[0], sr)
        after = _correction(reference, other, after_model, change.bracket[1], t1, sr)
        where = (
            None
            if before is None or after is None
            else crossing(
                reference, other,
                lambda t, m=before_model, d=before: m(t) + d,  # type: ignore[misc]
                lambda t, m=after_model, d=after: m(t) + d,  # type: ignore[misc]
                t0, t1, sr=sr, window_s=window_s, step_s=step_s,
            )
        )
        if where is None or not where.confident:
            log.info(
                "the jump near %.3f s could not be pinned down between %.2f and %.2f s; "
                "keeping the estimate from the measurement spacing",
                change.t, t0, t1,
            )
            refined.append(change)
            continue
        refined.append(
            Changepoint(
                t=where.t,
                bracket=(where.t - window_s / 2.0, where.t + window_s / 2.0),
                step_s=change.step_s,
                lag_before=change.lag_before,
                lag_after=change.lag_after,
                refined=True,
                margin=where.margin,
            )
        )
    return Segmentation(
        segments=found.segments, changepoints=tuple(refined),
        penalty=found.penalty, noise_ms=found.noise_ms,
    )
