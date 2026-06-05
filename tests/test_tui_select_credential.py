"""select_credential logic, driven by scripted key streams (no real terminal)."""

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


# --- plain selection (no target_site) -------------------------------------
def test_plain_select_second_with_down_enter():
    a = _cred("alpha")
    b = _cred("beta")
    chosen = tui.select_credential([a, b], _reader=FakeReader(["down", "enter"]))
    assert chosen is b


# --- site filter ----------------------------------------------------------
def test_site_filter_default_view_hides_other_site():
    match = _cred("match", site="https://hub.example.com")
    other = _cred("other", site="https://nope.example.com")
    legacy = _cred("legacy", site="")  # wildcard -> always visible
    creds = [match, other, legacy]
    # Default 'site' view shows match + legacy (2 rows); first row is `match`.
    chosen = tui.select_credential(
        creds,
        target_site="https://hub.example.com",
        _reader=FakeReader(["enter"]),
    )
    assert chosen is match


def test_toggle_a_shows_all_then_select_other():
    match = _cred("match", site="https://hub.example.com")
    other = _cred("other", site="https://nope.example.com")
    legacy = _cred("legacy", site="")
    creds = [match, other, legacy]
    # In 'site' view only match+legacy show. Press 'a' -> all 3 (match, other,
    # legacy). Navigate down once to `other`, then Enter.
    chosen = tui.select_credential(
        creds,
        target_site="https://hub.example.com",
        _reader=FakeReader(["a", "down", "enter"]),
    )
    assert chosen is other


# --- edit site via 's' ----------------------------------------------------
def test_edit_site_updates_cred_and_keeps_membership():
    target = "https://hub.example.com"
    match = _cred("match", site=target)
    legacy = _cred("legacy", site="")
    creds = [match, legacy]

    calls = []

    def on_set_site(cred, raw):
        calls.append((cred, raw))
        return target  # stub: typed URL resolves to the target origin

    # 'site' view shows match + legacy. Navigate to legacy (down), press 's',
    # type a URL, Enter to commit, then Enter to select the now-sited cred.
    keys = ["down", "s", "h", "t", "t", "p", "enter", "enter"]
    chosen = tui.select_credential(
        creds,
        target_site=target,
        on_set_site=on_set_site,
        _reader=FakeReader(keys),
    )
    assert legacy.site == target
    assert calls == [(legacy, "http")]
    # legacy now matches the target origin, so it remains visible and is the
    # highlighted selection.
    assert chosen is legacy


def test_edit_site_overwrites_existing_site():
    old = "https://old.example.com"
    new = "https://new.example.com"
    cred = _cred("sited", site=old)

    def on_set_site(c, raw):
        return new  # stub: typed URL resolves to a new origin

    # 's' must work even when the cred already has a site -> overwrite it.
    keys = ["s", "h", "t", "t", "p", "enter", "enter"]
    chosen = tui.select_credential(
        [cred],
        on_set_site=on_set_site,
        _reader=FakeReader(keys),
    )
    assert cred.site == new
    assert chosen is cred


def test_edit_site_noop_when_on_set_site_returns_empty():
    legacy = _cred("legacy", site="")

    def on_set_site(cred, raw):
        return ""  # failure -> TUI must not mutate

    keys = ["s", "x", "enter", "enter"]
    chosen = tui.select_credential(
        [legacy],
        target_site="https://hub.example.com",
        on_set_site=on_set_site,
        _reader=FakeReader(keys),
    )
    assert legacy.site == ""
    assert chosen is legacy


# --- cancel ----------------------------------------------------------------
def test_esc_returns_none():
    a = _cred("alpha")
    chosen = tui.select_credential([a], _reader=FakeReader(["esc"]))
    assert chosen is None


def test_empty_creds_returns_none():
    assert tui.select_credential([], _reader=FakeReader([])) is None
