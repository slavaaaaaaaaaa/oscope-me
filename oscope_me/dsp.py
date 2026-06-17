"""Streaming wideband-FM stereo demodulator.

The chain, all stateful so blocks join without clicks:

    IQ @ fs_in --decimate--> fs_mpx --FM discriminator--> MPX (mono+stereo mux)

From the MPX (multiplex) baseband we recover:
  * mono  = L+R   : lowpass 0..15 kHz
  * pilot = 19 kHz tone the station transmits as a phase reference
  * L-R   : the 38 kHz DSB-SC subcarrier, coherently downconverted using a
            38 kHz carrier regenerated from the pilot (pilot squared), then
            lowpassed to 15 kHz.

Then L = mono + (L-R),  R = mono - (L-R).

For oscilloscope music this stereo decode is the whole point: Left drives the
scope's X axis and Right drives Y, so the *difference* between the channels is
what draws the picture. A mono decode would collapse every image to a diagonal
line.
"""

from __future__ import annotations

import numpy as np
from scipy.signal import firwin, lfilter


def choose_rates(fs_audio: int, fs_in_override: int | None = None,
                 target_fs_in: int = 1_200_000):
    """Pick (fs_in, fs_mpx, D1, D2) with integer decimation all the way down.

    fs_mpx is the smallest integer multiple of fs_audio that is wide enough to
    hold the whole FM multiplex (pilot + 38 kHz subcarrier + RDS ~= 57 kHz),
    so we need fs_mpx/2 comfortably above ~57 kHz.
    """
    m = int(np.ceil(220_000 / fs_audio))      # fs_mpx = m * fs_audio >= 220 kHz
    fs_mpx = m * fs_audio
    d2 = m

    if fs_in_override:
        fs_in = int(fs_in_override)
        if fs_in % fs_mpx != 0:
            raise ValueError(
                f"--sample-rate {fs_in} must be an integer multiple of the "
                f"internal MPX rate {fs_mpx} (derived from --audio-rate "
                f"{fs_audio}). Try {fs_mpx * round(fs_in / fs_mpx)}.")
        d1 = fs_in // fs_mpx
        return fs_in, fs_mpx, d1, d2

    best = None
    for d1 in range(1, 16):
        cand = fs_mpx * d1
        if 900_000 <= cand <= 2_500_000:
            score = abs(cand - target_fs_in)
            if best is None or score < best[0]:
                best = (score, d1, cand)
    if best is None:
        d1 = int(np.ceil(1_000_000 / fs_mpx))
        return fs_mpx * d1, fs_mpx, d1, d2
    _, d1, fs_in = best
    return fs_in, fs_mpx, d1, d2


class _FIR:
    """FIR filter that carries its delay line across blocks."""

    def __init__(self, taps):
        self.b = np.asarray(taps, dtype=np.float64)
        self.zi = None

    def __call__(self, x):
        if self.zi is None:
            self.zi = np.zeros(len(self.b) - 1, dtype=x.dtype)
        y, self.zi = lfilter(self.b, [1.0], x, zi=self.zi)
        return y


class _DecimFIR:
    """Anti-alias FIR + integer downsample, with continuous decimation phase."""

    def __init__(self, taps, factor):
        self.fir = _FIR(taps)
        self.factor = factor
        self.count = 0

    def __call__(self, x):
        y = self.fir(x)
        start = (-self.count) % self.factor
        self.count += len(y)
        return y[start::self.factor]


class _IIR:
    """Small IIR (de-emphasis / DC block) that keeps state across blocks."""

    def __init__(self, b, a):
        self.b = np.asarray(b, dtype=np.float64)
        self.a = np.asarray(a, dtype=np.float64)
        self.zi = np.zeros(max(len(self.a), len(self.b)) - 1)

    def __call__(self, x):
        y, self.zi = lfilter(self.b, self.a, x, zi=self.zi)
        return y


class _Delay:
    """Integer sample delay with state, to time-align parallel filter paths."""

    def __init__(self, n):
        self.n = n
        self.buf = None

    def __call__(self, x):
        if self.n == 0:
            return x
        if self.buf is None:
            self.buf = np.zeros(self.n, dtype=x.dtype)
        y = np.concatenate([self.buf, x])
        self.buf = y[-self.n:].copy()
        return y[:len(x)]


class _Discriminator:
    """FM discriminator: instantaneous frequency via angle(x[n] * conj(x[n-1]))."""

    def __init__(self):
        self.prev = np.complex64(0)

    def __call__(self, x):
        xs = np.empty(len(x) + 1, dtype=x.dtype)
        xs[0] = self.prev
        xs[1:] = x
        self.prev = x[-1]
        return np.angle(xs[1:] * np.conj(xs[:-1])).astype(np.float32)


class FmStereoDemod:
    def __init__(self, fs_in, fs_mpx, d1, d2, fs_audio,
                 deemphasis_us=75, stereo=True, volume=1.0):
        self.fs_in = fs_in
        self.fs_mpx = fs_mpx
        self.fs_audio = fs_audio
        self.stereo = stereo
        self.pilot_present = False

        nyq = fs_mpx / 2.0
        # IQ -> fs_mpx (keep the full ~200 kHz FM channel before decimating).
        self.iq_decim = _DecimFIR(firwin(64, 0.9 * nyq, fs=fs_in), d1)
        self.disc = _Discriminator()

        # MPX-domain filters (fs_mpx). lp_mono and lp_lr share a tap count so
        # the L+R and L-R paths end up with identical group delay.
        n_pilot, n_dsc, n_lp = 257, 129, 129
        self.bp_pilot = _FIR(firwin(n_pilot, [18_500, 19_500], pass_zero=False, fs=fs_mpx))
        self.bp_dsc = _FIR(firwin(n_dsc, [37_000, 39_000], pass_zero=False, fs=fs_mpx))
        self.lp_mono = _FIR(firwin(n_lp, 15_000, fs=fs_mpx))
        self.lp_lr = _FIR(firwin(n_lp, 15_000, fs=fs_mpx))
        # The regenerated 38 kHz carrier is delayed by the pilot bandpass plus
        # the doubling bandpass; delay the raw MPX by the same so the coherent
        # product is time-aligned, and so mono lines up with L-R afterwards.
        self.mpx_delay = _Delay((n_pilot - 1) // 2 + (n_dsc - 1) // 2)

        # fs_mpx -> fs_audio.
        ac = 0.45 * fs_audio
        self.dec_mono = _DecimFIR(firwin(129, ac, fs=fs_mpx), d2)
        self.dec_lr = _DecimFIR(firwin(129, ac, fs=fs_mpx), d2)

        # De-emphasis (75 us North America, 50 us Europe) at audio rate.
        if deemphasis_us:
            d = np.exp(-1.0 / (fs_audio * deemphasis_us * 1e-6))
            self.de_l = _IIR([1 - d], [1.0, -d])
            self.de_r = _IIR([1 - d], [1.0, -d])
        else:
            self.de_l = self.de_r = None

        # DC block (~20 Hz) so the X/Y figure stays centred on the screen.
        r = np.exp(-2 * np.pi * 20.0 / fs_audio)
        self.dc_l = _IIR([1.0, -1.0], [1.0, -r])
        self.dc_r = _IIR([1.0, -1.0], [1.0, -r])

        # Equal-gain AGC: scales L and R by the SAME factor so the picture's
        # shape is preserved while the level stays sane for the DAC / scope.
        self.agc_gain = 0.0
        self.agc_target = 0.25 * float(volume)
        self._agc_ready = False

    def set_volume(self, volume):
        """Change the output level on the fly (instant; AGC keeps tracking after)."""
        new_target = 0.25 * float(volume)
        if self.agc_target > 1e-12:
            self.agc_gain *= new_target / self.agc_target
        self.agc_target = new_target

    def process(self, iq):
        base = self.iq_decim(iq)        # complex @ fs_mpx
        mpx = self.disc(base)           # real @ fs_mpx

        pilot = self.bp_pilot(mpx)
        # Delay-align the MPX with the regenerated carrier (and so mono with L-R).
        mpx_a = self.mpx_delay(mpx)
        mono = self.lp_mono(mpx_a)

        if self.stereo:
            prms = float(np.sqrt(np.mean(pilot * pilot) + 1e-12))
            mrms = float(np.sqrt(np.mean(mono * mono) + 1e-12))
            self.pilot_present = prms > 0.02 * mrms and prms > 1e-3
            if self.pilot_present:
                amp = np.sqrt(2.0) * prms                 # pilot amplitude
                c38 = self.bp_dsc(pilot * pilot)           # squaring -> 38 kHz
                c38 = c38 / (amp * amp / 2.0 + 1e-9)        # normalise to ~unit cos
                lr = self.lp_lr(2.0 * mpx_a * c38)          # coherent downconvert
            else:
                lr = np.zeros_like(mono)
        else:
            self.pilot_present = False
            lr = np.zeros_like(mono)

        mono_a = self.dec_mono(mono)
        lr_a = self.dec_lr(lr)
        n = min(len(mono_a), len(lr_a))
        mono_a, lr_a = mono_a[:n], lr_a[:n]

        left = mono_a + lr_a
        right = mono_a - lr_a
        if self.de_l is not None:
            left = self.de_l(left)
            right = self.de_r(right)
        left = self.dc_l(left)
        right = self.dc_r(right)

        level = float(np.sqrt(np.mean(left * left + right * right) + 1e-9))
        desired = self.agc_target / (level + 1e-9)
        if not self._agc_ready:
            if level > 1e-6:
                self.agc_gain = float(np.clip(desired, 0.0, 50.0))
                self._agc_ready = True
        else:
            self.agc_gain += 0.05 * (desired - self.agc_gain)
            self.agc_gain = float(np.clip(self.agc_gain, 0.0, 50.0))
        left = left * self.agc_gain
        right = right * self.agc_gain

        return left.astype(np.float32), right.astype(np.float32)
