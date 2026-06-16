"""Stereo audio output via PortAudio (sounddevice) with a thread-safe ring buffer.

We send to the system default output device by default, so plugging in
headphones (or a USB DAC wired to the scope) routes the X/Y signal there
automatically. Left channel -> scope X, Right channel -> scope Y.
"""

from __future__ import annotations

import threading
import time

import numpy as np
import sounddevice as sd


class AudioOutput:
    def __init__(self, samplerate=48_000, device=None, channels=2,
                 buffer_seconds=0.5):
        self.samplerate = int(samplerate)
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
            device=device, callback=self._callback)

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
