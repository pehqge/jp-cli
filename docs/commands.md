# Command reference

All commands accept the global flags `-q/--quiet` and `--no-color`. Run
`jp <command> --help` for the full option list of any command.

## Workspace

### `jp clone <url> [dir]`
Create a new local workspace from a remote Jupyter folder and download its tree.
The URL is the one from your browser's address bar, e.g.
`https://host/user/<name>/lab/tree/<folder>`; `tree`, `doc/tree`, `notebooks`,
named servers and standalone servers are also understood. Alternatively pass
`--base-url <api-base>` and `--prefix <remote-path>` explicitly. `--credential
<name>` picks a saved credential (see `jp login`); if omitted, jp uses the only
one or prompts you to choose. `--token-path` records a token file directly.
`--dry-run` previews without writing.

The clone **refuses** a prefix that resolves to the server root or a shared
directory (e.g. `compartilhado`, `lapix`), so a workspace can never point at
everyone's data.

### `jp init <url>`
Turn the current directory into a jp workspace without downloading anything
(useful for a first push of an existing local folder). Same URL / `--base-url` +
`--prefix` forms as `clone`.

### `jp login`
Save a **named credential** for a server, interactively. It explains how to get a
JupyterHub API token, prompts you to paste it (hidden, via `getpass`), asks for a
name (e.g. `myserver`), and asks whether to save it **global** (everywhere) or
**local** (this workspace only). The token value is written to a private `0600`
file and registered for redaction; it is never echoed and never written into
`.jp/config.json`. Run it repeatedly to store several servers.

Scriptable flags: `--name <name>`, `--global`/`--local` (local requires a
workspace), `--token-stdin` (read the value from stdin), `--token-path <file>`
(register an existing token file), `--force` (overwrite an existing name).

Token resolution at sync time: `$JP_TOKEN` → `$JP_TOKEN_FILE` → the recorded
credential (local registry before global) → legacy `token_path` → legacy
`~/.config/jp/token`.

## Synchronization

### `jp status`
Show, read-only, how the working tree differs from the last synced state and the
remote: added, modified, conflicting, and skipped (hidden) files. Never writes.

### `jp pull [path...]`
Download remote changes into the working tree. Additive by default — **never
deletes** a local file. A true three-way conflict (both sides changed) aborts
that file instead of overwriting your local copy. `--dry-run` previews.

### `jp push [path...]`
Upload local changes to the remote. Additive by default — **never deletes** a
remote file. Hidden files (dotfiles) are skipped and reported (the server
rejects them) without aborting the run. Conflicts abort that file rather than
overwriting a newer remote version. `--dry-run` previews.

### Mirror mode (`--mirror` / config `mirror`)
By default, sync is additive. With mirror mode on, `push`/`pull` additionally
*offer to delete* files that exist on one side but not the other:

- `push --mirror` — remote files with no local counterpart become deletion candidates.
- `pull --mirror` — local files with no remote counterpart become deletion candidates.

Deletion is **never silent**. In a terminal jp shows a keep/delete selector
(arrow keys; every file defaults to keep) and deletes only what you mark. With
`--yes` it deletes all candidates (for scripts). In a non-interactive shell
without `--yes`, it deletes nothing and lists what it would have offered.
Persist the setting with `jp config set mirror true`, or use `--mirror` /
`--no-mirror` for a single run.

### `jp diff [path...]`
Show file-level differences between the local copy and the last synced state
(and the remote with `--remote`).

## Inspection & configuration

### `jp ls [remote-path]`
List a remote directory through the Contents API without touching local disk.

### `jp config`
With no arguments, opens an interactive settings editor (arrow keys to move,
Space to change a value, `i` for help on the selected setting, `/` to search,
Enter to save, Esc to cancel). In a non-interactive shell it prints the current
settings instead. Scriptable forms:

```
jp config list
jp config get <key>
jp config set <key> <value>
```

Editable settings: `mirror`, `dotfiles`, `color`, `timeout`. Connection fields
(`base_url`, `prefix`, `token_path`) are shown for context and settable via
`jp config set`.

### `jp ignore [pattern...]`
Add or list `.jpignore` patterns (gitignore-style). Always-on ignores include
`.jp/`, `.git/`, `__pycache__/`, `.ipynb_checkpoints/`, `.DS_Store`, `*.pyc`.

### `jp doctor`
Diagnose your setup: token present and valid, server reachable, server running
(vs. stopped), and clock sanity. Prints actionable hints.

### `jp update`
Update jp to the latest version. Detects whether jp was installed via pipx, uv,
or pip and upgrades in place; for a standalone binary it prints the reinstall
command. `jp update --check` only reports whether a newer release exists.

### `jp version`
Print the jp version. Also available as `jp --version`.

## Deletion (gated)

### `jp rm <path>`
The **only** command that deletes on the remote outside of mirror mode, and it
is intentionally hard to misuse:

- the path is validated against the workspace prefix immediately before the call;
- it prints a dry-run of exactly what will be removed (`--dry-run` stops there);
- it requires a typed confirmation (or `--yes` in scripts; a non-interactive
  shell without `--yes` refuses);
- `--recursive` is required to remove a non-empty directory, and removal then
  proceeds bottom-up (the server does not delete recursively);
- `--local` also removes the local copy of a single file.

> On many JupyterHub deployments a remote delete moves the file to a trash
> folder rather than erasing it, and can leave the parent listing in a bad
> state — one more reason `push`/`pull` never delete by default.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | success |
| 1 | generic error |
| 2 | usage error (bad arguments) |
| 3 | not a jp workspace / config problem |
| 4 | authentication error |
| 5 | network error / server down |
| 6 | safety refusal (path-jail, bad prefix, conflict abort) |
| 7 | partial failure (some files failed) |
| 130 | interrupted (Ctrl-C) |
