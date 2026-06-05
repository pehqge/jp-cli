"""CI-only: generate professional, no-emoji release highlights with Gemini.

Runs in the release workflow after a minor/major GitHub Release is created. Reads
the commit log + diffstat between the previous and new tags, asks Gemini to write
a "Highlights -- what changed and how to use it" section, and appends it to the
GitHub Release body via ``gh release edit``. This is NOT part of the shipped
``jpsync`` package; it calls Gemini over stdlib urllib (no SDK, no project dep).

Env:
  GEMINI_API_KEY   (required) -- Gemini AI Studio API key (free tier on Flash)
  GEMINI_MODEL     (optional) -- model id, default 'gemini-flash-latest'
Args:
  <previous_tag> <new_tag>
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request

_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
_DEFAULT_MODEL = "gemini-flash-latest"


def _semver(tag: str) -> tuple[int, int, int]:
    parts = tag.lstrip("vV").split(".")
    nums = []
    for p in parts[:3]:
        digits = "".join(c for c in p if c.isdigit())
        nums.append(int(digits) if digits else 0)
    while len(nums) < 3:
        nums.append(0)
    return (nums[0], nums[1], nums[2])


def is_minor_or_major(prev_tag: str, new_tag: str) -> bool:
    p = _semver(prev_tag)
    n = _semver(new_tag)
    return n[0] > p[0] or (n[0] == p[0] and n[1] > p[1])


def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def build_prompt(commits: str, diffstat: str, readme: str) -> str:
    return (
        "You are writing release highlights for the open-source CLI 'jp' (PyPI: jpsync), "
        "a git-like tool that syncs local folders with a remote JupyterHub.\n\n"
        "Write a concise, PROFESSIONAL release-notes section titled exactly '## Highlights'. "
        "Use no emoji. For each notable user-facing change, explain in one or two sentences "
        "WHAT changed and HOW a user uses it (commands/flags). Ground every claim strictly in "
        "the commits and diff below -- do not invent features. Skip internal refactors and CI-only "
        "changes. Output Markdown only, no preamble.\n\n"
        f"## Commits\n{commits}\n\n## Diffstat\n{diffstat}\n\n"
        f"## README (for usage context)\n{readme[:6000]}\n"
    )


def _http_post_json(url: str, body: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def call_gemini(prompt: str, api_key: str, model: str) -> str:
    url = f"{_GEMINI_BASE}/{model}:generateContent?key={api_key}"
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    data = _http_post_json(url, body)
    return data["candidates"][0]["content"]["parts"][0]["text"].strip()


def _has_gh() -> bool:
    from shutil import which

    return which("gh") is not None


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: release_notes_ai.py <previous_tag> <new_tag>", file=sys.stderr)
        return 2
    prev_tag, new_tag = argv
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("GEMINI_API_KEY unset; skipping AI highlights.", file=sys.stderr)
        return 0
    if not is_minor_or_major(prev_tag, new_tag):
        print(f"{prev_tag} -> {new_tag} is a patch release; skipping AI highlights.")
        return 0

    model = os.environ.get("GEMINI_MODEL") or _DEFAULT_MODEL
    rng = f"{prev_tag}..{new_tag}"
    commits = _git("log", "--no-merges", "--pretty=format:- %s", rng)
    diffstat = _git("diff", "--stat", rng)
    try:
        with open("README.md", encoding="utf-8") as fh:
            readme = fh.read()
    except OSError:
        readme = ""

    try:
        highlights = call_gemini(build_prompt(commits, diffstat, readme), api_key, model)
    except Exception as exc:  # never block the release
        print(f"Gemini call failed ({exc}); skipping AI highlights.", file=sys.stderr)
        return 0

    existing = ""
    if _has_gh():
        existing = _git("release", "view", new_tag, "--json", "body", "-q", ".body")
    new_body = f"{existing}\n\n{highlights}".strip()
    subprocess.run(["gh", "release", "edit", new_tag, "--notes", new_body], check=True)
    print(f"Appended AI highlights to release {new_tag}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
