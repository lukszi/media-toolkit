"""Finding the jumps, and pinning them down.

The detector gets a curve whose shape somebody chose, so "did it find the
jump" has an answer rather than an opinion. The refinement gets the synthetic
pair, where the jump is at a known instant, and has to place it inside a few
tens of milliseconds.

The controls are as important as the measurements: a curve with no jump in it
must produce no jumps, and a curve that is one long slope must produce one
segment and not a staircase.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import numpy as np
import pytest
from dubalign.align import ANALYSIS_RATE
from dubalign.boundary import crossing, running_r
from dubalign.changepoints import (
    Changepoint,
    Segment,
    Segmentation,
    find_changepoints,
    refine_changepoints,
)
from dubalign.densemap import LagPoint, LagSeries, dense_map

from tests.synthetic import SyntheticPair, programme, synthetic_pair

SR = ANALYSIS_RATE
SEED = 20260923


def series_from(
    lag_at: object, *, t0: float = 0.0, t1: float = 200.0, step: float = 2.0,
    noise_ms: float = 0.4, seed: int = SEED,
) -> LagSeries:
    """A lag curve of a shape chosen here, with a little measurement noise."""
    rng = np.random.default_rng(seed)
    times = np.arange(t0, t1, step)
    points = tuple(
        LagPoint(
            t=float(t),
            lag_s=float(lag_at(float(t))) + rng.normal(0.0, noise_ms / 1000.0),  # type: ignore[operator]
            r=0.9,
            ratio=4.0,
        )
        for t in times
    )
    return LagSeries(points=points, window_s=4.0, step_s=step)


# ------------------------------------------------------------- the detection
def test_one_step_is_found_once_and_at_the_right_size() -> None:
    found = find_changepoints(series_from(lambda t: 0.120 if t < 100.0 else 0.040))
    assert len(found.changepoints) == 1
    jump = found.changepoints[0]
    assert jump.t == pytest.approx(100.0, abs=2.0)
    assert jump.step_ms == pytest.approx(-80.0, abs=2.0)
    assert jump.bracket[0] <= 100.0 <= jump.bracket[1]


def test_a_curve_with_no_jump_in_it_produces_no_jumps() -> None:
    """The control that matters most: a detector that always finds something
    has found nothing."""
    found = find_changepoints(series_from(lambda t: 0.120))
    assert found.changepoints == ()
    assert len(found.segments) == 1


def test_one_long_slope_is_one_segment_and_not_a_staircase() -> None:
    """A detector that can only fit flat lines invents a jump every few points."""
    found = find_changepoints(series_from(lambda t: 0.120 - 0.0005 * t))
    assert found.changepoints == ()
    assert len(found.segments) == 1
    assert found.segments[0].slope == pytest.approx(-0.0005, abs=2e-5)
    assert found.segments[0].drifts


def test_a_step_on_top_of_a_slope_is_both() -> None:
    def curve(t: float) -> float:
        return 0.120 - 0.0005 * t + (0.0 if t < 120.0 else -0.060)

    found = find_changepoints(series_from(curve))
    assert len(found.changepoints) == 1
    assert found.changepoints[0].step_ms == pytest.approx(-60.0, abs=3.0)
    assert all(s.slope == pytest.approx(-0.0005, abs=5e-5) for s in found.segments)


def test_three_steps_are_all_found() -> None:
    def curve(t: float) -> float:
        return 0.100 + (-0.040 if t > 60 else 0.0) + (0.090 if t > 110 else 0.0) + (
            -0.050 if t > 160 else 0.0
        )

    found = find_changepoints(series_from(curve))
    assert len(found.changepoints) == 3
    assert [round(c.t / 10) * 10 for c in found.changepoints] == [60, 110, 160]
    assert [round(c.step_ms, -1) for c in found.changepoints] == [-40.0, 90.0, -50.0]


def test_a_step_too_small_to_be_worth_a_seam_is_not_reported() -> None:
    found = find_changepoints(
        series_from(lambda t: 0.120 if t < 100.0 else 0.1185, noise_ms=0.05)
    )
    assert found.changepoints == ()


def test_the_penalty_is_the_knob_and_it_is_reported() -> None:
    noisy = series_from(lambda t: 0.120 if t < 100.0 else 0.040, noise_ms=4.0)
    assert find_changepoints(noisy).penalty > 0.0
    assert len(find_changepoints(noisy, penalty=1e9).changepoints) == 0
    assert len(find_changepoints(noisy, penalty=1e-12).changepoints) > 1


def test_a_noisier_curve_gets_a_larger_penalty_on_its_own() -> None:
    quiet = find_changepoints(series_from(lambda t: 0.120, noise_ms=0.2))
    loud = find_changepoints(series_from(lambda t: 0.120, noise_ms=5.0))
    assert loud.penalty > quiet.penalty * 10
    assert loud.noise_ms > quiet.noise_ms * 5


def test_too_few_points_produce_nothing_rather_than_a_guess() -> None:
    tiny = LagSeries(points=(LagPoint(0.0, 0.1, 0.9, 4.0),), window_s=4.0, step_s=2.0)
    found = find_changepoints(tiny)
    assert found.segments == () and found.changepoints == ()


def test_points_with_no_measurement_are_dropped_before_fitting() -> None:
    good = series_from(lambda t: 0.120, t1=60.0)
    with_holes = LagSeries(
        points=(*good.points, LagPoint(61.0, float("nan"), 0.0, 0.0)),
        window_s=4.0, step_s=2.0,
    )
    assert find_changepoints(with_holes).changepoints == ()


def test_the_model_can_be_read_at_any_instant() -> None:
    found = find_changepoints(series_from(lambda t: 0.120 if t < 100.0 else 0.040))
    assert found.lag_at(50.0) == pytest.approx(0.120, abs=0.002)
    assert found.lag_at(150.0) == pytest.approx(0.040, abs=0.002)
    # Past the end it extrapolates the last stretch, which is right for a
    # little way and meaningless for a long one. Reading it far outside the
    # measured range is the caller's mistake, not something to clamp silently.
    assert found.lag_at(205.0) == pytest.approx(0.040, abs=0.005)
    with pytest.raises(ValueError, match="empty segmentation"):
        Segmentation((), (), 0.0, 0.0).lag_at(0.0)


def test_a_segment_knows_whether_it_needs_resampling() -> None:
    flat = Segment(t0=0.0, t1=100.0, slope=0.0, intercept=0.1, n=50, residual_rms_ms=0.1)
    sliding = Segment(
        t0=0.0, t1=100.0, slope=-5e-4, intercept=0.1, n=50, residual_rms_ms=0.1
    )
    assert not flat.drifts
    assert sliding.drifts
    assert sliding.rate_ratio == pytest.approx(0.9995, abs=1e-9)


# ------------------------------------------------------------ the two curves
def test_the_right_offset_correlates_and_the_wrong_one_does_not() -> None:
    signal = programme(10.0)
    delayed = np.concatenate([np.zeros(int(0.100 * SR)), signal])
    _, right = running_r(signal, delayed, 0.100, 2.0, 4.0, sr=SR)
    _, wrong = running_r(signal, delayed, 0.300, 2.0, 4.0, sr=SR)
    assert np.nanmean(right) > 0.9
    assert np.nanmean(wrong) < 0.3


def test_a_window_that_falls_off_the_end_reads_as_no_measurement() -> None:
    signal = programme(4.0)
    _, values = running_r(signal, signal, 0.0, 3.0, 6.0, sr=SR)
    assert np.isnan(values).any()


def test_a_window_shorter_than_two_samples_is_refused() -> None:
    with pytest.raises(ValueError, match="two samples"):
        running_r(programme(1.0), programme(1.0), 0.0, 0.0, 1.0, sr=SR, window_s=1e-6)


def test_the_crossing_finds_the_instant_the_offset_changes() -> None:
    signal = programme(20.0)
    cut_at = 10.0
    index = int(cut_at * SR)
    # before the cut the other source is 100 ms late; after it, 20 ms late
    other = np.concatenate(
        [
            np.zeros(int(0.100 * SR)), signal[:index],
            signal[index + int(0.080 * SR) :],
        ]
    )
    found = crossing(signal, other, 0.100, 0.020, cut_at - 1.0, cut_at + 1.0, sr=SR)
    assert found is not None
    assert found.confident
    assert found.t == pytest.approx(cut_at, abs=0.060)


def test_the_crossing_declines_where_neither_offset_explains_anything() -> None:
    silence = np.zeros(10 * SR)
    found = crossing(silence, silence, 0.0, 0.1, 1.0, 3.0, sr=SR)
    assert found is None or not found.confident


def test_the_crossing_declines_when_the_bracket_is_shorter_than_a_window() -> None:
    assert crossing(programme(1.0), programme(1.0), 0.0, 0.1, 0.0, 0.15, sr=SR) is None


# ----------------------------------------------------------- the two together
@pytest.fixture(scope="module")
def pair() -> SyntheticPair:
    return synthetic_pair()


def test_the_step_in_the_synthetic_pair_is_found_from_the_measurement(
    pair: SyntheticPair,
) -> None:
    measured = dense_map(
        pair.reference, pair.other, sr=SR, window_s=8.0, step_s=4.0
    ).confident()
    found = find_changepoints(measured)
    assert len(found.changepoints) == 1, found.describe()
    jump = found.changepoints[0]
    assert jump.t == pytest.approx(pair.step_at_s, abs=8.0)
    assert jump.step_ms == pytest.approx(pair.step_size_s * 1000.0, abs=12.0)


def test_the_drift_after_the_step_is_in_the_model_and_the_head_is_flat(
    pair: SyntheticPair,
) -> None:
    measured = dense_map(
        pair.reference, pair.other, sr=SR, window_s=8.0, step_s=4.0
    ).confident()
    found = find_changepoints(measured)
    assert len(found.segments) == 2
    head, tail = found.segments
    assert not head.drifts
    assert tail.drifts
    assert tail.rate_ratio == pytest.approx(1.0 / pair.rate_after, rel=0.2)


def test_refining_moves_the_jump_onto_the_instant_it_happens(
    pair: SyntheticPair,
) -> None:
    """The whole point of the refinement: seconds become tens of milliseconds."""
    measured = dense_map(
        pair.reference, pair.other, sr=SR, window_s=8.0, step_s=4.0
    ).confident()
    rough = find_changepoints(measured)
    exact = refine_changepoints(rough, pair.reference, pair.other, sr=SR, pad_s=2.0)
    assert len(exact.changepoints) == 1
    jump = exact.changepoints[0]
    assert jump.refined
    assert jump.t == pytest.approx(pair.step_at_s, abs=0.15)
    assert jump.uncertainty_s < 0.2
    assert jump.uncertainty_s < rough.changepoints[0].uncertainty_s


def test_a_jump_that_cannot_be_pinned_down_keeps_its_rough_estimate() -> None:
    silence = np.zeros(20 * SR)
    rough = Segmentation(
        segments=(
            Segment(0.0, 8.0, 0.0, 0.1, 5, 0.1),
            Segment(12.0, 20.0, 0.0, 0.02, 5, 0.1),
        ),
        changepoints=(
            Changepoint(t=10.0, bracket=(8.0, 12.0), step_s=-0.08,
                        lag_before=0.1, lag_after=0.02),
        ),
        penalty=1.0, noise_ms=0.5,
    )
    kept = refine_changepoints(rough, silence, silence, sr=SR)
    assert kept.changepoints[0].t == 10.0
    assert not kept.changepoints[0].refined
