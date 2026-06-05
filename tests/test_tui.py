"""Interactive TUI logic, driven by scripted key streams (no real terminal)."""

from __future__ import annotations

import pytest

from jp import tui


class FakeReader:
    """A context manager yielding scripted keys, standing in for the raw reader."""

    def __init__(self, keys):
        self._keys = list(keys)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def read_key(self) -> str:
        if self._keys:
            return self._keys.pop(0)
        return ""  # EOF -> treated as cancel/esc


def _bool_setting(value=False):
    return tui.Setting("mirror", "Mirror mode", value, options=[False, True], help_text="help here")


# --- Setting --------------------------------------------------------------
def test_setting_cycle_bool():
    s = _bool_setting(False)
    assert s.display() == "false"
    s.cycle()
    assert s.value is True
    assert s.changed is True
    s.cycle()
    assert s.value is False
    assert s.changed is False  # back to original


def test_setting_cycle_enum():
    s = tui.Setting("color", "Color", "auto", options=["auto", "always", "never"])
    s.cycle()
    assert s.value == "always"
    s.cycle(-1)
    assert s.value == "auto"


# --- settings_menu --------------------------------------------------------
def test_menu_toggle_and_save():
    s = _bool_setting(False)
    result = tui.settings_menu([s], _reader=FakeReader(["space", "enter"]))
    assert result is not None
    assert s.value is True


def test_menu_cancel_returns_none():
    s = _bool_setting(False)
    result = tui.settings_menu([s], _reader=FakeReader(["space", "esc"]))
    assert result is None  # cancel; caller discards mutated values


def test_menu_navigation_down_then_toggle():
    a = tui.Setting("a", "A", False, options=[False, True])
    b = tui.Setting("b", "B", False, options=[False, True])
    tui.settings_menu([a, b], _reader=FakeReader(["down", "space", "enter"]))
    assert a.value is False
    assert b.value is True


def test_menu_search_filters_then_toggles():
    a = tui.Setting("mirror", "Mirror mode", False, options=[False, True])
    b = tui.Setting("color", "Colored output", "auto", options=["auto", "never"])
    keys = ["/", "c", "o", "l", "enter", "space", "enter"]
    tui.settings_menu([a, b], _reader=FakeReader(keys))
    assert a.value is False  # untouched
    assert b.value == "never"


def test_menu_requires_terminal_without_reader(monkeypatch):
    monkeypatch.setattr(tui, "interactive", lambda *a, **k: False)
    with pytest.raises(RuntimeError):
        tui.settings_menu([_bool_setting()])


# --- confirm_deletions (mirror-mode safety) -------------------------------
def test_confirm_default_keeps_everything_on_enter():
    # No toggles -> everything stays "keep" -> nothing returned for deletion.
    sel = tui.confirm_deletions(["a.txt", "b.txt"], "remote", _reader=FakeReader(["enter"]))
    assert sel == []


def test_confirm_toggle_one_then_enter():
    sel = tui.confirm_deletions(
        ["a.txt", "b.txt"], "remote", _reader=FakeReader(["space", "enter"])
    )
    assert sel == ["a.txt"]  # only the focused (first) item marked


def test_confirm_delete_all():
    sel = tui.confirm_deletions(["a.txt", "b.txt"], "local", _reader=FakeReader(["a", "enter"]))
    assert sel == ["a.txt", "b.txt"]


def test_confirm_none_after_marking():
    # 'n' clears all marks -> deletes nothing even after toggling.
    sel = tui.confirm_deletions(
        ["a.txt", "b.txt"], "remote", _reader=FakeReader(["space", "n", "enter"])
    )
    assert sel == []


def test_confirm_esc_cancels_keeps_all():
    sel = tui.confirm_deletions(["a.txt"], "remote", _reader=FakeReader(["space", "esc"]))
    assert sel == []  # esc cancels -> delete nothing


def test_confirm_empty_returns_empty():
    assert tui.confirm_deletions([], "remote") == []


# --- select_access (mount writable/read-only + remember) ------------------
_MOUNT = "privado/jp-live-test -> ~/Downloads/test/jp-live-test"


def test_access_down_then_enter_picks_readonly():
    # Move from Writable to Read-only, confirm without remembering.
    res = tui.select_access(_MOUNT, default_writable=True, _reader=FakeReader(["down", "enter"]))
    assert res == (False, False)


def test_access_enter_immediately_keeps_writable_default():
    res = tui.select_access(_MOUNT, default_writable=True, _reader=FakeReader(["enter"]))
    assert res == (True, False)


def test_access_space_then_enter_remembers():
    res = tui.select_access(_MOUNT, default_writable=True, _reader=FakeReader(["space", "enter"]))
    assert res == (True, True)


def test_access_default_readonly_start():
    # Highlight starts on Read-only when default_writable is False.
    res = tui.select_access(_MOUNT, default_writable=False, _reader=FakeReader(["enter"]))
    assert res == (False, False)


def test_access_esc_cancels():
    assert tui.select_access(_MOUNT, _reader=FakeReader(["esc"])) is None


def test_access_q_cancels():
    assert tui.select_access(_MOUNT, _reader=FakeReader(["q"])) is None


def test_access_renders_title_and_labels(capsys):
    tui.select_access(_MOUNT, _reader=FakeReader(["enter"]))
    out = capsys.readouterr().out
    assert _MOUNT in out
    assert "Writable" in out
    assert "Read-only" in out
    assert "Remember my choice" in out


def test_access_requires_terminal_without_reader(monkeypatch):
    monkeypatch.setattr(tui, "interactive", lambda *a, **k: False)
    with pytest.raises(RuntimeError):
        tui.select_access(_MOUNT)


# --- select_one / select_one_remember ------------------------------------


def test_select_one_enter_returns_index():
    assert tui.select_one(["a", "b", "c"], _reader=FakeReader(["down", "enter"])) == 1


def test_select_one_esc_returns_none():
    assert tui.select_one(["a", "b"], _reader=FakeReader(["esc"])) is None


def test_select_one_ignores_remember_key():
    # 'r' is not special for the plain picker -- it is ignored, not a selection.
    assert tui.select_one(["a", "b"], _reader=FakeReader(["r", "enter"])) == 0


def test_select_one_remember_enter_picks_once():
    assert tui.select_one_remember(["a", "b"], _reader=FakeReader(["down", "enter"])) == (1, False)


def test_select_one_remember_r_picks_and_remembers():
    assert tui.select_one_remember(["a", "b"], _reader=FakeReader(["down", "r"])) == (1, True)


def test_select_one_remember_esc_cancels():
    assert tui.select_one_remember(["a", "b"], _reader=FakeReader(["esc"])) == (None, False)
