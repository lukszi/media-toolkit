"""Rate conversion, the warp, and the pitch measurement.

Every answer here is one arithmetic could have been written down first, and
every one of them runs with nothing installed. The two tests that care whether
the optional numerical extra is present run either way and say which path they
took, because a fallback nobody exercises is a fallback that does not work.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import math

import numpy as np
import pytest
from dubalign.pal import (
    PAL_DOWN,
    PAL_RATIO,
    PAL_SEMITONES,
    PAL_UP,
    _resample_fft,
    log_spectrum,
    pitch_shift,
    resample,
    scipy_available,
    speed_filter,
    warp,
)

SEED = 20260923
SR = 16_000


def tone(freq: float, seconds: float, sr: int = SR) -> np.ndarray:
    t = np.arange(int(seconds * sr)) / sr
    return np.asarray(np.sin(2 * np.pi * freq * t), dtype=np.float64)


def chord(base: float, seconds: float, sr: int = SR) -> np.ndarray:
    """A few partials, so a spectrum has structure to shift."""
    out = np.zeros(int(seconds * sr))
    for harmonic, level in ((1, 1.0), (2, 0.5), (3, 0.3), (5, 0.2), (7, 0.1)):
        out += level * tone(base * harmonic, seconds, sr)
    return np.asarray(out / np.abs(out).max(), dtype=np.float64)


# ------------------------------------------------------------------ the ratio
def test_the_ratio_is_the_frame_rates_and_not_their_decimals() -> None:
    assert PAL_UP / PAL_DOWN == 25025 / 24000
    assert math.gcd(PAL_UP, PAL_DOWN) == 1
    assert PAL_RATIO == pytest.approx(1.0427083333, abs=1e-10)


def test_a_rounded_ratio_eats_the_tolerance_over_a_feature() -> None:
    """Why the fraction is written as a fraction, with what it costs.

    Neither decimal is obviously wrong to look at. Over two hours the first is
    a fifth of the whole tolerance and the second is past it on its own -- and
    this is only one of several errors that have to fit inside the same bar.
    """
    two_hours = 7200.0
    assert abs(25.0 / 23.976 - PAL_RATIO) * two_hours * 1000 == pytest.approx(7.5, abs=0.2)
    assert abs(1.0427 - PAL_RATIO) * two_hours * 1000 == pytest.approx(60.0, abs=1.0)


def test_the_pitch_moves_with_the_speed_by_three_quarters_of_a_semitone() -> None:
    assert PAL_SEMITONES == pytest.approx(0.7239, abs=1e-3)


# ----------------------------------------------------------------- the filter
def test_the_filter_chain_has_no_rounded_rate_in_it() -> None:
    assert speed_filter(48_000) == "aresample=50050,asetrate=48000,aresample=48000"
    assert speed_filter(44_100, up=2, down=1) == "aresample=88200,asetrate=44100,aresample=44100"


def test_a_rate_the_ratio_does_not_divide_is_refused_not_rounded() -> None:
    with pytest.raises(ValueError, match="without rounding"):
        speed_filter(44_100)


def test_the_chain_never_reaches_for_a_tempo_filter() -> None:
    """The one filter that would change the speed and leave the pitch wrong."""
    assert "atempo" not in speed_filter()


# -------------------------------------------------------------- the resampler
def test_resampling_changes_the_length_by_the_ratio() -> None:
    signal = tone(440.0, 2.0)
    out = resample(signal, PAL_UP, PAL_DOWN)
    assert len(out) == pytest.approx(len(signal) * PAL_RATIO, rel=1e-3)


def test_resampling_moves_the_frequency_by_the_inverse_of_the_ratio() -> None:
    """Slowing a transfer down drops its pitch; that is the whole point."""
    signal = tone(1000.0, 4.0)
    out = resample(signal, PAL_UP, PAL_DOWN)
    spectrum = np.abs(np.fft.rfft(out * np.hanning(len(out))))
    peak_hz = float(np.fft.rfftfreq(len(out), 1.0 / SR)[int(np.argmax(spectrum))])
    assert peak_hz == pytest.approx(1000.0 / PAL_RATIO, rel=2e-3)


def test_the_two_resamplers_agree() -> None:
    """The fallback is exercised whether or not the extra is installed."""
    signal = tone(300.0, 1.0) + 0.3 * tone(1100.0, 1.0)
    fallback = _resample_fft(signal, PAL_UP, PAL_DOWN)
    chosen = resample(signal, PAL_UP, PAL_DOWN)
    size = min(len(fallback), len(chosen))
    middle = slice(size // 8, 7 * size // 8)
    assert np.corrcoef(fallback[middle], chosen[middle])[0, 1] > 0.999
    assert isinstance(scipy_available(), bool)


def test_a_ratio_of_one_is_a_copy() -> None:
    signal = tone(440.0, 0.5)
    assert np.array_equal(resample(signal, 3, 3), signal)


def test_a_ratio_that_is_not_positive_is_refused() -> None:
    with pytest.raises(ValueError, match="positive"):
        resample(tone(440.0, 0.1), 0, 1)


# ------------------------------------------------------------------- the warp
def test_an_identity_warp_returns_the_signal() -> None:
    signal = np.asarray(
        np.random.default_rng(SEED).standard_normal(20_000), dtype=np.float64
    )
    out = warp(signal, np.arange(len(signal), dtype=np.float64))
    middle = slice(200, -200)
    assert np.allclose(out[middle], signal[middle], atol=1e-9)


def test_a_constant_offset_in_the_positions_shifts_the_signal() -> None:
    signal = tone(400.0, 1.0)
    shifted = warp(signal, np.arange(len(signal), dtype=np.float64) + 80.0)
    middle = slice(500, -500)
    assert np.allclose(shifted[middle], signal[580:-420], atol=1e-6)


def test_a_fractional_offset_is_interpolated_not_rounded() -> None:
    """Half a sample at 16 kHz is 31 microseconds, and it has to be real."""
    signal = tone(250.0, 1.0)
    half = warp(signal, np.arange(len(signal), dtype=np.float64) + 0.5)
    expected = tone(250.0, 1.0 + 1.0 / SR)[1:][: len(signal)]
    reference = (signal + expected) / 2.0
    middle = slice(1000, -1000)
    assert np.abs(half[middle] - reference[middle]).max() < 0.01
    assert not np.allclose(half[middle], signal[middle], atol=1e-3)


def test_a_rate_ratio_in_the_positions_is_a_resampling() -> None:
    signal = tone(500.0, 2.0)
    n_out = int(len(signal) / PAL_RATIO)
    warped = warp(signal, np.arange(n_out, dtype=np.float64) * PAL_RATIO)
    spectrum = np.abs(np.fft.rfft(warped * np.hanning(len(warped))))
    peak_hz = float(np.fft.rfftfreq(len(warped), 1.0 / SR)[int(np.argmax(spectrum))])
    assert peak_hz == pytest.approx(500.0 * PAL_RATIO, rel=2e-3)


def test_positions_past_the_end_read_as_silence_rather_than_raising() -> None:
    signal = tone(440.0, 0.2)
    positions = np.arange(len(signal) + 4000, dtype=np.float64)
    out = warp(signal, positions)
    assert len(out) == len(positions)
    assert np.abs(out[-2000:]).max() < 1e-9


def test_positions_before_the_start_read_as_silence_too() -> None:
    signal = tone(440.0, 0.2)
    out = warp(signal, np.arange(-4000, 0, dtype=np.float64).astype(np.float64))
    assert np.abs(out[:2000]).max() < 1e-9


def test_a_warp_longer_than_one_block_is_continuous() -> None:
    """The blocking is a memory choice and must not be audible at its seams."""
    signal = tone(300.0, 20.0)
    positions = np.arange(len(signal) - 100, dtype=np.float64) + 0.25
    out = warp(signal, positions)
    steps = np.abs(np.diff(out))
    assert steps.max() < 0.15


# ------------------------------------------------------------------ the pitch
def test_two_excerpts_at_the_same_pitch_read_zero() -> None:
    signal = chord(220.0, 3.0)
    shift = pitch_shift(signal, signal.copy(), SR)
    assert shift.semitones == pytest.approx(0.0, abs=0.05)
    assert shift.r > 0.9


def test_a_sped_up_excerpt_reads_the_shift_the_speed_implies() -> None:
    original = chord(220.0, 4.0)
    faster = resample(original, PAL_DOWN, PAL_UP)
    shift = pitch_shift(original, faster, SR)
    assert shift.semitones == pytest.approx(PAL_SEMITONES, abs=0.1)
    assert shift.speed_ratio == pytest.approx(PAL_RATIO, rel=0.01)


def test_the_sign_says_which_one_is_faster() -> None:
    original = chord(220.0, 4.0)
    slower = resample(original, PAL_UP, PAL_DOWN)
    assert pitch_shift(original, slower, SR).semitones < -0.5


def test_a_spectrum_needs_enough_signal_to_be_one() -> None:
    with pytest.raises(ValueError, match="at least"):
        log_spectrum(tone(440.0, 0.01), SR)
