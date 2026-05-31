"""Shared helpers for commands: locate the repo and build a live API client."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .. import config as config_mod
from ..api import Api
from ..config import Config
from ..errors import ConfigError
from ..ignore import IgnoreSet
from ..index import Index


@dataclass
class RepoContext:
    root: Path
    cfg: Config
    index: Index
    ignore: IgnoreSet


def load_repo() -> RepoContext:
    """Locate the jp repo from the cwd and load its config/index/ignore."""
    from ..paths import find_root

    root = find_root()
    if root is None:
        raise ConfigError(
            "not inside a jp repository (no .jp directory found). "
            "Run 'jp init' or 'jp clone' first."
        )
    cfg = config_mod.load(root)
    index = Index.load(root)
    ignore = IgnoreSet.from_root(root)
    return RepoContext(root=root, cfg=cfg, index=index, ignore=ignore)


def build_api(cfg: Config) -> Api:
    """Construct an Api client, loading the token (and registering it for redaction)."""
    token = config_mod.load_token(cfg)
    return Api(cfg.base_url, token)


def choose_credential(args: object, root: Path | None = None) -> str:
    """Decide which saved credential ``clone``/``init`` should record.

    Returns the credential NAME to store in the new workspace's config, or ``""``
    when no credential applies (an explicit ``--token-path`` or a ``JP_TOKEN``/
    ``JP_TOKEN_FILE`` env token is being used instead).

    Resolution: an explicit ``--credential`` is validated against what exists;
    otherwise zero credentials is an error (run ``jp login`` first), exactly one
    is used silently, and several trigger an interactive picker (or, in a
    non-interactive shell, an error asking for ``--credential``).
    """
    import os

    from .. import credentials, tui
    from ..errors import AuthError, UsageError

    # An explicit token path bypasses the credential system entirely.
    if getattr(args, "token_path", ""):
        return ""

    requested = (getattr(args, "credential", "") or "").strip()
    available = credentials.list_credentials(root=root)

    if requested:
        if not any(c.name == requested for c in available):
            names = ", ".join(c.name for c in available) or "(none)"
            raise UsageError(f"no saved credential named {requested!r}. Available: {names}")
        return requested

    if not available:
        if os.environ.get("JP_TOKEN") or os.environ.get("JP_TOKEN_FILE"):
            return ""
        raise AuthError("no credentials configured. Run 'jp login' first to save your API token.")

    if len(available) == 1:
        return available[0].name

    # More than one: let the user choose.
    if tui.interactive():
        labels = [f"{c.name}  ({c.scope})" for c in available]
        idx = tui.select_one(labels, title="Select a credential for this workspace")
        if idx is None:
            raise UsageError("no credential selected")
        return available[idx].name

    names = ", ".join(c.name for c in available)
    raise UsageError(
        f"multiple saved credentials; choose one with --credential NAME (one of: {names})"
    )
