"""End-to-end DSP check: build a synthetic FM stereo broadcast, modulate it to
IQ, demodulate, and confirm L and R come back separated. This is what makes the
X/Y picture correct, so it's the test that matters most.
"""

import numpy as np

from oscope_me.dsp import FmStereoDemod, choose_rates


def make_fm_stereo_iq(left, right, fs_audio, fs_in, deviation=75_000,
                      pilot_level=0.08):
    """Encode L/R into the FM multiplex and FM-modulate to complex IQ @ fs_in."""
    up = fs_in // fs_audio
    # Sample-and-hold upsample the audio to the IQ rate (good enough for a test).
    l = np.repeat(left, up)
    r = np.repeat(right, up)
    n = len(l)
    t = np.arange(n) / fs_in

    # Broadcast convention: pilot and the 38 kHz subcarrier are cosines, with
    # the subcarrier coherent with the doubled pilot phase.
    pilot = np.cos(2 * np.pi * 19_000 * t)
    sub = np.cos(2 * np.pi * 38_000 * t)        # = cos(2 * pilot phase)
    mono = 0.5 * (l + r)
    diff = 0.5 * (l - r)
    mpx = mono + pilot_level * pilot + diff * sub

    # FM modulate: phase is the integral of the (scaled) message.
    kf = 2 * np.pi * deviation / fs_in
    phase = np.cumsum(kf * mpx)
    return np.exp(1j * phase).astype(np.complex64)


def test_stereo_separation():
    fs_audio = 48_000
    fs_in, fs_mpx, d1, d2 = choose_rates(fs_audio)

    dur = 1.0
    n = int(fs_audio * dur)
    t = np.arange(n) / fs_audio
    # Distinct tones so we can tell the channels apart.
    left = 0.6 * np.sin(2 * np.pi * 700 * t)
    right = 0.6 * np.sin(2 * np.pi * 1500 * t)

    iq = make_fm_stereo_iq(left, right, fs_audio, fs_in)

    demod = FmStereoDemod(fs_in, fs_mpx, d1, d2, fs_audio, deemphasis_us=0)
    # Feed in blocks like the real app does.
    outL, outR = [], []
    step = fs_in // 10
    for i in range(0, len(iq), step):
        L, R = demod.process(iq[i:i + step])
        outL.append(L)
        outR.append(R)
    L = np.concatenate(outL)
    R = np.concatenate(outR)

    assert demod.pilot_present, "pilot was not detected"

    # Discard filter warm-up at the edges.
    s = len(L) // 4
    e = len(L) - len(L) // 8
    L, R = L[s:e], R[s:e]
    m = len(L)

    def tone(sig, freq):
        """Magnitude of `sig` at `freq` (delay-invariant via the DFT)."""
        k = np.exp(-2j * np.pi * freq * np.arange(m) / fs_audio)
        return abs(np.dot(sig - sig.mean(), k)) / m

    fl, fr = 700.0, 1500.0
    L_l, L_r = tone(L, fl), tone(L, fr)   # left channel's 700 vs 1500 content
    R_l, R_r = tone(R, fl), tone(R, fr)

    sep_L = 20 * np.log10(L_l / (L_r + 1e-12))   # want left to hold 700, not 1500
    sep_R = 20 * np.log10(R_r / (R_l + 1e-12))   # want right to hold 1500, not 700
    print(f"left  channel: 700Hz={L_l:.4f} 1500Hz={L_r:.4f}  separation {sep_L:5.1f} dB")
    print(f"right channel: 700Hz={R_l:.4f} 1500Hz={R_r:.4f}  separation {sep_R:5.1f} dB")

    assert sep_L > 18 and sep_R > 18, "poor stereo separation"


def test_set_volume_changes_level_immediately():
    fs_audio = 48_000
    fs_in, fs_mpx, d1, d2 = choose_rates(fs_audio)

    dur = 1.0
    n = int(fs_audio * dur)
    t = np.arange(n) / fs_audio
    left = 0.6 * np.sin(2 * np.pi * 700 * t)
    right = 0.6 * np.sin(2 * np.pi * 1500 * t)
    iq = make_fm_stereo_iq(left, right, fs_audio, fs_in)

    demod = FmStereoDemod(fs_in, fs_mpx, d1, d2, fs_audio, deemphasis_us=0,
                          volume=1.0)
    step = fs_in // 10
    for i in range(0, len(iq), step):
        demod.process(iq[i:i + step])

    _, rms_before = _output_rms(demod, iq[-step:])
    demod.set_volume(0.5)
    _, rms_after = _output_rms(demod, iq[-step:])

    ratio = rms_after / (rms_before + 1e-12)
    assert 0.40 < ratio < 0.60, f"expected ~0.5x level, got {ratio:.3f}"


def _output_rms(demod, iq_block):
    left, right = demod.process(iq_block)
    rms = float(np.sqrt(np.mean(left * left + right * right)))
    return left, rms


def test_agc_snaps_at_startup_not_loud_ramp():
    fs_audio = 48_000
    fs_in, fs_mpx, d1, d2 = choose_rates(fs_audio)

    n = int(fs_audio * 0.5)
    t = np.arange(n) / fs_audio
    left = 0.6 * np.sin(2 * np.pi * 700 * t)
    right = 0.6 * np.sin(2 * np.pi * 1500 * t)
    iq = make_fm_stereo_iq(left, right, fs_audio, fs_in)

    demod = FmStereoDemod(fs_in, fs_mpx, d1, d2, fs_audio, deemphasis_us=0,
                          volume=0.02)
    step = fs_in // 10
    rms_readings = []
    for i in range(0, len(iq), step):
        _, rms = _output_rms(demod, iq[i:i + step])
        if rms > 1e-6:
            rms_readings.append(rms)
        if len(rms_readings) >= 5:
            break

    assert demod._agc_ready
    assert demod.agc_gain < 1.0, f"startup gain should not stay at 1.0, got {demod.agc_gain}"
    # First audible block should already be near steady level, not a huge spike.
    assert rms_readings[0] < rms_readings[-1] * 3.0


if __name__ == "__main__":
    test_stereo_separation()
    print("OK")
