"""Named credential storage for jp -- multiple API tokens, global or per-repo.

A *credential* is a named API token (e.g. ``myserver``, ``lab-gpu``). The token
VALUE is written to a private 0600 file on the user's machine and is never
echoed; a small JSON registry maps each name to its token file. Two scopes
exist:

  * global -> ``~/.config/jp/``   (usable from any directory)
  * local  -> ``<repo>/.jp/``     (usable only inside that workspace)

Local credentials live inside ``.jp/``, which jp never syncs to the server, so
a per-repo token never leaves the machine. Nothing here ever prints a token:
values are registered with :func:`ui.register_secret` the moment they are read.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from . import ui
from .errors import AuthError, UsageError
from .paths import DOT_DIR

REGISTRY_NAME = "credentials.json"
TOKENS_DIRNAME = "credentials.d"

# A credential name must start with a letter/digit and use only a safe,
# filename-friendly alphabet (it becomes part of the token file name).
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_MAX_NAME = 64


@dataclass
class Credential:
    """A resolved credential: its name, token-file path, and storage scope."""

    name: str
    token_path: str
    scope: str  # "global" | "local"


# --------------------------------------------------------------------------- #
# Names & locations
# --------------------------------------------------------------------------- #
def validate_name(name: str) -> str:
    """Return the cleaned credential name, or raise ``UsageError``."""
    cleaned = (name or "").strip()
    if not cleaned:
        raise UsageError("credential name must not be empty")
    if len(cleaned) > _MAX_NAME:
        raise UsageError(f"credential name is too long (max {_MAX_NAME} characters)")
    if not _NAME_RE.match(cleaned):
        raise UsageError(
            f"invalid credential name {name!r}: use only letters, digits, '.', '-', '_' "
            "(and start with a letter or digit)"
        )
    return cleaned


def global_dir() -> Path:
    """The per-user global jp config directory (``~/.config/jp``)."""
    return Path(os.path.expanduser("~/.config/jp"))


def _local_dir(root: Path) -> Path:
    return Path(root) / DOT_DIR


def _scope_dir(scope: str, root: Path | None) -> Path:
    if scope == "global":
        return global_dir()
    if scope == "local":
        if root is None:
            raise UsageError(
                "a local credential requires being inside a jp workspace; "
                "run this inside a cloned/initialized folder, or use --global"
            )
        # A local token lands in .jp/; make sure git ignores the whole dir first
        # so the token can never be committed by accident.
        from . import config as _config

        _config.ensure_dot_gitignore(root)
        return _local_dir(root)
    raise UsageError(f"unknown credential scope: {scope!r}")


def _registry_path(scope_dir: Path) -> Path:
    return scope_dir / REGISTRY_NAME


def _token_file(scope_dir: Path, name: str) -> Path:
    return scope_dir / TOKENS_DIRNAME / f"{name}.token"


# --------------------------------------------------------------------------- #
# Registry I/O (private files only)
# --------------------------------------------------------------------------- #
def _read_registry(path: Path) -> dict:
    if not path.is_file():
        return {"version": 1, "credentials": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UsageError(f"could not read credential registry {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("credentials"), dict):
        raise UsageError(f"malformed credential registry: {path}")
    return data


def _write_private(path: Path, text: str) -> None:
    """Atomically write ``text`` to ``path`` with 0600 perms (dir 0700)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path.parent, 0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o600)
        os.replace(str(tmp), str(path))
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(str(tmp))
        raise


def _save_registry(path: Path, data: dict) -> None:
    data.setdefault("version", 1)
    _write_private(path, json.dumps(data, indent=2) + "\n")


def _entries(scope_dir: Path, scope: str) -> list[Credential]:
    reg = _read_registry(_registry_path(scope_dir))
    out: list[Credential] = []
    for name, entry in sorted(reg.get("credentials", {}).items()):
        if isinstance(entry, dict) and entry.get("token_path"):
            out.append(Credential(name=name, token_path=str(entry["token_path"]), scope=scope))
    return out


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def list_credentials(root: Path | None = None) -> list[Credential]:
    """All available credentials. A local credential shadows a global one of
    the same name (local wins when both exist)."""
    by_name: dict[str, Credential] = {}
    for cred in _entries(global_dir(), "global"):
        by_name[cred.name] = cred
    if root is not None:
        for cred in _entries(_local_dir(root), "local"):
            by_name[cred.name] = cred
    return sorted(by_name.values(), key=lambda c: c.name)


def resolve(name: str, root: Path | None = None) -> Credential | None:
    """Find a credential by name: local scope first, then global."""
    if not name:
        return None
    if root is not None:
        for cred in _entries(_local_dir(root), "local"):
            if cred.name == name:
                return cred
    for cred in _entries(global_dir(), "global"):
        if cred.name == name:
            return cred
    return None


def add(
    name: str,
    token: str,
    *,
    scope: str,
    root: Path | None = None,
    overwrite: bool = False,
) -> Credential:
    """Store a token VALUE under ``name`` in ``scope`` (writes a 0600 file)."""
    name = validate_name(name)
    token = (token or "").strip()
    if not token:
        raise AuthError("refusing to save an empty token")
    scope_dir = _scope_dir(scope, root)
    reg_path = _registry_path(scope_dir)
    reg = _read_registry(reg_path)
    if name in reg.get("credentials", {}) and not overwrite:
        raise UsageError(
            f"a {scope} credential named {name!r} already exists; "
            "choose another name or pass --force to overwrite"
        )
    tok_path = _token_file(scope_dir, name)
    _write_private(tok_path, token + "\n")
    ui.register_secret(token)
    reg.setdefault("credentials", {})[name] = {"token_path": str(tok_path)}
    _save_registry(reg_path, reg)
    return Credential(name=name, token_path=str(tok_path), scope=scope)


def add_path(
    name: str,
    token_path: str,
    *,
    scope: str,
    root: Path | None = None,
    overwrite: bool = False,
) -> Credential:
    """Register an EXISTING token file by path under ``name`` (no copy made)."""
    name = validate_name(name)
    path = Path(os.path.expanduser(token_path))
    if not path.is_file():
        raise AuthError(f"token file does not exist: {path}")
    scope_dir = _scope_dir(scope, root)
    reg_path = _registry_path(scope_dir)
    reg = _read_registry(reg_path)
    if name in reg.get("credentials", {}) and not overwrite:
        raise UsageError(
            f"a {scope} credential named {name!r} already exists; "
            "choose another name or pass --force to overwrite"
        )
    reg.setdefault("credentials", {})[name] = {"token_path": str(token_path)}
    _save_registry(reg_path, reg)
    return Credential(name=name, token_path=str(token_path), scope=scope)


def read_token(cred: Credential) -> str:
    """Read a credential's token value (registers it for redaction)."""
    path = Path(os.path.expanduser(cred.token_path))
    if not path.is_file():
        raise AuthError(f"token file for credential {cred.name!r} is missing: {path}")
    _warn_if_world_readable(path)
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise AuthError(f"could not read token file {path}: {exc}") from exc
    if not token:
        raise AuthError(f"token file is empty: {path}")
    ui.register_secret(token)
    return token


def _warn_if_world_readable(path: Path) -> None:
    """On POSIX, warn if the token file is group/other-readable (shared box)."""
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & (stat.S_IRGRP | stat.S_IROTH | stat.S_IWGRP | stat.S_IWOTH):
        ui.warn(f"token file {path} is accessible to other users; run: chmod 600 {path}")
