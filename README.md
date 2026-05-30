# jp

> A git-like CLI to sync local folders with a remote JupyterHub — zero dependencies, pure Python.

[![CI](https://github.com/pehqge/jp-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/pehqge/jp-cli/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/jp-cli.svg)](https://pypi.org/project/jp-cli/)
[![Python versions](https://img.shields.io/pypi/pyversions/jp-cli.svg)](https://pypi.org/project/jp-cli/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/pehqge/jp-cli/blob/main/LICENSE)

`jp` lets you `clone`, `pull`, and `push` against a JupyterHub server the same
way you would with `git` — but instead of a git remote, it talks to the
[Jupyter Contents API](https://jupyter-server.readthedocs.io/en/latest/developers/contents.html).
Edit notebooks and scripts in your own editor, keep a local backup, work
offline, and put your remote work under real version control.

> **Note on naming.** The PyPI package is `jp-cli` (the name `jp` was taken),
> but the command you actually run is `jp`.

---

## Why `jp`?

The Jupyter web UI is clumsy once you have more than a handful of files. If you
work on a shared JupyterHub (a university lab, a research cluster) you probably
want to:

- Edit in **your** editor — VS Code, vim, whatever — not the browser.
- Keep a **local backup** of remote work.
- Work **offline**, then sync when you reconnect.
- Wrap the local copy in **real `git`**.

`jp` bridges that gap with a workflow you already know.

---

## Demo

```console
$ jp clone https://hub.example.edu/user/pedro/work mywork
Cloning 'user/pedro/work' into 'mywork'...
  ↓ notebook.ipynb
  ↓ data/clean.csv
  ↓ utils.py
Done. 3 files, 1 directory.

$ cd mywork
# ...edit notebook.ipynb in your editor...

$ jp status
Changes to push:
  modified   notebook.ipynb
Up to date with remote otherwise.

$ jp push
Pushing to 'user/pedro/work'...
  ↑ notebook.ipynb
Done. 1 file pushed.
```

---

## Install

`jp` is pure standard-library Python, so any install method below gives you a
working `jp` command.

### Recommended: uv or pipx

```bash
# uv (fast, isolated)
uv tool install jp-cli

# or pipx
pipx install jp-cli

# or plain pip
pip install jp-cli
```

### Single-file zipapp (needs Python ≥ 3.8)

Download `jp.pyz` from the [latest release](https://github.com/pehqge/jp-cli/releases/latest)
and run it directly:

```bash
python jp.pyz version
# optionally drop it on your PATH:
chmod +x jp.pyz && mv jp.pyz ~/.local/bin/jp
```

### Standalone binary (no Python required)

Grab the binary for your OS from the
[latest release](https://github.com/pehqge/jp-cli/releases/latest), or use the
install scripts:

```bash
# macOS / Linux
curl -fsSL https://raw.githubusercontent.com/pehqge/jp-cli/main/scripts/install.sh | sh
```

```powershell
# Windows (PowerShell)
irm https://raw.githubusercontent.com/pehqge/jp-cli/main/scripts/install.ps1 | iex
```

Always verify what a piped installer does before running it — both scripts are
short and readable in [`scripts/`](scripts/).

---

## Quickstart

```bash
# 1. Get a token from the Hub UI (Token page) and clone
jp clone https://hub.example.edu/user/pedro/work
cd work

# 2. Edit files locally with whatever editor you like

# 3. See what changed
jp status

# 4. Sync
jp push      # upload your local changes
jp pull      # download remote changes
```

First time on a machine, run `jp login` to store your API token (it's tested
against `/api/me` before being saved). `jp` resolves the token in this order:
`--token-file` > `$JP_TOKEN_FILE` / `$JUPYTER_TOKEN` > the repo config >
global config > `~/.jupyter_ufsc_token`. Only the *path* to the token is stored
in config — the value itself lives in a file with mode `0600` and is **never**
committed, logged, or printed.

---

## Commands

| Command | Description |
|---------|-------------|
| `jp clone <url> [dir]` | Clone a remote directory into a local folder |
| `jp init [dir]` | Initialize `.jp/` in an existing directory |
| `jp login` | Onboard: store an API token (tested against `/api/me`) |
| `jp pull [path...]` | Download remote changes into the local folder |
| `jp push [path...]` | Upload local changes to the remote |
| `jp status [path...]` | Show local vs remote differences (read-only) |
| `jp ls [remote]` | List remote contents (read-only) |
| `jp diff [path...]` | Show the content diff for changed files |
| `jp config [get\|set\|unset\|list]` | Read/write configuration values |
| `jp ignore [pattern...]` | Manage `.jpignore` patterns |
| `jp rm --remote <path>` | Delete a remote path (gated; never run by sync) |
| `jp doctor` | Diagnose token, connectivity, version, perms, clock |
| `jp version` | Print the version |

Full reference: [`docs/commands.md`](docs/commands.md).

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | Success |
| `1` | Generic error |
| `2` | Usage error (bad arguments) |
| `3` | Not a `jp` repository |
| `4` | Auth error (bad/missing token) |
| `5` | Network error |
| `6` | Conflict (local and remote both changed) |
| `7` | Permission denied |
| `8` | Unsafe path (path-jail violation) |
| `130` | Interrupted (SIGINT) |

---

## Security

`jp` is built to be safe on **shared machines** (lab computers, cluster login
nodes). The guarantees:

- **Never deletes remote files by default.** Removing something locally does
  *not* remove it on the server. Remote deletion only happens through an
  explicit, gated command with confirmation.
- **Conflicts never overwrite.** If a file changed both locally and remotely,
  `jp` refuses, reports the path, and exits with code `6`. There is no silent
  merge.
- **Path-jail.** Every path from a remote listing is resolved inside the clone
  root. `..` traversal, absolute paths, and symlinks crossing the boundary are
  rejected — a malicious or buggy server cannot make `jp` write outside your
  folder.
- **Your token stays local.** Kept in a token file (mode `0600`) outside the
  tracked tree; only its *path* is recorded in config. Sent over HTTPS in the
  `Authorization` header only — never in a URL, never logged, never printed,
  never committed.
- **Dotfiles are skipped by default.** `.env`, `.ssh`, and friends are left out
  of sync (reported as `S`) unless you explicitly change the `dotfiles` policy.

See [`SECURITY.md`](SECURITY.md) and [`docs/security.md`](docs/security.md) for
the full threat model and how to report a vulnerability.

---

## FAQ

**Why doesn't `.gitignore` (or my other dotfiles) get pushed?**
By default `jp`'s dotfile policy is `skip`, so hidden files are left out of the
push plan and reported as `S` (skipped) — you don't accidentally leak secrets
like `.env` or `.ssh` from a shared machine. (Many Jupyter servers also reject
hidden-file writes outright with `allow_hidden=False`, returning HTTP 400.) If
you really need hidden files synced, opt in:

```bash
jp config set dotfiles mangle   # uploads e.g. .gitignore as dot__gitignore
```

See [`docs/commands.md`](docs/commands.md) for the dotfile policy trade-offs.

**Is this a replacement for `git`?**
No. `jp` has no branching or merging model — it just syncs one local folder
with one remote prefix. Put `git` *on top* of the local copy if you want
version control.

**Can `jp` delete my remote files if I `rm` them locally?**
No. Local deletions are not propagated. Remote deletion is a separate, gated,
confirmed action.

**Does it need `requests` or any other package?**
No. `jp` uses only the Python standard library (`urllib`), so it runs anywhere
Python ≥ 3.8 does — and the zipapp/binary builds need nothing at all.

**What about conflicts?**
If both sides changed, `jp status` flags the file and `push`/`pull` refuse
until you resolve it manually. Your data is never silently clobbered.

---

## Troubleshooting

**"Connection refused" / network errors (exit 4).**
The Hub may be asleep or down. Open it in a browser to confirm it's running,
check the URL in `.jp/config.json`, then retry. `jp` retries with backoff
before giving up.

**"Auth error" / 403 (exit 4).**
Your token is missing, wrong, or expired. Generate a fresh one from the Hub's
Token page and run `jp login` (or pass `--token-file` / set `JP_TOKEN_FILE`).

**`jp: command not found` after install.**
The install location isn't on your `PATH`. For `~/.local/bin` (macOS/Linux),
add it to your shell profile:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

On Windows the install script adds `%LOCALAPPDATA%\jp\bin` to your user PATH —
open a **new** terminal so the change takes effect.

---

## Contributing

Contributions are welcome! See [`CONTRIBUTING.md`](CONTRIBUTING.md) for setup,
testing, and PR guidelines, and please follow our
[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).

Architecture overview: [`docs/architecture.md`](docs/architecture.md).

---

## License

[MIT](LICENSE) © 2026 Pedro Gimenez.
