"""Unit tests for choose_rates: the integer-decimation rate plan that the whole
streaming chain depends on. These are pure arithmetic, no hardware or audio.
"""

import numpy as np
import pytest

from oscope_me.dsp import choose_rates


@pytest.mark.parametrize("fs_audio", [44_100, 48_000, 96_000])
def test_rate_plan_is_integer_decimation(fs_audio):
    fs_in, fs_mpx, d1, d2 = choose_rates(fs_audio)

    # Every stage must divide cleanly so decimation phase stays continuous.
    assert fs_mpx == fs_audio * d2
    assert fs_in == fs_mpx * d1
    assert fs_in % fs_mpx == 0
    assert fs_mpx % fs_audio == 0


@pytest.mark.parametrize("fs_audio", [44_100, 48_000, 96_000])
def test_mpx_is_wide_enough_for_the_multiplex(fs_audio):
    # fs_mpx/2 must sit comfortably above the 57 kHz top of the FM multiplex
    # (pilot + 38 kHz subcarrier + RDS), otherwise the stereo mux aliases.
    _, fs_mpx, _, _ = choose_rates(fs_audio)
    assert fs_mpx / 2 > 57_000


@pytest.mark.parametrize("fs_audio", [44_100, 48_000])
def test_fs_in_lands_in_rtlsdr_range(fs_audio):
    fs_in, _, _, _ = choose_rates(fs_audio)
    assert 1_000_000 <= fs_in <= 2_500_000


def test_override_must_be_a_multiple_of_mpx():
    _, fs_mpx, _, _ = choose_rates(48_000)
    # A clean multiple is accepted and reproduces the right decimation factor.
    fs_in, mpx2, d1, _ = choose_rates(48_000, fs_in_override=fs_mpx * 4)
    assert mpx2 == fs_mpx
    assert fs_in == fs_mpx * 4
    assert d1 == 4


def test_override_rejects_non_multiple():
    _, fs_mpx, _, _ = choose_rates(48_000)
    with pytest.raises(ValueError):
        choose_rates(48_000, fs_in_override=fs_mpx + 1)


def test_target_fs_in_prefers_lower_rate():
    fs_in, _, d1, _ = choose_rates(48_000, target_fs_in=960_000)
    assert fs_in == 960_000
    assert d1 >= 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
