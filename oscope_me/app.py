"""Orchestration: drive an SDR or a file source into X/Y audio + scope, with
live in-app controls (tune, volume, mono, load a file, ...).

Two source kinds feed the same audio/scope sink:

  * SDR  : RTL-SDR or Airspy HF IQ -> FmStereoDemod -> (left, right)
  * file : ffmpeg-decoded stereo PCM -> (left, right) directly

A single key/render loop runs the show. Param changes that the DSP/source bake
in at construction (frequency, gain, mono, file, loop, ...) trigger a quick
session restart; volume and mute apply live.
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time

from . import scope as scope_mod
from .audio import AudioOutput
from .controls import KeyReader, RepeatFilter
from .dsp import FmStereoDemod, choose_rates, choose_rates_airspyhf
from .file_source import FileSource, ffmpeg_available
from .file_source import install_hint as ffmpeg_hint
from .sdr import (AirspyHfSource, RtlSdrSource, backend_tools_available,
                  detect_backend, device_present, install_hint, tools_available)


def _term_size():
    sz = shutil.get_terminal_size((100, 32))
    return max(20, sz.columns), max(10, sz.lines)


def prompt_frequency(default_mhz):
    while True:
        try:
            raw = input(f"FM frequency in MHz [{default_mhz}]: ").strip()
        except EOFError:
            return float(default_mhz)
        if not raw:
            return float(default_mhz)
        raw = raw.lower().replace("mhz", "").strip()
        try:
            f = float(raw)
        except ValueError:
            print("  please enter a number like 100.1")
            continue
        if f < 80 or f > 110:
            print("  that's outside the 88-108 MHz FM band; try again")
            continue
        return f


def _parse_freq(s):
    if s is None:
        return None
    s = s.lower().replace("mhz", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _mmss(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def _apply_low_power(cfg):
    if cfg.low_power and not cfg.fps_explicit:
        cfg.fps = 15
    cfg.target_fs_in = 960_000 if cfg.low_power else 1_200_000
    if cfg.low_power:
        cfg.scope_history = 4096
        cfg.scope_max_npoints = 1200
    else:
        cfg.scope_history = 8192
        cfg.scope_max_npoints = 2400
    cfg.sdr_block_seconds = 0.2 if cfg.low_power else 0.1


def _plan_sdr_rates(cfg, backend):
    if backend == "airspyhf":
        return choose_rates_airspyhf(cfg.audio_rate)
    return choose_rates(cfg.audio_rate, cfg.sample_rate,
                        target_fs_in=cfg.target_fs_in)


def _sdr_preference(cfg):
    return getattr(cfg, "sdr_backend", "auto")


def _gain_status(cfg):
    if getattr(cfg, "_sdr_backend", None) == "airspyhf":
        if cfg.gain in (None, "auto", "Auto", "AUTO"):
            return "gain auto (AGC)"
        att = max(0, min(8, int(round(float(cfg.gain) / 6.0))))
        return f"gain -{att * 6}dB (att)"
    return f"gain {cfg.gain}"


def _sdr_rate_label(cfg):
    fs_in = getattr(cfg, "_fs_in", 0)
    if getattr(cfg, "_sdr_backend", None) == "airspyhf":
        return f"Airspy HF {fs_in / 1e3:.0f}k"
    return f"RTL {fs_in / 1e6:.3f}M"


# --------------------------------------------------------------------------- #
# Session: one running source + (optional) demod feeding audio/scope.
# --------------------------------------------------------------------------- #

class _Session:
    def __init__(self, cfg, audio, scope):
        self.cfg = cfg
        self.audio = audio
        self.scope = scope
        self.source = None
        self.demod = None
        self.thread = None
        self._stop = threading.Event()
        self.ended = threading.Event()
        self.error = None

    def start(self):
        cfg = self.cfg
        if cfg.mode == "sdr":
            backend = cfg._sdr_backend or detect_backend(_sdr_preference(cfg))
            if backend is None:
                raise RuntimeError("no SDR device")
            fs_in, fs_mpx, d1, d2 = _plan_sdr_rates(cfg, backend)
            cfg._sdr_backend = backend
            cfg._fs_in = fs_in
            cfg._fs_mpx = fs_mpx
            self.demod = FmStereoDemod(fs_in, fs_mpx, d1, d2, cfg.audio_rate,
                                       deemphasis_us=cfg.deemphasis,
                                       stereo=not cfg.mono, volume=cfg.volume)
            if backend == "airspyhf":
                self.source = AirspyHfSource(int(cfg.freq * 1e6), fs_in,
                                             gain=cfg.gain,
                                             block_seconds=cfg.sdr_block_seconds)
            else:
                self.source = RtlSdrSource(int(cfg.freq * 1e6), fs_in,
                                           gain=cfg.gain, ppm=cfg.ppm,
                                           block_seconds=cfg.sdr_block_seconds,
                                           device_index=cfg.device_index)
        else:
            self.demod = None
            self.source = FileSource(cfg.input_file, cfg.audio_rate,
                                     loop=cfg.loop,
                                     start_offset_seconds=cfg.file_offset_seconds)
        self.audio.reset()
        cfg.frames_played = 0
        self._stop.clear()
        self.ended.clear()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        cfg = self.cfg
        try:
            self.source.start()
            for block in self.source.blocks():
                if self._stop.is_set():
                    break
                if self.demod is not None:
                    left, right = self.demod.process(block)
                else:
                    left, right = block
                # File playback can be paused; just hold here (back-pressure
                # stops the decoder until we resume).
                while cfg.paused and not self._stop.is_set():
                    time.sleep(0.05)
                if self._stop.is_set():
                    break
                if self.demod is None:
                    # ffmpeg decodes much faster than real time; wait for room in
                    # the ring buffer so playback (and the scope) run at speed.
                    room = self.audio.N - len(left)
                    while (not self._stop.is_set()
                           and self.audio.fill() > room):
                        time.sleep(0.004)
                    if self._stop.is_set():
                        break
                if cfg.muted:
                    left = left * 0.0
                    right = right * 0.0
                elif self.demod is None:
                    # SDR volume rides the demod's AGC; files get a plain gain.
                    left = left * cfg.volume
                    right = right * cfg.volume
                self.audio.write(left, right)
                if self.scope is not None:
                    self.scope.push(left, right)
                cfg.frames_played += len(left)
        except Exception as e:                       # surfaced in the status bar
            self.error = e
        finally:
            self.source.stop()
            self.ended.set()

    def stop(self):
        self._stop.set()
        if self.source is not None:
            self.source.stop()
        if self.thread is not None:
            self.thread.join(timeout=2.0)

    def apply_volume(self):
        if self.demod is not None:
            self.demod.set_volume(self.cfg.volume)


# --------------------------------------------------------------------------- #
# Key handling.
# --------------------------------------------------------------------------- #

# Volume step per +/- keypress.
_VOLUME_STEP = 0.02
_FILE_SEEK_STEP = 10.0


def _handle_key(key, cfg, keys, session):
    """Return (action, message). action: None | 'restart' | 'quit'."""
    if key in ("q",):
        return "quit", ""

    # Volume / mute work in any mode, without a restart.
    if key in ("+", "="):
        cfg.volume = round(min(8.0, cfg.volume + _VOLUME_STEP), 2)
        if session:
            session.apply_volume()
        return None, f"volume {cfg.volume:.2f}"
    if key == "_" or key == "-":
        cfg.volume = round(max(0.0, cfg.volume - _VOLUME_STEP), 2)
        if session:
            session.apply_volume()
        return None, f"volume {cfg.volume:.2f}"

    if cfg.mode == "sdr":
        return _handle_sdr_key(key, cfg, keys)
    return _handle_file_key(key, cfg, keys, session)


def _handle_sdr_key(key, cfg, keys):
    if key in ("up", "."):
        cfg.freq = round(cfg.freq + 0.1, 2)
        return "restart", f"{cfg.freq:.1f} MHz"
    if key in ("down", ","):
        cfg.freq = round(cfg.freq - 0.1, 2)
        return "restart", f"{cfg.freq:.1f} MHz"
    if key in ("right", ">"):
        cfg.freq = round(cfg.freq + 1.0, 2)
        return "restart", f"{cfg.freq:.1f} MHz"
    if key in ("left", "<"):
        cfg.freq = round(cfg.freq - 1.0, 2)
        return "restart", f"{cfg.freq:.1f} MHz"
    if key == "f":
        f = _parse_freq(keys.read_line(f"Frequency MHz [{cfg.freq:.1f}]: "))
        if f is None:
            return None, "cancelled"
        cfg.freq = f
        return "restart", f"{cfg.freq:.1f} MHz"
    if key == "g":
        val = keys.read_line(f"Gain dB or 'auto' [{cfg.gain}]: ")
        if val is None or not val.strip():
            return None, "cancelled"
        v = val.strip().lower()
        if v in ("auto", "a"):
            cfg.gain = "auto"
        else:
            try:
                cfg.gain = float(v)
            except ValueError:
                return None, "bad gain"
        return "restart", f"gain {cfg.gain}"
    if key == "p":
        if getattr(cfg, "_sdr_backend", None) == "airspyhf":
            return None, "ppm N/A on Airspy HF"
        val = keys.read_line(f"ppm correction [{cfg.ppm}]: ")
        try:
            cfg.ppm = int(val.strip())
        except (TypeError, ValueError, AttributeError):
            return None, "cancelled"
        return "restart", f"ppm {cfg.ppm}"
    if key == "m":
        cfg.mono = not cfg.mono
        return "restart", "mono" if cfg.mono else "stereo"
    if key == "d":
        cfg.deemphasis = {75: 50, 50: 0, 0: 75}[cfg.deemphasis]
        return "restart", f"de-emph {cfg.deemphasis or 'off'}"
    if key == " ":
        cfg.muted = not cfg.muted
        return None, "muted" if cfg.muted else "unmuted"
    if key == "o":
        return _open_file(cfg, keys)
    return None, ""


def _file_position(cfg):
    return cfg.file_offset_seconds + cfg.frames_played / cfg.audio_rate


def _seek_file(cfg, session, delta):
    current = _file_position(cfg)
    dur = session.source.duration if (session and session.source) else None
    if dur:
        new_pos = current + delta
        if cfg.loop:
            new_pos = new_pos % dur
        else:
            new_pos = max(0.0, min(dur, new_pos))
    else:
        new_pos = max(0.0, current + delta)
    cfg.file_offset_seconds = new_pos
    cfg.paused = False
    return "restart", f"seek {_mmss(new_pos)}"


def _handle_file_key(key, cfg, keys, session):
    if key in ("left", "<"):
        return _seek_file(cfg, session, -_FILE_SEEK_STEP)
    if key in ("right", ">"):
        return _seek_file(cfg, session, _FILE_SEEK_STEP)
    if key == " ":
        cfg.paused = not cfg.paused
        return None, "paused" if cfg.paused else "playing"
    if key == "l":
        cfg.loop = not cfg.loop
        return "restart", "loop on" if cfg.loop else "loop off"
    if key == "r":
        cfg.paused = False
        cfg.file_offset_seconds = 0.0
        return "restart", "restarted"
    if key == "o":
        return _open_file(cfg, keys)
    if key == "f":
        return _open_sdr(cfg, keys)
    return None, ""


def _open_file(cfg, keys):
    val = keys.read_line("File to play: ")
    if val is None or not val.strip():
        return None, "cancelled"
    path = os.path.expanduser(val.strip().strip('"').strip("'"))
    if not os.path.exists(path):
        return None, f"not found: {os.path.basename(path)}"
    if not ffmpeg_available():
        return None, "ffmpeg not installed"
    cfg.input_file = path
    cfg.mode = "file"
    cfg.paused = False
    cfg.file_offset_seconds = 0.0
    return "restart", f"playing {os.path.basename(path)}"


def _open_sdr(cfg, keys):
    if not tools_available():
        return None, "SDR tools not installed"
    default = cfg.freq or cfg.default_freq
    f = _parse_freq(keys.read_line(f"Tune SDR to MHz [{default:.1f}]: "))
    if f is None:
        f = default
    cfg.freq = f
    cfg.mode = "sdr"
    return "restart", f"tuning {f:.1f} MHz"


# --------------------------------------------------------------------------- #
# Rendering.
# --------------------------------------------------------------------------- #

def _status_lines(cfg, session, audio, waiting_sdr, status_msg, skipped_frames=0):
    if cfg.mode == "sdr":
        deemph = f"{cfg.deemphasis}us" if cfg.deemphasis else "off"
        top = (f"  oscope-me  {cfg.freq:.1f} MHz | "
               f"{_sdr_rate_label(cfg)} -> MPX {cfg._fs_mpx / 1e3:.0f}k "
               f"-> audio {cfg.audio_rate / 1e3:.1f}k | de-emph {deemph} "
               f"| {_gain_status(cfg)}")
        if waiting_sdr:
            state = "waiting for SDR…"
        elif session and session.demod is not None and session.demod.pilot_present:
            state = "STEREO ●"
        else:
            state = "mono ○"
        bottom = f"  {state}  {_vol(cfg)}  out: {audio.device_name}  underruns: {audio.underruns}"
    else:
        name = os.path.basename(cfg.input_file) if cfg.input_file else "(no file)"
        top = f"  oscope-me  ♪ {name} | audio {cfg.audio_rate / 1e3:.1f}k"
        elapsed = _file_position(cfg)
        dur = session.source.duration if (session and session.source) else None
        if dur:
            pos = elapsed % dur if cfg.loop else min(elapsed, dur)
            clock = f"{_mmss(pos)}/{_mmss(dur)}"
        else:
            clock = _mmss(elapsed)
        flags = []
        if cfg.finished:
            flags.append("ended")
        elif cfg.paused:
            flags.append("PAUSED")
        flags.append("loop" if cfg.loop else "once")
        bottom = f"  {clock}  {'  '.join(flags)}  {_vol(cfg)}  out: {audio.device_name}"

    if status_msg:
        bottom = f"  » {status_msg}"
    if skipped_frames > 0:
        bottom += f"  skipped: {skipped_frames}"
    bottom += "   (? help, q quit)"
    return top, bottom


def _vol(cfg):
    return f"vol {cfg.volume:.2f}" + ("  MUTED" if cfg.muted else "")


def _help_frame(cfg, cols, rows):
    common = [
        "  + / =      volume up",
        "  -          volume down",
        "  ? or h     toggle this help",
        "  q          quit",
    ]
    if cfg.mode == "sdr":
        mode = [
            "  up / .     tune +0.1 MHz       down / ,   tune -0.1 MHz",
            "  right / >  tune +1.0 MHz       left / <   tune -1.0 MHz",
            "  f          type a frequency    g          set gain (dB or auto)",
            "  p          set ppm             m          mono / stereo",
            "  d          de-emphasis 75/50/off",
            "  space      mute                o          play a file instead",
        ]
    else:
        mode = [
            "  space      pause / resume      r          restart from start",
            "  ← / >      seek −10 s / +10 s",
            "  l          loop on / off       o          open another file",
            "  f          switch to SDR tuning",
        ]
    text = (["", "  oscope-me — controls", "",
             f"  current source: {cfg.mode.upper()}", ""]
            + mode + [""] + ["  any mode:"] + common
            + ["", "  (press ? or h to return)"])
    padded = (text + [""] * rows)[:rows]
    body = "\n".join(s.ljust(cols)[:cols] for s in padded)
    return scope_mod.HOME + scope_mod.GREEN + body + scope_mod.RESET


def _draw(cfg, scope, session, audio, out, using_scope, waiting_sdr,
          show_help, status_msg, skipped_frames=0):
    top, bottom = _status_lines(cfg, session, audio, waiting_sdr, status_msg,
                                skipped_frames)
    if using_scope:
        cols, rows = _term_size()
        if show_help:
            frame = _help_frame(cfg, cols, rows)
        else:
            frame = scope.render(cols, rows - 2, status_top=top,
                                 status_bottom=bottom)
        out.write(frame)
        out.flush()
    else:
        line = (top.strip() + "  " + bottom.strip())[:118]
        out.write("\r" + line.ljust(118))
        out.flush()


# --------------------------------------------------------------------------- #
# Engine.
# --------------------------------------------------------------------------- #

def _engine(cfg, audio, scope, keys):
    out = sys.stdout
    interactive = keys.enabled
    using_scope = scope is not None
    frame = (1.0 / max(1.0, cfg.fps)) if using_scope else 0.25

    if using_scope:
        out.write(scope_mod.ALT_SCREEN_ON + scope_mod.HIDE_CURSOR + scope_mod.CLEAR)
        out.flush()

    session = None
    show_help = False
    waiting_sdr = False
    status_msg = ""
    last_render = 0.0
    last_devcheck = 0.0
    skipped_frames = 0
    vol_filter = RepeatFilter()

    def teardown():
        nonlocal session
        if session is not None:
            session.stop()
            session = None

    try:
        while True:
            now = time.monotonic()

            # Arm a session if none is running.
            if session is None and not cfg.finished:
                if cfg.mode == "sdr":
                    if now - last_devcheck >= 1.0:
                        last_devcheck = now
                        pref = _sdr_preference(cfg)
                        backend = detect_backend(pref)
                        if backend is not None:
                            cfg._sdr_backend = backend
                            try:
                                cfg._fs_in, cfg._fs_mpx, _, _ = _plan_sdr_rates(
                                    cfg, backend)
                            except ValueError as e:
                                status_msg = f"error: {e}"
                                waiting_sdr = True
                                continue
                            waiting_sdr = False
                            session = _Session(cfg, audio, scope)
                            session.start()
                        else:
                            waiting_sdr = True
                else:
                    session = _Session(cfg, audio, scope)
                    session.start()

            # Reap a session that ended on its own.
            if session is not None and session.ended.is_set():
                err = session.error
                teardown()
                if err is not None:
                    status_msg = f"error: {err}"
                if cfg.mode == "file" and not cfg.loop and err is None:
                    cfg.finished = True
                    if not interactive:
                        audio.drain()
                        break
                # SDR unplug (or transient): leave session None to re-arm.

            if now - last_render >= frame:
                overdue = int((now - last_render) / frame) - 1
                if overdue > 0:
                    skipped_frames += overdue
                t0 = time.monotonic()
                _draw(cfg, scope, session, audio, out, using_scope,
                      waiting_sdr, show_help, status_msg, skipped_frames)
                render_elapsed = time.monotonic() - t0
                if render_elapsed > frame:
                    skipped_frames += int(render_elapsed / frame) - 1
                    last_render = time.monotonic()
                else:
                    last_render = now

            key = keys.get_key(timeout=frame) if interactive else None
            key = vol_filter.filter(key, now)
            if key is None:
                if not interactive:
                    time.sleep(frame)
                continue

            if key in ("h", "?"):
                show_help = not show_help
                continue

            status_msg = ""
            action, msg = _handle_key(key, cfg, keys, session)
            if msg:
                status_msg = msg
            if action == "quit":
                teardown()
                return 0
            if action == "restart":
                teardown()
                cfg.finished = False
                last_devcheck = 0.0     # re-check device immediately
    except KeyboardInterrupt:
        return 0
    finally:
        teardown()
        if using_scope:
            out.write(scope_mod.SHOW_CURSOR + scope_mod.ALT_SCREEN_OFF)
            out.flush()


def run(cfg):
    _apply_low_power(cfg)

    # Runtime state added onto the static config.
    cfg.muted = False
    cfg.paused = False
    cfg.finished = False
    cfg.frames_played = 0
    cfg.file_offset_seconds = 0.0

    if cfg.mode == "file":
        if not ffmpeg_available():
            print(ffmpeg_hint(), file=sys.stderr)
            return 2
        if not os.path.exists(cfg.input_file):
            print(f"file not found: {cfg.input_file}", file=sys.stderr)
            return 2
    else:
        pref = _sdr_preference(cfg)
        if pref != "auto":
            if not backend_tools_available(pref):
                print(install_hint(), file=sys.stderr)
                return 2
        elif not tools_available():
            print(install_hint(), file=sys.stderr)
            return 2

    # Precompute SDR rates when possible (also used in the status bar).
    if cfg.mode == "sdr":
        pref = _sdr_preference(cfg)
        backend = detect_backend(pref) if pref == "auto" else pref
        if backend is None and pref != "auto":
            backend = pref
        try:
            if backend == "airspyhf":
                cfg._fs_in, cfg._fs_mpx, _, _ = choose_rates_airspyhf(
                    cfg.audio_rate)
            elif backend == "rtl":
                cfg._fs_in, cfg._fs_mpx, _, _ = choose_rates(
                    cfg.audio_rate, cfg.sample_rate,
                    target_fs_in=cfg.target_fs_in)
            else:
                cfg._fs_in, cfg._fs_mpx, _, _ = choose_rates(
                    cfg.audio_rate, cfg.sample_rate,
                    target_fs_in=cfg.target_fs_in)
            cfg._sdr_backend = backend if backend in ("rtl", "airspyhf") else None
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2

    # Ask for a starting frequency if we're on the SDR and weren't told one.
    if cfg.mode == "sdr" and cfg.freq is None:
        cfg.freq = (prompt_frequency(cfg.default_freq)
                    if sys.stdin.isatty() else cfg.default_freq)

    audio = AudioOutput(samplerate=cfg.audio_rate, device=cfg.audio_device,
                        buffer_seconds=cfg.audio_buffer,
                        monitor_device=getattr(cfg, "monitor_device", None),
                        dual_analog=getattr(cfg, "dual_analog", False))
    audio.start()
    try:
        with KeyReader() as keys:
            return _engine(cfg, audio,
                           scope_mod.BrailleScope(history=cfg.scope_history,
                                                  max_npoints=cfg.scope_max_npoints)
                           if not cfg.no_scope else None, keys)
    finally:
        audio.stop()
