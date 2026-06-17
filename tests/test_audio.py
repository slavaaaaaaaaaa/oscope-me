"""Audio device selection (no hardware required)."""

import sys
from unittest.mock import MagicMock

import numpy as np
import pytest

# sounddevice loads PortAudio at import time; stub it so CI needs no system libs.
sys.modules["sounddevice"] = MagicMock()

import oscope_me.audio as audio


def test_resolve_output_device_honours_explicit():
    assert audio.resolve_output_device(3) == 3
    assert audio.resolve_output_device("hw:0,1") == "hw:0,1"


def test_resolve_output_device_non_linux_default(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert audio.resolve_output_device(None) is None


def test_resolve_output_device_linux_prefers_pulse(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    devices = [
        {"name": "HDA Intel Analog (hw:0,0), ALSA", "max_output_channels": 2},
        {"name": "pulse", "max_output_channels": 32},
        {"name": "default", "max_output_channels": 32},
    ]
    monkeypatch.setattr(audio.sd, "query_devices", lambda: devices)
    assert audio.resolve_output_device(None) == "pulse"


def test_resolve_output_device_linux_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    devices = [
        {"name": "HDA Intel Analog (hw:0,0), ALSA", "max_output_channels": 2},
        {"name": "default", "max_output_channels": 32},
    ]
    monkeypatch.setattr(audio.sd, "query_devices", lambda: devices)
    assert audio.resolve_output_device(None) == "default"


def test_resolve_output_device_linux_no_match(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    devices = [
        {"name": "HDA Intel Analog (hw:0,0), ALSA", "max_output_channels": 2},
    ]
    monkeypatch.setattr(audio.sd, "query_devices", lambda: devices)
    assert audio.resolve_output_device(None) is None


def test_alsa_card_from_device_hw_string():
    assert audio.alsa_card_from_device("hw:1,0") == 1


def test_alsa_card_from_device_defaults_to_zero():
    assert audio.alsa_card_from_device(None) == 0
    assert audio.alsa_card_from_device("pulse") == 0


def test_enable_linux_dual_analog_runs_amixer(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    calls = []
    monkeypatch.setattr(audio.subprocess, "run",
                        lambda args, **kw: calls.append(args) or MagicMock(returncode=0))
    audio.enable_linux_dual_analog(2)
    assert calls == [
        ("amixer", "-c", "2", "sset", "Auto-Mute Mode", "Disabled"),
        ("amixer", "-c", "2", "sset", "Speaker", "unmute"),
    ]


def test_enable_linux_dual_analog_noop_off_linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(audio.subprocess, "run",
                        lambda *a, **k: pytest.fail("should not run amixer"))
    audio.enable_linux_dual_analog()


def test_device_output_write_array(monkeypatch):
    class FakeStream:
        device = 0

        def start(self):
            pass

        def stop(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(audio.sd, "OutputStream", lambda **kw: FakeStream())
    out = audio._DeviceOutput(48_000, None, 2, 0.01)
    data = np.ones((100, 2), dtype=np.float32)
    out.write_array(data)
    assert out.count == 100


def test_audio_output_write_fans_out_to_monitor(monkeypatch):
    primary_writes = []
    monitor_writes = []

    class FakeDeviceOutput:
        def __init__(self, samplerate, device, channels, buffer_seconds):
            self.device = device
            self.underruns = 0

        @property
        def device_name(self):
            return str(self.device)

        def start(self):
            pass

        def stop(self):
            pass

        def reset(self):
            pass

        def fill(self):
            return 0

        def drain(self, timeout=5.0):
            pass

        def write_array(self, data):
            if self.device == "primary":
                primary_writes.append(data.copy())
            else:
                monitor_writes.append(data.copy())

    monkeypatch.setattr(audio, "_DeviceOutput", FakeDeviceOutput)
    ao = audio.AudioOutput(device="primary", monitor_device="monitor",
                           dual_analog=False)
    left = np.array([0.5, -0.5], dtype=np.float32)
    right = np.array([0.25, -0.25], dtype=np.float32)
    ao.write(left, right)
    assert len(primary_writes) == 1
    assert len(monitor_writes) == 1
    np.testing.assert_array_equal(primary_writes[0], monitor_writes[0])


def test_audio_output_device_name_with_monitor(monkeypatch):
    class FakeDeviceOutput:
        def __init__(self, samplerate, device, channels, buffer_seconds):
            self.device = device

        @property
        def device_name(self):
            return str(self.device)

        def start(self):
            pass

        def stop(self):
            pass

        def reset(self):
            pass

        def fill(self):
            return 0

        def drain(self, timeout=5.0):
            pass

        def write_array(self, data):
            pass

    monkeypatch.setattr(audio, "_DeviceOutput", FakeDeviceOutput)
    ao = audio.AudioOutput(device="pulse", monitor_device="usb")
    assert ao.device_name == "pulse + usb"

