"""RTL-SDR access by streaming raw IQ out of the `rtl_sdr` CLI tool.

Going through the CLI (rather than binding librtlsdr in-process) keeps us immune
to ctypes/ABI drift across Python versions and behaves identically on macOS and
Linux. `rtl_sdr -` writes interleaved unsigned-8-bit I/Q to stdout.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time

import numpy as np

_TOOLS = ("rtl_sdr", "rtl_test")


def tools_available() -> bool:
    return all(shutil.which(t) for t in _TOOLS)


def install_hint() -> str:
    return ("rtl_sdr / rtl_test not found. Install them:\n"
            "  macOS:  brew install librtlsdr\n"
            "  Debian/Ubuntu:  sudo apt install rtl-sdr\n"
            "  Arch:  sudo pacman -S rtl-sdr")


def device_present() -> bool:
    """True if at least one RTL2832 device is currently plugged in.

    Uses rtl_test, which prints 'Found N device(s):' to stderr and then starts
    benchmarking; we read just the count line and kill it.
    """
    try:
        p = subprocess.Popen(["rtl_test"], stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE, text=True)
    except FileNotFoundError:
        raise RuntimeError(install_hint())

    found = False
    try:
        deadline = time.time() + 3.0
        for line in p.stderr:
            if "No supported devices" in line:
                found = False
                break
            m = re.search(r"Found (\d+) device", line)
            if m:
                found = int(m.group(1)) > 0
                break
            if time.time() > deadline:
                break
    finally:
        p.terminate()
        try:
            p.wait(timeout=1.0)
        except Exception:
            p.kill()
    return found


def wait_for_device(poll=1.0, on_wait=None):
    """Block until a device is plugged in, calling on_wait() between polls."""
    waited = False
    while not device_present():
        if on_wait:
            on_wait(waited)
        waited = True
        time.sleep(poll)


class RtlSdrSource:
    def __init__(self, freq_hz, fs, gain="auto", ppm=0,
                 block_seconds=0.1, device_index=0):
        self.freq = int(freq_hz)
        self.fs = int(fs)
        self.gain = gain
        self.ppm = int(ppm)
        self.device_index = int(device_index)
        self.block = max(1, int(self.fs * block_seconds))   # complex samples
        self.nbytes = self.block * 2                        # u8 I + u8 Q
        self.proc = None
        self.stderr_tail: list[str] = []
        self._stderr_thread = None

    def start(self):
        if self.gain in (None, "auto", "Auto", "AUTO"):
            g = "0"  # rtl_sdr: 0 => automatic gain
        else:
            g = str(int(round(float(self.gain))))
        cmd = ["rtl_sdr",
               "-f", str(self.freq),
               "-s", str(self.fs),
               "-g", g,
               "-p", str(self.ppm),
               "-d", str(self.device_index),
               "-"]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, bufsize=0)
        self._stderr_thread = threading.Thread(target=self._drain_stderr,
                                             daemon=True)
        self._stderr_thread.start()
        return cmd

    def _drain_stderr(self):
        for raw in iter(self.proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                self.stderr_tail.append(line)
                del self.stderr_tail[:-30]

    def blocks(self):
        """Yield complex64 IQ arrays of `self.block` samples until the stream ends."""
        read = self.proc.stdout.read
        need = self.nbytes
        buf = bytearray()
        scale = np.float32(1.0 / 127.5)
        while True:
            chunk = read(need - len(buf))
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) < need:
                continue
            u8 = np.frombuffer(bytes(buf[:need]), dtype=np.uint8).astype(np.float32)
            del buf[:need]
            u8 = (u8 - 127.5) * scale
            yield (u8[0::2] + 1j * u8[1::2]).astype(np.complex64)

    def stop(self):
        if self.proc is None:
            return
        try:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=1.0)
            except Exception:
                self.proc.kill()
        except Exception:
            pass
        self.proc = None
