"""Non-blocking single-key terminal input for live, in-app controls.

While the scope is drawing we want to read individual keypresses (tune up,
volume, mono toggle, ...) without waiting for Enter, and occasionally read a
whole typed line (a new frequency, a file path). This wraps a POSIX terminal in
cbreak mode and offers both, degrading gracefully to a no-op when stdin is not
a TTY (e.g. piped input or `--no-scope` in a script).
"""

from __future__ import annotations

import select
import sys
import time

try:
    import termios
    import tty
    _HAVE_TERMIOS = True
except ImportError:                       # non-POSIX; controls disabled
    _HAVE_TERMIOS = False

# Escape sequences -> friendly names.
_ESC_MAP = {
    "[A": "up", "[B": "down", "[C": "right", "[D": "left",
    "[5~": "pageup", "[6~": "pagedown",
    "OA": "up", "OB": "down", "OC": "right", "OD": "left",
}

# Max wait for the rest of an escape sequence after ESC (macOS can be slow).
_ESC_DEADLINE = 0.05


def _parse_escape(seq: str) -> str:
    """Map a terminal escape suffix to a key name."""
    if not seq:
        return "esc"
    if seq in _ESC_MAP:
        return _ESC_MAP[seq]
    # CSI with numeric / modifier prefixes: [1;2A, [27;5;53~, etc.
    if seq.startswith("["):
        last = seq[-1]
        if last in "ABCD":
            return _ESC_MAP["[" + last]
        if seq.endswith("~"):
            if seq.startswith("[5"):
                return "pageup"
            if seq.startswith("[6"):
                return "pagedown"
    # SS3 application cursor keys: OA, OB, ...
    if seq.startswith("O") and len(seq) == 2 and seq[1] in "ABCD":
        return _ESC_MAP[seq]
    return "esc"

VOLUME_KEYS = frozenset({"+", "=", "-", "_"})


class RepeatFilter:
    """Drop OS key-repeat for keys that should act once per physical press."""

    def __init__(self, keys=VOLUME_KEYS, release_gap=0.15):
        self._keys = keys
        self._release_gap = release_gap
        self._held_key = None
        self._held_since = 0.0

    def filter(self, key, now):
        if self._held_key is not None and (now - self._held_since) > self._release_gap:
            self._held_key = None
        if key is None:
            return None
        if key in self._keys:
            if key == self._held_key:
                return None
            self._held_key = key
            self._held_since = now
            return key
        self._held_key = None
        return key


class KeyReader:
    def __init__(self, stream=None):
        self.stream = stream or sys.stdin
        self.enabled = _HAVE_TERMIOS and self._isatty()
        self.fd = self.stream.fileno() if self.enabled else None
        self._old = None
        self._raw = False

    def _isatty(self):
        try:
            return self.stream.isatty()
        except Exception:
            return False

    def __enter__(self):
        if self.enabled:
            self._old = termios.tcgetattr(self.fd)
            self._set_raw()
        return self

    def __exit__(self, *exc):
        self.restore()

    def _set_raw(self):
        tty.setcbreak(self.fd)
        self._raw = True

    def restore(self):
        if self.enabled and self._old is not None and self._raw:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self._old)
            self._raw = False

    def get_key(self, timeout=0.0):
        """Return one key (a character, or a name like 'up'), or None.

        Waits up to `timeout` seconds for input; doubles as the frame pacer.
        """
        if not self.enabled:
            return None
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return None
        ch = self.stream.read(1)
        if ch != "\x1b":
            return ch
        # Escape sequence (arrow / page keys); wait for the full sequence.
        seq = ""
        deadline = time.monotonic() + _ESC_DEADLINE
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            r, _, _ = select.select([self.fd], [], [], remaining)
            if not r:
                break
            seq += self.stream.read(1)
            if seq and (seq[-1].isalpha() or seq[-1] == "~"):
                break
        return _parse_escape(seq)

    def read_line(self, prompt):
        """Drop to cooked mode, read a full line with echo, then restore.

        Returns the typed string (without newline), or None if cancelled/EOF.
        """
        if not self.enabled:
            return None
        self.restore()
        sys.stdout.write("\x1b[?25h")        # show cursor
        sys.stdout.flush()
        try:
            line = input(prompt)
        except (EOFError, KeyboardInterrupt):
            line = None
        finally:
            sys.stdout.write("\x1b[?25l")    # hide cursor
            sys.stdout.flush()
            self._set_raw()
        return line
