"""credential_manager logic, driven by scripted key streams (no real terminal)."""

from __future__ import annotations

import types

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


def _cred(name, scope="global", site=""):
    return types.SimpleNamespace(name=name, scope=scope, site=site)


def _noop_delete(cred):
    return True


def _noop_set_site(cred, raw):
    return raw


def _noop_rename(cred, newname):
    return True


# --- delete ---------------------------------------------------------------
def test_delete_confirm_removes_cred():
    a = _cred("alpha")
    b = _cred("beta")
    creds = [a, b]
    calls = []

    def on_delete(cred):
        calls.append(cred)
        return True

    # d -> confirm prompt, y -> on_delete(alpha) True -> removed; q quits.
    tui.credential_manager(
        creds,
        on_delete=on_delete,
        on_set_site=_noop_set_site,
        on_rename=_noop_rename,
        _reader=FakeReader(["d", "y", "q"]),
    )
    assert calls == [a]
    assert creds == [b]


def test_delete_cancel_keeps_cred():
    a = _cred("alpha")
    creds = [a]
    calls = []

    def on_delete(cred):
        calls.append(cred)
        return True

    # d -> confirm, n -> abort; cred stays and on_delete never called.
    tui.credential_manager(
        creds,
        on_delete=on_delete,
        on_set_site=_noop_set_site,
        on_rename=_noop_rename,
        _reader=FakeReader(["d", "n", "q"]),
    )
    assert calls == []
    assert creds == [a]


def test_delete_callback_false_keeps_cred():
    a = _cred("alpha")
    creds = [a]

    # on_delete returns False -> TUI must not remove the cred.
    tui.credential_manager(
        creds,
        on_delete=lambda c: False,
        on_set_site=_noop_set_site,
        on_rename=_noop_rename,
        _reader=FakeReader(["d", "y", "q"]),
    )
    assert creds == [a]


# --- set site -------------------------------------------------------------
def test_set_site_updates_cred():
    target = "https://hub.example.com"
    a = _cred("alpha", site="")
    creds = [a]
    calls = []

    def on_set_site(cred, raw):
        calls.append((cred, raw))
        return target

    # s -> type "http", Enter commit; on_set_site returns origin -> cred.site set.
    keys = ["s", "h", "t", "t", "p", "enter", "q"]
    tui.credential_manager(
        creds,
        on_delete=_noop_delete,
        on_set_site=on_set_site,
        on_rename=_noop_rename,
        _reader=FakeReader(keys),
    )
    assert calls == [(a, "http")]
    assert a.site == target


def test_set_site_noop_when_callback_returns_empty():
    a = _cred("alpha", site="")
    creds = [a]

    keys = ["s", "x", "enter", "q"]
    tui.credential_manager(
        creds,
        on_delete=_noop_delete,
        on_set_site=lambda c, raw: "",
        on_rename=_noop_rename,
        _reader=FakeReader(keys),
    )
    assert a.site == ""


# --- rename ---------------------------------------------------------------
def test_rename_updates_cred_name():
    a = _cred("alpha")
    creds = [a]
    calls = []

    def on_rename(cred, newname):
        calls.append((cred, newname))
        return True

    # r -> type "beta", Enter; on_rename True -> cred.name updated.
    keys = ["r", "b", "e", "t", "a", "enter", "q"]
    tui.credential_manager(
        creds,
        on_delete=_noop_delete,
        on_set_site=_noop_set_site,
        on_rename=on_rename,
        _reader=FakeReader(keys),
    )
    assert calls == [(a, "beta")]
    assert a.name == "beta"


def test_rename_noop_when_callback_returns_false():
    a = _cred("alpha")
    creds = [a]

    keys = ["r", "b", "e", "t", "a", "enter", "q"]
    tui.credential_manager(
        creds,
        on_delete=_noop_delete,
        on_set_site=_noop_set_site,
        on_rename=lambda c, n: False,
        _reader=FakeReader(keys),
    )
    assert a.name == "alpha"


# --- quit & empty ---------------------------------------------------------
def test_quit_returns_none():
    a = _cred("alpha")
    result = tui.credential_manager(
        [a],
        on_delete=_noop_delete,
        on_set_site=_noop_set_site,
        on_rename=_noop_rename,
        _reader=FakeReader(["q"]),
    )
    assert result is None


def test_empty_list_only_quits():
    creds = []
    result = tui.credential_manager(
        creds,
        on_delete=_noop_delete,
        on_set_site=_noop_set_site,
        on_rename=_noop_rename,
        # A stray key is ignored; only q/esc quit the empty manager.
        _reader=FakeReader(["d", "s", "q"]),
    )
    assert result is None
    assert creds == []
