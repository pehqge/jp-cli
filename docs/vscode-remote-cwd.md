# Running notebooks in VS Code against a remote kernel

This guide covers the full workflow for the common jp setup: you **edit a
notebook locally** in VS Code but **run it on the remote JupyterHub** (e.g. to
use the cluster's GPUs). Two things need setting up:

1. [Connect VS Code to your remote kernel](#1-connect-vs-code-to-your-remote-kernel)
2. [Fix the working directory](#2-fix-the-working-directory) so relative paths
   like `pd.read_excel("dataset/x.xlsx")` resolve.

jp keeps your files in sync; this guide is about *running* them.

---

## 1. Connect VS Code to your remote kernel

Prerequisites: the [Jupyter](https://marketplace.visualstudio.com/items?itemName=ms-toolsai.jupyter)
and [Python](https://marketplace.visualstudio.com/items?itemName=ms-python.python)
extensions, and a **running** Hub server (open the Hub in your browser and click
*Start My Server*, or run `jp doctor` to check).

You connect VS Code to the *same* server jp talks to, using the *same* API token:

1. Open your `.ipynb` in VS Code.
2. Click the kernel picker (top-right of the notebook) →
   **Select Another Kernel…** → **Existing Jupyter Server…**
   (equivalently: Command Palette → *Jupyter: Specify Jupyter Server for
   Connections*).
3. Choose **Enter the URL of a running Jupyter Server** and paste your server
   URL with the token appended:

   ```
   https://<host>/user/<name>/?token=<YOUR_TOKEN>
   ```

   - `https://<host>/user/<name>` is the `base_url` from your `.jp/config.json`
     (without the trailing `/api`).
   - `<YOUR_TOKEN>` is the JupyterHub API token you saved with `jp login`. You
     can mint a fresh one at `https://<host>/hub/token`.
4. Give the server a display name if asked, then pick the **Python** kernel it
   offers.

> **Security:** that URL embeds your token — treat it like a password. Don't
> paste it into chats, commit it, or share your screen with it visible. jp never
> prints your token for this reason; you copy it into VS Code yourself.

You should now be able to run cells on the remote. If a cell that reads a file
fails with `FileNotFoundError`, continue to the next section.

---

## 2. Fix the working directory

### The problem

When VS Code runs a **local** `.ipynb` against a **remote** kernel, the kernel's
working directory is the server's home, **not** the notebook's folder. So
relative paths fail:

```python
FileNotFoundError: [Errno 2] No such file or directory: 'dataset/annotation_files/V1.xlsx'
```

VS Code's `jupyter.notebookFileRoot` setting does **not** fix this for remote
kernels — its value is a path on *your* machine, which is meaningless on the
server. This is a known limitation
([vscode-jupyter#8771](https://github.com/microsoft/vscode-jupyter/issues/8771),
[#8927](https://github.com/microsoft/vscode-jupyter/issues/8927),
[#15755](https://github.com/microsoft/vscode-jupyter/issues/15755)).

### The fix: `jp kernel`

From inside your workspace, run:

```console
$ jp kernel
```

It prints a small snippet (pre-filled with your workspace root and prefix) and
copies it to your clipboard. Then:

1. In VS Code, open a notebook connected to your remote kernel.
2. Add a new cell, paste, and run it once. It writes an IPython *startup script*
   on the server.
3. **Restart the kernel.**

That's it — from now on **every** notebook in the workspace (including new ones)
starts in the correct directory automatically. You only do this once.

Verify with a cell:

```python
from pathlib import Path
print(Path.cwd())   # should show your workspace folder on the remote
```

### Doing it by hand

`jp kernel` just builds this for you. If you prefer, paste the following into a
cell, **filling in the two values**, run it once, and restart the kernel:

```python
from pathlib import Path

JP_LOCAL_ROOT = "/abs/path/to/your/workspace"   # the local jp root (where .jp lives)
JP_PREFIX     = "your-prefix"                    # `prefix` from .jp/config.json
                                                 # (the folder that holds your notebooks)

SCRIPT = f'''import os
from pathlib import Path

JP_LOCAL_ROOT = {JP_LOCAL_ROOT!r}
JP_PREFIX = {JP_PREFIX!r}

def _jp_autocwd(info=None):
    try:
        p = get_ipython().user_ns.get("__vsc_ipynb_file__")  # local .ipynb path, set by VS Code
        if not p:
            return
        p = p.replace("\\\\", "/")
        root = JP_LOCAL_ROOT.replace("\\\\", "/").rstrip("/")
        if root and not p.casefold().startswith(root.casefold() + "/"):
            return
        rel = p[len(root):].lstrip("/")
        rel_dir = rel.rsplit("/", 1)[0] if "/" in rel else ""
        for base in (os.getcwd(), os.path.expanduser("~")):
            for cand in (Path(base) / JP_PREFIX / rel_dir, Path(base) / rel_dir):
                if cand.is_dir():
                    if Path.cwd() != cand:
                        os.chdir(cand)
                    return
    except Exception:
        pass

get_ipython().events.register("pre_run_cell", _jp_autocwd)
'''

d = Path.home() / ".ipython" / "profile_default" / "startup"
d.mkdir(parents=True, exist_ok=True)
(d / "50-jp-autocwd.py").write_text(SCRIPT)
print("installed:", d / "50-jp-autocwd.py")
```

---

## How it works

- **IPython runs every `*.py` in `~/.ipython/profile_default/startup/` at kernel
  boot.** That is what makes this automatic and global — it is per *IPython
  profile*, not per notebook, so new notebooks inherit it for free.
- **`__vsc_ipynb_file__`** is a variable VS Code injects into the kernel holding
  the notebook's path. On a remote kernel it is still the path *on your machine*
  — which is exactly what we use: we strip your local workspace root, keep the
  relative folder, and rebuild it under the remote `prefix`.
- **`pre_run_cell`** runs the `chdir` just before each cell, so it is set even on
  the first cell and re-applies if you switch notebooks on the same kernel.
- It searches both the kernel's start directory and `$HOME` for the mirrored
  folder, and is wrapped in `try/except` so it can never break a cell.

---

## Shared clusters: check before you install

The startup script lives under `~` of the **user the kernel runs as**. On most
JupyterHub setups each user gets an isolated (often containerized) server, so `~`
is private to you. Verify it before installing on a shared cluster — run this in
a cell:

```python
import subprocess
print(subprocess.run("ls -la /.dockerenv 2>/dev/null; "
                     "findmnt -T ~/.ipython -o SOURCE,FSTYPE 2>/dev/null || df -hT ~",
                     shell=True, capture_output=True, text=True).stdout)
```

- A container marker (`/.dockerenv`) and a **local** filesystem for `~/.ipython`
  (`overlay`, `ext4`, `xfs`, `btrfs`) mean it is **private to your container** —
  safe to install.
- If `~/.ipython` is on a **network** filesystem (`nfs`, `cifs`) it may be shared
  with other researchers — don't install there. The generated script already
  no-ops for any notebook outside *your* workspace and never raises, but on truly
  shared homes prefer a per-user location (see below).

---

## Ephemeral containers

If your server runs in a container, `~` is usually on an **overlay** layer that
is **wiped when the container is recreated** (idle cull, Hub restart, logout).
If that happens, just run `jp kernel` and reinstall — it's a one-liner.

To survive recreation, point IPython at a **persistent** directory (e.g. a
mounted data volume) by setting `IPYTHONDIR` for the kernel, and place the
`profile_default/startup/` tree there instead. How to set kernel environment
variables depends on your Hub's configuration.
