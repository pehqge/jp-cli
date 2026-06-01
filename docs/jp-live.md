# `jp live` — your remote folder, mounted locally

`jp live` makes a folder on your Jupyter/JupyterHub server appear as a normal
folder on your machine. You open files in your own editor; reads and (optionally)
writes travel to the server.

It does this **over the kernel websocket you already have access to** — the same
transport a notebook uses. There is **no SSH, no server-side install, and no new
service**. The only credential involved is your existing API token.

## How it works

`jp live --live`:

1. starts a kernel on your server,
2. injects a small **read-only file agent** into that kernel (a single
   `execute_request`; see `--print-agent` to read the exact code),
3. opens a Jupyter `comm` channel to talk to the agent, and
4. serves that remote folder over a **loopback-only** WebDAV server
   (`127.0.0.1`) that your OS can mount natively.

When you exit, the kernel is **deleted**, so it does not sit on a GPU/CPU slot.

## The three modes

| Command | What it does | Touches the network? |
| --- | --- | --- |
| `jp live --dry-run --root <folder>` | Offline self-test of the whole transport against a **local** folder. | No. |
| `jp live --live` | Connect to your configured server and mount the remote folder **read-only**. | Yes. |
| `jp live --live --writable` | As above, but local edits **write back** to the remote. | Yes. |

Read-only is always the default. Writing is only possible with `--writable`.

`jp live` **never auto-connects**: you must pass `--live` yourself. In a
non-interactive shell it refuses to start unless you also pass `--yes`.

## Safety model

`jp live` is deliberately paranoid because the server is often shared:

- **Read-only by default.** Every mutating WebDAV verb is refused with HTTP 403
  unless you ran with `--writable`.
- **Never recursive remote delete.** A directory delete only succeeds if the
  directory is already empty; a non-empty directory is refused. There is no code
  path that recursively deletes the remote.
- **Double path-jail.** The remote prefix is validated locally
  (`paths.validate_prefix`) *and* enforced again inside the agent on the server.
  Shared/too-broad names (`shared`, `public`, `common`, `compartilhado`,
  `lapix`, the server root, `..`, …) are refused before any connection is made.
- **Pre-flight confirmation.** Before any mount or write, `jp live` lists the
  top of the folder and asks you to confirm it is the folder you expect — so a
  wrong prefix is caught early.
- **No automatic undo when writable.** In `--writable` mode the agent overwrites
  remote files in place. There is **no automatic server-side undo** — make sure
  you have your own backup before writing. (Removals are still never recursive.)
- **Kernel released on exit.** The serving loop deletes the kernel in a
  `finally`, even on `Ctrl-C` or error.
- **Token never in a URL.** Your token travels only in the `Authorization`
  header on the websocket handshake.

## Mounting natively

Pass `--mount <point>` and `jp live` will try to mount for you; if that fails it
prints the exact command so you can run it yourself. The WebDAV URL is always
`http://127.0.0.1:<port>/`.

- **macOS** (Finder / `mount_webdav`):

  ```sh
  mkdir -p /tmp/jpmnt
  mount_webdav -S http://127.0.0.1:<port>/ /tmp/jpmnt
  # unmount:
  umount /tmp/jpmnt
  ```

  You can also use Finder → Go → Connect to Server (`⌘K`) and paste the URL.

- **Windows** (WebClient redirector):

  ```bat
  net use * http://127.0.0.1:<port>/
  rem unmount (replace Z: with the assigned drive):
  net use Z: /delete /y
  ```

- **Linux** (GVfs, userspace, no root):

  ```sh
  gio mount dav://127.0.0.1:<port>/
  # unmount:
  gio mount -u dav://127.0.0.1:<port>/
  ```

  `davfs2` (`mount -t davfs http://127.0.0.1:<port>/ /mnt/jp`) also works if you
  prefer a system mount.

Leave `jp live` running while you use the folder; it keeps the kernel alive
(pinging every ~30s to defeat idle-culling) until you press `Ctrl-C`.

## Auditing the injected code: `--print-agent`

You do not have to trust a description of what runs on the server — you can read
it. `jp live --print-agent` prints the **exact** bootstrap code that `--live`
would inject (using your workspace's configured prefix) and exits without
touching the network:

```sh
jp live --print-agent            # read-only agent
jp live --print-agent --writable # the writable variant
```

The output is the verbatim agent source plus the small registration shim, so you
can review precisely what the kernel will execute before you trust it.
