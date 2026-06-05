"""``jp clone`` -- create a local workspace from a Jupyter URL and pull it.

Usage mirrors git::

    jp clone https://host/user/<name>/lab/tree/<folder> [dir]

The URL is parsed into a Contents-API base URL and a remote prefix; you can
also pass ``--base-url``/``--prefix`` explicitly instead of a URL.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .. import config as config_mod
from .. import sync, ui
from ..config import Config
from ..errors import EXIT_OK, EXIT_PARTIAL, UsageError
from ..ignore import IgnoreSet
from ..index import Index
from ..paths import DOT_DIR, validate_prefix
from ..urls import parse_clone_url
from . import _context
from ._report import report_outcome


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "clone",
        help="clone a remote Jupyter folder into a new local directory",
    )
    p.add_argument(
        "url",
        nargs="?",
        default="",
        help="Jupyter folder URL, e.g. https://host/user/<name>/lab/tree/<folder>",
    )
    p.add_argument("dir", nargs="?", default="", help="target directory (default: prefix basename)")
    p.add_argument("--base-url", default="", help="Contents API base URL (instead of a URL)")
    p.add_argument("--prefix", default="", help="remote prefix (instead of a URL)")
    p.add_argument("--token-path", default="", help="path to the token file")
    p.add_argument(
        "--credential", default="", help="name of a saved credential to use (see 'jp login')"
    )
    p.add_argument(
        "--dry-run", action="store_true", help="show what would be downloaded; write nothing"
    )
    p.add_argument(
        "--history",
        action="store_true",
        help="also fetch the committed version history backup from the remote (verified)",
    )
    p.set_defaults(func=run)


def _resolve_source(args: argparse.Namespace) -> tuple[str, str]:
    """Return (base_url, prefix) from either a URL or explicit flags."""
    if args.url:
        base_url, prefix = parse_clone_url(args.url)
    else:
        base_url, prefix = str(args.base_url).strip(), str(args.prefix).strip()
    if not base_url:
        raise UsageError("provide a Jupyter URL, or both --base-url and --prefix")
    if not base_url.startswith(("http://", "https://")):
        raise UsageError(f"base URL must be http(s): {base_url!r}")
    # validate_prefix refuses an empty/root/shared prefix with a clear message.
    prefix = validate_prefix(prefix)
    return base_url.rstrip("/"), prefix


def run(args: argparse.Namespace) -> int:
    base_url, prefix = _resolve_source(args)

    target = args.dir or prefix.rstrip("/").split("/")[-1]
    root = Path(target).resolve()
    if (root / DOT_DIR).exists():
        raise UsageError(f"{root} is already a jp workspace")
    root.mkdir(parents=True, exist_ok=True)

    # Pick which saved credential this workspace will use (no repo exists yet,
    # so only global credentials are in play here).
    credential = _context.choose_credential(args, root=None)
    cfg = Config(
        base_url=base_url,
        prefix=prefix,
        token_path=str(args.token_path or ""),
        credential=credential,
    )
    cfg._config_dir = root / DOT_DIR
    if not args.dry_run:
        config_mod.save(root, cfg)
        Index(root).save()

    index = Index.load(root) if not args.dry_run else Index(root)
    ignore = IgnoreSet.from_root(root)
    api = _context.build_api(cfg)

    ui.heading(f"cloning {cfg.prefix} -> {root}")
    outcome = sync.pull(root, cfg, api, index, ignore, dry_run=args.dry_run)
    report_outcome("clone", outcome, dry_run=args.dry_run)

    # --history: after the normal file pull, also fetch the committed version
    # history backup from <prefix>/__jp/ (every object byte-VERIFIED before it is
    # placed -- see jp.versioning.fetch). Without --history, clone is byte-identical
    # to before. Never runs on a dry-run (which writes nothing locally).
    if getattr(args, "history", False) and not args.dry_run:
        _fetch_history(root, cfg, api)

    return EXIT_PARTIAL if outcome.had_failures else EXIT_OK


def _fetch_history(root: Path, cfg: Config, api) -> None:
    """Fetch the remote version-history backup after a clone (best-effort report).

    A verification failure (a corrupt/hostile remote object) RAISES out of
    :func:`jp.versioning.fetch.fetch_history` so the CLI maps it to a non-zero exit
    -- a clone of a backup whose history is untrustworthy should fail loudly. A
    remote with no ``__jp`` backup is a friendly note, not an error.
    """
    from ..versioning import fetch as fetch_mod

    ui.heading("fetching version history backup")
    result = fetch_mod.fetch_history(root, cfg, api)
    for w in result.warnings:
        ui.warn(w)
    if result.downloaded == 0 and all(not b.ref_updated for b in result.branches):
        ui.info("no version history backup found on the remote (or already present)")
    else:
        ui.success(f"history: {result.downloaded} object(s) downloaded")
