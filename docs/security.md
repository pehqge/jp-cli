# Security & safety model

`jp` is designed for a **shared** JupyterHub server, where a mistake can destroy
another researcher's data. Safety is the project's first priority, not an
afterthought. This document explains the guarantees and how they are enforced.

## The guarantees

1. **`push` and `pull` never delete.** Synchronization is additive. A file that
   exists on one side and not the other is uploaded/downloaded or left alone —
   never removed. The only command that deletes is `jp rm`, and it is gated
   (dry-run + typed confirmation + per-call path validation).

2. **Conflicts are never silently overwritten.** `jp` keeps a base state (the
   last successful sync) and compares it against both the local file and the
   remote file. If both changed, that file is a conflict: `jp` aborts it and
   reports it, leaving both versions intact. There is no "last writer wins".

3. **Every remote operation is path-jailed.** All remote paths are composed and
   validated by a single module and re-checked immediately before any write or
   delete. A path that would escape the workspace prefix — via `..`, an absolute
   path, a Windows drive letter, a sibling-prefix trick (`prefixEVIL`), or the
   prefix root itself — is refused.

4. **The server is treated as untrusted on download.** File names returned by
   the server are sanitized before anything is written locally, defeating
   path-traversal / "Zip-Slip" attacks (`../../etc/...`, absolute paths, NUL
   bytes, Windows reserved names). Writes are atomic and never follow a symlink,
   so a planted symlink cannot redirect a write outside the working tree.

5. **Your token never leaks.** `jp login` saves the token to a private `0600`
   file (global in `~/.config/jp/`, or local in a workspace's `.jp/`); the config
   stores only the credential *name* — never the token value. The value stays on
   your machine and is sent only in the `Authorization` header, never in a URL.
   A **local** token lives in `.jp/`, and `jp` writes a `.jp/.gitignore` (`*`) so
   that a workspace which is also a git repo can never commit the token by
   accident. All output passes through a redaction layer that scrubs the token
   (and server filesystem paths) from logs and error messages. TLS certificates
   are always verified; `jp` refuses to send a token over plain HTTP. The two
   exceptions that put the token in a URL are strictly opt-in and confirmed:
   `jp kernel --link`, and the Windows-only browser fallback of `jp terminal`
   (which has no PTY there). On POSIX, `jp terminal` sends the token only in the
   `Authorization` header — including on the websocket handshake.

   **At rest, the token is stored in plaintext** — the same model Git uses for
   `~/.git-credentials`. The `0600` permission protects it from *other* users on
   the machine, but not from software running as *you*: malware, a backup/sync
   agent, or any process with your privileges can read the file. `jp`
   deliberately does not encrypt it or use an OS keychain, because that would
   add a runtime dependency and break the zero-dependency guarantee. If your
   threat model includes a compromised local account, treat the token as
   exposed — revoke it on the JupyterHub side and issue a new one.

6. **Versioning history is verified, opt-in, and never blocks sync.** The
   optional version store lives entirely under `.jp/` and is a separate concern
   from sync (it never touches `.jp/index.json`). Objects are content-addressed
   and write-once, and every read re-hashes the decompressed payload — so
   bit-rot, truncation, or tampering becomes a loud error, not corrupt history.
   When history is mirrored to the server, the remote is treated as **untrusted**
   on the way back: `jp fetch`/`jp restore`/`jp clone --history` re-hash every
   object (with a decompression cap against "zip bombs") before placing it, and
   advance refs only on a proven fast-forward. The reserved `__jp`/`jp-tmp`
   top-level names are excluded from sync on both ends so the backup can't be
   pulled into the tree or offered for deletion. The history mirror is
   best-effort and can never change a `jp push`'s exit code, and `jp unversion`
   removes local history without ever touching the remote or your working files.

## Why these specific rules

These rules come from probing a real JupyterHub Contents API and observing how
it behaves:

- Creating a hidden file (a dotfile) is rejected by the server, so `jp` skips
  dotfiles on push and reports them rather than failing the whole run.
- A remote delete may move the file to a trash folder rather than erasing it,
  and in some cases corrupts the parent directory listing. This is why deletion
  is never part of normal sync.
- The server provides a content hash, so `jp` can detect changes by content
  without always downloading files.

## Reporting a vulnerability

Please report security issues privately — see [SECURITY.md](../SECURITY.md).
