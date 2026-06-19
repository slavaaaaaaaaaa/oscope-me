# oscope-me

Tune an FM stereo broadcast with an RTL-SDR or **Airspy HF+ / HF Discovery** —
**or play an audio file** — and send it to an **X/Y oscilloscope** as oscilloscope
music. A terminal app: pick a
station (or a file), and it streams stereo audio to your headphone/line out while
drawing a live X/Y preview in the terminal. Tune, change volume, swap files, and
flip options live from the keyboard.

![Terminal vectorscope](screenshot.png)

## How it works

Oscilloscope music is **stereo** audio: the Left channel drives the scope's
horizontal (X) deflection and Right drives the vertical (Y). The picture is
drawn by the *difference* between the channels, so the app does a proper FM
stereo multiplex decode:

```
SDR IQ (RTL-SDR or Airspy HF) ─▶ decimate ─▶ FM discriminator ─▶ MPX baseband
   ├─ lowpass 15 kHz ─────────────────────────▶ mono (L+R)
   └─ 19 kHz pilot ─(square)─▶ 38 kHz carrier ─▶ coherent decode ─▶ (L−R)
                                                  L = mono + (L−R)
                                                  R = mono − (L−R)
```

Plus 75 µs de-emphasis (50 µs available for Europe), a DC blocker to keep the
figure centred, and an equal-gain AGC that scales L and R together so the
*shape* of the image is preserved. Measured stereo separation is ~30 dB, so the
figures come through clean.

> Note: broadcast FM band-limits each channel to 15 kHz, so the finest detail in
> a piece of oscilloscope music is softened over the air — that's a property of
> the medium, not this app.

## Install

You need SDR command-line tools for at least one supported backend and Python
3.10+:

| Backend | Tools | Install |
|---------|-------|---------|
| RTL-SDR | `rtl_sdr`, `rtl_test` | `brew install librtlsdr` / `apt install rtl-sdr` |
| Airspy HF+ / HF Discovery | `airspyhf_rx`, `airspyhf_info` | `brew install airspyhf` / `apt install airspyhf` |

With **no device plugged in**, the app waits. When both an Airspy HF and an
RTL-SDR are connected, **Airspy is preferred** automatically. Override with
`--sdr-backend rtl` or `--sdr-backend airspyhf`.

**macOS (Apple Silicon or Intel):**
```bash
brew install librtlsdr            # RTL-SDR: rtl_sdr, rtl_test, ...
brew install airspyhf             # Airspy HF+: airspyhf_rx, airspyhf_info, ...
git clone <this repo> && cd oscope-me
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

**Linux (Debian/Ubuntu):**
```bash
sudo apt install rtl-sdr librtlsdr-dev airspyhf libportaudio2 make python3.14-venv pulseaudio ffmpeg
git clone <this repo> && cd oscope-me
make venv
make run
```

`make run` and `make play` automatically disable ALSA **Auto-Mute Mode** on Linux
so the headphone jack (scope) and built-in speakers (monitor) can play together.
Override the sound card if needed: `make run ALSA_CARD=1`.

Use `alsamixer` to control volume. To persist auto-mute disabled across reboots:
`sudo alsactl store` (after disabling Auto-Mute Mode manually once).

On Linux you may need udev rules so the SDR is usable without root. For
RTL-SDR, blacklist the DVB-T kernel driver:
```bash
echo 'blacklist dvb_usb_rtl28xxu' | sudo tee /etc/modprobe.d/blacklist-rtl.conf
```

For Airspy HF+, ensure your user is in the `plugdev` group (the `airspyhf`
package installs udev rules on Debian/Ubuntu):
```bash
sudo usermod -a -G plugdev "$USER"   # log out and back in
airspyhf_info                          # should list the receiver
```

## Usage

```bash
oscope-me                 # waits for SDR, then prompts for a frequency
oscope-me -f 102.5        # tune 102.5 MHz directly
oscope-me -i song.flac    # play a file (flac/mp3/wav/ogg/m4a/...) instead
oscope-me -i song.wav --no-loop   # play once instead of looping
oscope-me -f 102.5 --no-scope     # audio only, no terminal preview
oscope-me --list-audio            # list output devices
oscope-me -f 102.5 --audio-device "External Headphones"
oscope-me -f 102.5 --audio-rate 192000   # high-rate output for the DAC
oscope-me -f 102.5 --sdr-backend airspyhf   # force Airspy HF+
oscope-me -f 102.5 --sdr-backend rtl        # force RTL-SDR
```

In **SDR mode** it waits for a supported receiver (Airspy HF or RTL-SDR), asks
for a frequency (or uses `--default-freq`), then streams. Airspy HF uses a fixed
768 kS/s IQ rate; FM tuning (88–108 MHz) is within its 60–260 MHz VHF range.
Use `--audio-rate 48000` or `96000` with Airspy HF (44100 Hz is not compatible
with the fixed 768 kS/s decimation plan). In **file mode** (`-i FILE`) it decodes the file with ffmpeg and plays it straight
to X/Y (oscilloscope-music files are already stereo, so no FM decode is needed);
it loops by default. Either way, plug headphones / a line-out cable in and the
audio follows your system default output. Ctrl-C (or `q`) to quit.

### Live controls

The app is interactive — press keys while it's running:

| Key | Action |
|-----|--------|
| `+` / `-` | volume up / down |
| `?` or `h` | toggle the help overlay |
| `q` | quit |
| **SDR mode** | |
| `↑` / `↓` (or `.` / `,`) | tune ∓0.1 MHz |
| `→` / `←` (or `>` / `<`) | tune ∓1.0 MHz |
| `f` | type in a frequency |
| `g` | set tuner gain (dB or `auto`) |
| `p` | set ppm correction |
| `m` | mono / stereo toggle |
| `d` | cycle de-emphasis 75 → 50 → off |
| `space` | mute / unmute |
| `o` | switch to playing a file |
| **File mode** | |
| `space` | pause / resume |
| `→` / `←` (or `>` / `<`) | seek ∓10 s |
| `r` | restart from the beginning |
| `l` | loop on / off |
| `o` | open another file |
| `f` | switch to SDR tuning |

Changes that the radio/decoder bakes in (frequency, gain, mono, file, loop) take
effect with a quick restart; volume and mute are instant.

### Wiring to the oscilloscope

1. Set the scope to **X/Y mode**.
2. Laptop **Left** channel → scope **X** (horizontal) input.
3. Laptop **Right** channel → scope **Y** (vertical) input.
4. Use a shielded stereo cable; for a high `--audio-rate` keep it short and
   well shielded to avoid degradation.

Start with the scope's X and Y gains roughly equal, then trim to taste.

### Key options

| Flag | Meaning |
|------|---------|
| `-i, --input FILE` | Play an audio file instead of the SDR (needs ffmpeg). |
| `--loop` / `--no-loop` | Loop the file forever (default) or play once. |
| `-f, --freq MHz` | FM frequency. Omit to be prompted. |
| `-g, --gain dB` | Tuner gain, or `auto` (default). |
| `--audio-rate Hz` | Output sample rate: 48000 (default), 96000, 192000. |
| `--deemphasis` | `75` (Americas, default), `50` (Europe), or `off`. |
| `--mono` | Force mono — collapses the X/Y image to a diagonal line. |
| `-p, --ppm` | Tuner frequency correction in ppm (RTL-SDR only). |
| `--sdr-backend` | `auto` (default, prefer Airspy HF), `rtl`, or `airspyhf`. |
| `--no-scope` | Skip the terminal preview (audio only). |
| `--low-power` | Reduce CPU: lower FPS, smaller buffers, lower SDR rate. |
| `--volume` | Output level multiplier (default 0.02). |
| `--dual-analog` | Linux: disable ALSA auto-mute (done automatically by `make run`/`make play`). |
| `--monitor-device` | Second output device when speaker and headphone are separate sinks. |

## Ubuntu: scope on jack, monitor on speakers

On Ubuntu laptops, plugging in headphones usually mutes the built-in speakers
(ALSA auto-mute). For oscilloscope use you want **both**: jack → scope X/Y,
speakers → audio monitor.

**Single command (recommended):**

```bash
make run                  # SDR — dual analog enabled on Linux
make play FILE=song.flac  # file mode
```

**Direct CLI** (without make):

```bash
oscope-me -f 102.5 --dual-analog
```

**Separate speaker/headphone sinks** (USB speakers, some desktops):

```bash
make run ARGS='--monitor-device "Built-in Audio Analog Stereo"'
oscope-me --list-audio    # list device names
```

If auto-mute keeps re-enabling after reboot, run `alsamixer` → F6 pick card →
disable **Auto-Mute Mode** → unmute **Speaker** → `sudo alsactl store`. Wrong
card index: `make run ALSA_CARD=1`.

## Slow computers

The terminal scope redraw and SDR decode can be heavy on older laptops. Try:

```bash
oscope-me --low-power 100.1
oscope-me --fps 15 --sample-rate 960000 100.1
oscope-me --no-scope 100.1
oscope-me --low-power --audio-buffer 2.0 100.1
```

`--low-power` lowers scope FPS to 15, picks a 960 kHz SDR rate, uses a smaller
scope buffer, and reads the SDR in larger blocks. `--no-scope` skips the Braille
preview entirely (audio only). Increase `--audio-buffer` if you hear crackling.

## Troubleshooting

- **"No SDR tools found"** — install `rtl-sdr` and/or `airspyhf` (see above).
- **Stuck on "Waiting for SDR…"** — run `airspyhf_info` or `rtl_test` to confirm
  the device is visible; on Linux check udev/`plugdev` (Airspy) and the RTL
  DVB-T driver blacklist.
- **`mono ○` instead of `STEREO ●`** — the station has no 19 kHz pilot or the
  signal is weak. Try a stronger station, a better antenna, or set `-g` manually.
- **Crackling / `underruns` climbing** — increase `--audio-buffer` (e.g. `2.0`).
- **No sound on headphones (Linux)** — check the status bar `out:` line; if it
  shows a raw `hw:…` device, try `oscope-me --audio-device pulse` (or
  `--list-audio` and pick the PulseAudio/PipeWire entry). Plug headphones in
  *before* starting, or restart after plugging in — the output device is opened
  once at launch. Also check `alsamixer` (Headphone channel not muted) and press
  `+` a few times (default volume is very low).
- **Speakers silent with headphones plugged in (Linux)** — use `make run` / `make play`
  (disables ALSA auto-mute), or `oscope-me --dual-analog`. See
  [Ubuntu: scope on jack, monitor on speakers](#ubuntu-scope-on-jack-monitor-on-speakers).

## Tests

```bash
python tests/test_dsp.py   # synthesises an FM stereo signal and checks separation
```

## Requirements

- Python 3.10+ with `numpy`, `scipy`, `sounddevice` (installed via `pip install -e .`)
- For **SDR mode**: CLI tools for at least one backend — **RTL-SDR**
  (`rtl-sdr` package + RTL2832U dongle) or **Airspy HF+ / HF Discovery**
  (`airspyhf` package). Auto-detect prefers Airspy when both are plugged in.
- For **file mode**: `ffmpeg` (`brew install ffmpeg` / `apt install ffmpeg`) — only
  needed if you use `-i`
