# Command reference

All commands accept the global flags `-v/--verbose`, `-q/--quiet`, `--json`,
`--no-color`, and `--timeout <seconds>`. Run `jp <command> --help` for details.

## Workspace

### `jp clone <url> [dir]`
Create a new local workspace from a remote Jupyter folder. Parses the lab/tree
URL, stores the validated configuration in `.jp/`, and downloads the remote tree.
If `dir` is omitted it is inferred from the last path component.

The clone **refuses** a remote prefix that resolves to the server root or to a
shared directory (e.g. `compartilhado`), so you can never accidentally point a
workspace at everyone's data.

### `jp init [dir]`
Initialize a `.jp/` workspace in an existing local folder (the first-push case),
prompting for the server URL and remote prefix.

### `jp login`
Interactive onboarding. Asks where your API token lives (or pastes one and saves
it to a local file with `0600` permissions), tests the connection, and stores
only the token's **path** — never the token itself — in the config.

## Synchronization

### `jp status`
Show, read-only, how the working tree differs from the last synced state and the
remote: added, modified, conflicting, and skipped (hidden) files. Never writes
anything.

### `jp pull [path...]`
Download remote changes into the working tree. Writes atomically and **never
deletes** a local file. A true three-way conflict (both sides changed) aborts
that file instead of overwriting your local copy.

### `jp push [path...]`
Upload local changes to the remote. **Never deletes** a remote file. Hidden
files (dotfiles) are skipped and reported — the server rejects them — without
aborting the run. A three-way conflict aborts that file rather than overwriting
someone else's newer version.

Both `pull` and `push` accept `--dry-run` (`-n`) to preview the plan.

### `jp diff [path...]`
Show file-level differences between the local copy and the last synced state
(and the remote with `--remote`).

## Inspection

### `jp ls [remote-path]`
List a remote directory through the Contents API without touching local disk.

### `jp config [get|set|unset|list] [key] [value]`
Read or write workspace configuration. Use `--global` for the user-level config.

### `jp ignore [pattern...]`
Add or list `.jpignore` patterns (gitignore-style). Some ignores are always on:
`.jp/`, `.git/`, `__pycache__/`, `.ipynb_checkpoints/`, `.DS_Store`, `*.pyc`.

### `jp doctor`
Diagnose your setup: token present and valid, server reachable, server running
(vs. stopped), prefix writable, and clock sanity. Prints actionable hints.

### `jp version`
Print the jp version. Also available as `jp --version`.

## Deletion (gated)

### `jp rm <path>`
The **only** command that deletes on the remote, and it is intentionally hard to
misuse:

- the path is validated against the workspace prefix immediately before the call;
- it prints a dry-run of exactly what will be removed;
- it requires a typed confirmation (or `--yes` in scripts; in a non-interactive
  shell without `--yes` it refuses);
- `--recursive` is required to remove a non-empty directory, and removal then
  proceeds bottom-up (the server does not delete recursively).

> Note: on many JupyterHub deployments a remote delete moves the file to a trash
> folder rather than erasing it, and can leave the parent listing in a bad state.
> This is one more reason `push`/`pull` never delete, and why `jp rm` is gated.

## Exit codes

| Code | Meaning                          |
|------|----------------------------------|
| 0    | success                          |
| 1    | generic error                    |
| 2    | usage error (bad arguments)      |
| 3    | not a jp workspace               |
| 4    | authentication error             |
| 5    | network error / server down      |
| 6    | conflict (aborted to avoid loss) |
| 7    | permission denied on the server  |
| 8    | unsafe path refused              |
| 130  | interrupted (Ctrl-C)             |
