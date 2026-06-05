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
    monkeypatch.setattr(rna, "_http_post_json", lambda url, body: fake)
    out = rna.call_gemini("prompt", api_key="k", model="gemini-x")
    assert "Highlights" in out
