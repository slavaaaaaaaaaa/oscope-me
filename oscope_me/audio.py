"""Stereo audio output via PortAudio (sounddevice) with a thread-safe ring buffer.

Left channel -> scope X, Right channel -> scope Y.

On macOS the system default follows headphone hot-plug. On Linux, PortAudio often
opens a raw ALSA PCM device that does not; when no --audio-device is given we
prefer routing through PulseAudio/PipeWire so jack switching works.

On Ubuntu laptops, ALSA Auto-Mute Mode silences speakers when headphones are
plugged in. Use --dual-analog (or `make run` / `make play`) to disable it so
one pulse stream reaches both jack and built-in speakers.
"""

from __future__ import annotations

import re
import subprocess
import sys
import threading
import time

import numpy as np
import sounddevice as sd

# Substrings matched against PortAudio output device names (case-insensitive).
_LINUX_OUTPUT_PREFER = ("pulse", "pipewire", "default")

_HW_CARD_RE = re.compile(r"hw:(\d+)")


def resolve_output_device(device):
    """Return the PortAudio device argument to use for playback."""
    if device is not None:
        return device
    if sys.platform != "linux":
        return None
    try:
        devices = sd.query_devices()
    except Exception:
        return None
    for prefer in _LINUX_OUTPUT_PREFER:
        for dev in devices:
            if dev.get("max_output_channels", 0) < 1:
                continue
            if prefer in dev["name"].lower():
                return prefer
    return None


def alsa_card_from_device(device) -> int:
    """Guess ALSA card index from a PortAudio device name or hw: string."""
    if device is None:
        return 0
    text = str(device)
    m = _HW_CARD_RE.search(text)
    if m:
        return int(m.group(1))
    try:
        info = sd.query_devices(device, "output")
        m = _HW_CARD_RE.search(info.get("name", ""))
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return 0


def enable_linux_dual_analog(card: int = 0) -> None:
    """Disable ALSA auto-mute so headphones and speakers can play together."""
    if sys.platform != "linux":
        return
    for args in (
        ("amixer", "-c", str(card), "sset", "Auto-Mute Mode", "Disabled"),
        ("amixer", "-c", str(card), "sset", "Speaker", "unmute"),
    ):
        result = subprocess.run(args, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            print(f"warning: {' '.join(args)} failed", file=sys.stderr)


class _DeviceOutput:
    """One PortAudio output stream with a ring buffer."""

    def __init__(self, samplerate, device, channels, buffer_seconds):
        self.samplerate = int(samplerate)
        self.device = device
        self.channels = channels
        self.N = max(1, int(self.samplerate * buffer_seconds))
        self.buf = np.zeros((self.N, channels), dtype=np.float32)
        self.r = 0
        self.w = 0
        self.count = 0
        self.lock = threading.Lock()
        self.underruns = 0
        self.prefill = int(self.N * 0.4)
        self.primed = False
        self.stream = sd.OutputStream(
            samplerate=self.samplerate, channels=channels, dtype="float32",
            device=self.device, callback=self._callback)

    @property
    def device_name(self):
        try:
            return sd.query_devices(self.stream.device, "output")["name"]
        except Exception:
            return str(self.stream.device)

    def start(self):
        self.stream.start()

    def stop(self):
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:
            pass

    def reset(self):
        with self.lock:
            self.r = self.w = self.count = 0
            self.underruns = 0
            self.primed = False

    def _callback(self, outdata, frames, time_info, status):
        with self.lock:
            if not self.primed:
                if self.count >= self.prefill:
                    self.primed = True
                else:
                    outdata[:] = 0.0
                    return
            n = min(frames, self.count)
            first = min(n, self.N - self.r)
            outdata[:first] = self.buf[self.r:self.r + first]
            if n > first:
                outdata[first:n] = self.buf[:n - first]
            self.r = (self.r + n) % self.N
            self.count -= n
        if n < frames:
            outdata[n:] = 0.0
            self.underruns += 1

    def fill(self):
        with self.lock:
            return self.count

    def drain(self, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                if self.count <= 0:
                    return
            time.sleep(0.02)

    def write_array(self, data):
        m = len(data)
        with self.lock:
            if m >= self.N:
                data = data[-self.N:]
                m = self.N
            first = min(m, self.N - self.w)
            self.buf[self.w:self.w + first] = data[:first]
            if m > first:
                self.buf[:m - first] = data[first:]
            self.w = (self.w + m) % self.N
            self.count += m
            if self.count > self.N:
                over = self.count - self.N
                self.r = (self.r + over) % self.N
                self.count = self.N


class AudioOutput:
    def __init__(self, samplerate=48_000, device=None, channels=2,
                 buffer_seconds=0.5, monitor_device=None, dual_analog=False):
        self.channels = channels
        primary_device = resolve_output_device(device)
        if dual_analog:
            enable_linux_dual_analog(alsa_card_from_device(device or primary_device))
        monitor_resolved = (resolve_output_device(monitor_device)
                            if monitor_device is not None else None)
        self._primary = _DeviceOutput(samplerate, primary_device, channels,
                                      buffer_seconds)
        self._monitor = None
        if monitor_resolved is not None:
            self._monitor = _DeviceOutput(samplerate, monitor_resolved, channels,
                                          buffer_seconds)

    @property
    def device_name(self):
        primary = self._primary.device_name
        if self._monitor is None:
            return primary
        return f"{primary} + {self._monitor.device_name}"

    @property
    def N(self):
        """Ring-buffer capacity in frames (paced against the primary stream)."""
        return self._primary.N

    @property
    def underruns(self):
        total = self._primary.underruns
        if self._monitor is not None:
            total += self._monitor.underruns
        return total

    def start(self):
        self._primary.start()
        if self._monitor is not None:
            self._monitor.start()

    def stop(self):
        self._primary.stop()
        if self._monitor is not None:
            self._monitor.stop()

    def reset(self):
        self._primary.reset()
        if self._monitor is not None:
            self._monitor.reset()

    def fill(self):
        return self._primary.fill()

    def drain(self, timeout=5.0):
        self._primary.drain(timeout)
        if self._monitor is not None:
            self._monitor.drain(timeout)

    def write(self, left, right):
        data = np.empty((len(left), self.channels), dtype=np.float32)
        data[:, 0] = left
        data[:, 1] = right
        np.clip(data, -1.0, 1.0, out=data)
        self._primary.write_array(data)
        if self._monitor is not None:
            self._monitor.write_array(data)


def list_devices() -> str:
    return str(sd.query_devices())


def default_output_name() -> str:
    try:
        return sd.query_devices(kind="output")["name"]
    except Exception:
        return "default"
