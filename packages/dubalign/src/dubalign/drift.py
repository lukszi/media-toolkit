"""dubalign.drift -- the fine measurement inside one stretch, and its slope.

The dense map says roughly where the offset is, to about a millisecond, over
the whole timeline. This measures it properly: the raw waveform, in a narrow
bracket around what the map already found, which is the only reason searching
the waveform is affordable at all.

The reason to do it is the slope. Inside a stretch where the offset looks
constant, the residual is not noise -- it is usually a straight line, and that
line is the rate difference between the two transfers. A slope of one part in
a thousand is a millisecond a second: inaudible for ten seconds, forty
milliseconds after forty, and half a second by the end of a feature. It cannot
be fixed by moving anything; it has to be resampled out, which is what
:func:`dubalign.pal.warp` does with the fit this module produces.

The distinction that matters, and the one the original work got wrong before it
got it right: **a slope and a step are different defects with different fixes,
and a measurement too coarse to separate them turns a step into a slope.** A
plateau of a few seconds averaged into its neighbours becomes one gentle ramp
that is wrong everywhere instead of one jump that is wrong nowhere. So the fit
here reports its residual, and a residual that is not small is the series
saying it is not one stretch.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from .align import ANALYSIS_RATE, Signal, best_with_ratio, ncc_full
from .densemap import LagPoint, LagSeries

__all__ = [
    "RateFit",
    "drift",
    "fit_rate",
]

log = logging.getLogger(__name__)

#: How far either side of the base offset the waveform search looks.
SEARCH_S = 0.30

Base = float | Callable[[float], float]


def _base_at(base: Base, t: float) -> float:
    return base(t) if callable(base) else base


def drift(
    reference: Signal,
    other: Signal,
    base: Base,
    *,
    sr: int = ANALYSIS_RATE,
    window_s: float = 4.0,
    step_s: float = 10.0,
    search_s: float = SEARCH_S,
    limit: tuple[float, float] | None = None,
) -> LagSeries:
    """Measure the offset on the raw waveform around a base the map supplied.

    ``base`` is either one number, for a stretch that looks constant, or a
    function of reference time, for a piecewise model that has already been
    fitted. Passing a function is how a whole programme with several plateaus
    is measured in one call without averaging across the steps between them.
    """
    length = int(window_s * sr)
    reach = int(search_s * sr)
    start = 0.0 if limit is None else limit[0]
    end = len(reference) / sr if limit is None else limit[1]
    points: list[LagPoint] = []
    t = start
    while t + window_s <= end:
        here = _base_at(base, t)
        index_a = round(t * sr)
        index_b = round((t + here - search_s) * sr)
        span = length + 2 * reach
        window = reference[index_a : index_a + length]
        if (
            len(window) == length
            and index_b >= 0
            and index_b + span <= len(other)
            and window.std() > 1e-9
        ):
            peak = best_with_ratio(
                ncc_full(window, other[index_b : index_b + span]), max(1, int(0.002 * sr))
            )
            points.append(
                LagPoint(
                    # The centre of the window, for the reason on LagPoint.
                    t=t + window_s / 2.0,
                    lag_s=here - search_s + peak.index / sr,
                    r=peak.r,
                    ratio=peak.ratio,
                )
            )
        t += step_s
    series = LagSeries(
        points=tuple(points), window_s=window_s, step_s=step_s,
        base_s=_base_at(base, start),
    )
    log.info("drift: %s", series.confident().summary())
    return series


@dataclass(frozen=True)
class RateFit:
    """A straight line through a stretch of the lag curve.

    ``slope`` is seconds of lag gained per second of programme. ``rate_ratio``
    is what that slope means as a speed difference: the other source runs this
    many times faster than the reference, so reading it back at that ratio is
    the correction.
    """

    slope: float
    intercept: float
    residual_rms_ms: float
    residual_max_ms: float
    n: int
    t0: float
    t1: float

    @property
    def rate_ratio(self) -> float:
        """The speed ratio the slope implies. One means no drift."""
        return 1.0 / (1.0 + self.slope)

    @property
    def drift_ms_per_s(self) -> float:
        return self.slope * 1000.0

    def lag_at(self, t: float) -> float:
        return self.slope * t + self.intercept

    def total_ms(self) -> float:
        """How much lag the slope accumulates across the stretch it was fitted on."""
        return (self.lag_at(self.t1) - self.lag_at(self.t0)) * 1000.0

    def describe(self) -> str:
        return (
            f"{self.t0:.1f}-{self.t1:.1f} s: {self.drift_ms_per_s:+.3f} ms/s "
            f"({self.total_ms():+.1f} ms across the stretch), rate ratio "
            f"{self.rate_ratio:.7f}, residual {self.residual_rms_ms:.2f} ms rms / "
            f"{self.residual_max_ms:.2f} ms worst, n={self.n}"
        )


def fit_rate(series: LagSeries) -> RateFit:
    """Fit a straight line to a lag curve and report how badly it fits.

    The residual is the useful half. A stretch that really is one rate
    difference fits to well under a millisecond; a residual of tens of
    milliseconds means the stretch contains a step, and averaging across it
    would produce a model that is wrong on both sides of the step rather than
    right on either.
    """
    if len(series) < 2:
        raise ValueError("a line needs at least two points")
    times = series.times()
    lags = series.lags()
    design = np.vstack([times, np.ones_like(times)]).T
    (slope, intercept), *_ = np.linalg.lstsq(design, lags, rcond=None)
    residual = lags - (slope * times + intercept)
    return RateFit(
        slope=float(slope),
        intercept=float(intercept),
        residual_rms_ms=float(np.sqrt(np.mean(residual * residual)) * 1000.0),
        residual_max_ms=float(np.abs(residual).max() * 1000.0),
        n=len(series),
        t0=float(times[0]),
        t1=float(times[-1]),
    )
