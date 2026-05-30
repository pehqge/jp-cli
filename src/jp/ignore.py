"""Ignore-file handling (``.jpignore``): gitignore-flavored matching.

Supports a practical subset of gitignore semantics, implemented with stdlib
``fnmatch``-style globbing over normalized POSIX relative paths:

  * blank lines and ``#`` comments are ignored
  * ``!pattern`` negates (re-includes) a previously ignored path
  * a trailing ``/`` matches directories only
  * a leading ``/`` anchors to the repo root
  * ``**`` matches across path separators
  * otherwise a pattern with no slash matches any path component

The ``.jp`` metadata directory is ALWAYS ignored and can never be un-ignored.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .paths import DOT_DIR

IGNORE_NAME = ".jpignore"


@dataclass
class _Rule:
    regex: re.Pattern[str]
    negate: bool
    dir_only: bool


class IgnoreSet:
    """Compiled ignore rules; query with :meth:`is_ignored`."""

    def __init__(self, patterns: Iterable[str]) -> None:
        self._rules: list[_Rule] = []
        # Hard rule: the metadata dir is always ignored, first and unconditional.
        self._rules.append(_Rule(regex=_compile(f"{DOT_DIR}/**"), negate=False, dir_only=False))
        self._rules.append(_Rule(regex=_compile(DOT_DIR), negate=False, dir_only=True))
        for raw in patterns:
            rule = _parse_line(raw)
            if rule is not None:
                self._rules.append(rule)

    @classmethod
    def from_root(cls, root: Path) -> IgnoreSet:
        path = Path(root) / IGNORE_NAME
        lines: list[str] = []
        if path.is_file():
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                lines = []
        return cls(lines)

    def is_ignored(self, rel: str, is_dir: bool = False) -> bool:
        """Return True if ``rel`` (POSIX relative path) is ignored.

        Later matching rules win (gitignore semantics), so a ``!`` negation can
        re-include a path -- except for the always-ignored ``.jp`` dir, which is
        guarded explicitly below.
        """
        norm = rel.replace("\\", "/").strip("/")
        if norm == DOT_DIR or norm.startswith(DOT_DIR + "/"):
            return True  # never sync our own metadata, negations notwithstanding

        ignored = False
        for rule in self._rules:
            if rule.dir_only and not is_dir:
                continue
            if rule.regex.match(norm):
                ignored = not rule.negate
        return ignored


def _parse_line(raw: str) -> _Rule | None:
    line = raw.rstrip("\n")
    # Strip trailing whitespace unless escaped (rare; keep it simple).
    line = line.rstrip()
    if not line or line.lstrip().startswith("#"):
        return None
    negate = False
    if line.startswith("!"):
        negate = True
        line = line[1:]
    dir_only = line.endswith("/")
    if dir_only:
        line = line[:-1]
    if not line:
        return None
    return _Rule(regex=_compile(line), negate=negate, dir_only=dir_only)


def _compile(pattern: str) -> re.Pattern[str]:
    """Translate a gitignore-ish glob into an anchored regex over POSIX paths."""
    anchored = pattern.startswith("/")
    pat = pattern.lstrip("/")

    i = 0
    out: list[str] = []
    n = len(pat)
    while i < n:
        c = pat[i]
        if c == "*":
            if i + 1 < n and pat[i + 1] == "*":
                # '**' -> match anything including separators
                # consume optional following slash for '**/'
                if i + 2 < n and pat[i + 2] == "/":
                    out.append("(?:.*/)?")
                    i += 3
                    continue
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == ".":
            out.append(r"\.")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1

    body = "".join(out)
    if anchored:
        # Anchored to root: match the path, optionally with children.
        regex = f"^{body}(?:/.*)?$"
    elif "/" in pat:
        # Has a slash but not anchored: still match from root for simplicity,
        # plus allow it to match anywhere as a path segment.
        regex = f"^(?:.*/)?{body}(?:/.*)?$"
    else:
        # Bare name: matches that component anywhere in the tree.
        regex = f"^(?:.*/)?{body}(?:/.*)?$"
    return re.compile(regex)
