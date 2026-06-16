"""The streaming filter blocks all carry state so that processing IQ in blocks
gives bit-for-bit the same result as processing it all at once. That continuity
is what keeps the audio (and the X/Y picture) click-free, so it's worth pinning.
"""

import numpy as np
import pytest
from scipy.signal import firwin

from oscope_me.dsp import _Delay, _DecimFIR, _Discriminator, _FIR


def _split(n, sizes):
    """Yield slice bounds that partition range(n) into the given block sizes,
    cycling through `sizes` (so block boundaries land at awkward places)."""
    i = 0
    k = 0
    while i < n:
        step = sizes[k % len(sizes)]
        yield i, min(i + step, n)
        i += step
        k += 1


def _blockwise(make_filter, x, sizes=(1, 7, 100, 3, 251)):
    """Run a fresh filter over x, once whole and once in uneven blocks."""
    whole = make_filter()(x)
    f = make_filter()
    parts = [f(x[a:b]) for a, b in _split(len(x), list(sizes))]
    streamed = np.concatenate(parts) if parts else np.array([])
    return whole, streamed


def test_fir_block_continuity():
    rng = np.random.default_rng(0)
    x = rng.standard_normal(1000)
    taps = firwin(31, 0.2)
    whole, streamed = _blockwise(lambda: _FIR(taps), x)
    assert np.allclose(whole, streamed, atol=1e-12)


def test_decimfir_block_continuity_and_factor():
    rng = np.random.default_rng(1)
    x = rng.standard_normal(1000)
    taps = firwin(31, 0.2)
    whole, streamed = _blockwise(lambda: _DecimFIR(taps, 5), x)
    # Same samples regardless of how the input was chopped up.
    assert np.allclose(whole, streamed, atol=1e-12)
    # And it actually decimates by ~factor.
    assert abs(len(whole) - len(x) // 5) <= 1


def test_delay_shifts_and_preserves_signal():
    x = np.arange(20.0)
    whole, streamed = _blockwise(lambda: _Delay(3), x)
    assert np.allclose(whole, streamed)
    # First 3 samples are the zero-fill, then the original signal, delayed by 3.
    assert np.allclose(whole[:3], 0.0)
    assert np.allclose(whole[3:], x[:-3])


def test_delay_zero_is_identity():
    x = np.arange(10.0)
    assert np.array_equal(_Delay(0)(x), x)


def test_discriminator_recovers_constant_frequency():
    # A pure complex tone has constant instantaneous frequency w per sample.
    w = 0.3
    n = np.arange(500)
    x = np.exp(1j * w * n).astype(np.complex64)
    out = _Discriminator()(x)
    # Skip the first sample (depends on the zero initial state).
    assert np.allclose(out[1:], w, atol=1e-3)


def test_discriminator_block_continuity():
    rng = np.random.default_rng(2)
    x = (rng.standard_normal(400) + 1j * rng.standard_normal(400)).astype(np.complex64)
    whole, streamed = _blockwise(lambda: _Discriminator(), x)
    assert np.allclose(whole, streamed, atol=1e-5)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
