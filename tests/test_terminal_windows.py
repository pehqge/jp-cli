"""Pure-logic tests for the Windows console VT mode computation in jp.pty.

These run on any OS (no ctypes calls); the live console path is exercised
manually on Windows. The same backend powers both `jp terminal` and `jp run`.
"""

from __future__ import annotations

from jp import pty


def test_raw_input_mode_clears_cooked_and_sets_vt():
    old = pty._ENABLE_LINE_INPUT | pty._ENABLE_ECHO_INPUT | pty._ENABLE_PROCESSED_INPUT | 0x0008
    new = pty.raw_console_input_mode(old)
    assert not new & pty._ENABLE_LINE_INPUT
    assert not new & pty._ENABLE_ECHO_INPUT
    assert not new & pty._ENABLE_PROCESSED_INPUT  # Ctrl-C forwarded, not handled locally
    assert new & pty._ENABLE_VIRTUAL_TERMINAL_INPUT  # keys arrive as VT sequences
    assert new & 0x0008  # unrelated flags preserved


def test_vt_output_mode_sets_processing():
    old = 0x0002  # ENABLE_WRAP_AT_EOL_OUTPUT
    new = pty.vt_console_output_mode(old)
    assert new & pty._ENABLE_PROCESSED_OUTPUT
    assert new & pty._ENABLE_VIRTUAL_TERMINAL_PROCESSING
    assert new & 0x0002  # preserved


def test_console_size_default_on_error(monkeypatch):
    import os as _os

    monkeypatch.setattr(_os, "get_terminal_size", lambda fd: (_ for _ in ()).throw(OSError()))
    assert pty._console_size(1) == (80, 24)


def test_drive_dispatches_to_posix_or_windows(monkeypatch):
    calls = {}

    def _posix(ws, **k):
        calls["posix"] = k
        return 0

    def _win(ws, **k):
        calls["win"] = k
        return 1

    monkeypatch.setattr(pty, "drive_pty", _posix)
    monkeypatch.setattr(pty, "drive_pty_windows", _win)
    monkeypatch.setattr(pty, "HAS_PTY", True)
    assert pty.drive(object(), initial=["x"], scanner=None) == 0
    monkeypatch.setattr(pty, "HAS_PTY", False)
    assert pty.drive(object(), initial=["x"], scanner=None) == 1
    assert "posix" in calls and "win" in calls
