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
    # Apply the workspace color policy (env/--no-color still override it).
    from .. import ui

    ui.set_color_mode(cfg.color)
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
    return resolve_credential(
        credential=getattr(args, "credential", "") or "",
        token_path=getattr(args, "token_path", "") or "",
        root=root,
    )


def resolve_credential(
    *, credential: str = "", token_path: str = "", root: Path | None = None
) -> str:
    """Resolve which saved credential to use, from explicit values.

    This is the credential-selection core shared by :func:`choose_credential`
    (which adapts an argparse-style object) and :func:`config_from_url`. The
    resolution rules are identical: an explicit ``token_path`` bypasses the
    credential system; an explicit ``credential`` is validated against what
    exists; otherwise zero credentials is an error, exactly one is used
    silently, and several trigger an interactive picker (or, non-interactively,
    an error asking for ``--credential``).
    """
    import os

    from .. import credentials, tui
    from ..errors import AuthError, UsageError

    # An explicit token path bypasses the credential system entirely.
    if token_path:
        return ""

    requested = (credential or "").strip()
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


def config_from_url(url: str, *, credential: str = "", token_path: str = "") -> Config:
    """Build an in-memory :class:`Config` from a Jupyter URL + a credential.

    This is the workspace-free path shared by ``jp live <URL>`` and
    ``jp terminal <URL>``: it parses the URL into ``(base_url, prefix)``, resolves
    a credential exactly like :func:`choose_credential`, and returns a ``Config``
    held only in memory. Nothing is written to disk -- no ``.jp/`` workspace is
    created.

    - ``url`` is parsed with :func:`urls.parse_clone_url` and the prefix is
      validated with :func:`paths.validate_prefix` (so the shared/too-broad and
      empty-prefix refusals still apply).
    - ``credential`` / ``token_path`` follow the same rules as
      :func:`choose_credential`: an explicit ``token_path`` bypasses credentials;
      an explicit ``credential`` is validated; otherwise one is picked (silently
      if unique, interactively if several, error if none).
    """
    from ..paths import validate_prefix
    from ..urls import parse_clone_url

    base_url, raw_prefix = parse_clone_url(url)
    prefix = validate_prefix(raw_prefix)
    # Credential resolution is workspace-free here: there is no .jp/ root, so only
    # global credentials are in scope (root=None), matching clone/init at setup.
    name = resolve_credential(credential=credential, token_path=token_path, root=None)
    return Config(
        base_url=base_url,
        prefix=prefix,
        token_path=token_path,
        credential=name,
    )
