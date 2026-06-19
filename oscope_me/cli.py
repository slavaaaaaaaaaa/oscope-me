"""Command-line entry point."""

from __future__ import annotations

import argparse
import sys
from types import SimpleNamespace


def build_parser():
    p = argparse.ArgumentParser(
        prog="oscope-me",
        description="Tune an FM stereo broadcast with an RTL-SDR and play it as "
                    "X/Y oscilloscope music. Left channel = scope X, Right = Y.")
    p.add_argument("-i", "--input", default=None, metavar="FILE",
                   help="Play an audio file (wav/flac/mp3/ogg/m4a/...) as X/Y "
                        "music instead of tuning the SDR. Needs ffmpeg.")
    p.add_argument("--loop", dest="loop", action="store_true", default=True,
                   help="Loop the input file forever (default for files).")
    p.add_argument("--no-loop", dest="loop", action="store_false",
                   help="Play the input file once, then stop.")
    p.add_argument("-f", "--freq", type=float, default=None, metavar="MHz",
                   help="FM frequency in MHz (default: --default-freq, 100.1).")
    p.add_argument("--default-freq", type=float, default=100.1, metavar="MHz",
                   help="Starting FM frequency when -f is omitted (default 100.1).")
    p.add_argument("-g", "--gain", default="auto", metavar="dB",
                   help="Tuner gain in dB, or 'auto' (default).")
    p.add_argument("-p", "--ppm", type=int, default=0,
                   help="Frequency correction in ppm (default 0).")
    p.add_argument("-s", "--sample-rate", type=int, default=None, metavar="Hz",
                   help="SDR sample rate. Default is chosen automatically.")
    p.add_argument("--audio-rate", type=int, default=48_000, metavar="Hz",
                   help="Audio output sample rate (e.g. 48000, 96000, 192000).")
    p.add_argument("--deemphasis", default="75",
                   help="De-emphasis time constant: 75 (Americas), 50 (Europe), "
                        "or off (default 75).")
    p.add_argument("--volume", type=float, default=0.02,
                   help="Output level multiplier (default 0.02).")
    p.add_argument("--mono", action="store_true",
                   help="Force mono decode (collapses the X/Y image to a line).")
    p.add_argument("--device-index", type=int, default=0,
                   help="RTL-SDR device index (default 0).")
    p.add_argument("--sdr-backend", default="auto", choices=("auto", "rtl", "airspyhf"),
                   help="SDR backend: auto (prefer Airspy HF, else RTL-SDR), "
                        "rtl, or airspyhf (default auto).")
    p.add_argument("--audio-device", default=None,
                   help="Output device name or index (default: system default).")
    p.add_argument("--monitor-device", default=None,
                   help="Second output device for monitoring (Linux: when speaker "
                        "and headphone are separate sinks).")
    p.add_argument("--dual-analog", action="store_true",
                   help="Linux: disable ALSA auto-mute so jack and speakers play "
                        "together (make run/play does this automatically).")
    p.add_argument("--audio-buffer", type=float, default=1.0, metavar="SEC",
                   help="Audio ring-buffer size in seconds (default 1.0).")
    p.add_argument("--fps", type=float, default=40.0,
                   help="Scope redraw rate (default 40).")
    p.add_argument("--low-power", action="store_true",
                   help="Reduce CPU use: lower scope FPS, smaller buffers, "
                        "lower SDR sample rate.")
    p.add_argument("--no-scope", action="store_true",
                   help="Audio only; skip the terminal X/Y preview.")
    p.add_argument("--list-audio", action="store_true",
                   help="List audio output devices and exit.")
    return p


def _parse_audio_device(value):
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return value


def _parse_deemphasis(value):
    v = str(value).strip().lower()
    if v in ("off", "none", "0"):
        return 0
    if v in ("75", "75us"):
        return 75
    if v in ("50", "50us"):
        return 50
    raise SystemExit("--deemphasis must be 75, 50, or off")


def main(argv=None):
    args = build_parser().parse_args(argv)
    fps_explicit = "--fps" in (argv if argv is not None else sys.argv)

    if args.list_audio:
        from .audio import list_devices
        print(list_devices())
        return 0

    audio_device = _parse_audio_device(args.audio_device)
    monitor_device = _parse_audio_device(args.monitor_device)

    input_file = None
    if args.input is not None:
        import os
        input_file = os.path.expanduser(args.input)

    cfg = SimpleNamespace(
        mode="file" if input_file else "sdr",
        input_file=input_file,
        loop=args.loop,
        freq=args.freq,
        default_freq=args.default_freq,
        gain=args.gain,
        ppm=args.ppm,
        sample_rate=args.sample_rate,
        audio_rate=args.audio_rate,
        deemphasis=_parse_deemphasis(args.deemphasis),
        volume=args.volume,
        mono=args.mono,
        device_index=args.device_index,
        sdr_backend=args.sdr_backend,
        audio_device=audio_device,
        monitor_device=monitor_device,
        dual_analog=args.dual_analog,
        audio_buffer=args.audio_buffer,
        fps=args.fps,
        fps_explicit=fps_explicit,
        low_power=args.low_power,
        no_scope=args.no_scope,
    )

    from .app import run
    try:
        return run(cfg)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
