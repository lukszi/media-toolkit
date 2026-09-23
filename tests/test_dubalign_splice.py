"""Building the output, checking the channel order, and matching the level.

The splice tests are the ones that matter: a plan whose answer is known gets
built, and what comes out has to be what the plan said. The crossfade gets a
test of its own because equal power is a property that can be checked as
arithmetic rather than by listening.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest
from dubalign.channels import channel_matrix, compare_channels, rms_dbfs
from dubalign.loudness import (
    Loudness,
    integrated_loudness,
    match_gain_db,
    parse_loudness,
    rms_gain_db,
)
from dubalign.plan import PlanError, Seam, Segment, SplicePlan
from dubalign.splice import SpliceError, as_block, equal_power, splice
from mkvkit.run import Result

from tests.synthetic import interleave, programme

SR = 16_000
METER_OUTPUT = """
[Parsed_ebur128_0 @ 0000] Summary:

  Integrated loudness:
    I:         -23.00 LUFS
    Threshold: -33.00 LUFS

  Loudness range:
    LRA:         7.00 LU
    Threshold: -43.00 LUFS

  True peak:
    Peak:       -6.00 dBFS
"""


def plan_for(
    total_frames: int, seam_t: float, left_offset: int, right_offset: int,
    *, channels: int = 1, half_width_s: float = 0.010, right_gain_db: float = 0.0,
    right_ratio: float = 1.0,
) -> SplicePlan:
    return SplicePlan(
        sample_rate=SR,
        channels=channels,
        total_frames=total_frames,
        seams=(Seam(t=seam_t, half_width_s=half_width_s, step_s=0.0),),
        segments=(
            Segment("head", "other", offset_samples=left_offset),
            Segment(
                "tail", "other", offset_samples=right_offset,
                anchor_frame=round(seam_t * SR), gain_db=right_gain_db,
                rate_ratio=right_ratio,
            ),
        ),
    )


# ------------------------------------------------------------- the crossfade
def test_an_equal_power_crossfade_holds_its_power() -> None:
    out, into = equal_power(512)
    assert np.allclose(out**2 + into**2, 1.0, atol=1e-12)
    assert out[0] == pytest.approx(1.0)
    assert into[0] == pytest.approx(0.0)


def test_a_linear_fade_would_dip_and_this_one_does_not() -> None:
    """Three decibels in the middle, which is what a listener hears as the join."""
    n = 512
    out, into = equal_power(n)
    linear = np.linspace(0.0, 1.0, n, endpoint=False)
    middle = n // 2
    assert (linear[middle] ** 2 + (1 - linear[middle]) ** 2) == pytest.approx(0.5, abs=0.01)
    assert (out[middle] ** 2 + into[middle] ** 2) == pytest.approx(1.0, abs=1e-9)


def test_a_crossfade_of_nothing_is_refused() -> None:
    with pytest.raises(SpliceError, match="at least one sample"):
        equal_power(0)


# ----------------------------------------------------------------- the build
def test_a_plan_with_one_segment_reproduces_the_source_shifted() -> None:
    source = programme(10.0, SR)
    plan = SplicePlan(
        sample_rate=SR, channels=1, total_frames=len(source) - 2000,
        seams=(), segments=(Segment("all", "other", offset_samples=1000),),
    )
    built = splice(plan, {"other": source})[:, 0]
    assert np.array_equal(built, source[1000 : 1000 + plan.total_frames])


def test_a_crossfade_between_two_copies_of_one_signal_rises_by_three_decibels() -> None:
    """Why a seam between two segments of the same source at the same offset is
    not a free edit: equal power assumes the two sides are different signals,
    and where they are identical the sum is louder than either."""
    source = programme(10.0, SR)
    built = splice(plan_for(len(source) - 2000, 5.0, 1000, 1000), {"other": source})[:, 0]
    plain = source[1000 : 1000 + len(built)]
    centre = int(5.0 * SR)
    half = int(0.010 * SR)
    outside = slice(centre + 2 * half, centre + 20 * half)
    assert np.allclose(built[outside], plain[outside], atol=1e-9)
    inside = slice(centre - half, centre + half)
    louder = rms_dbfs(built[inside]) - rms_dbfs(plain[inside])
    assert 1.0 < louder < 3.1


def test_the_two_sides_of_a_seam_each_come_from_their_own_offset() -> None:
    source = programme(10.0, SR)
    plan = plan_for(int(9.0 * SR), 5.0, 0, 800, half_width_s=0.001)
    built = splice(plan, {"other": source})[:, 0]
    before = slice(int(2.0 * SR), int(4.5 * SR))
    after = slice(int(5.5 * SR), int(8.0 * SR))
    assert np.allclose(built[before], source[before], atol=1e-9)
    shifted = source[800:]
    assert np.allclose(built[after], shifted[after], atol=1e-9)


def test_the_join_is_a_crossfade_and_not_a_cut() -> None:
    source = programme(10.0, SR)
    plan = plan_for(int(9.0 * SR), 5.0, 0, 8000)
    built = splice(plan, {"other": source})[:, 0]
    centre = int(5.0 * SR)
    half = int(0.010 * SR)
    inside = built[centre - half : centre + half]
    from_left = source[centre - half : centre + half]
    from_right = source[8000:][centre - half : centre + half]
    assert not np.allclose(inside, from_left, atol=1e-6)
    assert not np.allclose(inside, from_right, atol=1e-6)


def test_a_gain_is_applied_to_the_segment_that_asked_for_it() -> None:
    source = programme(10.0, SR)
    plan = plan_for(int(9.0 * SR), 5.0, 0, 0, right_gain_db=-6.0)
    built = splice(plan, {"other": source})[:, 0]
    quiet = slice(int(6.0 * SR), int(8.0 * SR))
    assert rms_dbfs(built[quiet]) == pytest.approx(rms_dbfs(source[quiet]) - 6.0, abs=0.01)


def test_a_segment_with_a_rate_is_resampled_and_one_without_is_copied() -> None:
    """A plain copy must stay a plain copy: no kernel on material that is right."""
    source = programme(10.0, SR)
    copied = splice(plan_for(int(9.0 * SR), 5.0, 0, 0), {"other": source})[:, 0]
    assert np.array_equal(copied[: int(4.0 * SR)], source[: int(4.0 * SR)])
    warped = splice(
        plan_for(int(9.0 * SR), 5.0, 0, 0, right_ratio=1.0005), {"other": source}
    )[:, 0]
    assert not np.array_equal(warped[int(6.0 * SR) :], copied[int(6.0 * SR) :])


def test_a_segment_that_reaches_past_its_source_produces_silence_not_a_short_file() -> None:
    source = programme(4.0, SR)
    plan = plan_for(int(6.0 * SR), 2.0, 0, 0)
    built = splice(plan, {"other": source})
    assert len(built) == plan.total_frames
    assert np.abs(built[int(5.0 * SR) :]).max() == 0.0


def test_multichannel_material_keeps_its_channels() -> None:
    source = interleave(programme(6.0, SR), channels=6)
    plan = plan_for(int(5.0 * SR), 2.5, 0, 480, channels=6)
    built = splice(plan, {"other": source})
    assert built.shape == (plan.total_frames, 6)
    early = slice(int(0.5 * SR), int(2.0 * SR))
    assert np.allclose(built[early, 2], source[early, 2], atol=1e-6)


def test_a_plan_whose_sources_are_missing_names_them() -> None:
    plan = plan_for(1000, 0.02, 0, 0)
    with pytest.raises(SpliceError, match="other"):
        splice(plan, {"reference": programme(1.0, SR)})


def test_a_source_with_the_wrong_channel_count_is_refused() -> None:
    with pytest.raises(SpliceError, match="cannot build"):
        as_block(interleave(programme(1.0, SR), channels=2), 6)


def test_a_broken_plan_is_refused_before_anything_is_built() -> None:
    broken = SplicePlan(
        sample_rate=SR, channels=1, total_frames=1000,
        seams=(Seam(t=0.01), Seam(t=0.02)),
        segments=(Segment("only", "other", 0),),
    )
    with pytest.raises(PlanError, match="segment"):
        splice(broken, {"other": programme(1.0, SR)})


# --------------------------------------------------------------- the channels
def test_a_block_against_itself_has_a_clean_diagonal() -> None:
    block = interleave(programme(4.0, SR), channels=6)
    check = compare_channels(block, block)
    assert check.agreed
    assert all(value == pytest.approx(1.0, abs=1e-9) for value in check.diagonal)


def test_two_channels_swapped_are_caught() -> None:
    """The failure this exists for: the alignment is perfect and the centre
    channel is in the surrounds."""
    block = interleave(programme(4.0, SR), channels=6)
    swapped = block.copy()
    swapped[:, [2, 4]] = swapped[:, [4, 2]]
    check = compare_channels(block, swapped)
    assert not check.agreed
    assert any("not carry their channels in the same order" in p or "not a match" in p
               for p in check.problems)


def test_the_matrix_is_correlations_and_can_be_printed() -> None:
    block = interleave(programme(2.0, SR), channels=6)
    matrix = channel_matrix(block, block)
    assert matrix.shape == (6, 6)
    assert np.abs(matrix).max() == pytest.approx(1.0, abs=1e-9)
    assert any("FC" in line for line in compare_channels(block, block).describe())


def test_blocks_with_no_overlap_are_refused() -> None:
    block = interleave(programme(1.0, SR), channels=2)
    with pytest.raises(ValueError, match="enough overlap"):
        channel_matrix(block, block[:1])
    with pytest.raises(ValueError, match="frames, channels"):
        channel_matrix(programme(1.0, SR), block)


def test_the_level_of_nothing_is_not_a_number() -> None:
    assert rms_dbfs(np.zeros(0)) == float("-inf")
    assert rms_dbfs(np.zeros(100)) == float("-inf")


# --------------------------------------------------------------- the loudness
def test_the_meter_summary_is_read_as_it_is_printed() -> None:
    read = parse_loudness(METER_OUTPUT)
    assert read == Loudness(integrated_lufs=-23.00, range_lu=7.00, true_peak_dbfs=-6.00)


def test_output_with_no_summary_in_it_reads_as_no_measurement() -> None:
    assert parse_loudness("frame= 1 fps=0.0 q=-0.0 size=N/A time=00:00:01.00") is None


def test_a_peak_the_meter_could_not_express_is_not_a_number() -> None:
    text = METER_OUTPUT.replace("-6.00 dBFS", "-inf dBFS")
    read = parse_loudness(text)
    assert read is not None and read.true_peak_dbfs is None


def test_the_measurement_goes_through_the_meter_when_it_is_there() -> None:
    seen: list[list[str]] = []

    def run(tool: str, args: Any, *, ok: Any = (0,)) -> Result:
        seen.append([str(a) for a in args])
        return Result(tool=tool, argv=(tool,), returncode=0, stdout="", stderr=METER_OUTPUT)

    block = interleave(programme(1.0, SR), channels=6)
    read = integrated_loudness(block, sample_rate=SR, layout="5.1", runner=run)
    assert read is not None and read.integrated_lufs == -23.00
    assert "ebur128=peak=true" in seen[0]
    assert seen[0][seen[0].index("-ac") + 1] == "6"


def test_a_meter_that_cannot_run_falls_back_to_levels() -> None:
    def run(tool: str, args: Any, *, ok: Any = (0,)) -> Result:
        raise RuntimeError("not installed")

    reference = programme(2.0, SR)
    other = reference * 0.5
    gain = match_gain_db(reference, other, sample_rate=SR, runner=run)
    assert gain == pytest.approx(6.02, abs=0.05)


def test_two_stretches_already_at_the_same_level_get_no_gain() -> None:
    def run(tool: str, args: Any, *, ok: Any = (0,)) -> Result:
        raise RuntimeError("not installed")

    signal = programme(2.0, SR)
    assert match_gain_db(signal, signal * 1.05, sample_rate=SR, runner=run) == 0.0


def test_a_level_difference_is_measured_in_the_direction_it_is_applied() -> None:
    signal = programme(2.0, SR)
    assert rms_gain_db(signal, signal * 0.5) == pytest.approx(6.02, abs=0.05)
    assert rms_gain_db(signal * 0.5, signal) == pytest.approx(-6.02, abs=0.05)
    assert rms_gain_db(np.zeros(10), signal) == 0.0


@pytest.mark.needs_ffmpeg
def test_the_meter_output_the_parser_expects_is_the_one_that_comes_out(
    tmp_path: Path,
) -> None:
    """The summary above has to stay in the shape the meter really prints."""
    block = interleave(programme(4.0, 48_000), channels=2)
    read = integrated_loudness(block, sample_rate=48_000, layout="stereo")
    assert read is not None
    assert -70.0 < read.integrated_lufs < 0.0
    assert read.range_lu is not None
