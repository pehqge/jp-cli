from __future__ import annotations

from jp import credentials
from jp import global_prefs as gp


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(credentials, "global_dir", lambda: tmp_path)


def test_defaults(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    assert gp.get("auto_update") is False
    assert gp.get("update_notifier") is True


def test_set_get_roundtrip(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    gp.set("auto_update", True)
    assert gp.get("auto_update") is True
    assert gp.load() == {"auto_update": True}


def test_corrupt_file_falls_back_to_defaults(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    (tmp_path / "prefs.json").write_text("nonsense", encoding="utf-8")
    assert gp.get("update_notifier") is True


def test_coerce_bool():
    assert gp.coerce_bool("true") is True
    assert gp.coerce_bool("0") is False
    assert gp.coerce_bool("on") is True
