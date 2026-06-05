"""``jp log`` -- walk and print commit history (read-only, offline).

Resolves a start point (an optional ``REF`` -- a branch name or a commit sha --
else HEAD), then walks the first-parent chain via :func:`jp.versioning.repo.
iter_history`. With ``--oneline`` it prints ``<short> <subject>``; with ``--stat``
it appends the tree diff (added/modified/deleted) against the first parent (the
empty tree for a root commit). An unborn/empty repo prints a friendly note and
exits 0 -- never a traceback. Read-only and offline.
"""

from __future__ import annotations

import argparse
import re

from .. import ui
from ..errors import EXIT_OK
from ..versioning import refs, repo
from ..versioning.objects import ObjectStore, VersioningError
from ..versioning.refs import validate_ref_name
from ._context import load_repo

# A commit sha is 64 lowercase hex (mirrors the object-store gate).
_SHA_RE = re.compile(r"[0-9a-f]{64}")


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("log", help="show commit history (read-only)")
    p.add_argument("ref", nargs="?", default="", help="branch name or commit sha (default: HEAD)")
    p.add_argument("-n", "--max-count", type=int, default=0, help="limit to the N latest commits")
    p.add_argument("--oneline", action="store_true", help="one line per commit")
    p.add_argument("--stat", action="store_true", help="show changed paths per commit")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    ctx = load_repo()
    store = ObjectStore(ctx.root)

    start = _resolve_start(ctx.root, store, args.ref)
    if start is None:
        ui.info("no commits yet")
        return EXIT_OK

    limit = args.max_count if args.max_count and args.max_count > 0 else None
    for shown, (sha, commit) in enumerate(repo.iter_history(store, start), start=1):
        _print_commit(store, sha, commit, oneline=args.oneline, stat=args.stat)
        if limit is not None and shown >= limit:
            break
    return EXIT_OK


def _resolve_start(root, store: ObjectStore, ref: str) -> str | None:
    """Resolve the ``REF`` argument (or HEAD) to a starting commit sha, or None.

    A non-empty ``ref`` is tried first as a 64-hex commit sha (validated and
    required to exist in the store), then as a branch name (validated and resolved
    to its tip). An unresolvable ref is a clear error. With no ref we use HEAD,
    returning None for an unborn/empty repo so the caller can print a friendly note.
    """
    ref = (ref or "").strip()
    if not ref:
        return refs.resolve_head(root)

    # Try as a commit sha first.
    if _SHA_RE.fullmatch(ref):
        if not store.has(ref):
            raise VersioningError(f"no such commit: {ref}")
        return ref

    # Otherwise it must be a branch name (validated -> resolved to its tip).
    validate_ref_name(ref)  # raises VersioningError on a traversal-y name
    sha = refs.read_ref(root, ref)
    if sha is None:
        raise VersioningError(f"no such branch or commit: {ref}")
    return sha


def _print_commit(store: ObjectStore, sha: str, commit: dict, *, oneline: bool, stat: bool) -> None:
    message = str(commit.get("message", ""))
    subject = message.splitlines()[0] if message else ""
    if oneline:
        ui.out(f"{sha[:12]} {subject}")
    else:
        # Print the SHORT 12-char hash (git convention). A full 64-hex sha would be
        # masked by ui.redact's bare-hex heuristic (>=32 hex -> ***REDACTED***),
        # breaking the output. Short is below that threshold and unambiguous; real
        # tokens are still caught by redact's registered-secret + token/bearer
        # patterns, so this does not weaken secret scrubbing.
        ui.heading(f"commit {sha[:12]}")
        ui.out(f"Author: {commit.get('author', '')}")
        ui.out(f"Date:   {commit.get('time', '')}")
        ui.out("")
        ui.out(f"    {message}")
        ui.out("")

    if stat:
        _print_stat(store, commit)


def _print_stat(store: ObjectStore, commit: dict) -> None:
    """Print the tree diff of ``commit`` against its first parent (empty if root)."""
    cur = repo.read_tree(store, commit["tree"])
    parents = commit.get("parents") or []
    if parents:
        parent_commit = repo.read_commit(store, parents[0])
        prev = repo.read_tree(store, parent_commit["tree"])
    else:
        prev = {}
    delta = repo.diff_trees(prev, cur)
    a, m, d = len(delta["added"]), len(delta["modified"]), len(delta["deleted"])
    ui.detail(f"    {a + m + d} file(s) changed (+{a} ~{m} -{d})")
    for rel in delta["added"]:
        ui.detail(f"    + {rel}")
    for rel in delta["modified"]:
        ui.detail(f"    ~ {rel}")
    for rel in delta["deleted"]:
        ui.detail(f"    - {rel}")
    ui.out("")
