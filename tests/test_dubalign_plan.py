"""The plan: where the joins go, and the document they are written into.

The placement tests are the interesting ones. They build a signal with a known
quiet patch and a known loud one and check that the join lands where a listener
would want it -- and, just as importantly, that it does not wander off to a
quieter place a long way from the jump it is meant to render.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import numpy as np
import pytest
from dubalign.align import ANALYSIS_RATE, FULL_RATE
from dubalign.changepoints import Changepoint, Segmentation
from dubalign.changepoints import Segment as ModelSegment
from dubalign.plan import (
    BIG_STEP_S,
    PlanError,
    Seam,
    Segment,
    SplicePlan,
    place_seam,
    plan_from_measurements,
    span_level_db,
)

from tests.synthetic import programme

SR = ANALYSIS_RATE


def with_quiet_patch(
    loud_seconds: float = 30.0, patch_at: float = 15.0, patch_s: float = 0.4
) -> np.ndarray:
    """Continuous material with one genuinely quiet stretch in it."""
    signal = programme(loud_seconds)
    start, end = int(patch_at * SR), int((patch_at + patch_s) * SR)
    signal[start:end] *= 0.0005
    return signal


def simple_plan(**overrides: object) -> SplicePlan:
    base = {
        "sample_rate": FULL_RATE,
        "channels": 6,
        "total_frames": FULL_RATE * 100,
        "seams": (Seam(t=40.0, half_width_s=0.010, reason="on the jump", step_s=-0.04),),
        "segments": (
            Segment("head", "other", offset_samples=5760),
            Segment("tail", "other", offset_samples=3840, anchor_frame=FULL_RATE * 40),
        ),
        "reference": "reference",
    }
    base.update(overrides)
    return SplicePlan(**base)  # type: ignore[arg-type]


# ------------------------------------------------------------- the placement
def test_a_join_lands_in_the_quiet_patch_when_it_is_near_the_jump() -> None:
    signal = with_quiet_patch()
    seam = place_seam(signal, 15.2, -0.080, sr=SR)
    assert 15.0 <= seam.t <= 15.4
    assert seam.margin_db > 20.0


def test_a_big_step_is_not_allowed_to_wander_off_to_find_a_pause() -> None:
    """The failure that leaves a whole passage out of sync to get a tidy join."""
    signal = with_quiet_patch(patch_at=15.0)
    seam = place_seam(signal, 18.0, -0.080, sr=SR)
    assert seam.distance_from_jump_s <= 1.0
    assert seam.t > 16.5


def test_a_small_step_is_allowed_to_look_further() -> None:
    signal = with_quiet_patch(patch_at=15.0)
    seam = place_seam(signal, 17.5, -0.010, sr=SR)
    assert 15.0 <= seam.t <= 15.4
    assert seam.distance_from_jump_s > 1.0


def test_the_span_scored_is_as_long_as_the_step_and_not_an_instant() -> None:
    """A join inside speech must not be scored on the gap between two words.

    The signal has a 30 ms hole inside loud material and a 300 ms quiet patch
    beside it. A scorer that looks at an instant takes the hole; one that
    scores the span a 250 ms step actually repeats takes the patch.
    """
    signal = programme(30.0)
    hole = slice(int(15.0 * SR), int(15.03 * SR))
    patch = slice(int(15.5 * SR), int(15.8 * SR))
    signal[hole] *= 0.0
    signal[patch] *= 0.001
    seam = place_seam(signal, 15.2, -0.250, sr=SR, bracket_s=1.0)
    assert 15.5 <= seam.t <= 15.8, "the join must avoid the span it would repeat"


def test_the_seam_records_how_far_it_moved_and_why() -> None:
    signal = with_quiet_patch()
    on_the_jump = place_seam(signal, 15.2, -0.080, sr=SR, bracket_s=0.0)
    assert on_the_jump.reason == "on the jump"
    assert on_the_jump.distance_from_jump_s == 0.0
    moved = place_seam(signal, 15.4, -0.080, sr=SR)
    assert "quietest span" in moved.reason
    assert moved.jump_t == 15.4


def test_a_join_at_the_very_head_still_gets_a_position() -> None:
    seam = place_seam(programme(5.0), 0.0, -0.010, sr=SR, bracket_s=0.001)
    assert seam.t >= 0.0


def test_a_join_near_the_end_never_lands_past_the_signal() -> None:
    """Past the end nothing is heard, which used to score as the quietest span."""
    signal = programme(5.0)
    seam = place_seam(signal, 4.9, -0.080, sr=SR, bracket_s=1.0)
    span = max(0.080, 0.030)
    assert seam.t + span / 2.0 <= len(signal) / SR


def test_a_jump_past_the_end_is_clamped_to_the_signal() -> None:
    signal = programme(5.0)
    seam = place_seam(signal, 7.0, -0.080, sr=SR, bracket_s=0.5)
    assert 0.0 <= seam.t <= len(signal) / SR


def test_in_silence_the_join_stays_nearest_the_jump() -> None:
    """Equally quiet everywhere: the join moves no further than it has to."""
    signal = programme(30.0)
    signal[int(10.0 * SR) : int(13.0 * SR)] = 0.0
    seam = place_seam(signal, 11.5, -0.010, sr=SR)
    assert seam.t == pytest.approx(11.5, abs=1e-6)
    assert seam.reason == "on the jump"


def test_the_level_of_an_empty_span_is_not_a_number_to_act_on() -> None:
    assert span_level_db(programme(1.0), 5.0, 6.0, SR) == float("-inf")


def test_the_boundary_between_a_big_step_and_a_small_one_is_written_down() -> None:
    assert BIG_STEP_S == pytest.approx(0.040)


# ------------------------------------------------------------- the self-check
def test_a_sound_plan_has_no_problems() -> None:
    assert simple_plan().problems() == []
    simple_plan().check()


def test_seams_and_segments_have_to_agree_in_number() -> None:
    broken = simple_plan(segments=(Segment("only", "other", 0),))
    assert any("segment(s) need" in p for p in broken.problems())
    with pytest.raises(PlanError):
        broken.check()


def test_seams_out_of_order_are_caught() -> None:
    broken = simple_plan(
        seams=(Seam(t=40.0), Seam(t=20.0)),
        segments=(
            Segment("a", "other", 0), Segment("b", "other", 0), Segment("c", "other", 0)
        ),
    )
    assert any("not after the one before" in p for p in broken.problems())


def test_a_seam_outside_the_output_is_caught() -> None:
    assert any("outside the output" in p for p in simple_plan(
        seams=(Seam(t=500.0),)
    ).problems())


def test_overlapping_crossfades_are_caught() -> None:
    broken = simple_plan(
        seams=(Seam(t=40.0, half_width_s=0.5), Seam(t=40.2, half_width_s=0.5)),
        segments=(
            Segment("a", "other", 0), Segment("b", "other", 0), Segment("c", "other", 0)
        ),
    )
    assert any("reaches into the seam before it" in p for p in broken.problems())


def test_a_rate_that_is_not_a_drift_correction_is_caught() -> None:
    broken = simple_plan(
        segments=(
            Segment("a", "other", 0, rate_ratio=1.0005),
            Segment("b", "other", 0, rate_ratio=4.0),
        )
    )
    assert any("different programme" in p for p in broken.problems())


def test_an_output_with_no_length_is_caught() -> None:
    assert any("no length" in p for p in simple_plan(total_frames=0).problems())


# --------------------------------------------------------------- the document
def test_a_plan_survives_being_written_out_and_read_back() -> None:
    original = simple_plan()
    again = SplicePlan.from_toml(original.to_toml())
    assert again == original


def test_a_plan_with_a_drift_and_a_note_round_trips() -> None:
    original = simple_plan(
        note="the head is the reference's own track",
        segments=(
            Segment("head", "reference", 0, gain_db=-4.6),
            Segment("body", "other", -578400, rate_ratio=1.0005, anchor_frame=1920000),
        ),
    )
    assert SplicePlan.from_toml(original.to_toml()) == original


def test_a_hand_edited_plan_reads_with_the_defaults_filled_in() -> None:
    text = """
        sample_rate = 48000
        channels = 2
        total_frames = 480000

        [[segment]]
        source = "other"
        offset_samples = 120
    """
    plan = SplicePlan.from_toml(text)
    assert plan.segments[0].rate_ratio == 1.0
    assert plan.segments[0].gain_db == 0.0
    assert plan.seams == ()


def test_a_plan_that_is_not_a_document_is_refused_with_a_reason() -> None:
    with pytest.raises(PlanError, match="not readable"):
        SplicePlan.from_toml("this is not = = a document")
    with pytest.raises(PlanError, match="missing"):
        SplicePlan.from_toml("channels = 2\n")


def test_the_bounds_split_the_output_at_the_seams() -> None:
    plan = simple_plan()
    bounds = plan.bounds()
    assert len(bounds) == len(plan.segments)
    assert bounds[0][0] == 0
    assert bounds[-1][1] == plan.total_frames
    assert bounds[0][1] == bounds[1][0] == FULL_RATE * 40


# ---------------------------------------------------- from a real measurement
def model(step_at: float = 20.0) -> Segmentation:
    head = ModelSegment(t0=0.0, t1=step_at, slope=0.0, intercept=0.120, n=10,
                        residual_rms_ms=0.1)
    tail = ModelSegment(t0=step_at, t1=120.0, slope=-5e-4, intercept=0.090, n=25,
                        residual_rms_ms=0.2)
    jump = Changepoint(
        t=step_at, bracket=(step_at - 2.0, step_at + 2.0), step_s=-0.040,
        lag_before=head.lag_at(step_at), lag_after=tail.lag_at(step_at),
        slope_before=head.slope, slope_after=tail.slope, refined=True,
    )
    return Segmentation(segments=(head, tail), changepoints=(jump,), penalty=1e-7,
                        noise_ms=0.3)


def test_a_measurement_becomes_a_plan_that_checks_out() -> None:
    plan = plan_from_measurements(
        model(), total_frames=FULL_RATE * 120, quiet_search=False
    )
    assert plan.problems() == []
    assert len(plan.segments) == 2
    assert len(plan.seams) == 1
    assert plan.seams[0].t == pytest.approx(20.0)


def test_the_drifting_stretch_carries_its_rate_and_the_flat_one_does_not() -> None:
    plan = plan_from_measurements(
        model(), total_frames=FULL_RATE * 120, quiet_search=False
    )
    head, tail = plan.segments
    assert not head.drifts
    assert tail.drifts
    assert tail.rate_ratio == pytest.approx(1.0 - 5e-4)
    assert tail.anchor_frame == FULL_RATE * 20


def test_each_segment_carries_the_offset_at_its_own_start() -> None:
    plan = plan_from_measurements(
        model(), total_frames=FULL_RATE * 120, quiet_search=False
    )
    head, tail = plan.segments
    assert head.offset_samples == round(0.120 * FULL_RATE)
    assert tail.offset_samples == round((0.090 - 5e-4 * 20.0) * FULL_RATE)


def test_the_quiet_search_moves_the_join_when_it_is_given_the_material() -> None:
    signal = with_quiet_patch(loud_seconds=40.0, patch_at=20.3, patch_s=0.3)
    plan = plan_from_measurements(
        model(), total_frames=FULL_RATE * 120, other=signal, quiet_search=True
    )
    assert 20.3 <= plan.seams[0].t <= 20.6
    assert plan.seams[0].margin_db > 15.0


def test_a_measurement_with_no_segments_cannot_become_a_plan() -> None:
    with pytest.raises(PlanError, match="nothing to plan"):
        plan_from_measurements(
            Segmentation((), (), 0.0, 0.0), total_frames=100, quiet_search=False
        )
