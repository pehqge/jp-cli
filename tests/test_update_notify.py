from __future__ import annotations

import argparse

import jp.update_notify as un
from jp import credentials


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(credentials, "global_dir", lambda: tmp_path)


def test_cache_roundtrip(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    un._save_cache({"latest": "v1.2.0", "last_check": 123.0})
    assert un._load_cache() == {"latest": "v1.2.0", "last_check": 123.0}


def test_load_cache_missing_returns_empty(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    assert un._load_cache() == {}


def test_load_cache_corrupt_returns_empty(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    (tmp_path / "update-check.json").write_text("{not json", encoding="utf-8")
    assert un._load_cache() == {}


def _args(command="status", quiet=False):
    return argparse.Namespace(command=command, quiet=quiet)


def _force_tty(monkeypatch):
    monkeypatch.setattr(un.sys.stderr, "isatty", lambda: True, raising=False)


def _no_ci(monkeypatch):
    for v in un._CI_ENV_VARS + ("JP_NO_UPDATE_NOTIFIER",):
        monkeypatch.delenv(v, raising=False)


def test_suppressed_when_quiet(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _force_tty(monkeypatch)
    _no_ci(monkeypatch)
    assert un._suppressed(_args(quiet=True)) is True


def test_suppressed_when_not_tty(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(un.sys.stderr, "isatty", lambda: False, raising=False)
    _no_ci(monkeypatch)
    assert un._suppressed(_args()) is True


def test_suppressed_in_ci(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _force_tty(monkeypatch)
    _no_ci(monkeypatch)
    monkeypatch.setenv("CI", "1")
    assert un._suppressed(_args()) is True


def test_suppressed_for_excluded_command(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _force_tty(monkeypatch)
    _no_ci(monkeypatch)
    monkeypatch.setattr(un, "_install_is_special", lambda: False)
    monkeypatch.setattr(un, "_notifier_pref_on", lambda: True)
    assert un._suppressed(_args(command="update")) is True


def test_not_suppressed_for_normal_command(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _force_tty(monkeypatch)
    _no_ci(monkeypatch)
    monkeypatch.setattr(un, "_install_is_special", lambda: False)
    monkeypatch.setattr(un, "_notifier_pref_on", lambda: True)
    assert un._suppressed(_args(command="status")) is False


def _ready(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _force_tty(monkeypatch)
    _no_ci(monkeypatch)
    monkeypatch.setattr(un, "_install_is_special", lambda: False)
    monkeypatch.setattr(un, "_notifier_pref_on", lambda: True)
    monkeypatch.setattr(un, "__version__", "1.1.1")


def test_notice_printed_when_newer(monkeypatch, tmp_path, capsys):
    _ready(monkeypatch, tmp_path)
    spawned = []
    monkeypatch.setattr(un, "_spawn_worker", lambda: spawned.append(True))
    un._save_cache({"latest": "v1.2.0", "last_check": un.time.time()})
    un.maybe_notify(_args())
    err = capsys.readouterr().err
    assert "1.1.1" in err and "1.2.0" in err
    assert spawned == []  # cache fresh -> no refresh


def test_no_notice_when_same_version(monkeypatch, tmp_path, capsys):
    _ready(monkeypatch, tmp_path)
    monkeypatch.setattr(un, "_spawn_worker", lambda: None)
    un._save_cache({"latest": "v1.1.1", "last_check": un.time.time()})
    un.maybe_notify(_args())
    assert capsys.readouterr().err == ""


def test_stale_cache_triggers_refresh(monkeypatch, tmp_path):
    _ready(monkeypatch, tmp_path)
    spawned = []
    monkeypatch.setattr(un, "_spawn_worker", lambda: spawned.append(True))
    un._save_cache({"latest": "v1.1.1", "last_check": 0.0})
    un.maybe_notify(_args())
    assert spawned == [True]


def test_pending_announcement_printed_and_cleared(monkeypatch, tmp_path, capsys):
    _ready(monkeypatch, tmp_path)
    monkeypatch.setattr(un, "_spawn_worker", lambda: None)
    un._save_cache({"pending_announcement": {"from": "1.1.1", "to": "1.2.0"}})
    un.maybe_notify(_args())
    err = capsys.readouterr().err
    assert "auto-updated" in err and "1.2.0" in err
    assert "pending_announcement" not in un._load_cache()


def test_maybe_notify_never_raises(monkeypatch, tmp_path):
    _ready(monkeypatch, tmp_path)
    monkeypatch.setattr(un, "_load_cache", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    un.maybe_notify(_args())  # must not raise


def test_notifier_off_pref_suppresses(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _force_tty(monkeypatch)
    _no_ci(monkeypatch)
    monkeypatch.setattr(un, "_install_is_special", lambda: False)
    from jp import global_prefs

    monkeypatch.setattr(
        global_prefs, "get", lambda k, d=None: False if k == "update_notifier" else d
    )
    assert un._suppressed(_args(command="status")) is True
