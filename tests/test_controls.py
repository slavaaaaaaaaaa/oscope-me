"""Key-repeat suppression for volume keys."""

import pytest

from oscope_me.controls import RepeatFilter, VOLUME_KEYS


def test_volume_keys_defined():
    assert VOLUME_KEYS == frozenset({"+", "=", "-", "_"})


def test_single_press_passes_through():
    f = RepeatFilter()
    t = 1000.0
    assert f.filter("-", t) == "-"


def test_repeat_suppressed_while_held():
    f = RepeatFilter()
    t = 1000.0
    assert f.filter("-", t) == "-"
    assert f.filter("-", t + 0.05) is None
    assert f.filter("-", t + 0.10) is None


def test_new_press_after_release_gap():
    f = RepeatFilter(release_gap=0.15)
    t = 1000.0
    assert f.filter("-", t) == "-"
    assert f.filter("-", t + 0.05) is None
    assert f.filter(None, t + 0.20) is None
    assert f.filter("-", t + 0.20) == "-"


def test_different_volume_key_not_suppressed():
    f = RepeatFilter()
    t = 1000.0
    assert f.filter("-", t) == "-"
    assert f.filter("+", t + 0.05) == "+"


def test_non_volume_key_clears_held_state():
    f = RepeatFilter()
    t = 1000.0
    assert f.filter("-", t) == "-"
    assert f.filter("q", t + 0.05) == "q"
    assert f.filter("-", t + 0.06) == "-"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
