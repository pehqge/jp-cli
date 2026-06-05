# `jp run` — run a local script on the remote without pushing it

**Date:** 2026-06-05
**Branch:** `feat/jp-run-remote` (worktree off `origin/main`)
**Status:** approved design, ready for implementation plan

## Problem

While editing a Python script locally (e.g. in VS Code against a remote
JupyterHub), the user wants to run it on the remote machine **without** first
`jp push`-ing it — exactly as if they had typed `python script.py` in a local
shell sitting in the current folder. Any *data* files the script opens must
already exist on the remote (the user's responsibility); only the **script
source itself** is delivered on the fly.

## Goals

- One command that runs a local file on the remote and streams output live.
- Runs in the **exact mapped folder** the user is currently in (workspace root
  or any subfolder) — relative `open()`, sibling `import`, `__file__` all behave
  as if executed locally in that folder ("perfect parity").
- Interpreter-agnostic: shebang first, else extension map, else `--as` override.
- Works only inside an initialized `.jp` workspace (no URL mode).
- Propagates the remote process exit code.
- **Absolutely never overwrites or deletes any of the user's files**, even with
  many concurrent `jp run` invocations in different terminals.
- macOS + Linux now. Windows degrades with a clear message and is wired to light
  up when the separate `jp terminal` Windows work lands.

## Non-goals (YAGNI)

- No auto-sync of the data files the script touches (user's responsibility; a
  one-line reminder only).
- No Jupyter-kernel execution path (kernel = Python-only REPL semantics; we want
  true subprocess/CLI parity via terminado).
- No notebook (`.ipynb`) execution in v1.
- No URL / workspace-free mode (unlike `jp terminal`/`jp live`).

## Approach

Reuse the existing `terminado` PTY transport that `jp terminal` already uses
(`POST /api/terminals` → websocket, raw-mode local proxy). The script source is
delivered **through the PTY session itself** — never via the Contents API, so it
never goes through jp's sync/push machinery.

### Why a temp file in the *current folder* (not stdin-stream, not a subdir)

- **Perfect parity requires the temp file to sit in the same folder as the real
  code.** `python /tmp/x.py` sets `sys.path[0] = /tmp`, so `import sibling`
  (a workspace module) fails; `node` `require('./x')` resolves relative to the
  script's dir too. Only placing the temp file *in the folder the user is in*
  makes sibling imports and relative paths resolve identically to a local run.
- A `.jp-run/` **subfolder** would reintroduce the same `sys.path[0]` problem, so
  the temp file goes directly in the mapped folder, hidden + randomized.
- Because the temp file lives in a folder that already exists, **we never create
  or delete a directory** — eliminating the whole "delete the temp folder only
  if we created it" concern. We only ever create and delete one file: ours.

### Temp filename — triple anti-overwrite guarantee

Name derived from the real file so the user recognizes it, hidden, with strong
entropy so it cannot collide with anything already present:

```
.<stem>.jp-run-<32 hex chars><ext>      e.g.  .train.jp-run-9f3a…b7.py
```

(`<32 hex>` = 128 bits from `os.urandom`; `<stem>`/`<ext>` from the real file.)

Three independent layers, all of which must pass before anything runs:

1. **128-bit random** component → collision is statistically impossible.
2. **Remote pre-check** `[ -e "$f" ]` → if it somehow exists, abort (sentinel
   `__JP_COLLIDE__`); the CLI regenerates a new name and retries (bounded).
3. **`set -C` (noclobber)** on the redirection → the shell *itself* refuses to
   overwrite an existing file; if the redirect fails, nothing runs and nothing
   is deleted.

### Remote command (emitted once over the PTY)

```sh
f='.train.jp-run-RAND.py'
if [ -e "$f" ]; then printf '\n__JP_COLLIDE__\n'; else
  set -C
  cat > "$f" <<'JP_<rand-delim>'
<local source, newlines normalized to \n, literal — no expansion>
JP_<rand-delim>
  set +C
  <exec>            # shebang: chmod +x "$f" && "./$f" "$@"   |  else: <interp> "$f" "$@"
  __rc=$?
  rm -f -- "$f"     # deletes ONLY our file; never a directory
  printf '\n__JP_EXIT__%d__\n' "$__rc"
fi
```

Portability decisions baked in:

- **Raw quoted heredoc with a random delimiter** (`<<'JP_<rand>'`) instead of
  `base64 -d`: macOS/BSD `base64` uses `-D`, GNU uses `-d`, so a `base64 -d` on a
  BSD remote would break. Raw heredoc with a 128-bit random delimiter is
  portable and cannot be terminated early by the source. `'…'` quoting disables
  all shell expansion, so the source is written byte-for-byte.
- **CRLF → LF normalization** of the source locally before sending (a file
  edited on Windows must not inject `\r`, which would break bash and pollute the
  written file).
- **Shebang honored exactly** via `chmod +x` + `./$f`; only files without a
  shebang use the extension→interpreter map.
- The whole block is POSIX `sh`; the remote single-user Jupyter server is
  effectively always Linux (works on a macOS/BSD remote too).

### Working directory

The terminado terminal is created with `cwd=<remote_cwd>` where
`remote_cwd = prefix + (cwd relative to workspace root)`:

- `find_root()` locates the `.jp` workspace; `rel = Path.cwd().relative_to(root)`
  rendered as POSIX; `remote_cwd = posixpath.join(prefix, rel)` (just `prefix`
  when `rel == "."`).
- Validated to stay within the prefix (reuses `paths` helpers).
- **Existence pre-check:** `api.stat(remote_cwd)` before opening the terminal; if
  the mapped folder is missing remotely, fail early with
  `the remote folder '<remote_cwd>' does not exist — run 'jp push' first`.
- If the server ignores the native `cwd` (old `jupyter_server_terminals`), fall
  back to the same silent `cd` mechanism `jp terminal` already uses.

Because the temp file is created relative to that cwd and executed from it,
`sys.path[0]`/`__file__`/`open("rel")` all match a local run in that folder.

### Interpreter resolution

1. If the local file's first line starts with `#!`, run via shebang
   (`chmod +x` + `./$f`).
2. Else map the extension: `.py→python3`, `.sh→bash`, `.js→node`, `.mjs→node`,
   `.rb→ruby`, `.pl→perl`, `.R→Rscript`, `.lua→lua`, `.php→php`. (Final table
   confirmed during implementation.)
3. `--as <interp>` overrides both (and is required for an unknown extension or
   for reading from stdin).

### Arguments & exit code

- Everything after the file path is forwarded as the script's argv, each token
  `shlex.quote`-d into the emitted command. (`argv[0]` is the temp filename —
  cosmetic only.)
- A trailing `__JP_EXIT__<n>__` sentinel carries the remote exit status; the CLI
  parses it, strips it from the displayed output, and exits with that code so
  `jp run x.py && echo ok` works.

### Output / echo hygiene

- Output streams live through the same PTY proxy used by `jp terminal`.
- `stty -echo` is set before sending the heredoc (and the `clear`/sentinel
  framing) so the source blob and orchestration commands do not flash on screen;
  only the program's own output is shown.

### Concurrency

Each invocation uses its own 128-bit-random filename and creates/deletes **only
that file**. No shared temp directory, no directory ever removed. N concurrent
`jp run`s in N terminals cannot touch each other's file or any user data.

## Cross-OS summary

| Layer | macOS | Linux | Windows |
|------|-------|-------|---------|
| Local PTY proxy (`termios`/`tty`/`select`/`SIGWINCH`) | ✅ | ✅ | ❌ → clear error, shares transport so it lights up with the `jp terminal` Windows fix |
| Emitted remote `sh` (POSIX, raw heredoc, no `base64 -d`) | ✅ | ✅ | n/a (remote is the Jupyter server, ~always Linux) |

On Windows (`_HAS_PTY` false) `jp run` raises a clear `UsageError`
("`jp run` needs a POSIX terminal; Windows support arrives with the `jp terminal`
fix") rather than the browser fallback (a browser cannot run a script).

## Code structure

- **New** `src/jp/pty.py` — extract the reusable terminado protocol helpers
  (`stdin_message`, `setsize_message`, `parse_server_message`, `_write_all`,
  `_send_winsize`) and a PTY session driver supporting two modes:
  - *interactive* (today's `jp terminal` behavior), and
  - *orchestrated run* (send an initial payload, watch for the
    `__JP_EXIT__`/`__JP_COLLIDE__` sentinels, return the captured exit code).
- **Refactor** `src/jp/commands/terminal.py` to use `pty.py`; **re-export** the
  protocol helpers so `tests/test_terminal.py` (which calls
  `terminal.stdin_message`, `terminal._pump_output`, etc.) keeps passing
  unchanged. Behavior must be byte-for-byte identical.
- **New** `src/jp/commands/run.py` — the `jp run` command: workspace + cwd
  resolution, interpreter detection, temp-name generation, remote-script
  assembly, and driving the orchestrated PTY session.
- **Register** `run` in `src/jp/commands/__init__.py` (`ALL`).

## Testing

Pure, OS-independent units (no real PTY/socket — mirror `test_terminal.py`):

- **Interpreter resolution:** shebang wins; extension map; `--as` override;
  unknown extension without `--as` → error.
- **Temp-name generation:** matches `.<stem>.jp-run-<32hex><ext>`; two calls
  differ; preserves stem/ext; hidden (leading dot).
- **Remote-script assembly:** contains `set -C`, `[ -e ]` pre-check, random
  heredoc delimiter not present in source, `rm -f --` of exactly our file,
  `__JP_EXIT__` sentinel; args are `shlex`-quoted; shebang path uses
  `chmod +x`/`./`, non-shebang uses the interpreter.
- **CRLF normalization:** `\r\n` source → `\n` in the emitted heredoc body.
- **Sentinel parsing:** `__JP_EXIT__0__` → rc 0; non-zero → that code; sentinel
  stripped from displayed output; `__JP_COLLIDE__` → regenerate/retry.
- **cwd resolution:** root → `prefix`; subfolder → `prefix/<rel>`; missing remote
  folder (`api.stat` None) → early error.
- **`--dry-run`:** prints target folder + interpreter + source, makes no terminal.
- **Windows (`_HAS_PTY` false):** clear `UsageError`, no terminal created.
- **`run` happy path / cleanup:** with a fake api + fake ws (as in
  `test_terminal.py`), the session is created, driven, and
  `delete_terminal` is always called (even if the proxy raises).

## CLI surface

```
jp run <file> [args…]      run a local file on the remote in the current mapped folder
  --as <interp>            force the interpreter (e.g. python3, bash, node)
  --dry-run                show target folder, interpreter, and source without running
  -q / --no-color          (inherited global flags)
```

Examples:

```
jp run train.py --epochs 5      # from workspace root → runs in <prefix>
jp run analyze.py               # from a subfolder → runs in <prefix>/<subfolder>
jp run cleanup.sh               # shebang/extension picks bash
jp run --as python3 script      # extensionless file
```
