from __future__ import annotations

from jp import changelog as cl


def test_releases_since_filters_by_version(monkeypatch):
    data = [
        {"tag_name": "v1.3.0", "name": "1.3.0", "body": "x"},
        {"tag_name": "v1.2.0", "name": "1.2.0", "body": "y"},
        {"tag_name": "v1.1.0", "name": "1.1.0", "body": "z"},
    ]
    monkeypatch.setattr(cl, "_api", lambda path: data)
    rels = cl.releases_since("1.2.0")
    assert [r.tag for r in rels] == ["v1.3.0"]


def test_releases_since_network_failure_returns_empty(monkeypatch):
    def boom(path):
        raise OSError("no network")

    monkeypatch.setattr(cl, "_api", boom)
    assert cl.releases_since("1.0.0") == []


def test_render_outputs_body(capsys):
    cl.render(cl.Release(tag="v1.2.0", name="1.2.0", body="line one\nline two"))
    out = capsys.readouterr().out
    assert "1.2.0" in out
    assert "line one" in out and "line two" in out


def test_release_for_normalizes_tag(monkeypatch):
    seen = {}

    def fake_api(path):
        seen["path"] = path
        return {"tag_name": "v1.2.0", "name": "1.2.0", "body": "b"}

    monkeypatch.setattr(cl, "_api", fake_api)
    cl.release_for("1.2.0")
    assert seen["path"] == "releases/tags/v1.2.0"
