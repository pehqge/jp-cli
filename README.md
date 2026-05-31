<h1 align="center">jp</h1>

<p align="center">
  <em>A git-like CLI to safely sync local folders with a remote JupyterHub — zero dependencies, pure Python.</em>
</p>

<p align="center">
  <a href="https://github.com/pehqge/jp-cli/actions/workflows/ci.yml"><img src="https://github.com/pehqge/jp-cli/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/pehqge/jp-cli/releases"><img src="https://img.shields.io/github/v/release/pehqge/jp-cli?include_prereleases&sort=semver&label=release" alt="Release"></a>
  <img src="https://img.shields.io/badge/python-3.9%2B-blue" alt="Python 3.9+">
  <a href="https://github.com/pehqge/jp-cli/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-yellow" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/dependencies-zero-brightgreen" alt="Zero dependencies">
</p>

---

`jp` keeps a local folder in sync with a directory on a **JupyterHub** server —
the way `git` keeps you in sync with a remote. You edit notebooks and scripts on
your laptop, `jp push` to send them up, run your training on the server's GPUs,
and `jp pull` the results back down.

It talks to the JupyterHub REST API directly, has **zero third-party
dependencies** (pure Python standard library), and runs anywhere Python 3.9+
runs — macOS, Windows, Linux.

```console
$ jp clone https://jupyter.vlab.ufsc.br/user/pedro.gimenez/lab/tree/privado
cloning privado -> ./privado
✓ clone: 12 transferred

$ cd privado
$ # ...edit files locally...
$ jp push
  push: train.py
  push: data/config.yaml
✓ push: 2 transferred, 10 up to date, 0 skipped, 0 conflict(s), 0 deleted, 0 failed
```

## Why jp?

- **Git-like workflow** — `jp clone`, `jp status`, `jp push`, `jp pull`. Same muscle memory.
- **Safe by default** — on a *shared* research machine, jp never deletes remote files unless you explicitly turn that on, and even then it asks you file-by-file. Conflicts are never silently overwritten.
- **Zero dependencies** — one install, no dependency hell; ships as a wheel, a single `.pyz`, or a standalone binary.
- **Cross-platform** — macOS, Windows, Linux; Python 3.9 → 3.13.

---

## Installation

> Recommended: install in an isolated environment with **uv** or **pipx** so the
> `jp` command lands on your `PATH` without touching system Python.

**With uv** (fastest):
```bash
uv tool install jp-cli
```

**With pipx**:
```bash
pipx install jp-cli
```

**Straight from GitHub** (before the first PyPI release):
```bash
pipx install "git+https://github.com/pehqge/jp-cli"
# or:  uv tool install "git+https://github.com/pehqge/jp-cli"
```

**Install script (macOS / Linux)** — downloads the standalone binary, no Python needed:
```bash
curl -fsSL https://raw.githubusercontent.com/pehqge/jp-cli/main/scripts/install.sh | sh
```

**Install script (Windows, PowerShell)**:
```powershell
powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/pehqge/jp-cli/main/scripts/install.ps1 | iex"
```

**Single file, no install** — grab `jp.pyz` from the
[latest release](https://github.com/pehqge/jp-cli/releases/latest) and run it
with any Python 3.9+:
```bash
python jp.pyz --help
```

Verify:
```bash
jp --version
```

> If `jp: command not found` after a pipx/uv install, run `pipx ensurepath` (or
> `uv tool update-shell`) and reopen your terminal.

---

## Getting started

### 1. Get your JupyterHub API token

`jp` authenticates with a personal API token from your JupyterHub.

1. Open your JupyterHub in a browser and log in (e.g. `https://jupyter.vlab.ufsc.br`).
2. Go to the **Token** page — usually the **Token** link in the top bar, or
   visit `https://<your-hub>/hub/token` directly.
3. Type a note (e.g. `jp`), leave the scopes blank (full access to what *you*
   can already do), and click **Request new API token**.
4. **Copy the token now** — JupyterHub shows it only once.

> Security: the token is like a password for your account. `jp` stores only the
> *path* to a token file, never the token value, and never logs or commits it.

### 2. Save the token to a file

Put the token in a private file on your machine (not in any repo):

```bash
# macOS / Linux
printf '%s\n' 'PASTE_YOUR_TOKEN_HERE' > ~/.jupyter_token
chmod 600 ~/.jupyter_token
```

```powershell
# Windows (PowerShell)
'PASTE_YOUR_TOKEN_HERE' | Out-File -Encoding ascii "$HOME\.jupyter_token"
```

`jp` looks for a token, in order, from: `--token-path` → `$JP_TOKEN` (the value
itself, handy for CI) → `$JP_TOKEN_FILE` (a path) → the path saved in your
workspace config → `~/.config/jp/token`.

### 3. Make sure your server is running

`jp` talks to your *single-user* server, so it must be started: open JupyterHub
and, if needed, click **Start My Server**. (`jp doctor` will tell you if it's
stopped.)

### 4. Clone your folder

Copy the URL of the folder from your browser's address bar — the `lab/tree/...`
URL works directly:

```bash
jp clone https://jupyter.vlab.ufsc.br/user/<you>/lab/tree/privado --token-path ~/.jupyter_token
cd privado
```

That creates a `privado/` folder with a `.jp/` workspace inside (like `.git/`),
records the token *path* in its config, and downloads the remote tree.

### 5. Work like git

```bash
jp status          # what changed, locally vs the server
jp push            # send local changes up
jp pull            # bring remote changes (e.g. training output) down
```

That's it. From any subdirectory of the workspace, `jp` finds its root
automatically (it walks up looking for `.jp/`, stopping at your home folder).

---

## Command reference

| Command | What it does |
|---|---|
| `jp clone <url> [dir]` | Clone a remote Jupyter folder into a new local directory. Accepts a `lab/tree` URL or `--base-url`/`--prefix`. |
| `jp init <url>` | Turn the current folder into a jp workspace (no download). |
| `jp login` | Register your token (path only) interactively. |
| `jp status` | Show local vs. remote differences. Read-only. |
| `jp push` | Upload local changes. Additive by default. |
| `jp pull` | Download remote changes. Additive by default. |
| `jp diff [path]` | Show file-level differences. |
| `jp ls [remote-path]` | List a remote directory (no local writes). |
| `jp config` | Interactive settings editor (see below). Also `config get/set/list`. |
| `jp ignore [pattern]` | Manage `.jpignore` patterns. |
| `jp rm <path>` | Delete on the remote — gated, dry-run + typed confirmation. The only deleter. |
| `jp doctor` | Diagnose token, connectivity, server status. |
| `jp update` | Update jp to the latest version. |
| `jp version` | Print the version (also `jp --version`). |

Global flags: `-q/--quiet`, `--no-color`. Every command has `--help`.

### `jp config` — interactive settings

Run `jp config` with no arguments in a terminal for a settings screen:

```
  Mirror mode (allow deletes)              false
> Dotfile policy                           skip
  Colored output                           auto
  Network timeout (s)                      30.0

Up/Down move · Space change · i info · / search · Enter save · Esc cancel
```

- **↑/↓** move · **Space** cycle the value · **i** show help for the selected
  setting · **/** search · **Enter** save · **Esc** cancel.

For scripts, the classic forms still work: `jp config list`,
`jp config get <key>`, `jp config set <key> <value>`.

### Mirror mode (deleting files to match the other side)

By default `jp push`/`jp pull` are **additive** — they never delete. If you want
true mirroring (delete on the remote when you delete locally, and vice-versa),
turn on **mirror mode**:

```bash
jp config set mirror true      # persist it, or use --mirror for one run
jp push --mirror               # one-off
```

With mirror on, after the normal sync jp finds files that exist on one side but
not the other and — **always, before deleting anything** — shows you the list
and lets you choose, with the arrow keys, which to **keep** and which to
**delete**:

```
Mirror mode: 2 file(s) exist on remote but not on the other side.
Choose which to DELETE on remote. Default is KEEP.

> [keep]   old_experiment.py
  [keep]   scratch.ipynb

Up/Down move · Space toggle · a delete-all · n keep-all · Enter confirm · Esc cancel
```

Nothing is deleted unless you mark it. In a non-interactive shell, mirror
deletions are refused unless you pass `--yes`. Conflicts (both sides changed) are
*never* deleted or overwritten.

### Keeping jp up to date

```bash
jp update           # detects pipx / uv / pip and upgrades in place
jp update --check   # just check; don't install
```

For a standalone binary install, `jp update` prints the one-line reinstall
command for your OS.

---

## Configuration

Each workspace stores its settings in `.jp/config.json` (JSON, never the token
value). Keys: `base_url`, `prefix`, `token_path`, `mirror`, `dotfiles`, `color`,
`timeout`. See [docs/commands.md](docs/commands.md) and
[docs/architecture.md](docs/architecture.md).

---

## Security

`jp` is built for a **shared** machine where a mistake can destroy someone
else's research. The guarantees:

- **`push`/`pull` never delete** unless you opt into mirror mode — and even then
  jp asks you, file by file, defaulting to keep.
- **Conflicts are never silently overwritten.** If both sides changed since the
  last sync, jp aborts that file and tells you.
- **Path-jailing.** Every remote operation is confined to your workspace's
  prefix. The server root and shared spaces (`compartilhado`, `lapix`,
  `shared`, …) are refused outright.
- **Untrusted server on download.** File names from the server are sanitized
  before anything is written locally (anti path-traversal / Zip-Slip), and
  writes are atomic and never follow a symlink.
- **Your token never leaks** — stored by path only, sent in the `Authorization`
  header (never a URL), redacted from all output, never committed.

Found a vulnerability? See [SECURITY.md](SECURITY.md) — please don't open a
public issue.

---

## FAQ

**Is `jp` related to git?** No — it borrows git's *workflow*, not its internals.
There's no remote version history on a JupyterHub.

**Does it need Jupyter installed locally?** No. Just Python 3.9+; it talks to the
Hub over HTTPS.

**Why won't my `.gitignore` (or any dotfile) upload?** Most JupyterHub servers
run with `allow_hidden=False`, which rejects creating hidden files (names
starting with `.`). `jp` detects this and *skips* dotfiles on push, reporting
them instead of failing — your `.git/`, `.gitignore`, `.env` etc. simply stay
local (which is usually what you want). A nice side effect: secrets in dotfiles
never get pushed by accident.

**Will it overwrite my work?** Never silently. A conflict aborts that file;
remote deletes are opt-in (mirror mode) and confirmed file-by-file.

## Troubleshooting

- **`jp: command not found`** — run `pipx ensurepath` / `uv tool update-shell`, reopen the terminal.
- **`your JupyterHub server appears to be stopped`** — open the Hub UI and click *Start My Server*.
- **`authentication failed` / HTTP 403** — your token expired; create a new one and `jp login` again (or update the token file).
- **A big upload times out** — raise the timeout: `jp config set timeout 120`.

Run `jp doctor` for a guided check.

---

## Contributing

Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) and the
[Code of Conduct](CODE_OF_CONDUCT.md). The project is standard-library only;
please keep it dependency-free.

## License

[MIT](LICENSE) © Pedro Gimenez
