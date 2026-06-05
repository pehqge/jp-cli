# `jp live` — your remote folder, mounted locally

`jp live <URL>` makes a folder on your Jupyter/JupyterHub server appear as a
normal folder on your machine. You open files in your own editor; reads and
(by default) writes travel to the server.

It does this **over the kernel websocket you already have access to** — the same
transport a notebook uses. There is **no SSH, no server-side install, and no new
service**. The only credential involved is your existing API token.

```sh
# mount the folder the URL points at, under ./jp-live-test/ (writable by default)
jp live https://host/user/you/lab/tree/privado/jp-live-test

# read-only, no prompt
jp live <URL> --read-only

# open it in VS Code with a remote shell already wired up
jp live <URL> --code
```

`jp live` is **workspace-free**: it does NOT need `jp init`/`jp clone` and does
NOT create a `.jp/` folder. The URL is the same kind you paste into `jp clone`.

## How it works

`jp live <URL>`:

1. parses the URL into a server + a remote sub-path (the *prefix*),
2. starts a kernel on your server,
3. injects a small **file agent** into that kernel (a single `execute_request`;
   see `--print-agent` to read the exact code),
4. opens a Jupyter `comm` channel to talk to the agent,
5. serves that remote folder over a **loopback-only** WebDAV server (`127.0.0.1`),
   and
6. mounts it under **one auto-managed local handle named after the prefix leaf**
   — a folder on macOS/Linux, a drive letter on Windows.

When you exit (`Ctrl-C`), the mount is removed, anything `jp` created is cleaned
up, and the kernel is **deleted**, so it does not sit on a GPU/CPU slot.

## Command interface

```
jp live <URL> [--read-only] [--credential NAME] [--code] [--yes] [--mount POINT]
jp live --dry-run --root <folder> [--read-only] [--mount POINT] [--stats]   # offline self-test
jp live --print-agent [--read-only]                                          # transparency
```

- `<URL>` — the Jupyter URL of the folder, copied from your browser address bar
  (e.g. `https://host/user/you/lab/tree/privado/jp-live-test`).
- **Writable is the default.** `--read-only` opts out (and skips the prompt).
- `--credential NAME` — pick a saved credential (otherwise: the single one is
  used silently, or you are prompted; run `jp login` first if none).
- `--code` — open the mounted folder in VS Code with a remote shell running.
- `--yes` — skip the writable confirmation (assumes writable). Required for
  unattended / non-tty runs.
- `--mount POINT` — override the auto target (advanced): a folder on
  macOS/Linux, or a drive letter (`Z:`) on Windows. You own the target; it is
  **not** removed on cleanup.
- `--dry-run --root <folder>` — offline self-test of the whole transport against
  a **local** folder. No network. Writable is the default here too; `--read-only`
  opts out.
- `--print-agent` — print the **exact** agent code that would be injected, then
  exit (no network). With a `<URL>` it uses that URL's prefix; otherwise it falls
  back to a workspace config.

## The writable confirmation

Before mounting, `jp live` lists the top of the folder so you can confirm it is
the folder you expect, then folds the write/read choice into a single prompt:

```
top of 'privado/jp-live-test':
  • dummy.txt
Mount at ./jp-live-test -- [W]ritable (edits/deletes reach the server) or [r]ead-only? ([W]/r, c=cancel):
```

- **Enter** or `w` → writable. `r` → read-only. `c` (or anything else) → cancel.
- `--read-only` forces read-only and skips the prompt.
- `--yes` assumes writable and skips the prompt.
- A non-interactive shell (no tty) **without** `--yes` is refused.

The prompt (and the refresh warning, below) name the **per-OS handle** — the
folder on macOS/Linux, the drive letter on Windows — not a hardcoded path.

## Refresh semantics (important)

After mounting, `jp live` prints:

> Edits you make here save to the server immediately. Changes made ON the server
> appear here when your editor/file-browser re-reads a file (open or reload it)
> — there is no live push. For a live view of server-side changes (logs, job
> output), use a remote shell + `tail -f` (e.g. `jp terminal <URL>`).

## Cross-OS support matrix

| Capability | macOS | Linux | Windows |
| --- | --- | --- | --- |
| Mount + edit files | folder `./leaf/` | folder `./leaf/` (symlink→gvfs) | drive `Z:` |
| Write/read prompt, credential picker, workspace guard | ✅ | ✅ | ✅ |
| `jp terminal <URL>` standalone | ✅ PTY | ✅ PTY | ⚠️ web fallback (Win-PTY = follow-up) |
| `--code` opens VS Code | `open -a` | `code` / `vscode://` URI | `code` / `vscode://` URI |
| `--code` integrated remote shell | ✅ | ✅ | ⚠️ web fallback |

The auto handle is named after the **prefix leaf**: `privado/jp-live-test`
mounts under `./jp-live-test/` (macOS/Linux) or the first free drive letter
(Windows).

## `--code`: VS Code with a remote shell

`jp live <URL> --code`:

1. mounts the folder as above,
2. writes a **local** `<leaf>.code-workspace` next to the launch dir — never
   inside the mount, because the server rejects dotfiles,
3. opens it in VS Code (macOS: `open -a "Visual Studio Code"`; else the `code`
   CLI if on PATH; else the `vscode://file/...` URI; else it prints the path),
4. VS Code shows a one-time **"Allow Automatic Tasks"** prompt and then runs
   `jp terminal "<URL>"` in an integrated terminal (a real PTY on macOS/Linux; a
   web-terminal fallback on Windows until the Windows-PTY follow-up ships).

On exit, the `.code-workspace` file `jp` created is removed along with the mount.

## Safety model

`jp live` is deliberately paranoid because the server is often shared:

- **Refuses to run inside a `.jp` workspace.** If you are in a `jp` workspace
  tree (the root or any subfolder), `jp live` refuses, so a live mount can never
  be nested inside something a `push`/`pull` would walk. `cd` to a plain
  directory first.
- **Never deletes your data.** The auto handle is created only if absent, reused
  only if empty, and **refused** if it is a non-empty existing folder. Cleanup
  uses `rmdir` (empty-only) / removes only the symlink and the `.code-workspace`
  file `jp` itself created. Never `rm -rf`. With `--mount POINT` the target is
  yours and is never removed.
- **Writable is gated.** Writable is the default, but a single confirmation
  (showing the folder's top entries) precedes the mount; `--read-only` forces
  read-only, `--yes` assumes writable, and a non-tty without `--yes` is refused.
- **Never recursive remote delete.** A directory delete only succeeds if the
  directory is already empty; a non-empty directory is refused. There is no code
  path that recursively deletes the remote.
- **Double path-jail.** The remote prefix is validated locally
  (`paths.validate_prefix`) *and* enforced again inside the agent on the server.
  Shared/too-broad names (`shared`, `public`, `common`, `compartilhado`,
  `lapix`, the server root, `..`, …) are refused before any connection is made.
- **No automatic undo when writable.** In writable mode the agent overwrites
  remote files in place. There is **no automatic server-side undo** — keep your
  own backup before writing. (Removals are still never recursive.)
- **`--code` writes nothing to the server.** The `.code-workspace` is local.
- **Kernel released on exit.** The serving loop deletes the kernel in a
  `finally`, even on `Ctrl-C` or error.
- **Token never in a URL.** Your token travels only in the `Authorization`
  header on the websocket handshake.
- **Capability-secret on the mount.** Loopback is not a per-user boundary: any
  local process that finds the ephemeral port could otherwise read (or, when
  writable, write) the mounted files. So the server serves only under a random
  128-bit secret path segment — the URL is `http://127.0.0.1:<port>/<secret>/`
  — and refuses any request without it (HTTP 404). This closes the trivial
  port-scan vector.

  **Residual, be honest about it:** while the mount is active, the full URL
  (secret included) appears in the OS mount table — `mount` on macOS/Linux,
  `net use` on Windows — which any local user can read. So the secret defends
  against blind local port-scanners, **not** against a local user who actively
  inspects the mount table on the *same* machine. Native OS mounting records
  the URL by design; fully closing this would require a non-native transport
  (e.g. a Unix-domain socket with per-user permissions), which would drop the
  "mount with the OS's built-in client, no FUSE" property. On a hostile
  multi-user host: prefer read-only, and unmount (`Ctrl-C`) when idle rather
  than leaving a writable mount up.

## Overriding the mount target

Auto-mount is the default. Pass `--mount <point>` to choose your own target — a
folder (macOS/Linux) or a drive letter like `Z:` (Windows). `jp live` mounts
there and, on exit, only **unmounts** it: the target is yours and is never
removed. If the mount fails, `jp live` prints the exact command to run by hand.
The WebDAV URL is `http://127.0.0.1:<port>/<secret>/` (printed each run).

Manual mount commands (the `<port>` stands in for the printed value):

- **macOS** (`mount_webdav`):

  ```sh
  mkdir -p ./jp-live-test
  mount_webdav -S http://127.0.0.1:<port>/<secret>/ ./jp-live-test
  umount ./jp-live-test
  ```

- **Windows** (WebClient redirector):

  ```bat
  net use Z: http://127.0.0.1:<port>/<secret>/
  net use Z: /delete /y
  ```

- **Linux** (GVfs, userspace, no root):

  ```sh
  gio mount dav://127.0.0.1:<port>/<secret>/
  gio mount -u dav://127.0.0.1:<port>/<secret>/
  ```

  `davfs2` (`mount -t davfs http://127.0.0.1:<port>/<secret>/ /mnt/jp`) also
  works if you prefer a system mount.

Leave `jp live` running while you use the folder; it keeps the kernel alive
(pinging every ~30s to defeat idle-culling) until you press `Ctrl-C`.

## Auditing the injected code: `--print-agent`

You do not have to trust a description of what runs on the server — you can read
it. `jp live --print-agent` prints the **exact** bootstrap code that the mount
would inject and exits without touching the network:

```sh
jp live <URL> --print-agent              # the (writable-by-default) agent
jp live <URL> --print-agent --read-only  # the read-only variant
```

The output is the verbatim agent source plus the small registration shim, so you
can review precisely what the kernel will execute before you trust it.

## Manual per-OS test checklist

Run against a real, **personal** test folder on a server you control. Repeat per
OS (macOS, Linux, Windows).

1. **Mount.** `jp live <URL>` → confirm the top-of-folder listing matches, press
   Enter (writable). Verify the handle appears: `./<leaf>/` (macOS/Linux) or the
   drive letter (Windows).
2. **ls / cat.** List the mounted folder and open a known file in your editor;
   the contents match the server.
3. **Add propagates.** Create a new file in the mount; confirm it appears on the
   server (Jupyter file browser).
4. **Edit propagates.** Edit and save a file; confirm the server copy updates.
5. **Delete propagates.** Delete a file; confirm it is gone on the server. (A
   non-empty directory delete is refused — expected.)
6. **Read-only.** Re-run with `--read-only`; confirm writes/deletes are rejected.
7. **`--code`.** `jp live <URL> --code` opens VS Code on the folder; accept
   "Allow Automatic Tasks"; a `jp terminal` shell appears in the integrated
   terminal (web fallback on Windows). Confirm the `.code-workspace` file is in
   the launch dir, **not** inside the mount.
8. **Ctrl-C cleanup.** Press `Ctrl-C`; confirm the mount is gone, the auto
   folder/symlink and the `.code-workspace` file are removed (an empty auto
   folder is `rmdir`-ed; a `--mount` target is left in place), and the kernel is
   deleted on the server.
9. **Workspace guard.** From inside a `jp` workspace tree, `jp live <URL>`
   refuses with the "must run OUTSIDE a workspace" message.
