"""Pure-logic tests for the Windows console VT mode computation (run on any OS)."""

from __future__ import annotations

from jp.commands import terminal as t


def test_raw_input_mode_clears_cooked_and_sets_vt():
    # Start from the default cooked input mode (line + echo + processed + others).
    old = (
        t._ENABLE_LINE_INPUT | t._ENABLE_ECHO_INPUT | t._ENABLE_PROCESSED_INPUT | 0x0008  # window
    )
    new = t._raw_console_input_mode(old)
    assert not new & t._ENABLE_LINE_INPUT
    assert not new & t._ENABLE_ECHO_INPUT
    assert not new & t._ENABLE_PROCESSED_INPUT  # Ctrl-C is forwarded, not handled locally
    assert new & t._ENABLE_VIRTUAL_TERMINAL_INPUT  # keys arrive as VT sequences
    assert new & 0x0008  # unrelated flags preserved


def test_vt_output_mode_sets_processing():
    old = 0x0002  # ENABLE_WRAP_AT_EOL_OUTPUT
    new = t._vt_console_output_mode(old)
    assert new & t._ENABLE_PROCESSED_OUTPUT
    assert new & t._ENABLE_VIRTUAL_TERMINAL_PROCESSING
    assert new & 0x0002  # preserved


def test_console_size_default_on_error(monkeypatch):
    import os as _os

    def _boom(fd):
        raise OSError

    monkeypatch.setattr(_os, "get_terminal_size", _boom)
    assert t._console_size(1) == (80, 24)
