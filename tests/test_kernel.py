"""`jp kernel`: outside-repo guard, output, and the generated startup logic."""

from __future__ import annotations

import ast
from pathlib import Path

from jp import cli
from jp.commands import kernel
from jp.errors import EXIT_CONFIG, EXIT_OK


def test_kernel_outside_repo_returns_config_exit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["kernel", "--no-clipboard"])
    assert rc == EXIT_CONFIG


def test_kernel_default_hides_script_and_links_guide(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    rc = cli.main(["kernel", "--no-clipboard"])
    assert rc == EXIT_OK
    out = capsys.readouterr().out
    # Default output stays compact: no snippet body, but it points to --script
    # and the GitHub guide.
    assert "SCRIPT = " not in out
    assert "jp kernel --script" in out
    assert "github.com/pehqge/jp-cli" in out


def test_kernel_script_flag_prints_snippet_with_prefix(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    rc = cli.main(["kernel", "--script"])
    assert rc == EXIT_OK
    out = capsys.readouterr().out
    # The workspace prefix is baked into the printed snippet.
    assert "users/alice" in out
    assert "50-jp-autocwd.py" in out


def test_build_snippet_is_valid_python():
    snippet = kernel.build_snippet("/abs/work", "users/alice")
    compile(snippet, "<snippet>", "exec")  # installer compiles
    inner = _extract_inner(snippet)
    compile(inner, "<inner>", "exec")  # the startup file it writes compiles too


def _extract_inner(snippet: str) -> str:
    """Pull the `SCRIPT = '...'` string literal out of the installer.

    Uses ast.literal_eval (not eval) -- it only parses a Python string literal,
    never executes code.
    """
    body = snippet.split("SCRIPT = ", 1)[1]
    literal = body.split("\n\nd = Path.home", 1)[0]
    return ast.literal_eval(literal)


class _FakeEvents:
    def __init__(self) -> None:
        self.callbacks: list = []

    def register(self, name, cb) -> None:
        assert name == "pre_run_cell"
        self.callbacks.append(cb)


class _FakeIP:
    def __init__(self, ipynb_path: str) -> None:
        self.user_ns = {"__vsc_ipynb_file__": ipynb_path}
        self.events = _FakeEvents()


def _run_generated(inner: str, ipynb_path: str):
    """Exec the generated startup file with a fake get_ipython and return the hook."""
    ip = _FakeIP(ipynb_path)
    ns: dict = {"get_ipython": lambda: ip}
    exec(inner, ns)  # noqa: S102 - trusted, self-generated code under test
    assert ip.events.callbacks, "startup script should register a pre_run_cell hook"
    return ip.events.callbacks[0]


def test_generated_hook_chdirs_into_mirrored_dir(tmp_path, monkeypatch):
    # Simulate: local notebook at <root>/proj/nb.ipynb, prefix users/alice.
    local_root = "/Users/me/work"
    inner = _extract_inner(kernel.build_snippet(local_root, "users/alice"))

    # The mirrored dir exists on the (fake) remote under the kernel's cwd.
    remote = tmp_path / "users" / "alice" / "proj"
    remote.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)

    hook = _run_generated(inner, f"{local_root}/proj/nb.ipynb")
    hook()
    assert Path.cwd() == remote


def test_generated_hook_noops_for_foreign_notebook(tmp_path, monkeypatch):
    # A notebook outside this workspace must not move the cwd.
    inner = _extract_inner(kernel.build_snippet("/Users/me/work", "users/alice"))
    (tmp_path / "users" / "alice").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    start = Path.cwd()

    hook = _run_generated(inner, "/Users/someone-else/other/nb.ipynb")
    hook()
    assert Path.cwd() == start
