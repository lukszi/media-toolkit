"""The whole pipeline, on a file, against answers chosen before it ran.

This is the test the package exists to pass. A container holds two transfers of
one programme; the second has a leading gap, a stretch missing from the middle
and a rate difference after it, and none of that is told to the code. It
decodes both, maps the offset across the whole runtime, finds the jump, pins it
down, plans the joins, builds the track and measures the result against the
reference at a fixed spacing edge to edge.

Two assertions carry the weight, and the second is the one that makes the first
mean anything:

* the rebuilt track is inside the forty-millisecond bar at **every** measured
  window;
* the track it was built from is **not** -- so the scan is known to fail when
  it should, and a pass is a measurement rather than a formality.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

from pathlib import Path

import pytest
from dubalign.changepoints import find_changepoints, refine_changepoints
from dubalign.decode import Decoded, decode
from dubalign.densemap import dense_map
from dubalign.plan import SplicePlan, plan_from_measurements
from dubalign.splice import splice
from dubalign.verify_pcm import verify_against

from tests.synthetic import HEAD_LAG_S, RATE_AFTER, STEP_AT_S, STEP_DROP_S

pytestmark = pytest.mark.needs_ffmpeg


@pytest.fixture(scope="module")
def decoded(
    media_fixtures: dict[str, Path], tmp_path_factory: pytest.TempPathFactory
) -> tuple[Decoded, Decoded]:
    if "drift_pair.mka" not in media_fixtures:
        pytest.skip("the drifting pair was not built")
    work = tmp_path_factory.mktemp("drift")
    source = media_fixtures["drift_pair.mka"]
    return (
        decode(source, out_dir=work, audio_index=0, name="reference"),
        decode(source, out_dir=work, audio_index=1, name="transfer"),
    )


@pytest.fixture(scope="module")
def measured(decoded: tuple[Decoded, Decoded]) -> SplicePlan:
    """Everything from the decode to the plan, done once for the file."""
    reference, other = decoded
    series = dense_map(
        reference.analysis(), other.analysis(), sr=reference.analysis_rate,
        window_s=8.0, step_s=4.0,
    ).confident()
    rough = find_changepoints(series)
    exact = refine_changepoints(
        rough, reference.analysis(), other.analysis(), sr=reference.analysis_rate,
        pad_s=2.0,
    )
    return plan_from_measurements(
        exact,
        sample_rate=reference.sample_rate,
        total_frames=reference.frames,
        other=other.analysis(),
        analysis_rate=reference.analysis_rate,
        source="transfer",
        reference="reference",
        channels=reference.channels,
    )


def test_the_two_transfers_are_the_lengths_they_were_built_to_be(
    decoded: tuple[Decoded, Decoded],
) -> None:
    reference, other = decoded
    assert reference.duration_s == pytest.approx(120.0, abs=0.05)
    # the head gap, less the stretch that is missing, less the rate difference
    expected = HEAD_LAG_S + STEP_AT_S + (120.0 - STEP_AT_S - STEP_DROP_S) / RATE_AFTER
    assert other.duration_s == pytest.approx(expected, abs=0.05)


def test_the_map_sees_the_offset_move_across_the_programme(
    decoded: tuple[Decoded, Decoded],
) -> None:
    reference, other = decoded
    series = dense_map(
        reference.analysis(), other.analysis(), sr=reference.analysis_rate,
        window_s=8.0, step_s=4.0,
    ).confident()
    assert len(series) >= 15
    assert series.spread_ms() > 40.0, "a single number cannot describe this pair"


def test_the_jump_is_found_where_it_was_put(measured: SplicePlan) -> None:
    assert len(measured.seams) == 1
    seam = measured.seams[0]
    assert seam.jump_t == pytest.approx(STEP_AT_S, abs=0.3)
    assert seam.step_s == pytest.approx(-STEP_DROP_S, abs=0.005)


def test_the_rate_difference_is_in_the_model(measured: SplicePlan) -> None:
    head, tail = measured.segments
    assert not head.drifts
    assert tail.rate_ratio == pytest.approx(1.0 / RATE_AFTER, rel=0.05)
    assert head.offset_samples == pytest.approx(HEAD_LAG_S * 48_000, abs=200)


def test_the_plan_checks_out_and_can_be_written_down(measured: SplicePlan) -> None:
    assert measured.problems() == []
    assert SplicePlan.from_toml(measured.to_toml()) == measured


def test_the_rebuilt_track_is_inside_the_bar_at_every_point(
    decoded: tuple[Decoded, Decoded], measured: SplicePlan
) -> None:
    reference, other = decoded
    built = splice(measured, {"transfer": other.full()})
    report = verify_against(
        reference.full(), built, sample_rate=reference.sample_rate, step_s=5.0
    )
    assert report.passed, "\n".join(report.describe())
    assert len(report.points) >= 20
    assert report.worst_ms < 10.0


def test_the_transfer_it_was_built_from_fails_the_same_scan(
    decoded: tuple[Decoded, Decoded],
) -> None:
    """The control that makes the test above mean something."""
    reference, other = decoded
    report = verify_against(
        reference.full(), other.full(), sample_rate=reference.sample_rate, step_s=5.0
    )
    assert not report.passed
    assert len(report.outside) >= 10
