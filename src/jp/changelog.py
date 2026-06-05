"""Fetch and render GitHub release notes for ``jp changelog``.

Stdlib-only (urllib), mirroring ``commands/update._latest_release_tag``. The
version compare is reused from ``commands/update`` via a lazy import to avoid an
import cycle through ``commands/__init__``.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from . import ui

_REPO = "pehqge/jpsync"


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


def render(release: Release) -> None:
    ui.heading(release.name or release.tag)
    for line in release.body.splitlines():
        ui.out(line)
