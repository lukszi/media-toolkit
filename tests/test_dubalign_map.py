"""The measurement layer, against a pair whose defects were chosen in advance.

The synthetic pair carries a head offset, a step, and a rate difference after
it. The map has to find all three as a shape, the drift measurement has to
resolve the slope, and the sample-exact stage has to come back with an integer
that every point agrees on where the offset really is constant.

Nothing here needs a program installed.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import numpy as np
import pytest
from dubalign.align import ANALYSIS_RATE, FULL_RATE
from dubalign.densemap import LagPoint, LagSeries, dense_map
from dubalign.drift import RateFit, drift, fit_rate
from dubalign.lag48k import sample_exact_lag

from tests.synthetic import SyntheticPair, interleave, noise, programme, synthetic_pair

SR = ANALYSIS_RATE


@pytest.fixture(scope="module")
def pair() -> SyntheticPair:
    return synthetic_pair()


# ------------------------------------------------------------------- the map
def test_the_map_finds_the_offset_before_the_step(pair: SyntheticPair) -> None:
    series = dense_map(pair.reference, pair.other, sr=SR, limit=(2.0, 18.0)).confident()
    assert len(series) >= 3
    for point in series:
        assert point.lag_s == pytest.approx(pair.head_lag_s, abs=0.004)


def test_the_map_follows_the_offset_after_the_step(pair: SyntheticPair) -> None:
    series = dense_map(
        pair.reference, pair.other, sr=SR, window_s=8.0, step_s=4.0, limit=(22.0, 116.0)
    ).confident()
    assert len(series) >= 10
    for point in series:
        assert point.lag_s == pytest.approx(pair.true_lag(point.t), abs=0.008)


def test_the_map_shows_a_shape_that_a_single_measurement_would_hide(
    pair: SyntheticPair,
) -> None:
    """The reason the map exists: one number cannot describe this pair."""
    whole = dense_map(pair.reference, pair.other, sr=SR, step_s=4.0).confident()
    assert whole.spread_ms() > 40.0
    head = dense_map(pair.reference, pair.other, sr=SR, limit=(2.0, 18.0)).confident()
    assert head.spread_ms() < 8.0


def test_a_silent_window_reports_no_measurement_rather_than_an_argmax() -> None:
    reference = np.concatenate([np.zeros(20 * SR), programme(20.0)])
    series = dense_map(reference, reference, sr=SR, window_s=5.0, step_s=5.0)
    silent = [p for p in series if p.r == 0.0]
    assert silent, "a window of digital silence has to be reported as no measurement"
    assert all(np.isnan(p.lag_s) for p in silent)
    assert all(not p.confident() for p in silent)


def test_a_track_against_itself_reads_zero() -> None:
    """The first control, as a unit test."""
    signal = programme(40.0)
    measured = dense_map(signal, signal, sr=SR, step_s=5.0).confident()
    assert len(measured) >= 5
    for point in measured:
        assert point.lag_s == pytest.approx(0.0, abs=0.002)


def test_the_bar_for_an_envelope_measurement_is_not_the_bar_for_a_waveform_one(
    pair: SyntheticPair,
) -> None:
    """A correlation of 0.12 between two envelopes is a good match, and the
    series has to carry that rather than leave it to whoever reads it."""
    series = dense_map(pair.reference, pair.other, sr=SR, window_s=8.0, step_s=8.0)
    assert series.min_r < 0.3
    after_the_step = [p for p in series if p.t > 30.0]
    assert after_the_step
    assert max(p.r for p in after_the_step) < 0.5, "the height is genuinely low"
    assert series.confident().points, "and the ratio is what keeps these points"


def test_a_window_that_does_not_fit_is_refused_with_both_lengths() -> None:
    with pytest.raises(ValueError, match="does not fit"):
        dense_map(programme(2.0), programme(2.0), sr=SR, window_s=10.0)


# ---------------------------------------------------------------- the series
def test_the_series_filters_on_confidence_and_keeps_how_it_was_measured() -> None:
    points = (
        LagPoint(t=0.0, lag_s=0.1, r=0.9, ratio=4.0),
        LagPoint(t=2.0, lag_s=9.9, r=0.1, ratio=1.01),
    )
    series = LagSeries(points=points, window_s=10.0, step_s=2.0)
    kept = series.confident()
    assert len(kept) == 1
    assert kept.window_s == 10.0 and kept.step_s == 2.0
    assert kept.median_lag_s() == pytest.approx(0.1)


def test_an_explicit_bar_overrides_the_one_the_series_carries() -> None:
    series = LagSeries(
        points=(LagPoint(0.0, 0.1, 0.12, 3.0),), window_s=8.0, step_s=4.0, min_r=0.08
    )
    assert len(series.confident()) == 1
    assert len(series.confident(min_r=0.5)) == 0


def test_an_empty_series_says_so_rather_than_raising_in_a_summary() -> None:
    assert LagSeries((), 10.0, 2.0).summary() == "no confident points"
    assert LagSeries((), 10.0, 2.0).spread_ms() == 0.0
    with pytest.raises(ValueError, match="no median"):
        LagSeries((), 10.0, 2.0).median_lag_s()


# ----------------------------------------------------------------- the drift
def test_the_drift_measurement_resolves_the_slope(pair: SyntheticPair) -> None:
    series = drift(
        pair.reference, pair.other, pair.lag_after_step,
        sr=SR, window_s=2.0, step_s=5.0, limit=(22.0, 116.0),
    ).confident()
    assert len(series) >= 15
    fit = fit_rate(series)
    assert fit.slope == pytest.approx(pair.slope, abs=1e-5)
    assert fit.rate_ratio == pytest.approx(1.0 / pair.rate_after, abs=1e-5)
    assert fit.speed_ratio == pytest.approx(pair.rate_after, abs=1e-5)
    assert fit.residual_max_ms < 2.0


def test_the_slope_is_a_defect_a_constant_offset_cannot_absorb(
    pair: SyntheticPair,
) -> None:
    """What the drift costs if it is treated as an offset: past the tolerance."""
    series = drift(
        pair.reference, pair.other, pair.lag_after_step,
        sr=SR, window_s=2.0, step_s=5.0, limit=(22.0, 116.0),
    ).confident()
    assert abs(fit_rate(series).total_ms()) > 40.0


def test_a_base_that_is_a_function_measures_across_a_step(
    pair: SyntheticPair,
) -> None:
    """One call over a programme with two plateaus, without averaging them."""
    series = drift(
        pair.reference, pair.other,
        lambda t: pair.head_lag_s if t < pair.step_at_s else pair.lag_after_step,
        sr=SR, window_s=2.0, step_s=4.0, limit=(2.0, 40.0),
    ).confident()
    assert len(series) >= 8
    for point in series:
        assert point.lag_s == pytest.approx(pair.true_lag(point.t), abs=0.003)


def test_fitting_a_line_across_a_step_says_so_in_its_residual(
    pair: SyntheticPair,
) -> None:
    """The failure this residual exists to catch: a step read as a ramp."""
    across = drift(
        pair.reference, pair.other, pair.head_lag_s,
        sr=SR, window_s=2.0, step_s=2.0, limit=(10.0, 32.0),
    ).confident()
    assert len(across) >= 8
    assert fit_rate(across).residual_max_ms > 10.0


def test_a_line_needs_two_points() -> None:
    with pytest.raises(ValueError, match="at least two"):
        fit_rate(LagSeries((LagPoint(0.0, 0.0, 1.0, 9.0),), 4.0, 10.0))


# ---------------------------------------------------------- the whole samples
def test_the_sample_exact_stage_agrees_with_itself_on_a_constant_offset() -> None:
    signal = noise(20.0, FULL_RATE)
    offset = 1234
    delayed = np.concatenate([np.zeros(offset), signal])
    result = sample_exact_lag(
        interleave(signal), interleave(delayed), offset / FULL_RATE,
        [2.0, 6.0, 10.0, 14.0], sample_rate=FULL_RATE,
    )
    assert result.agrees(), result.describe()
    assert result.median() == offset


def test_it_measures_on_the_centre_channel_and_not_the_low_frequency_one() -> None:
    signal = noise(10.0, FULL_RATE)
    result = sample_exact_lag(
        interleave(signal), interleave(signal), 0.0, [2.0, 5.0], sample_rate=FULL_RATE
    )
    assert {x.channel for x in result.lags} == {2}


def test_a_stretch_that_drifts_does_not_agree_which_is_the_point() -> None:
    pair = synthetic_pair(sr=FULL_RATE, duration_s=60.0)
    result = sample_exact_lag(
        interleave(pair.reference), interleave(pair.other), pair.lag_after_step,
        [24.0, 40.0, 56.0], sample_rate=FULL_RATE, window_s=2.0,
    )
    assert not result.agrees()
    assert len(set(result.offsets)) >= 2


def test_a_window_past_the_end_is_skipped_not_guessed() -> None:
    signal = noise(5.0, FULL_RATE)
    result = sample_exact_lag(
        interleave(signal), interleave(signal), 0.0, [1.0, 99.0], sample_rate=FULL_RATE
    )
    assert len(result.lags) == 1


def test_a_silent_stretch_is_skipped_in_every_channel() -> None:
    quiet = np.zeros((5 * FULL_RATE, 6), dtype=np.float32)
    result = sample_exact_lag(quiet, quiet, 0.0, [1.0], sample_rate=FULL_RATE)
    assert result.lags == ()
    assert result.describe() == "nothing was measured"
    with pytest.raises(ValueError, match="nothing was measured"):
        result.median()


def test_the_printed_rate_ratio_is_the_one_the_plan_uses() -> None:
    """A slope of -0.5 ms/s is read back at 0.9995, and that is what is printed.

    The printout used to show the reciprocal, 1.0005, while the plan wrote
    0.9995 -- two numbers for one stretch, one of them not the one built from.
    """
    from dubalign.changepoints import Segment as ModelSegment
    from dubalign.changepoints import Segmentation
    from dubalign.plan import plan_from_measurements

    fit = RateFit(slope=-5e-4, intercept=0.09, residual_rms_ms=0.1,
                  residual_max_ms=0.2, n=20, t0=0.0, t1=100.0)
    assert fit.rate_ratio == pytest.approx(0.9995)
    assert f"{fit.rate_ratio:.7f}" in fit.describe()
    assert "1.0005" not in fit.describe()
    stretch = ModelSegment(t0=0.0, t1=100.0, slope=fit.slope, intercept=fit.intercept,
                           n=20, residual_rms_ms=0.1)
    plan = plan_from_measurements(
        Segmentation(segments=(stretch,), changepoints=(), penalty=1e-7, noise_ms=0.3),
        total_frames=FULL_RATE * 100, quiet_search=False,
    )
    assert plan.segments[0].rate_ratio == fit.rate_ratio == stretch.rate_ratio
