from __future__ import annotations

import argparse

import jp.update_notify as un
from jp import credentials
from jp.commands import update as upd
from jp.commands import update_check as uc


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(credentials, "global_dir", lambda: tmp_path)


def test_worker_writes_cache(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(uc, "__version__", "1.1.1")
    monkeypatch.setattr(upd, "_latest_release_tag", lambda: "v1.3.0")
    uc.run(argparse.Namespace())
    cache = un._load_cache()
    assert cache["latest"] == "v1.3.0"
    assert cache["checked_version"] == "1.1.1"
    assert "last_check" in cache


def test_worker_no_autoupdate_when_disabled(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(uc, "__version__", "1.1.1")
    monkeypatch.setattr(upd, "_latest_release_tag", lambda: "v1.3.0")
    from jp import global_prefs

    monkeypatch.setattr(global_prefs, "get", lambda k, d=None: False)
    ran = []
    monkeypatch.setattr(uc, "_perform_auto_update", lambda: ran.append(True) or True)
    uc.run(argparse.Namespace())
    assert ran == []
    assert "pending_announcement" not in un._load_cache()


def test_worker_autoupdate_records_announcement(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(uc, "__version__", "1.1.1")
    monkeypatch.setattr(upd, "_latest_release_tag", lambda: "v1.3.0")
    from jp import global_prefs

    monkeypatch.setattr(global_prefs, "get", lambda k, d=None: True)
    monkeypatch.setattr(upd, "_editable_source", lambda: None)
    monkeypatch.setattr(upd, "_running_as_binary", lambda: False)
    monkeypatch.setattr(un, "_is_ci", lambda: False)
    monkeypatch.setattr(uc, "_perform_auto_update", lambda: True)
    uc.run(argparse.Namespace())
    ann = un._load_cache()["pending_announcement"]
    assert ann == {"from": "1.1.1", "to": "1.3.0"}


def test_worker_never_raises(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(
        upd, "_latest_release_tag", lambda: (_ for _ in ()).throw(RuntimeError("x"))
    )
    assert uc.run(argparse.Namespace()) == 0
