# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Named credentials: `jp login` is now interactive — it explains how to get a
  JupyterHub API token, reads it with hidden input, asks for a name, and saves it
  **globally** (`~/.config/jp/`) or **locally** (a workspace's `.jp/`). Token
  values are written to private `600` files and never leave the machine; only the
  credential name is recorded in config. Scriptable via `--name`,
  `--global`/`--local`, `--token-stdin`, `--token-path`, `--force`.
- `jp clone` / `jp init` select a saved credential automatically when only one
  exists, or prompt to choose when several do (`--credential <name>` to skip the
  prompt); the choice is recorded in the workspace config.
- New `credential` config key and a `credentials.json` registry per scope.
- Every `.jp/` workspace now gets a `.jp/.gitignore` (`*`) so a workspace that is
  also a git repo can never commit its local state or a local token file.

### Changed

- Token resolution order is now `$JP_TOKEN` → `$JP_TOKEN_FILE` → the workspace's
  saved credential → legacy `token_path` / `~/.config/jp/token` (the legacy paths
  remain supported).

## [0.1.0] - 2026-05-30

### Added

- Initial release of `jp` (PyPI package `jp-cli`, command `jp`).
- Git-like commands: `clone`, `pull`, `push`, `status`, `diff`, `init`,
  `config`, `auth`, `version`.
- Three-way sync model (local / remote / base) with conflict detection.
- Conflict-safe synchronization: conflicting files are never overwritten
  silently (exit code `6`).
- Token-based authentication resolved as `--token-file` > `$JP_TOKEN_FILE` /
  `$JUPYTER_TOKEN` > repo config > global config > `~/.jupyter_ufsc_token`;
  only the token path is stored in config, and the token file is expected to be
  mode `0600`.
- Path-jail confinement: remote paths are resolved inside the clone root, with
  `..` traversal, absolute paths, and boundary-crossing symlinks rejected.
- `.jpignore` support plus built-in defaults (`.jp/`, dotfiles, `.git/`,
  `__pycache__/`, `*.pyc`); hidden files opt-in via `allow_hidden`.
- Pure standard-library HTTP client (`urllib`) for the Jupyter Contents API,
  with retry/backoff on network errors.
- Distribution via PyPI, a single-file `jp.pyz` zipapp, standalone per-OS
  PyInstaller binaries, and `install.sh` / `install.ps1` installers.

[Unreleased]: https://github.com/pehqge/jp-cli/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/pehqge/jp-cli/releases/tag/v0.1.0
