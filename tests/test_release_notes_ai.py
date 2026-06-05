from __future__ import annotations

import importlib.util
import pathlib

_spec = importlib.util.spec_from_file_location(
    "release_notes_ai",
    pathlib.Path(__file__).resolve().parents[1] / "scripts" / "release_notes_ai.py",
)
rna = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rna)


def test_semver_parse():
    assert rna._semver("v1.2.3") == (1, 2, 3)
    assert rna._semver("1.2.3") == (1, 2, 3)


def test_is_minor_or_major():
    assert rna.is_minor_or_major("v1.1.0", "v1.2.0") is True  # minor
    assert rna.is_minor_or_major("v1.1.0", "v2.0.0") is True  # major
    assert rna.is_minor_or_major("v1.1.0", "v1.1.1") is False  # patch
    assert rna.is_minor_or_major("v1.1.0", "v1.1.0") is False  # same


def test_build_prompt_includes_context():
    prompt = rna.build_prompt(
        commits="feat: add live mount", diffstat="1 file changed", readme="# jp"
    )
    assert "feat: add live mount" in prompt
    assert "no emoji" in prompt.lower()


def test_call_gemini_parses_response(monkeypatch):
    fake = {"candidates": [{"content": {"parts": [{"text": "## Highlights\nStuff."}]}}]}
    monkeypatch.setattr(rna, "_http_post_json", lambda url, body, api_key=None: fake)
    out = rna.call_gemini("prompt", api_key="k", model="gemini-x")
    assert "Highlights" in out


def test_main_appends_highlights_via_gh(monkeypatch):
    # End-to-end main() with all I/O mocked. Guards against the regression where
    # the release body was read with `git release` instead of `gh release`.
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(rna, "_git", lambda *a: "log/diff output")
    monkeypatch.setattr(rna, "call_gemini", lambda *a, **k: "## Highlights\n- new thing")
    monkeypatch.setattr(rna, "_has_gh", lambda: True)
    monkeypatch.setattr(rna, "_gh", lambda *a: "EXISTING BODY")

    calls = []
    monkeypatch.setattr(rna.subprocess, "run", lambda cmd, **k: calls.append(cmd) or None)

    rc = rna.main(["v1.1.1", "v1.2.0"])  # minor bump -> should generate + append
    assert rc == 0
    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[0] == "gh" and cmd[1] == "release" and cmd[2] == "edit" and cmd[3] == "v1.2.0"
    notes = cmd[cmd.index("--notes") + 1]
    assert "EXISTING BODY" in notes and "## Highlights" in notes


def test_main_gh_failure_does_not_break_release(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(rna, "_git", lambda *a: "x")
    monkeypatch.setattr(rna, "call_gemini", lambda *a, **k: "## Highlights")
    monkeypatch.setattr(rna, "_has_gh", lambda: True)

    def boom(*a):
        raise rna.subprocess.CalledProcessError(1, ["gh"])

    monkeypatch.setattr(rna, "_gh", boom)
    assert rna.main(["v1.1.1", "v1.2.0"]) == 0  # never fails the release
