# Architecture

`jp` is a pure standard-library Python package. There are **no third-party
runtime dependencies** — only `urllib`, `ssl`, `json`, `base64`, `hashlib`,
`argparse`, and friends. That keeps installation trivial across macOS, Windows,
and Linux and makes the tool easy to ship as a single `.pyz` or a standalone
binary.

## Module layout

```
src/jp/
├── cli.py          # argparse subcommands + dispatch + top-level error boundary
├── errors.py       # JpError hierarchy with stable exit codes
├── ui.py           # color/quiet/verbose, progress, prompts, secret redaction
├── config.py       # .jp/config + global config; token-path resolution
├── paths.py        # THE path-safety layer (jail + sanitization + atomic write)
├── api.py          # Contents API client over urllib (TLS, retries, redaction)
├── index.py        # .jp/index: the last-synced base state
├── ignore.py       # built-in ignores + .jpignore matching; dotfile policy
├── sync.py         # scan, 3-way diff, push, pull engine
└── commands/       # one module per subcommand
```

## Key design decisions

- **`paths.py` is the single security boundary.** It is the only place allowed
  to translate between local and remote paths, and the only place that composes
  a remote path. Every mutating remote call re-asserts containment immediately
  before it runs. Concentrating this logic in one audited module makes the safety
  guarantees testable.

- **A local index, like git.** `.jp/index` records, per file, the size, hashes,
  and remote modification time at the last successful sync. This is what makes
  three-way conflict detection possible, and it is written only *after* a
  transfer is verified — so an interrupted run leaves a consistent state and the
  next run resumes cleanly.

- **Change detection prefers content hashes.** The Contents API can return a
  file's SHA-256 without sending the body, so `jp` compares hashes and downloads
  only what actually differs.

- **Workspace discovery walks up for `.jp/`**, the same way git looks for
  `.git/`, so commands work from any subdirectory of a workspace.

- **Named credentials keep token values out of synced files (`credentials.py`).**
  `jp login` writes the token *value* to a private `0600` file under a `0700`
  directory and maps a name to it in a `credentials.json` registry — global
  (`~/.config/jp/`) or local (a workspace's `.jp/`, never synced). The config
  records only the credential *name*; `config.load_token` resolves it at call
  time (local registry before global), falling back to the legacy `token_path` /
  `~/.config/jp/token`. This lets one machine hold several servers' tokens while
  keeping secrets off the synced tree.

## What is intentionally out of scope (for now)

- Remote kernel execution (`jp run`/`jp exec` over WebSocket) is planned as an
  optional, isolated module so the safe-sync core never depends on it.
- Branching/merging: there is no version model on the server side, so `jp`
  deliberately does not pretend to offer one.
