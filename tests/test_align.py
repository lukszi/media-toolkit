"""The alignment primitives, against answers written down before they run.

None of these tests needs a program, a model or a media file. A lag is planted
in an array and the arithmetic has to find it; that is the whole contract, and
it is the contract every later module in the package leans on.

Noise rather than a tone throughout, on purpose: a sine correlates with itself
one period later, so a periodic signal cannot tell a lag of 250 ms from one of
252 ms. The one test that *does* use a tone is the test that proves the
confidence measure notices.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from dubalign.align import (
    ANALYSIS_RATE,
    ENVELOPE_RATE,
    best_with_ratio,
    load_mono,
    ncc_full,
    onset_env,
    refine,
)

SEED = 20260923


def noise(seconds: float, sr: int = ANALYSIS_RATE, seed: int = SEED) -> np.ndarray:
    return np.asarray(
        np.random.default_rng(seed).standard_normal(int(seconds * sr)), dtype=np.float64
    )


# ------------------------------------------------------------ the correlation
def test_a_planted_lag_is_recovered_exactly() -> None:
    signal = noise(4.0)
    template = signal[8000:16000]
    r = ncc_full(template, signal)
    peak = best_with_ratio(r, 64)
    assert peak.index == 8000
    assert peak.r == pytest.approx(1.0)


def test_the_correlation_is_pearsons_r_at_every_position() -> None:
    """Checked against the definition, on a signal short enough to loop over."""
    rng = np.random.default_rng(SEED)
    a = rng.standard_normal(64)
    b = rng.standard_normal(256)
    fast = ncc_full(a, b)
    assert len(fast) == len(b) - len(a) + 1
    for k in (0, 1, 97, len(fast) - 1):
        window = b[k : k + len(a)]
        expected = np.corrcoef(a, window)[0, 1]
        assert fast[k] == pytest.approx(expected, abs=1e-9)


def test_level_does_not_beat_similarity() -> None:
    """A loud passage that matches badly must not outscore a quiet one that matches."""
    rng = np.random.default_rng(SEED)
    template = rng.standard_normal(4000)
    quiet_match = template * 0.001
    loud_mismatch = rng.standard_normal(4000) * 50.0
    signal = np.concatenate([loud_mismatch, quiet_match])
    peak = best_with_ratio(ncc_full(template, signal), 64)
    assert peak.index == 4000


def test_a_template_longer_than_the_signal_is_refused() -> None:
    with pytest.raises(ValueError, match="does not fit"):
        ncc_full(noise(2.0), noise(1.0))


def test_a_precomputed_transform_gives_the_same_answer() -> None:
    """The reuse that makes a whole-timeline search affordable must be exact."""
    signal = noise(4.0)
    template = signal[3000:11000]
    n_fft = 1 << (len(signal) + len(template) - 1).bit_length()
    shared = np.fft.rfft(signal, n_fft)
    direct = ncc_full(template, signal)
    reused = ncc_full(template, signal, b_fft=shared, n_fft=n_fft)
    assert np.allclose(direct, reused, atol=1e-12)


# ------------------------------------------------------------- the confidence
def test_a_unique_match_is_confident() -> None:
    signal = noise(6.0)
    peak = best_with_ratio(ncc_full(signal[16000:48000], signal), 64)
    assert peak.confident()
    assert peak.ratio > 2.0


def test_a_repeating_signal_is_not_confident_even_though_r_is_high() -> None:
    """The failure the ratio exists for: forty equally good answers."""
    sr = ANALYSIS_RATE
    t = np.arange(4 * sr) / sr
    tone = np.sin(2 * np.pi * 440.0 * t)
    peak = best_with_ratio(ncc_full(tone[: sr // 2], tone), int(0.002 * sr))
    assert peak.r == pytest.approx(1.0, abs=1e-6)
    assert peak.ratio < 1.05
    assert not peak.confident()


def test_the_guard_band_is_what_keeps_a_real_peak_confident() -> None:
    """With no guard, the sample beside the peak is reported as the second peak.

    Band-limited on purpose: white noise decorrelates in one sample, so it is
    the one signal for which the guard band would not be needed. Real audio,
    and every envelope in this package, has a main lobe several samples wide.
    """
    smooth = np.convolve(noise(4.0), np.ones(200) / 200.0, mode="same")
    r = ncc_full(smooth[8000:24000], smooth)
    assert best_with_ratio(r, 0).ratio < 1.05
    assert best_with_ratio(r, 400).ratio > 1.5


def test_an_empty_correlation_is_an_error_not_a_zero() -> None:
    with pytest.raises(ValueError, match="no correlation"):
        best_with_ratio(np.zeros(0), 4)


# --------------------------------------------------------------- the envelope
def test_the_envelope_comes_out_at_one_frame_per_millisecond() -> None:
    envelope = onset_env(noise(3.0))
    assert len(envelope) == pytest.approx(3.0 * ENVELOPE_RATE, abs=2)


def test_the_envelope_keeps_onsets_and_drops_decays() -> None:
    sr = ANALYSIS_RATE
    silence = np.zeros(sr)
    burst = np.random.default_rng(SEED).standard_normal(sr)
    envelope = onset_env(np.concatenate([silence, burst, silence]))
    at_onset = int(1.0 * ENVELOPE_RATE)
    at_decay = int(2.0 * ENVELOPE_RATE)
    assert envelope[at_onset - 2 : at_onset + 3].max() > 1.0
    assert envelope[at_decay - 2 : at_decay + 3].max() == 0.0


def test_the_envelope_survives_a_level_change_that_the_waveform_does_not() -> None:
    """Two transfers of one programme differ in level; their onsets do not."""
    signal = noise(3.0)
    quiet = onset_env(signal * 0.05)
    loud = onset_env(signal * 3.0)
    assert np.corrcoef(quiet, loud)[0, 1] == pytest.approx(1.0, abs=1e-6)


def test_a_signal_shorter_than_one_frame_gives_an_empty_envelope() -> None:
    assert len(onset_env(np.zeros(4))) == 0


def test_a_hop_of_zero_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one sample"):
        onset_env(noise(1.0), hop=0)


# ------------------------------------------------------------- the refinement
def test_the_refinement_measures_how_wrong_the_coarse_answer_was() -> None:
    sr = ANALYSIS_RATE
    signal = noise(20.0)
    delayed = np.concatenate([np.zeros(int(0.100 * sr)), signal])
    got = refine(signal, delayed, 5.0, 5.0, dur=2.0, search=0.3, sr=sr)
    assert got is not None
    assert got.delta_s == pytest.approx(0.100, abs=1.0 / sr)
    assert got.r > 0.99


def test_the_refinement_declines_rather_than_guessing_at_the_end() -> None:
    signal = noise(4.0)
    assert refine(signal, signal, 3.9, 3.9, dur=2.0) is None
    assert refine(signal, signal, -1.0, 0.0, dur=1.0) is None


# ------------------------------------------------------------------- the dump
def test_a_raw_float_dump_reads_back_as_float64(tmp_path: Path) -> None:
    written = np.linspace(-1.0, 1.0, 1000, dtype=np.float32)
    path = tmp_path / "analysis.f32"
    written.tofile(path)
    read = load_mono(path)
    assert read.dtype == np.float64
    assert np.allclose(read, written, atol=1e-7)
