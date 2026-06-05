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
├── sync.py         # scan, 3-way diff, push, pull engine (excludes __jp/jp-tmp)
├── versioning/     # the opt-in git-like version store (see "Versioning" below)
│   ├── objects.py    # content-addressed, write-once object store (blob/tree/commit)
│   ├── refs.py       # HEAD, refs/heads, the on-disk format marker, lazy init
│   ├── staging.py    # .jp/staged.json (the next commit's tree)
│   ├── repo.py       # tree/commit model + add/commit/diff orchestration
│   ├── notebooks.py  # hybrid notebook normalization (change-detection + diff)
│   ├── checkout.py   # plan-then-apply working-tree restore (the destructive path)
│   ├── mirror.py     # push history to the remote <prefix>/__jp/ backup
│   ├── fetch.py      # verified read-back of remote history
│   ├── gc.py / fsck.py  # reclaim / integrity-verify the local store
│   └── lock.py       # cross-process advisory lock for versioning writes
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

## Versioning

An **opt-in**, git-like version store lives entirely under `.jp/` (the
`jp.versioning` package). It is a separate concern from sync: it never reads or
writes `.jp/index.json` (the sync base), and a repo that never adopted it
(`.jp/HEAD` absent) behaves byte-identically to a pre-versioning `jp`. The
on-disk `format` marker (`{"versioning": 1}`) is a forward-compat guard — a newer
store is refused by an older `jp` rather than misread.

### Content-addressed object model

Three object kinds, each keyed by `sha256(content)` and stored at
`.jp/objects/<sha[:2]>/<sha[2:]>` (a git-style 2-char fan-out):

- **blob** — a file's bytes (for a notebook, the *original* `.ipynb` bytes);
- **tree** — a *flat* (non-recursive) canonical-JSON manifest mapping each
  normalized rel path to `{sha256, size, mode, nb?}` (the optional `nb.norm_sha`
  carries a notebook's normalized hash);
- **commit** — canonical JSON: `{version, tree, parents[], message, author, time,
  epoch, jp}`; `parents` is `[]` for the root commit.

The store is **write-once / immutable**: an object's name *is* its hash, so a
write is idempotent and we never overwrite or rename — which sidesteps the two
things the Contents API cannot do (atomic cross-dir rename; overwrite-via-PATCH).
The blob's sha is the *same* sha256 `sync` already computes, so an unchanged
tracked file needs no rehash and deduplicates to one object. Each object file is
a 1-byte compression marker (`raw` or `zlib`) + body; zlib is used **only when it
shrinks** the data, so an object is never larger than its content + 1 byte.
**Reads re-hash** the decompressed payload and raise on any mismatch (bit-rot,
truncation, tampering, unknown marker), and **decompression is capped** (to a
tree-entry's declared size, or a 2 GiB ceiling) so a crafted "zip bomb" cannot
OOM the process before the re-hash rejects it.

### `.jp/` layout

```
.jp/
├── config.json     # EXISTING — gains versioning.* keys (only when non-default)
├── index.json      # EXISTING — the sync base; UNCHANGED by versioning
├── format          # {"versioning": 1} — forward-compat marker
├── HEAD            # 'ref: refs/heads/main\n', or a raw sha (detached)
├── refs/heads/main # one 64-hex sha + newline (the branch tip)
├── staged.json     # the staging area (next commit's tree), 0600, atomic
├── versioning.lock # cross-process advisory lock for commit/checkout/mirror/gc
├── objects/<2>/<62># write-once content-addressed objects (objtmp-* = in-flight)
└── packs/          # RESERVED (future FastCDC chunk store); empty, no code reads it
```

Writes are atomic and symlink-refusing throughout: a unique same-dir temp (via
`mkstemp`), `fsync`, then `os.replace` onto the hashed/ref path, then a
best-effort dir-fsync — so a crash leaves no partial object and the new name is
durable before any ref can point at it. A `commit` writes objects **first** and
moves the ref **last** (a compare-and-swap on the branch tip), so a crash leaves
harmless orphan objects and HEAD still resolves to the previous commit.

### Remote history mirror (`<prefix>/__jp/`)

History can be mirrored to a reserved, non-dotted `__jp/` folder on the server
(`objects/<2>/<62>` + `refs/heads/<branch>`), so it survives deleting the local
`.jp/`. The same write-once/idempotent model applies: object PUTs are
byte-identical re-PUTs, the single mutable file (the ref) is PUT **last**, and it
is advanced only on a proven **fast-forward** (the remote sha is in our reachable
history) plus an optimistic re-read CAS (abort if another machine moved it).
Fetch is **fast-forward-only** and reachability-driven — it follows shas from
inside already-verified objects and never enumerates the remote `__jp/` listing,
and every byte the (untrusted) server serves is decoded, capped, and re-hashed
before it is placed locally. `__jp` and `jp-tmp` are **excluded from sync on both
ends** (only as the first path segment): `scan_remote` never returns them (so
`pull` never downloads the store and a mirror `push` never lists them deletable),
and `scan_local` never returns a top-level one (so a normal push can't upload a
local meta dir over the mirror).

### Safety invariants

- **Opt-in.** No `.jp/HEAD` → no versioning, byte-identical behavior; the
  commit-gate, status section, and history mirror are all no-ops.
- **Never touches `index.json`.** The sync base and the version store are wholly
  separate code paths; an `add`/`commit`/`log`/`show`/`checkout` cycle leaves the
  sync base's bytes unchanged.
- **Cross-process lock.** `commit`, `checkout`, `gc`, `unversion`, and the mirror
  hold an advisory `flock`/`msvcrt` lock on `.jp/versioning.lock` (auto-released
  by the OS on exit/crash — no stale lockfile). A second `jp` fails fast with
  "another operation is in progress".
- **Symlink-safe / traversal-safe.** Every object name and ref name is validated
  (`^[0-9a-f]{64}$` / a single safe segment) *before* it is composed into a path;
  every write refuses a symlinked temp/final/ancestor; `checkout` re-sanitizes
  each target key (anti zip-slip) and never writes through a symlink.
- **Never blocks the data push.** The commit-gate is offline and skipped in
  CI/non-tty; the history mirror is best-effort and a failure is only a warning —
  neither can change the `jp push` exit code.

### Notebooks (hybrid)

Notebooks are stored **hybrid**: the object store always holds the *original*
`.ipynb` bytes (so `checkout` restores outputs and figures faithfully), but
change-detection and diff use a *normalized* form that strips each cell's
`outputs`/`execution_count` and volatile metadata (`widgets`,
`language_info.version`). The tree entry records both the original blob sha and
the normalized sha (`nb.norm_sha`), so a pure re-run (same code, new outputs)
hashes identically and never creates a new version. nbformat v4 is handled;
anything that isn't a JSON dict with a `cells` list (e.g. a v3 `worksheets`
notebook, or a file mid-write) is treated as an opaque binary blob, never a
crash. `versioning.notebook_outputs=full` disables normalization (every byte
change is versioned); `strip` is deliberately unsupported (lossy).

### Branching

**No branches in v1** — single linear `main`. The on-disk format keeps the door
open: a commit's `parents` is a list and `refs/heads/` holds N files, so branches
are an additive future change, not a rewrite.

### Non-goals (versioning)

- **No sub-file delta.** A meaningfully changed large binary stores a whole new
  blob (mitigated by dedup + notebook handling + zlib + `gc`); `packs/` is
  reserved for a future FastCDC chunk store behind the same object interface.
- **Remote `gc` deferred.** `jp gc` reclaims the *local* store; the remote `__jp`
  mirror is append-only in v1 (non-recursive DELETE makes remote gc an
  O(objects) bottom-up walk — a follow-up). Until then, remove a remote backup by
  deleting `<prefix>/__jp/` by hand.
- **Not git interop.** Object framing is jp's own (raw sha256, no `type size\0`
  header); we never claim to be readable by the `git` binary.

## What is intentionally out of scope (for now)

- Remote kernel execution (`jp run`/`jp exec` over WebSocket) is planned as an
  optional, isolated module so the safe-sync core never depends on it.
