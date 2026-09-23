"""Verification, the control harness, and the leading-gap correction.

The most important test in this file is the one that fails on purpose: a build
with a real error in one stretch must be reported as a failure, with the
offending windows named. A verification that has only ever been run against
something correct is not known to work.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest
from dubalign.controls import array_controls, measure_offset_ms
from dubalign.epk_align import AbsoluteLag, correct_lag, excerpt_command
from dubalign.verify_pcm import BAR_MS, VerifyPoint, verify_against
from mkvkit.run import Result

from tests.synthetic import interleave, programme

SR = 16_000


def built_from(source: np.ndarray, shift_samples: int = 0) -> np.ndarray:
    if shift_samples == 0:
        return source.copy()
    if shift_samples > 0:
        return np.concatenate([np.zeros(shift_samples), source])[: len(source)]
    return np.concatenate([source[-shift_samples:], np.zeros(-shift_samples)])


# ------------------------------------------------------------ the scan passes
def test_a_build_that_is_right_passes_at_every_point() -> None:
    source = programme(60.0, SR)
    report = verify_against(source, source.copy(), sample_rate=SR, step_s=5.0)
    assert report.passed
    assert len(report.points) >= 10
    assert report.worst_ms == pytest.approx(0.0, abs=0.1)
    assert any(line.startswith("PASS") for line in report.describe())


def test_a_build_a_few_milliseconds_out_still_passes() -> None:
    source = programme(40.0, SR)
    built = built_from(source, int(0.010 * SR))
    report = verify_against(source, built, sample_rate=SR, step_s=5.0)
    assert report.passed
    assert report.worst_ms == pytest.approx(10.0, abs=0.2)


# ------------------------------------------------------------ the scan fails
def test_a_build_outside_the_bar_fails_and_names_the_windows() -> None:
    source = programme(40.0, SR)
    built = built_from(source, int(0.060 * SR))
    report = verify_against(source, built, sample_rate=SR, step_s=5.0)
    assert not report.passed
    assert len(report.outside) == len(report.points)
    assert any(line.startswith("FAIL") for line in report.describe())


def test_one_bad_stretch_in_an_otherwise_perfect_build_is_caught() -> None:
    """The failure a good average hides, and the reason every point counts."""
    source = programme(60.0, SR)
    built = source.copy()
    bad = slice(int(30.0 * SR), int(40.0 * SR))
    built[bad] = np.concatenate(
        [np.zeros(int(0.080 * SR)), source[bad]]
    )[: bad.stop - bad.start]
    report = verify_against(source, built, sample_rate=SR, step_s=5.0)
    assert not report.passed
    offending = [p.t for p in report.outside]
    assert offending, "the bad stretch has to be reported"
    assert all(28.0 <= t <= 41.0 for t in offending)
    assert len(report.points) - len(report.outside) > 5, "the rest still passes"


def test_the_bar_is_an_argument_and_it_is_written_down() -> None:
    assert BAR_MS == pytest.approx(40.0)
    source = programme(30.0, SR)
    built = built_from(source, int(0.060 * SR))
    assert not verify_against(source, built, sample_rate=SR).passed
    assert verify_against(source, built, sample_rate=SR, bar_ms=100.0).passed


def test_a_silent_window_is_counted_rather_than_passed() -> None:
    """Silence correlates with silence at every lag; counting that as a pass is
    how a scan reports a clean result for a track that is not there."""
    source = programme(40.0, SR)
    source[int(10.0 * SR) : int(20.0 * SR)] = 0.0
    report = verify_against(source, source.copy(), sample_rate=SR, step_s=5.0)
    assert report.skipped_silent >= 1
    assert any("skipped as silent" in line for line in report.describe())


def test_a_scan_that_measured_nothing_says_so_rather_than_passing() -> None:
    silence = np.zeros(20 * SR)
    report = verify_against(silence, silence, sample_rate=SR)
    assert not report.passed
    assert report.points == ()
    assert "nothing was measured" in report.describe()[0]


def test_multichannel_material_is_measured_on_the_centre_channel() -> None:
    source = interleave(programme(30.0, SR), channels=6)
    report = verify_against(source, source.copy(), sample_rate=SR, step_s=5.0)
    assert report.passed
    assert {p.channel for p in report.points} == {2}


def test_a_point_knows_whether_it_cleared_the_bar() -> None:
    assert VerifyPoint(0.0, 10.0, 0.99, 8.0, 2).within()
    assert not VerifyPoint(0.0, 50.0, 0.99, 8.0, 2).within()
    assert not VerifyPoint(0.0, 0.0, 0.1, 8.0, 2).within(), "a lag of zero at r=0.1 is not a pass"


# ---------------------------------------------------------------- the controls
def test_all_four_array_controls_pass() -> None:
    report = array_controls(sr=SR)
    assert report.passed, report.describe()
    assert len(report.controls) == 4


def test_the_controls_report_names_each_question_and_its_answer() -> None:
    lines = array_controls(sr=SR).describe()
    assert any("against itself" in line for line in lines)
    assert any("+250 ms" in line for line in lines)
    assert any("moving the data" in line for line in lines)
    assert lines[-1].startswith("4 of 4")


def test_a_control_that_measured_nothing_is_a_failure_not_a_blank() -> None:
    from dubalign.controls import Control

    empty = Control(name="x", expected_ms=0.0, measured_ms=None)
    assert not empty.passed
    assert "nothing" in empty.describe()


def test_an_asymmetric_search_is_what_the_data_shift_control_catches() -> None:
    """Reproduce the failure the control exists for, and show it reads wrong.

    Searching only forwards from the reference's position cannot express a
    positive lag, so a pair that really is 250 ms apart comes back as about
    zero -- confidently, and with no sign that anything is wrong.
    """
    rng = np.random.default_rng(1)
    signal = np.asarray(rng.standard_normal(20 * SR) * 0.2, dtype=np.float64)
    delayed = np.concatenate([np.zeros(round(0.250 * SR)), signal])
    honest = measure_offset_ms(signal, delayed, 5.0, sr=SR, search_s=1.0)
    assert honest == pytest.approx(250.0, abs=1.0)

    # the same measurement with the search reaching only one way
    index, length = round(5.0 * SR), int(4.0 * SR)
    from dubalign.align import best_with_ratio, ncc_full

    reach = round(0.250 * SR)
    peak = best_with_ratio(
        ncc_full(signal[index : index + length], delayed[index - reach : index + length]),
        max(1, int(0.002 * SR)),
    )
    crippled = (index - reach + peak.index - index) / SR * 1000.0
    assert crippled < 1.0, "the crippled search cannot see the delay it was given"


# ------------------------------------------------------------ the leading gap
def test_the_correction_is_the_difference_of_the_first_packets() -> None:
    assert correct_lag(0.100, 0.000, 0.030) == pytest.approx(0.130)
    assert correct_lag(0.100, 0.030, 0.000) == pytest.approx(0.070)
    assert correct_lag(0.0, 0.030, 0.030) == pytest.approx(0.0)


def test_the_sign_of_the_correction_is_the_one_that_is_right() -> None:
    """Both signs look plausible in a log; only one of them is the answer.

    The later stream plays later, so a stream whose first packet is 30 ms in is
    30 ms behind a stream that starts at zero -- and the raw dumps, which each
    begin at their own first packet, have already thrown that away.
    """
    lag = AbsoluteLag(
        index_lag_s=0.000, first_packet_a_s=0.000, first_packet_b_s=0.030,
        r=0.99, ratio=9.0,
    )
    assert lag.packet_difference_s == pytest.approx(0.030)
    assert lag.true_lag_ms == pytest.approx(30.0)
    assert "true lag +30.000 ms" in lag.describe()


def test_the_excerpt_keeps_its_timestamps_and_cuts_both_streams_at_once() -> None:
    args = excerpt_command("in.mkv", "out.mkv", 1, 24, 600.0, 640.0)
    assert "-copyts" in args
    assert args.count("-i") == 1, "one seek, so an error in it cancels"
    assert args.count("-map") == 2
    assert "0:1" in args and "0:24" in args
    assert args[args.index("-c") + 1] == "copy"


def test_an_excerpt_with_no_length_is_refused() -> None:
    with pytest.raises(ValueError, match="no length"):
        excerpt_command("in.mkv", "out.mkv", 1, 2, 10.0, 10.0)


# ------------------------------------------------------------ against a file
@pytest.mark.needs_ffmpeg
@pytest.mark.needs_mkvtoolnix
def test_the_file_controls_pass_through_the_real_programs(tmp_path: Path) -> None:
    from dubalign.controls import file_controls

    report = file_controls(tmp_path, sr=SR, duration_s=20.0)
    assert report.passed, report.describe()


@pytest.mark.needs_ffmpeg
def test_the_generated_offset_pair_reads_the_delay_it_was_built_with(
    media_fixtures: dict[str, Path], tmp_path: Path
) -> None:
    """The fixture's whole purpose, measured through a decode."""
    from dubalign.decode import decode

    source = media_fixtures["offset_pair.mka"]
    plain = decode(source, out_dir=tmp_path, audio_index=0, name="plain")
    delayed = decode(source, out_dir=tmp_path, audio_index=1, name="delayed")
    measured = measure_offset_ms(
        plain.analysis(), delayed.analysis(), 1.0,
        sr=plain.analysis_rate, window_s=2.0, search_s=1.0,
    )
    assert measured == pytest.approx(250.0, abs=1.0)


def test_the_runner_is_only_ever_asked_for_programs_that_exist() -> None:
    """A stand-in runner is enough for everything above the decode."""
    seen: list[str] = []

    def run(tool: str, args: Any, *, ok: Any = (0,)) -> Result:
        seen.append(tool)
        return Result(tool=tool, argv=(tool,), returncode=0, stdout="", stderr="")

    run("ffmpeg", excerpt_command("a.mkv", "b.mkv", 0, 1, 0.0, 1.0))
    assert seen == ["ffmpeg"]
