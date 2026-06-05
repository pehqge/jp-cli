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


_BODY_WITH_HL = (
    f"{cl._HL_START}\n## Highlights\n\nNice summary.\n\n### New\n- **jp open** — opens the UI.\n"
    f"{cl._HL_END}\n\n## [1.2.0]\n### Features\n* a ([abc123])\n* b ([def456])\n"
)


def test_highlights_extracts_block():
    hl = cl.highlights(_BODY_WITH_HL)
    assert "Nice summary." in hl
    assert "jp open" in hl
    assert cl._HL_START not in hl and "abc123" not in hl


def test_highlights_none_when_absent():
    assert cl.highlights("## [1.1.0]\n### Features\n* x") is None


def test_render_shows_only_highlights_by_default(capsys):
    cl.render(cl.Release(tag="v1.2.0", name="1.2.0", body=_BODY_WITH_HL))
    out = capsys.readouterr().out
    assert "Nice summary." in out and "jp open" in out
    assert "abc123" not in out  # the noisy commit list is hidden
    assert cl._HL_START not in out  # markers never shown


def test_render_full_shows_whole_body(capsys):
    cl.render(cl.Release(tag="v1.2.0", name="1.2.0", body=_BODY_WITH_HL), full=True)
    out = capsys.readouterr().out
    assert "abc123" in out  # full body includes the commit list
    assert cl._HL_START not in out  # markers still stripped from display
