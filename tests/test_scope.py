"""Unit tests for BrailleScope rendering limits."""

import numpy as np

from oscope_me.scope import BrailleScope


def test_npoints_capped_by_max_npoints_and_terminal_size():
    scope = BrailleScope(max_npoints=100)
    n = 200
    scope.push(np.linspace(-1, 1, n, dtype=np.float32),
               np.linspace(1, -1, n, dtype=np.float32))

    cols, rows = 10, 5
    dot_cap = cols * 2 * rows * 4 * 2
    expected = min(100, dot_cap)

    captured = []
    original = scope._snapshot

    def capture(npoints):
        captured.append(npoints)
        return original(npoints)

    scope._snapshot = capture
    scope.render(cols, rows, npoints=2400)
    assert captured == [expected]


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
