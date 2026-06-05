"""`jp open`: URL building, the outside-repo guard, flags, and the picker."""

from __future__ import annotations

from jp import cli, clipboard, tui
from jp.commands import open_cmd
from jp.errors import EXIT_CONFIG, EXIT_OK, EXIT_USAGE


# --------------------------------------------------------------------------- #
# Pure URL builder
# --------------------------------------------------------------------------- #
def test_folder_url_root_strips_api_and_appends_lab_tree():
    url = open_cmd.folder_url("https://hub.example/api", "users/alice")
    assert url == "https://hub.example/lab/tree/users/alice"


def test_folder_url_without_api_suffix():
    url = open_cmd.folder_url("https://hub.example/user/alice", "projeto")
    assert url == "https://hub.example/user/alice/lab/tree/projeto"


def test_folder_url_appends_subpath():
    url = open_cmd.folder_url("https://hub.example/api", "users/alice", "sub/dir")
    assert url == "https://hub.example/lab/tree/users/alice/sub/dir"


def test_folder_url_percent_encodes_spaces_and_accents():
    url = open_cmd.folder_url("https://hub.example", "proj eto", "ção")
    # Each segment is encoded independently; the '/' separators stay literal.
    assert url == "https://hub.example/lab/tree/proj%20eto/%C3%A7%C3%A3o"


def test_folder_url_empty_subpath_is_noop():
    assert open_cmd.folder_url("https://h", "p", "") == "https://h/lab/tree/p"
    assert open_cmd.folder_url("https://h", "p", ".") == "https://h/lab/tree/p"


# --------------------------------------------------------------------------- #
# Guards
# --------------------------------------------------------------------------- #
def test_open_outside_repo_returns_config_exit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["open", "--print"]) == EXIT_CONFIG


def test_open_non_interactive_without_flags_is_usage_error(repo, monkeypatch):
    monkeypatch.chdir(repo)
    monkeypatch.setattr(tui, "interactive", lambda *a, **k: False)
    assert cli.main(["open"]) == EXIT_USAGE


def test_open_inside_dot_jp_is_refused(repo, monkeypatch):
    # .jp/ is local metadata, never on the remote -- refuse, even with --print.
    monkeypatch.chdir(repo / ".jp")
    assert cli.main(["open", "--print"]) == EXIT_USAGE


def test_open_inside_dot_jp_subdir_is_refused(repo, monkeypatch):
    sub = repo / ".jp" / "tokens"
    sub.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(sub)
    assert cli.main(["open", "--print"]) == EXIT_USAGE


def test_open_inside_hidden_dir_is_refused(repo, monkeypatch):
    # Hidden (dot-name) folders are never uploaded (server rejects them).
    sub = repo / ".cache"
    sub.mkdir()
    monkeypatch.chdir(sub)
    assert cli.main(["open", "--print"]) == EXIT_USAGE


def test_open_inside_jpignored_dir_is_refused(repo, monkeypatch):
    (repo / ".jpignore").write_text("build/\n", encoding="utf-8")
    sub = repo / "build"
    sub.mkdir()
    monkeypatch.chdir(sub)
    assert cli.main(["open", "--print"]) == EXIT_USAGE


def test_open_inside_normal_subdir_is_allowed(repo, monkeypatch, capsys):
    (repo / ".jpignore").write_text("build/\n", encoding="utf-8")
    sub = repo / "src"
    sub.mkdir()
    monkeypatch.chdir(sub)
    assert cli.main(["open", "--print"]) == EXIT_OK
    assert capsys.readouterr().out.strip() == "https://hub.example/lab/tree/users/alice/src"


def test_open_force_prints_blocked_folder(repo, monkeypatch, capsys):
    sub = repo / ".cache"
    sub.mkdir()
    monkeypatch.chdir(sub)
    assert cli.main(["open", "--force", "--print"]) == EXIT_OK
    out = capsys.readouterr().out.strip()
    assert out == "https://hub.example/lab/tree/users/alice/.cache"


def test_open_force_yes_opens_blocked_folder(repo, monkeypatch):
    sub = repo / ".cache"
    sub.mkdir()
    monkeypatch.chdir(sub)
    opened: list[str] = []
    monkeypatch.setattr(open_cmd.webbrowser, "open", lambda url: opened.append(url) or True)
    assert cli.main(["open", "-f", "-y"]) == EXIT_OK
    assert opened == ["https://hub.example/lab/tree/users/alice/.cache"]


def test_open_force_shows_prompt_and_follows_choice(repo, monkeypatch):
    sub = repo / ".cache"
    sub.mkdir()
    monkeypatch.chdir(sub)
    monkeypatch.setattr(tui, "interactive", lambda *a, **k: True)
    seen: list[list[str]] = []

    def _pick(labels, *a, **k):
        seen.append(list(labels))
        return (1, False)  # copy

    monkeypatch.setattr(tui, "select_one_remember", _pick)
    copied: list[str] = []
    monkeypatch.setattr(clipboard, "copy", lambda text: copied.append(text) or "pbcopy")
    assert cli.main(["open", "--force"]) == EXIT_OK
    assert seen  # the prompt was shown
    assert copied == ["https://hub.example/lab/tree/users/alice/.cache"]


def test_open_force_ignores_remembered_choice_and_reprompts(repo, monkeypatch):
    from jp import prefs

    prefs.set("open_action", "browser")  # would normally skip the prompt
    sub = repo / ".cache"
    sub.mkdir()
    monkeypatch.chdir(sub)
    monkeypatch.setattr(tui, "interactive", lambda *a, **k: True)
    prompted = {"n": 0}

    def _pick(*a, **k):
        prompted["n"] += 1
        return (None, False)  # cancel

    monkeypatch.setattr(tui, "select_one_remember", _pick)
    opened: list[str] = []
    monkeypatch.setattr(open_cmd.webbrowser, "open", lambda url: opened.append(url) or True)
    assert cli.main(["open", "--force"]) == EXIT_OK
    assert prompted["n"] == 1  # asked despite the saved choice
    assert opened == []  # cancelled, did not auto-open from the saved pref


def test_open_force_on_normal_folder_is_harmless(repo, monkeypatch, capsys):
    sub = repo / "src"
    sub.mkdir()
    monkeypatch.chdir(sub)
    assert cli.main(["open", "-f", "--print"]) == EXIT_OK
    assert capsys.readouterr().out.strip() == "https://hub.example/lab/tree/users/alice/src"


def test_unsynced_reason_classifies_paths():
    from jp.ignore import IgnoreSet

    ig = IgnoreSet(["build/"])
    assert open_cmd._unsynced_reason("", ig) is None
    assert open_cmd._unsynced_reason("data/raw", ig) is None
    assert open_cmd._unsynced_reason(".jp", ig) is not None
    assert open_cmd._unsynced_reason(".jp/tokens", ig) is not None
    assert open_cmd._unsynced_reason(".cache", ig) is not None
    assert open_cmd._unsynced_reason("a/.hidden/b", ig) is not None
    assert open_cmd._unsynced_reason("build", ig) is not None


# --------------------------------------------------------------------------- #
# Flags
# --------------------------------------------------------------------------- #
def test_open_print_emits_url_only(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    assert cli.main(["open", "--print"]) == EXIT_OK
    assert capsys.readouterr().out.strip() == "https://hub.example/lab/tree/users/alice"


def test_open_print_from_subfolder_targets_subfolder(repo, monkeypatch, capsys):
    sub = repo / "data" / "raw"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    assert cli.main(["open", "--print"]) == EXIT_OK
    out = capsys.readouterr().out.strip()
    assert out == "https://hub.example/lab/tree/users/alice/data/raw"


def test_open_copy_invokes_clipboard(repo, monkeypatch):
    monkeypatch.chdir(repo)
    copied: list[str] = []
    monkeypatch.setattr(clipboard, "copy", lambda text: copied.append(text) or "pbcopy")
    assert cli.main(["open", "--copy"]) == EXIT_OK
    assert copied == ["https://hub.example/lab/tree/users/alice"]


def test_open_yes_opens_browser(repo, monkeypatch):
    monkeypatch.chdir(repo)
    opened: list[str] = []
    monkeypatch.setattr(open_cmd.webbrowser, "open", lambda url: opened.append(url) or True)
    assert cli.main(["open", "-y"]) == EXIT_OK
    assert opened == ["https://hub.example/lab/tree/users/alice"]


# --------------------------------------------------------------------------- #
# Interactive three-way picker
# --------------------------------------------------------------------------- #
def test_open_picker_yes_opens_browser(repo, monkeypatch):
    monkeypatch.chdir(repo)
    monkeypatch.setattr(tui, "interactive", lambda *a, **k: True)
    monkeypatch.setattr(tui, "select_one_remember", lambda *a, **k: (0, False))
    opened: list[str] = []
    monkeypatch.setattr(open_cmd.webbrowser, "open", lambda url: opened.append(url) or True)
    assert cli.main(["open"]) == EXIT_OK
    assert opened == ["https://hub.example/lab/tree/users/alice"]


def test_open_picker_copy_only_copies(repo, monkeypatch):
    monkeypatch.chdir(repo)
    monkeypatch.setattr(tui, "interactive", lambda *a, **k: True)
    monkeypatch.setattr(tui, "select_one_remember", lambda *a, **k: (1, False))
    copied: list[str] = []
    monkeypatch.setattr(clipboard, "copy", lambda text: copied.append(text) or "pbcopy")
    opened: list[str] = []
    monkeypatch.setattr(open_cmd.webbrowser, "open", lambda url: opened.append(url) or True)
    assert cli.main(["open"]) == EXIT_OK
    assert copied == ["https://hub.example/lab/tree/users/alice"]
    assert opened == []  # never opened the browser


def test_open_picker_cancel_does_nothing(repo, monkeypatch):
    monkeypatch.chdir(repo)
    monkeypatch.setattr(tui, "interactive", lambda *a, **k: True)
    monkeypatch.setattr(tui, "select_one_remember", lambda *a, **k: (None, False))
    opened: list[str] = []
    monkeypatch.setattr(open_cmd.webbrowser, "open", lambda url: opened.append(url) or True)
    monkeypatch.setattr(clipboard, "copy", lambda text: "pbcopy")
    assert cli.main(["open"]) == EXIT_OK
    assert opened == []


# --------------------------------------------------------------------------- #
# "Don't ask again" (global pref) + --ask undo
# --------------------------------------------------------------------------- #
def test_open_picker_remember_browser_saves_pref(repo, monkeypatch):
    from jp import prefs

    monkeypatch.chdir(repo)
    monkeypatch.setattr(tui, "interactive", lambda *a, **k: True)
    # idx 0 (open), remember=True
    monkeypatch.setattr(tui, "select_one_remember", lambda *a, **k: (0, True))
    monkeypatch.setattr(open_cmd.webbrowser, "open", lambda url: True)
    assert cli.main(["open"]) == EXIT_OK
    assert prefs.get("open_action") == "browser"


def test_open_picker_pick_once_does_not_save(repo, monkeypatch):
    from jp import prefs

    monkeypatch.chdir(repo)
    monkeypatch.setattr(tui, "interactive", lambda *a, **k: True)
    monkeypatch.setattr(tui, "select_one_remember", lambda *a, **k: (1, False))
    monkeypatch.setattr(clipboard, "copy", lambda text: "pbcopy")
    assert cli.main(["open"]) == EXIT_OK
    assert prefs.get("open_action") == ""  # nothing remembered


def test_open_uses_saved_browser_pref_without_prompting(repo, monkeypatch):
    from jp import prefs

    prefs.set("open_action", "browser")
    monkeypatch.chdir(repo)
    opened: list[str] = []
    monkeypatch.setattr(open_cmd.webbrowser, "open", lambda url: opened.append(url) or True)

    # If the prompt were reached it would explode (no fake reader); it must not be.
    def _boom(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("prompt should be skipped when a choice is remembered")

    monkeypatch.setattr(tui, "select_one_remember", _boom)
    assert cli.main(["open"]) == EXIT_OK
    assert opened == ["https://hub.example/lab/tree/users/alice"]


def test_open_uses_saved_copy_pref(repo, monkeypatch):
    from jp import prefs

    prefs.set("open_action", "copy")
    monkeypatch.chdir(repo)
    copied: list[str] = []
    monkeypatch.setattr(clipboard, "copy", lambda text: copied.append(text) or "pbcopy")
    assert cli.main(["open"]) == EXIT_OK
    assert copied == ["https://hub.example/lab/tree/users/alice"]


def test_open_ask_clears_saved_pref_and_reprompts(repo, monkeypatch):
    from jp import prefs

    prefs.set("open_action", "browser")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(tui, "interactive", lambda *a, **k: True)
    # Re-prompt: pick copy, once (no remember) -> pref ends up cleared.
    monkeypatch.setattr(tui, "select_one_remember", lambda *a, **k: (1, False))
    monkeypatch.setattr(clipboard, "copy", lambda text: "pbcopy")
    assert cli.main(["open", "--ask"]) == EXIT_OK
    assert prefs.get("open_action") == ""


def test_open_explicit_flag_ignores_and_keeps_saved_pref(repo, monkeypatch):
    from jp import prefs

    prefs.set("open_action", "browser")
    monkeypatch.chdir(repo)
    # --print is an explicit override: it neither prompts nor changes the pref.
    assert cli.main(["open", "--print"]) == EXIT_OK
    assert prefs.get("open_action") == "browser"


# --------------------------------------------------------------------------- #
# prefs module
# --------------------------------------------------------------------------- #
def test_prefs_roundtrip_and_clear():
    from jp import prefs

    assert prefs.get("open_action") == ""
    prefs.set("open_action", "copy")
    assert prefs.get("open_action") == "copy"
    prefs.clear("open_action")
    assert prefs.get("open_action") == ""


def test_prefs_clear_missing_is_noop():
    from jp import prefs

    prefs.clear("open_action")  # no file yet -- must not raise
    assert prefs.get("open_action", "x") == "x"
