"""Shared alignment helpers: onset envelope, full-range normalised sliding cross-correlation."""
import numpy as np

ESR = 1000      # envelope rate
ASR = 16000     # analysis rate


def load_mono(path):
    return np.fromfile(path, dtype=np.float32).astype(np.float64)


def onset_env(x, hop=16):
    """log-RMS envelope at ASR/hop Hz, differenced, half-wave rectified."""
    n = len(x) // hop
    e = np.sqrt(np.mean(x[:n * hop].reshape(n, hop) ** 2, axis=1) + 1e-10)
    d = np.diff(np.log(e))
    d[d < 0] = 0.0
    return d


def ncc_full(a, b, bfft=None, nf=None):
    """Normalised cross-correlation of template a against every position of b.
    returns r[k] for k = 0 .. len(b)-len(a), where r[k] = corr(a, b[k:k+len(a)])."""
    M, N = len(a), len(b)
    a0 = a - a.mean()
    na = np.linalg.norm(a0)
    if nf is None:
        nf = 1 << (N + M - 1).bit_length()
    if bfft is None:
        bfft = np.fft.rfft(b, nf)
    num = np.fft.irfft(bfft * np.conj(np.fft.rfft(a0, nf)), nf)[:N - M + 1]
    cs = np.concatenate(([0.0], np.cumsum(b)))
    cs2 = np.concatenate(([0.0], np.cumsum(b * b)))
    s = cs[M:] - cs[:-M]
    s2 = cs2[M:] - cs2[:-M]
    den = np.sqrt(np.maximum(s2 - s * s / M, 1e-12)) * max(na, 1e-12)
    return num / den


def best_with_ratio(r, guard):
    """argmax plus peak/2nd ratio (2nd peak outside +/- guard samples)."""
    k = int(np.argmax(r))
    r2 = r.copy()
    lo, hi = max(0, k - guard), min(len(r2), k + guard + 1)
    r2[lo:hi] = -2.0
    second = float(r2.max()) if len(r2) else 0.0
    return k, float(r[k]), float(r[k] / max(second, 1e-6)), second


def refine(a_wav, b_wav, ta, tb, dur=2.0, search=0.30, sr=ASR):
    """Refine lag with the raw waveform: a window of a at ta vs b around tb.
    returns (delta_seconds, r) where the true b position is tb + delta."""
    ia = int(round(ta * sr)); M = int(dur * sr)
    a = a_wav[ia:ia + M]
    if len(a) < M:
        return None, 0.0
    ib = int(round((tb - search) * sr)); N = M + 2 * int(search * sr)
    if ib < 0 or ib + N > len(b_wav):
        return None, 0.0
    b = b_wav[ib:ib + N]
    r = ncc_full(a, b)
    k, rv, ratio, _ = best_with_ratio(r, int(0.02 * sr))
    return (k / sr) - search, rv
