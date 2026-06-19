"""SDR access by streaming raw IQ out of CLI tools (RTL-SDR or Airspy HF+).

Going through the CLI (rather than binding librtlsdr/libairspyhf in-process)
keeps us immune to ctypes/ABI drift across Python versions and behaves
identically on macOS and Linux.

  * rtl_sdr -       : interleaved unsigned-8-bit I/Q on stdout
  * airspyhf_rx -r stdout : interleaved float32 I/Q on stdout (768 kS/s)
"""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time

import numpy as np

_RTL_TOOLS = ("rtl_sdr", "rtl_test")
_AIRSPYHF_TOOLS = ("airspyhf_rx", "airspyhf_info")
_AIRSPYHF_FS = 768_000


def rtl_tools_available() -> bool:
    return all(shutil.which(t) for t in _RTL_TOOLS)


def airspyhf_tools_available() -> bool:
    return all(shutil.which(t) for t in _AIRSPYHF_TOOLS)


def tools_available() -> bool:
    return rtl_tools_available() or airspyhf_tools_available()


def backend_tools_available(backend: str) -> bool:
    if backend == "airspyhf":
        return airspyhf_tools_available()
    if backend == "rtl":
        return rtl_tools_available()
    return tools_available()


def install_hint() -> str:
    return ("No SDR tools found. Install at least one backend:\n"
            "  RTL-SDR:\n"
            "    macOS:  brew install librtlsdr\n"
            "    Debian/Ubuntu:  sudo apt install rtl-sdr\n"
            "  Airspy HF+ / HF Discovery:\n"
            "    macOS:  brew install airspyhf\n"
            "    Debian/Ubuntu:  sudo apt install airspyhf")


def rtl_device_present() -> bool:
    """True if at least one RTL2832 device is currently plugged in.

    Uses rtl_test, which prints 'Found N device(s):' to stderr and then starts
    benchmarking; we read just the count line and kill it.
    """
    if not rtl_tools_available():
        return False
    try:
        p = subprocess.Popen(["rtl_test"], stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE, text=True)
    except FileNotFoundError:
        return False

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


def airspyhf_device_present() -> bool:
    """True if at least one Airspy HF+ device is currently plugged in."""
    if not airspyhf_tools_available():
        return False
    try:
        p = subprocess.run(["airspyhf_info"], capture_output=True,
                             timeout=3.0, text=True)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return p.returncode == 0


def detect_backend(preference: str = "auto") -> str | None:
    """Return 'airspyhf' or 'rtl' when a device is present, else None.

    With preference 'auto', Airspy HF is checked first, then RTL-SDR.
    With preference 'airspyhf' or 'rtl', only that backend is probed.
    """
    if preference in ("auto", "airspyhf") and airspyhf_device_present():
        return "airspyhf"
    if preference in ("auto", "rtl") and rtl_device_present():
        return "rtl"
    return None


def device_present(preference: str = "auto") -> bool:
    return detect_backend(preference) is not None


def wait_for_device(poll=1.0, on_wait=None, preference: str = "auto"):
    """Block until a device is plugged in, calling on_wait() between polls."""
    waited = False
    while not device_present(preference):
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


class AirspyHfSource:
    """Airspy HF+ / HF Discovery via airspyhf_rx (768 kS/s float32 IQ)."""

    def __init__(self, freq_hz, fs, gain="auto", ppm=0,
                 block_seconds=0.1, device_index=0):
        del ppm, device_index  # not supported on Airspy HF
        self.freq = int(freq_hz)
        self.fs = int(fs)
        if self.fs != _AIRSPYHF_FS:
            raise ValueError(f"Airspy HF requires {_AIRSPYHF_FS} Hz sample rate")
        self.gain = gain
        self.block = max(1, int(self.fs * block_seconds))
        self.nbytes = self.block * 8                        # f32 I + f32 Q
        self.proc = None
        self.stderr_tail: list[str] = []
        self._stderr_thread = None

    def start(self):
        freq_mhz = self.freq / 1e6
        cmd = ["airspyhf_rx", "-r", "stdout",
               "-f", f"{freq_mhz}",
               "-a", str(_AIRSPYHF_FS)]
        if self.gain in (None, "auto", "Auto", "AUTO"):
            cmd.extend(["-g", "on"])
        else:
            att = max(0, min(8, int(round(float(self.gain) / 6.0))))
            cmd.extend(["-g", "off", "-t", str(att)])
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
        while True:
            chunk = read(need - len(buf))
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) < need:
                continue
            f32 = np.frombuffer(bytes(buf[:need]), dtype=np.float32)
            del buf[:need]
            yield (f32[0::2] + 1j * f32[1::2]).astype(np.complex64)

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
