"""Terminal X/Y preview of the stereo signal on a Unicode Braille canvas.

Each character cell packs a 2x4 grid of dots, so an 80x40 terminal gives a
160x160 dot raster. Left channel -> X, Right channel -> Y, matching how the
real analog scope draws it in X/Y mode.
"""

from __future__ import annotations

import threading

import numpy as np

# Braille dot bit for [row 0..3][col 0..1], base codepoint U+2800.
_DOT = np.array([[0x01, 0x08],
                 [0x02, 0x10],
                 [0x04, 0x20],
                 [0x40, 0x80]], dtype=np.uint8)

_BRAILLE_CHARS = np.array([chr(0x2800 + i) for i in range(256)])

GREEN = "\x1b[38;5;46m"
DIM = "\x1b[2m"
RESET = "\x1b[0m"
HOME = "\x1b[H"
CLEAR = "\x1b[2J"
HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"
ALT_SCREEN_ON = "\x1b[?1049h"
ALT_SCREEN_OFF = "\x1b[?1049l"


class BrailleScope:
    def __init__(self, history=8192, max_npoints=2400):
        self.history = history
        self.max_npoints = max_npoints
        self.bufL = np.zeros(history, dtype=np.float32)
        self.bufR = np.zeros(history, dtype=np.float32)
        self.pos = 0
        self.filled = 0
        self.lock = threading.Lock()
        self.scale = 1.0

    def push(self, left, right):
        n = len(left)
        with self.lock:
            if n >= self.history:
                self.bufL[:] = left[-self.history:]
                self.bufR[:] = right[-self.history:]
                self.pos = 0
                self.filled = self.history
                return
            end = self.pos + n
            if end <= self.history:
                self.bufL[self.pos:end] = left
                self.bufR[self.pos:end] = right
            else:
                k = self.history - self.pos
                self.bufL[self.pos:] = left[:k]
                self.bufR[self.pos:] = right[:k]
                self.bufL[:n - k] = left[k:]
                self.bufR[:n - k] = right[k:]
            self.pos = end % self.history
            self.filled = min(self.history, self.filled + n)

    def _snapshot(self, npoints):
        with self.lock:
            if self.filled < self.history:
                lo = max(0, self.filled - npoints)
                return self.bufL[lo:self.filled].copy(), self.bufR[lo:self.filled].copy()
            count = min(npoints, self.history)
            idx = (self.pos - count + np.arange(count)) % self.history
            return self.bufL[idx].copy(), self.bufR[idx].copy()

    def render(self, cols, rows, npoints=2400, status_top="", status_bottom=""):
        dot_cols = cols * 2
        dot_rows = rows * 4
        npoints = min(npoints, self.max_npoints, dot_cols * dot_rows * 2)
        left, right = self._snapshot(npoints)
        grid = np.zeros((rows, cols), dtype=np.uint8)

        if len(left):
            peak = float(max(np.max(np.abs(left)), np.max(np.abs(right)), 1e-6))
            # Smoothly track peak so the trace fills the box without jitter.
            self.scale += 0.2 * (0.92 / peak - self.scale)
            x = np.clip(left * self.scale, -1.0, 1.0)
            y = np.clip(right * self.scale, -1.0, 1.0)
            px = ((x + 1.0) * 0.5 * (dot_cols - 1)).astype(np.intp)
            py = ((1.0 - (y + 1.0) * 0.5) * (dot_rows - 1)).astype(np.intp)
            cx = px // 2
            cy = py // 4
            bits = _DOT[py % 4, px % 2]
            np.bitwise_or.at(grid, (cy, cx), bits)

        lines = ["".join(_BRAILLE_CHARS[row]) for row in grid]

        out = [HOME]
        if status_top:
            out.append(GREEN + status_top.ljust(cols)[:cols] + RESET + "\n")
        out.append(GREEN)
        out.append("\n".join(lines))
        out.append(RESET)
        if status_bottom:
            out.append("\n" + DIM + status_bottom.ljust(cols)[:cols] + RESET)
        return "".join(out)
