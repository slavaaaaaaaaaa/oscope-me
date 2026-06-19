"""SDR backend discovery and IQ parsing (no hardware required)."""

import sys
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

sys.modules.setdefault("sounddevice", MagicMock())

from oscope_me import sdr


def test_tools_available_either_backend(monkeypatch):
    monkeypatch.setattr(sdr.shutil, "which", lambda name: None)
    assert not sdr.tools_available()

    monkeypatch.setattr(sdr.shutil, "which",
                        lambda name: "/usr/bin/rtl_sdr" if name == "rtl_sdr"
                        else "/usr/bin/rtl_test" if name == "rtl_test" else None)
    assert sdr.rtl_tools_available()
    assert sdr.tools_available()
    assert not sdr.airspyhf_tools_available()

    monkeypatch.setattr(sdr.shutil, "which",
                        lambda name: "/usr/bin/airspyhf_rx"
                        if name == "airspyhf_rx"
                        else "/usr/bin/airspyhf_info"
                        if name == "airspyhf_info" else None)
    assert sdr.airspyhf_tools_available()
    assert sdr.tools_available()


def test_airspyhf_device_present_success(monkeypatch):
    monkeypatch.setattr(sdr, "airspyhf_tools_available", lambda: True)
    proc = MagicMock(returncode=0, stderr="")
    monkeypatch.setattr(sdr.subprocess, "run", lambda *a, **k: proc)
    assert sdr.airspyhf_device_present()


def test_airspyhf_device_present_none_attached(monkeypatch):
    monkeypatch.setattr(sdr, "airspyhf_tools_available", lambda: True)
    proc = MagicMock(returncode=1, stderr="No devices attached.\n")
    monkeypatch.setattr(sdr.subprocess, "run", lambda *a, **k: proc)
    assert not sdr.airspyhf_device_present()


def test_rtl_device_present_found(monkeypatch):
    monkeypatch.setattr(sdr, "rtl_tools_available", lambda: True)

    class FakeProc:
        def __init__(self, *args, **kwargs):
            self.stderr = iter(["Found 1 device(s):\n"])
            self.stdout = None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            pass

    monkeypatch.setattr(sdr.subprocess, "Popen", FakeProc)
    assert sdr.rtl_device_present()


def test_detect_backend_prefers_airspy(monkeypatch):
    monkeypatch.setattr(sdr, "airspyhf_device_present", lambda: True)
    monkeypatch.setattr(sdr, "rtl_device_present", lambda: True)
    assert sdr.detect_backend("auto") == "airspyhf"


def test_detect_backend_falls_back_to_rtl(monkeypatch):
    monkeypatch.setattr(sdr, "airspyhf_device_present", lambda: False)
    monkeypatch.setattr(sdr, "rtl_device_present", lambda: True)
    assert sdr.detect_backend("auto") == "rtl"


def test_detect_backend_respects_forced_backend(monkeypatch):
    monkeypatch.setattr(sdr, "airspyhf_device_present", lambda: True)
    monkeypatch.setattr(sdr, "rtl_device_present", lambda: True)
    assert sdr.detect_backend("rtl") == "rtl"
    assert sdr.detect_backend("airspyhf") == "airspyhf"


def test_airspyhf_source_blocks_float32_iq():
    src = sdr.AirspyHfSource(100_100_000, 768_000, block_seconds=1 / 768_000)
    assert src.block == 1
    assert src.nbytes == 8

    i = np.array([0.5], dtype=np.float32)
    q = np.array([0.1], dtype=np.float32)
    raw = np.empty(2, dtype=np.float32)
    raw[0::2] = i
    raw[1::2] = q
    payload = raw.tobytes()

    class FakeStdout:
        def __init__(self, data):
            self._data = data
            self._done = False

        def read(self, n):
            if self._done or n <= 0:
                return b""
            self._done = True
            return self._data[:n]

    class FakeProc:
        stdout = FakeStdout(payload)
        stderr = iter([])

    src.proc = FakeProc()
    blocks = list(src.blocks())
    assert len(blocks) == 1
    np.testing.assert_allclose(blocks[0], [0.5 + 0.1j], rtol=1e-5)


def test_airspyhf_source_rejects_wrong_sample_rate():
    with pytest.raises(ValueError, match="768000"):
        sdr.AirspyHfSource(100_000_000, 1_200_000)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
