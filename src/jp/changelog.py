"""Fetch and render GitHub release notes for ``jp changelog``.

Stdlib-only (urllib), mirroring ``commands/update._latest_release_tag``. The
version compare is reused from ``commands/update`` via a lazy import to avoid an
import cycle through ``commands/__init__``.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

from . import ui

_REPO = "pehqge/jpsync"

# Invisible markers (shared with scripts/release_notes_ai.py) wrapping the clean,
# AI-written highlights block inside the full release body.
_HL_START = "<!-- jp-changelog:start -->"
_HL_END = "<!-- jp-changelog:end -->"


@dataclass
class Release:
    tag: str
    name: str
    body: str


def _api(path: str):
    url = f"https://api.github.com/repos/{_REPO}/{path}"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _to_release(d: dict) -> Release:
    return Release(
        tag=str(d.get("tag_name") or ""),
        name=str(d.get("name") or ""),
        body=str(d.get("body") or ""),
    )


def latest_release() -> Release | None:
    try:
        return _to_release(_api("releases/latest"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None


def release_for(tag: str) -> Release | None:
    norm = tag if tag.lower().startswith("v") else f"v{tag.lstrip('vV')}"
    try:
        return _to_release(_api(f"releases/tags/{norm}"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None


def releases_since(version: str) -> list[Release]:
    from .commands.update import _norm

    try:
        data = _api("releases?per_page=30")
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return []
    base = _norm(version)
    out: list[Release] = []
    for d in data if isinstance(data, list) else []:
        rel = _to_release(d)
        if rel.tag and _norm(rel.tag) > base:
            out.append(rel)
    return out


def highlights(body: str) -> str | None:
    """Return the clean AI-written highlights block from a release body, if present."""
    if _HL_START in body and _HL_END in body:
        block = body.split(_HL_START, 1)[1].split(_HL_END, 1)[0].strip()
        return block or None
    return None


_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


def _inline(text: str) -> str:
    """Render markdown **bold** as terminal bold (or strip the markers if no color)."""
    import sys

    if ui._color_enabled(sys.stdout):
        return _BOLD_RE.sub(lambda m: ui._Style.BOLD + m.group(1) + ui._Style.RESET, text)
    return _BOLD_RE.sub(lambda m: m.group(1), text)


def _render_markdown(text: str) -> None:
    """Render lightly-styled markdown to the terminal: bold headings, clean bullets."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            ui.heading(stripped.lstrip("# ").strip())
        elif stripped.startswith(("- ", "* ")):
            ui.out("  • " + _inline(stripped[2:]))
        else:
            ui.out(_inline(line))


def render(release: Release, full: bool = False) -> None:
    """Show a release. By default shows only the clean highlights block when the
    release has one; pass ``full=True`` (or for releases without highlights) to
    show the entire body."""
    ui.heading(release.name or release.tag)
    hl = highlights(release.body)
    if hl and not full:
        _render_markdown(hl)
    else:
        for line in release.body.splitlines():
            if line.strip() in (_HL_START, _HL_END):
                continue
            ui.out(line)
