# Security Policy

`jp` is built to be safe to run on **shared machines** — university lab
computers, cluster login nodes, and other multi-user environments. This
document summarizes the threat model, the guarantees `jp` makes, and how to
report a vulnerability.

A deeper write-up lives in [docs/security.md](docs/security.md).

## Supported versions

`jp` is pre-1.0. Security fixes are applied to the latest released version.
Please upgrade to the newest release before reporting.

| Version | Supported |
|---------|-----------|
| 0.1.x   | ✅ |
| < 0.1   | ❌ |

## Threat model (summary)

**Assets we protect:**

- Your **API token** (it grants access to your JupyterHub account).
- Your **remote files** (your work on the server).
- Your **local files**.

**Threats we defend against:**

- Token leakage — committed to git, printed in logs, or left world-readable.
- Accidental remote data loss — overwrite or deletion.
- Path traversal — a remote listing tricking `jp` into writing outside the
  intended local directory.
- Symlink attacks on shared machines.

## Guarantees

- **Never deletes remote files by default.** Deleting a file locally does not
  delete it on the server. Remote deletion only happens through an explicit,
  gated command that asks for confirmation.
- **Never overwrites on conflict.** If a file changed both locally and remotely,
  `jp` refuses, reports the conflicting path, and exits with code `6`. There is
  no automatic merge and no silent clobber. This is the core safety guarantee.
- **Path-jail confinement.** Every path derived from a remote listing is
  resolved and verified to stay inside the clone root. `..` traversal, absolute
  paths, and symlinks that cross the jail boundary are rejected. A malicious or
  buggy server cannot make `jp` read or write outside your folder.
- **Token stays local and private.** The token is resolved in the order
  `--token-file` > `$JP_TOKEN_FILE` / `$JUPYTER_TOKEN` > repo config > global
  config > `~/.jupyter_token`. Only the *path* to the token is stored in
  config; the token file itself should be mode `0600` (`jp` warns when it is
  group/other-readable and refuses a group/world-writable one). During normal
  sync the token is sent only in the HTTPS `Authorization` header — never in a
  URL — and is never logged, never printed, and never committed. The single
  exception is `jp kernel --link`: it builds a connection URL containing the
  token and prints it so you can paste it into VS Code's kernel picker. It is
  strictly opt-in and prompts for confirmation first (skip with `--yes`); treat
  that URL like a password.
- **Dotfiles skipped by default.** With the default `dotfiles = skip` policy,
  hidden files (`.env`, `.ssh`, `*.token`, `.jupyter_token`, …) are left
  out of the sync plan, so secrets on a shared machine are not pushed by
  accident. (Most Jupyter servers also reject hidden-file writes with
  `allow_hidden=False`.)

## Reporting a vulnerability

**Please report security issues privately — do not open a public issue.**

Two options:

1. **GitHub Security Advisories (preferred):** open a private report at
   <https://github.com/pehqge/jp-cli/security/advisories/new>.
2. **Email:** [pehqge@gmail.com](mailto:pehqge@gmail.com) with `jp security`
   in the subject line.

Please include:

- A description of the issue and its impact.
- Steps to reproduce (a minimal proof of concept if possible).
- The `jp` version (`jp version`), OS, and Python version.

**Do not** include real tokens, credentials, or server URLs with embedded
secrets in your report.

### What to expect

- Acknowledgement within a few days.
- An assessment and, where applicable, a fix in a timely manner.
- Coordinated disclosure: we'll agree on a reasonable window before any public
  details are published, and credit you in the release notes if you'd like.

Thank you for helping keep `jp` users safe.
