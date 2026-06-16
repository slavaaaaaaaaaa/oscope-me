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
        # Escape sequence (arrow / page keys); grab the rest if it's there.
        seq = ""
        while True:
            r, _, _ = select.select([self.fd], [], [], 0.0008)
            if not r:
                break
            seq += self.stream.read(1)
            if seq[-1].isalpha() or seq[-1] == "~":
                break
        return _ESC_MAP.get(seq, "esc")

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
