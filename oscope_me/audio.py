"""Stereo audio output via PortAudio (sounddevice) with a thread-safe ring buffer.

Left channel -> scope X, Right channel -> scope Y.

On macOS the system default follows headphone hot-plug. On Linux, PortAudio often
opens a raw ALSA PCM device that does not; when no --audio-device is given we
prefer routing through PulseAudio/PipeWire so jack switching works.
"""

from __future__ import annotations

import sys
import threading
import time

import numpy as np
import sounddevice as sd

# Substrings matched against PortAudio output device names (case-insensitive).
_LINUX_OUTPUT_PREFER = ("pulse", "pipewire", "default")


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


class AudioOutput:
    def __init__(self, samplerate=48_000, device=None, channels=2,
                 buffer_seconds=0.5):
        self.samplerate = int(samplerate)
        self.device = resolve_output_device(device)
        self.channels = channels
        self.N = max(1, int(self.samplerate * buffer_seconds))
        self.buf = np.zeros((self.N, channels), dtype=np.float32)
        self.r = 0
        self.w = 0
        self.count = 0
        self.lock = threading.Lock()
        self.underruns = 0
        # Output silence until the buffer first fills to ~40%, so playback
        # starts smoothly instead of stuttering while the SDR spins up.
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
        """Clear the buffer and counters for a fresh streaming session."""
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
        """Frames currently buffered (0..N)."""
        with self.lock:
            return self.count

    def drain(self, timeout=5.0):
        """Block until the buffer has played out (or timeout), for clean exit."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                if self.count <= 0:
                    return
            time.sleep(0.02)

    def write(self, left, right):
        data = np.empty((len(left), self.channels), dtype=np.float32)
        data[:, 0] = left
        data[:, 1] = right
        np.clip(data, -1.0, 1.0, out=data)
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
            if self.count > self.N:        # overflow: drop oldest
                over = self.count - self.N
                self.r = (self.r + over) % self.N
                self.count = self.N


def list_devices() -> str:
    return str(sd.query_devices())


def default_output_name() -> str:
    try:
        return sd.query_devices(kind="output")["name"]
    except Exception:
        return "default"
