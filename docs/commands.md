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

`--history` additionally fetches the committed version-history backup from the
remote `<prefix>/__jp/` after the file pull (see [`jp fetch`](#jp-fetch-branch)).
Every object is byte-verified before it is placed; a corrupt or hostile object
aborts the clone non-zero. A remote with no backup is a friendly note, not an
error. Without `--history`, clone is byte-identical to before; it never runs on
`--dry-run`.

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

When **versioning is active** (HEAD resolves to a commit *or* a `staged.json`
exists) it also appends an offline **Versioning** section computed without any
API call and without touching `.jp/index.json`:

- *staged (to be committed)* — `staged.json` vs the HEAD tree;
- *modified / new / deleted (not staged)* — the working tree vs `staged.json`;
- *outputs only (not staged)* — a hybrid notebook whose code is unchanged but
  whose outputs churned (a pure re-run); never reported under
  `versioning.notebook_outputs=full`.

If versioning is inactive the output is byte-identical to before. A conflict
still exits with code 6.

### `jp pull [path...]`
Download remote changes into the working tree. Additive by default — **never
deletes** a local file. A true three-way conflict (both sides changed) aborts
that file instead of overwriting your local copy. `--dry-run` previews.

### `jp push [path...]`
Upload local changes to the remote. Additive by default — **never deletes** a
remote file. Hidden files (dotfiles) are skipped and reported (the server
rejects them) without aborting the run. Conflicts abort that file rather than
overwriting a newer remote version. `--dry-run` previews.

**Path-scoped push.** With one or more `PATH` arguments, push **only** those
files (or all eligible files under a named directory) — the quick path for
pushing a single file without committing. A scoped push is additive-only: mirror
deletes are never offered (naming one local file can't imply which remote files
to delete). A named path that matches nothing eligible (not found, ignored, or a
skipped dotfile) prints a per-path error; if every named path matched nothing the
command exits with code 2.

**The commit-gate (versioning).** When versioning is active (HEAD resolves to a
commit) *and* the working tree has uncommitted changes (within the pushed scope),
`jp push` first offers, interactively:

```
You have uncommitted changes.
  [c] commit them first, then push
  [p] push without versioning  (this once)
  [a] always push without versioning  (don't ask again)
  [x] cancel
```

- `c` stages all and commits (asking for a message; Enter uses a dated default),
  then pushes; `p` pushes raw once; `a` persists `versioning.push_prompt=never`
  and pushes; `x` cancels with **nothing sent** (exit 0).
- `--raw` (alias `--no-verify`) skips the gate entirely.
- `versioning.push_prompt=never` disables the prompt permanently.
- The gate is **offline** (decided from local state before any network call) and
  **never blocks CI**: in a non-tty it prints one warning and proceeds.
- A repo that never adopted versioning never reaches the gate.

**History backup.** After a successful data push, `jp push` can mirror the
committed version history to `<prefix>/__jp/` on the remote (objects + refs).
Governed by `versioning.mirror_history` (`ask`/`always`/`never`); on the first
push with unmirrored history the `ask` mode shows a one-time prompt
(`[a]` always / `[n]` never / `[o]` once). `--backup-history` forces a one-shot
backup regardless of the setting (and does not change config). The mirror is
**best-effort**: a failure is a warning and **never** changes the push exit code.
`--dry-run` never mirrors and never persists config.

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

### `jp diff [path]`
Show file-level differences between the local copy and the remote: a unified
diff for text files, `binary differs` for binary. Read-only.

With `--staged` (alias `--cached`) it instead diffs the versioning **staging
area against the HEAD commit's tree**, fully offline — it never builds the API.
Notebooks are diffed outputs-free (the hybrid normalized code text), so a pure
re-run shows no change. An unborn HEAD treats everything staged as new.

## Versioning (opt-in)

A git-like, content-addressed version store living entirely under `.jp/`. It is
**opt-in**: until the first `jp add` / `jp commit` every other command behaves
byte-identically and `config.json` gains no versioning key. `add`, `commit`,
`log`, `show`, `checkout`, `fsck`, `gc` and `unversion` are entirely **offline**;
`fetch` and `restore` talk to the remote backup. There are **no branches** in v1
(single linear `main`). Hashes are shown short (12 chars). Config keys live under
`versioning.*` (see [`jp config`](#jp-config)).

### `jp add [path...]`
Stage files for the next commit into `.jp/staged.json`, snapshotting their
content into the object store **at add time** (the full content of the next
commit's tree, not a delta). `-A`/`--all` stages the whole working tree (and
stages the *deletion* of any tracked path that is gone). With **no paths and no
`-A`** it is a read-only preview of what `-A` would stage. `-n`/`--dry-run`
writes nothing. Honors `.jpignore`, skips `.jp/` and symlinks, and hard-skips
`.git/`. Files over `versioning.max_blob_mb` (default 100 MiB) are warned and
not versioned. Offline and idempotent. Exits with code 2 if every named path
matched nothing and nothing else was staged.

```bash
jp add -A
jp add notebook.ipynb src/train.py
```

### `jp commit -m <msg>`
Record the staged tree as a new commit and advance the current branch (under the
per-repo versioning lock, with a compare-and-swap on the ref). `-m`/`--message`
is **required** and must be non-empty. `-A`/`--all` stages the whole working tree
first. An **empty** commit (tree unchanged vs the parent) is refused unless
`--allow-empty`. `-n`/`--dry-run` shows what would be committed and writes
nothing. Objects are written before the ref moves, so a crash leaves harmless
orphans and HEAD still resolves to the previous commit. Offline.

```bash
jp commit -A -m "tuned learning rate"
```

### `jp log [ref]`
Walk and print commit history from `ref` (a branch name or commit sha; default
HEAD) back along the first-parent chain. `--oneline` prints `<short> <subject>`;
`--stat` appends the per-commit tree diff (added/modified/deleted, vs the empty
tree for the root commit); `-n`/`--max-count N` limits to the N latest. An
unborn/empty repo prints `no commits yet` and exits 0. Read-only, offline.

```bash
jp log --oneline -n 10
```

### `jp show [commit] [path]`
Show a commit (default HEAD) and the diff it introduced vs its first parent (the
empty tree for the root commit). Text files render as a unified diff; binary as
`binary differs`; notebooks as an **outputs-free** code-cell diff (a pure re-run
shows nothing). `path` limits output to one file; `--stat` prints only the
changed-path summary. An unborn repo with the default HEAD prints `no commits
yet` (exit 0); an explicit unknown revision is a non-zero error. Read-only,
offline.

```bash
jp show 1a2b3c4d5e6f
```

### `jp checkout <commit> [path...]`
Restore files from a commit into the working tree — the **most destructive**
versioning command, so it is conservative by construction:

- `jp checkout <commit>` (full mode) restores the whole tree and moves HEAD
  (attaching to a branch when `commit` is symbolic, else detaching to the sha);
- `jp checkout <commit> path...` (path-scoped) restores only those paths, never
  moves HEAD, and never deletes extras.

A file with **uncommitted local edits blocks the whole checkout** (zero writes)
unless `-f`/`--force`. In full mode, working files **not** in the target commit
are reported but left alone unless `--remove-extra`; removing a never-committed
("untracked") file then needs confirmation (`-y`/`--yes`) and is **refused in a
non-tty** (exit 6). `-n`/`--dry-run` prints the full plan and writes nothing.
Every restored file is re-hashed against the tree; a symlink conflict or corrupt
object fails just that path (exit 7, partial) without leaving wrong bytes. After
a full checkout jp warns that the working tree now matches an older commit, so
the next `jp push` will overwrite the remote with this content. Offline.

```bash
jp checkout HEAD~ src/train.py   # restore one file from the parent commit
jp checkout 1a2b3c4d5e6f -f      # full roll-back, overwriting local edits
```

### `jp fetch [branch...]`
Download committed version history from the remote `<prefix>/__jp/` backup into
the **local** object store, verifying every object (re-hash) before placing it.
With no argument it fetches the configured default branch (`main`). The local ref
is advanced **only on a proven fast-forward**; diverged history downloads the
objects but leaves the ref and reports it (exit 7, partial) for manual reconcile.
A verification failure aborts non-zero and plants nothing.

### `jp restore`
Rebuild **local** history from the remote backup (recovery for a lost `.jp/`),
then check out HEAD into the working tree. The checkout respects its safety
gates: an empty dir gets everything written, but a working file with uncommitted
edits blocks unless `-f`/`--force`. A corrupt/hostile remote object aborts before
any working-tree write. If the remote has no backup, nothing is checked out and
it exits 0 with a note.

### `jp fsck`
Verify the integrity of the local store (read-only): walk reachability from HEAD
and every branch tip and re-hash every reachable object, reporting **missing**
(referenced but absent), **corrupt** (fails its re-hash), and **dangling**
HEAD/refs. `--full` additionally re-hashes every loose object on disk (including
unreachable ones). Exit 0 if clean (or no history yet); non-zero (1) if any
problem is found.

### `jp gc`
Reclaim space by pruning unreachable loose objects. Default is a **dry run**: it
reports how many objects/bytes *would* be reclaimed and writes nothing.
`--prune` actually deletes them. An object is a candidate only when it is both
unreachable (no commit/branch/HEAD/staged reference) **and** older than
`--grace DAYS` (default 14), so an in-flight commit's objects are never pruned.
Runs under the versioning lock. **Local only** — the remote `__jp` mirror is
append-only and is not reclaimed in v1.

```bash
jp gc            # preview
jp gc --prune    # actually reclaim
```

### `jp unversion`
Opt out: remove the **local** version store — `HEAD`, `staged.json`, the
`format` marker, the versioning lock, and the `objects/` `refs/` `packs/`
directories. It leaves **everything else** untouched: `config.json`,
credentials, `.jp/index.json` (the sync base), `.jp/.gitignore`, and **all
working files**. Requires confirmation (`-y`/`--yes`; **refused** in a non-tty
without `--yes`, exit 6 — history is never destroyed silently). Removal is
symlink-safe and runs under the versioning lock. It **never** touches the
remote: if a history mirror exists it only **prints** how to remove it.

> **Removing the remote backup.** `jp unversion` does not delete the server-side
> mirror (too dangerous on a shared box). To remove it, delete `<prefix>/__jp/`
> on the server by hand — e.g. via the Jupyter file browser. `jp unversion`
> prints the exact path for your workspace.

## Notebooks

### `jp kernel`
Set up a VS Code **remote** Jupyter kernel so notebooks run in the right
directory. When VS Code runs a local `.ipynb` against a remote kernel, the
kernel's working directory is the server's home, not the notebook's folder, so
relative paths (`pd.read_excel("dataset/x.xlsx")`) raise `FileNotFoundError`;
VS Code's `notebookFileRoot` does not apply to remote kernels.

`jp kernel` does **not** touch the remote. It prints a one-time IPython startup
snippet — pre-filled with this workspace's local root and `prefix` — and copies
it to your clipboard. You paste it into one cell, run it once, and restart the
kernel; every notebook in the workspace then starts in the correct directory
automatically. By default the snippet is only copied (kept out of the output);
`--script` prints it, and `--no-clipboard` skips copying.

`--link` instead shows and copies the **server connection URL** (with your
token) to paste into VS Code's kernel picker. This is the one command that
prints your token, so it is opt-in and asks for confirmation first (`-y/--yes`
skips the prompt); treat the URL like a password.

Must be run inside a workspace. See
[vscode-remote-cwd.md](vscode-remote-cwd.md) for the full walkthrough, including
how to connect VS Code to the remote kernel.

## Remote shell

### `jp terminal`
Turn your local terminal into the **remote machine's shell**, in one command and
with no setup. It creates an ephemeral Jupyter terminal on the server (the same
thing the web UI's "New → Terminal" opens), connects to it, and proxies your
terminal to it — so you get a real, interactive remote shell, starting in this
workspace's mapped folder.

It is intentionally narrow and safe on a shared server: the **only** remote calls
it makes are creating and deleting an ephemeral terminal session
(`POST`/`DELETE /api/terminals`). It never reads, writes, moves, or deletes a
file, and the session it creates is always deleted on exit (only that session,
never anyone else's). The token is sent only in the `Authorization` header on the
websocket handshake, never in a URL.

- `--no-cd` starts in the server's default directory instead of the workspace
  folder.
- `-y/--yes` skips the one-time confirmation prompt.
- Press **Ctrl-]** to force-disconnect (telnet convention) if the remote shell
  ever wedges; `exit`/Ctrl-D end it normally.
- The websocket client is hand-rolled on the standard library, so `jp` keeps its
  zero-dependency promise.
- Requires a POSIX terminal (`termios`). On Windows there is no raw PTY, so the
  command falls back to opening the Jupyter web UI (use New → Terminal there);
  that fallback shows a token-bearing URL and asks for confirmation first.
- Needs server-side terminals to be enabled (most Jupyter Server / Lab
  deployments enable them by default); if they are disabled, `jp terminal` says
  so and exits without creating anything.

Must be run inside a workspace.

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

Editable settings: `mirror`, `dotfiles`, `color`, `timeout`, and the opt-in
versioning keys `versioning.push_prompt` (`ask`/`never`/`always`),
`versioning.mirror_history` (`ask`/`always`/`never`), `versioning.notebook_outputs`
(`hybrid`/`full`), `versioning.max_blob_mb` (positive int, default 100),
`versioning.author` (freeform `Name <email>`). Versioning keys are written to
`config.json` only when changed from their default. Connection fields
(`base_url`, `prefix`, `token_path`) are shown for context and settable via
`jp config set`.

> There is **no** `versioning.enabled` key — the presence of `.jp/HEAD` (i.e.
> your first `add`/`commit`) is the switch. A hand-edited unsupported value
> (e.g. the lossy notebook `strip` mode) silently degrades to the safe default.

### `jp ignore [pattern...]`
Add or list `.jpignore` patterns (gitignore-style). Always-on ignores include
`.jp/`, `.git/`, `__pycache__/`, `.ipynb_checkpoints/`, `.DS_Store`, `*.pyc`.

> **Reserved names.** A **top-level** directory named `__jp` (the remote history
> mirror) or `jp-tmp` (the remote temp dir for atomic writes) is **reserved on
> both ends** and is never synced — `jp pull` never downloads it and a
> mirror-mode `jp push` never offers to delete it, so the history backup can't
> be clobbered. Only the first path segment is reserved; a nested `proj/__jp`
> is an ordinary file/dir and syncs normally.

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
