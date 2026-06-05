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

# Invisible HTML-comment markers (shared with src/jp/changelog.py) so `jp
# changelog` can extract just this clean section from the full release body.
_HL_START = "<!-- jp-changelog:start -->"
_HL_END = "<!-- jp-changelog:end -->"


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
        "You write release highlights for 'jp' (PyPI: jpsync), a git-like CLI that syncs "
        "local folders with a remote JupyterHub. The audience is end users, not contributors.\n\n"
        "Produce a SHORT, clean, well-organized highlights section. Rules:\n"
        "- Start with one plain sentence summarizing the release. No title line, no preamble.\n"
        "- Then group the notable, USER-FACING changes under these headings, in this order, "
        "OMITTING any heading with no items: '### New', '### Improvements', '### Fixes'.\n"
        "- Each item is one bullet: `- **Short title** — what it does, and how to use it "
        "(name the exact command/flag).` One sentence. Plain, concrete, professional.\n"
        "- Be selective: at most 6 bullets total. Merge related commits. Skip anything internal "
        "(refactors, CI, tests, docs-only, release chores, dependency bumps).\n"
        "- No emoji. No commit hashes or PR numbers. No marketing fluff. Markdown only.\n\n"
        f"## Commits\n{commits}\n\n## Diffstat\n{diffstat}\n\n"
        f"## README (for usage/context)\n{readme[:6000]}\n"
    )


def _http_post_json(url: str, body: dict, api_key: str | None = None) -> dict:
    headers = {"Content-Type": "application/json"}
    # Pass the key in a header, never in the URL query string -- keeps it out of
    # any URL that might be logged (the API accepts ?key= too, but x-goog-api-key
    # is the safer equivalent).
    if api_key:
        headers["x-goog-api-key"] = api_key
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def call_gemini(prompt: str, api_key: str, model: str) -> str:
    url = f"{_GEMINI_BASE}/{model}:generateContent"
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    data = _http_post_json(url, body, api_key)
    return data["candidates"][0]["content"]["parts"][0]["text"].strip()


def _gh(*args: str) -> str:
    return subprocess.check_output(["gh", *args], text=True).strip()


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

    block = f"{_HL_START}\n## Highlights\n\n{highlights}\n{_HL_END}"

    if not _has_gh():
        print("gh CLI not available; highlights generated but not written:\n")
        print(block)
        return 0
    # Prepend the highlights to the release body so they lead on GitHub and can
    # be extracted by `jp changelog`. Never fail the release on a gh error --
    # the highlights are a nice-to-have, not a release gate. If a previous run
    # already inserted a block, replace it instead of stacking another.
    try:
        existing = _gh("release", "view", new_tag, "--json", "body", "-q", ".body")
        existing = _strip_block(existing)
        new_body = f"{block}\n\n{existing}".strip()
        subprocess.run(["gh", "release", "edit", new_tag, "--notes", new_body], check=True)
    except (subprocess.CalledProcessError, OSError) as exc:
        print(f"gh release edit failed ({exc}); highlights not written.", file=sys.stderr)
        return 0
    print(f"Wrote AI highlights to release {new_tag}.")
    return 0


def _strip_block(body: str) -> str:
    """Remove a previously-inserted highlights block (idempotent re-runs)."""
    if _HL_START in body and _HL_END in body:
        head, rest = body.split(_HL_START, 1)
        _, tail = rest.split(_HL_END, 1)
        return (head + tail).strip()
    return body


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
