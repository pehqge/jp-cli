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
