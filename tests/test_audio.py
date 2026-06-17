"""Audio device selection (no hardware required)."""

import sys

import pytest

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
