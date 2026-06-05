"""``jp show [COMMIT] [PATH]`` -- show a commit and the diff it introduced.

Resolves ``COMMIT`` (default ``HEAD``) to a full sha via
:func:`jp.versioning.repo.resolve_commitish`, prints a short (12-char) header, then
diffs the commit's tree against its FIRST parent (the empty tree for a root
commit). Each changed path is rendered by kind:

* text -> a unified diff of the two blob contents (decoded with the same NUL/UTF-8
  heuristic ``jp diff`` uses; binary -> "binary differs");
* notebook (tree entry carries ``"nb"``) -> a unified diff of the OUTPUTS-FREE,
  human-readable :func:`jp.versioning.notebooks.notebook_code_text` of each side,
  so a pure re-run shows no change and only real code edits appear.

``PATH`` limits output to one file; ``--stat`` prints only the diff summary (counts
and paths), no bodies. Read-only and entirely OFFLINE -- it never builds the API.
An unborn/empty repo with the default HEAD prints a friendly note and exits 0; an
explicit unknown revision is a non-zero error.
"""

from __future__ import annotations

import argparse
import difflib

from .. import ui
from ..errors import EXIT_OK
from ..versioning import refs, repo
from ..versioning.notebooks import notebook_code_text
from ..versioning.objects import ObjectStore, VersioningError
from ._context import load_repo
from .diff import _decode


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("show", help="show a commit and its diff (read-only, offline)")
    p.add_argument("commit", nargs="?", default="HEAD", help="commit-ish to show (default: HEAD)")
    p.add_argument("path", nargs="?", default="", help="limit to a single relative path")
    p.add_argument("--stat", action="store_true", help="show only the changed-path summary")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    ctx = load_repo()
    store = ObjectStore(ctx.root)

    ref = (args.commit or "HEAD").strip() or "HEAD"
    # An empty repo with the default HEAD is a friendly no-op, not an error.
    if ref == "HEAD" and refs.resolve_head(ctx.root) is None:
        ui.info("no commits yet")
        return EXIT_OK

    sha = repo.resolve_commitish(ctx.root, store, ref)
    commit = repo.read_commit(store, sha)

    this_tree = repo.read_tree(store, commit["tree"])
    prev_tree = _parent_tree(store, commit)

    _print_header(commit, sha)

    only = args.path.replace("\\", "/").strip("/") if args.path else ""
    delta = repo.diff_trees(prev_tree, this_tree)

    if args.stat:
        _print_stat(delta, only)
        return EXIT_OK

    _print_bodies(store, delta, prev_tree, this_tree, only)
    return EXIT_OK


def _parent_tree(store: ObjectStore, commit: dict) -> dict:
    """Return the first parent's tree entries, or the EMPTY tree for a root commit."""
    parents = commit.get("parents") or []
    if not parents:
        return {}
    parent_commit = repo.read_commit(store, parents[0])
    return repo.read_tree(store, parent_commit["tree"])


def _print_header(commit: dict, sha: str) -> None:
    # SHORT 12-char hash (git convention): a full 64-hex sha would be masked by
    # ui.redact's bare-hex heuristic; short is below that threshold (see log.py).
    ui.heading(f"commit {sha[:12]}")
    ui.out(f"Author: {commit.get('author', '')}")
    ui.out(f"Date:   {commit.get('time', '')}")
    ui.out("")
    ui.out(f"    {commit.get('message', '')}")
    ui.out("")


def _print_stat(delta: dict, only: str) -> None:
    """Print only the counts + changed paths (the ``--stat`` form)."""

    def _keep(rel: str) -> bool:
        return not only or rel == only

    added = [r for r in delta["added"] if _keep(r)]
    modified = [r for r in delta["modified"] if _keep(r)]
    deleted = [r for r in delta["deleted"] if _keep(r)]
    a, m, d = len(added), len(modified), len(deleted)
    ui.detail(f"    {a + m + d} file(s) changed (+{a} ~{m} -{d})")
    for rel in added:
        ui.detail(f"    + {rel}")
    for rel in modified:
        ui.detail(f"    ~ {rel}")
    for rel in deleted:
        ui.detail(f"    - {rel}")


def _print_bodies(
    store: ObjectStore, delta: dict, prev_tree: dict, this_tree: dict, only: str
) -> None:
    """Print a per-path header + diff body for every change (respecting ``only``)."""
    shown = 0
    statuses = (
        [("added", r) for r in delta["added"]]
        + [("modified", r) for r in delta["modified"]]
        + [("deleted", r) for r in delta["deleted"]]
    )
    statuses.sort(key=lambda sr: sr[1])
    for status, rel in statuses:
        if only and rel != only:
            continue
        old_entry = prev_tree.get(rel)
        new_entry = this_tree.get(rel)
        ui.heading(f"{status} {rel}")
        _print_one_diff(store, rel, old_entry, new_entry)
        shown += 1

    if shown == 0:
        if only:
            ui.info(f"no changes to {only} in this commit")
        else:
            ui.info("this commit introduced no file changes")


def _print_one_diff(
    store: ObjectStore, rel: str, old_entry: dict | None, new_entry: dict | None
) -> None:
    """Render the diff body for one path by kind (notebook / text / binary).

    ``old_entry``/``new_entry`` is ``None`` when the path is absent on that side
    (a deletion has no new entry; an addition has no old entry).
    """
    old_bytes = _read_entry(store, old_entry)
    new_bytes = _read_entry(store, new_entry)

    # A notebook on EITHER side -> outputs-free code-text diff (so a re-run is silent).
    if _is_nb(old_entry) or _is_nb(new_entry):
        old_text = notebook_code_text(old_bytes) if old_bytes is not None else ""
        new_text = notebook_code_text(new_bytes) if new_bytes is not None else ""
        if old_text is None or new_text is None:
            # A ".ipynb" entry whose bytes are not a real notebook -> opaque binary.
            ui.out("binary differs")
            return
        _emit_unified(old_text, new_text, rel)
        return

    old_text = _decode(old_bytes) if old_bytes is not None else ""
    new_text = _decode(new_bytes) if new_bytes is not None else ""
    if old_text is None or new_text is None:
        ui.out("binary differs")
        return
    _emit_unified(old_text, new_text, rel)


def _emit_unified(old_text: str, new_text: str, rel: str) -> None:
    for line in difflib.unified_diff(
        old_text.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile=f"a/{rel}",
        tofile=f"b/{rel}",
    ):
        ui.out(line.rstrip("\n"))


def _is_nb(entry: dict | None) -> bool:
    return isinstance(entry, dict) and "nb" in entry


def _read_entry(store: ObjectStore, entry: dict | None) -> bytes | None:
    """Read a tree entry's blob with a TIGHT decompression cap (its tree size).

    ``None`` means the path is absent on that side (added/deleted). A corrupt or
    missing object raises :class:`VersioningError` -- show never fabricates content.
    """
    if entry is None:
        return None
    sha = str(entry.get("sha256", ""))
    try:
        size = int(entry.get("size", 0))
    except (TypeError, ValueError):
        size = 0
    if not sha:
        raise VersioningError("tree entry is missing its blob sha")
    return store.read(sha, max_size=size)
