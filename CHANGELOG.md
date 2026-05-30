# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
