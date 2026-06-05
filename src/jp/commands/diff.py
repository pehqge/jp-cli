"""``jp diff`` -- show a unified text diff of changed files (read-only).

For text files, prints a unified diff between the remote (or base) and local
content. Binary files are reported as "binary differs". Read-only: never writes.

With ``--staged`` (alias ``--cached``) it instead diffs the versioning STAGING
area against the HEAD commit's tree, fully OFFLINE -- it never builds the API or
touches the network. Notebooks are diffed OUTPUTS-FREE (via the hybrid normalized
code text), so a pure re-run shows nothing.
"""

from __future__ import annotations

import argparse
import difflib

from .. import ui
from ..errors import EXIT_OK
from ..sync import Change
from . import _context
from ._context import load_repo


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("diff", help="show unified diffs of changed files (read-only)")
    p.add_argument("path", nargs="?", default="", help="limit to a single relative path")
    p.add_argument(
        "--staged",
        "--cached",
        dest="staged",
        action="store_true",
        help="diff the staging area against HEAD (offline; no network)",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    # --staged is a fully OFFLINE staged-vs-HEAD diff: do NOT build the api at all.
    if getattr(args, "staged", False):
        return _run_staged(args)

    ctx = load_repo()
    api = _context.build_api(ctx.cfg)
    from .. import sync

    states = sync.diff(ctx.root, ctx.cfg, api, ctx.index, ctx.ignore)
    only = args.path.replace("\\", "/").strip("/") if args.path else ""

    shown = 0
    for st in states:
        if only and st.rel != only:
            continue
        if st.change in (Change.UNCHANGED,):
            continue
        if st.change in (Change.REMOTE_NEW,):
            ui.heading(f"remote-only: {st.rel}")
            continue
        if st.change == Change.LOCAL_NEW:
            ui.heading(f"local-only: {st.rel}")
            continue

        local_text = _read_local_text(ctx.root, st.rel)
        remote_text = _read_remote_text(api, st)
        if local_text is None or remote_text is None:
            ui.heading(f"{st.rel}: binary differs")
            shown += 1
            continue

        ui.heading(f"diff {st.rel}")
        diff_lines = difflib.unified_diff(
            remote_text.splitlines(keepends=True),
            local_text.splitlines(keepends=True),
            fromfile=f"remote/{st.rel}",
            tofile=f"local/{st.rel}",
        )
        for line in diff_lines:
            ui.out(line.rstrip("\n"))
        shown += 1

    if shown == 0:
        ui.info("no textual differences")
    return EXIT_OK


def _run_staged(args: argparse.Namespace) -> int:
    """OFFLINE diff of the staging area against the HEAD commit's tree.

    Builds the staged tree from ``.jp/staged.json`` and the HEAD tree from the
    current commit (the EMPTY tree if HEAD is unborn -> everything is "new"), then
    renders a per-path diff: notebooks via the outputs-free code text, text via a
    unified diff of the two blobs, binary as "binary differs". Never constructs the
    API or touches the network.
    """
    from ..versioning import refs, repo
    from ..versioning.notebooks import notebook_code_text
    from ..versioning.objects import ObjectStore

    ctx = load_repo()
    store = ObjectStore(ctx.root)

    staging = repo.Staging.load(ctx.root)
    staged_tree: dict[str, dict] = {
        rel: repo._tree_entry_for(e)
        for rel, e in staging.entries.items()
        if not repo._is_git_path(rel)
    }

    head_sha = refs.resolve_head(ctx.root)
    if head_sha is None:
        head_tree: dict[str, dict] = {}
    else:
        head_commit = repo.read_commit(store, head_sha)
        head_tree = repo.read_tree(store, head_commit["tree"])

    only = args.path.replace("\\", "/").strip("/") if args.path else ""
    delta = repo.diff_trees(head_tree, staged_tree)

    statuses = (
        [("added", r) for r in delta["added"]]
        + [("modified", r) for r in delta["modified"]]
        + [("deleted", r) for r in delta["deleted"]]
    )
    statuses.sort(key=lambda sr: sr[1])

    shown = 0
    for status, rel in statuses:
        if only and rel != only:
            continue
        old_entry = head_tree.get(rel)
        new_entry = staged_tree.get(rel)
        ui.heading(f"{status} {rel}")
        old_bytes = _read_staged_blob(store, old_entry)
        new_bytes = _read_staged_blob(store, new_entry)

        if _entry_is_nb(old_entry) or _entry_is_nb(new_entry):
            old_text = notebook_code_text(old_bytes) if old_bytes is not None else ""
            new_text = notebook_code_text(new_bytes) if new_bytes is not None else ""
            if old_text is None or new_text is None:
                ui.out("binary differs")
                shown += 1
                continue
        else:
            old_text = _decode(old_bytes) if old_bytes is not None else ""
            new_text = _decode(new_bytes) if new_bytes is not None else ""
            if old_text is None or new_text is None:
                ui.out("binary differs")
                shown += 1
                continue

        for line in difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"HEAD/{rel}",
            tofile=f"staged/{rel}",
        ):
            ui.out(line.rstrip("\n"))
        shown += 1

    if shown == 0:
        ui.info("no staged changes")
    return EXIT_OK


def _entry_is_nb(entry: dict | None) -> bool:
    return isinstance(entry, dict) and "nb" in entry


def _read_staged_blob(store, entry: dict | None) -> bytes | None:
    """Read a tree entry's blob with a tight size cap; None if the side is absent."""
    if entry is None:
        return None
    sha = str(entry.get("sha256", ""))
    try:
        size = int(entry.get("size", 0))
    except (TypeError, ValueError):
        size = 0
    if not sha:
        return None
    return store.read(sha, max_size=size)


def _read_local_text(root, rel: str) -> str | None:
    try:
        data = (root / rel).read_bytes()
    except OSError:
        return None
    return _decode(data)


def _read_remote_text(api, st) -> str | None:
    if st.remote_entry is None:
        return ""
    try:
        data = api.get_file_bytes(st.remote_entry.path)
    except Exception:
        return None
    return _decode(data)


def _decode(data: bytes) -> str | None:
    if b"\x00" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None
