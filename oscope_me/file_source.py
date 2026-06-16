"""Play an audio file as oscilloscope music by decoding it through ffmpeg.

Oscilloscope music is already stereo audio (Left = scope X, Right = Y), so a
file source skips the FM demodulator entirely: we just decode the file to
stereo float32 PCM at the audio rate and hand the L/R blocks straight to the
audio output and the scope.

ffmpeg is used as a subprocess (the same CLI-not-bindings approach as the SDR
path), so we get wav / flac / mp3 / ogg / m4a / aac and anything else ffmpeg
can read, identically on macOS and Linux.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading

import numpy as np

AUDIO_EXTS = {".wav", ".flac", ".mp3", ".ogg", ".oga", ".m4a", ".aac",
              ".opus", ".aiff", ".aif", ".wma", ".alac"}


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def install_hint() -> str:
    return ("ffmpeg not found (needed to play files). Install it:\n"
            "  macOS:  brew install ffmpeg\n"
            "  Debian/Ubuntu:  sudo apt install ffmpeg\n"
            "  Arch:  sudo pacman -S ffmpeg")


def probe_duration(path) -> float | None:
    """Return the file's duration in seconds via ffprobe, or None if unknown."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nokey=1:noprint_wrappers=1", str(path)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            timeout=5.0)
        return float(out.stdout.strip())
    except (ValueError, subprocess.SubprocessError):
        return None


class FileSource:
    """Decode `path` to stereo float32 @ `fs` and yield (left, right) blocks.

    Mirrors the iterate-blocks shape of RtlSdrSource, but its blocks are already
    demodulated audio: each is a (left, right) tuple of float32 arrays.
    """

    def __init__(self, path, fs, block_seconds=0.05, loop=True):
        self.path = str(path)
        self.fs = int(fs)
        self.loop = bool(loop)
        self.block = max(1, int(self.fs * block_seconds))  # frames per block
        self.nbytes = self.block * 2 * 4                   # 2 ch * float32
        self.duration = probe_duration(self.path)
        self.proc = None
        self.stderr_tail: list[str] = []
        self._stderr_thread = None

    def start(self):
        if not os.path.exists(self.path):
            raise RuntimeError(f"file not found: {self.path}")
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error"]
        if self.loop:
            cmd += ["-stream_loop", "-1"]      # loop the input forever
        cmd += ["-i", self.path,
                "-f", "f32le", "-acodec", "pcm_f32le",
                "-ac", "2", "-ar", str(self.fs), "-"]
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
        """Yield (left, right) float32 arrays until the stream ends."""
        read = self.proc.stdout.read
        need = self.nbytes
        dt = np.dtype("<f4")
        buf = bytearray()
        while True:
            chunk = read(need - len(buf))
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) < need:
                continue
            frames = np.frombuffer(bytes(buf[:need]), dtype=dt)
            del buf[:need]
            yield frames[0::2].copy(), frames[1::2].copy()
        # Flush any final partial block (drop a dangling half-frame).
        if len(buf) >= 8:
            tail = np.frombuffer(bytes(buf[: (len(buf) // 8) * 8]), dtype=dt)
            if len(tail):
                yield tail[0::2].copy(), tail[1::2].copy()

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
