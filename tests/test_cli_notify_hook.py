from __future__ import annotations

import jp.cli as cli


def test_main_calls_maybe_notify(monkeypatch):
    called = {}
    monkeypatch.setattr(
        cli.update_notify, "maybe_notify", lambda args: called.setdefault("ok", args.command)
    )
    rc = cli.main(["version"])
    assert rc == 0
    assert called.get("ok") == "version"


def test_notify_failure_does_not_break_command(monkeypatch):
    def boom(_):
        raise RuntimeError("notifier exploded")

    monkeypatch.setattr(cli.update_notify, "maybe_notify", boom)
    rc = cli.main(["version"])
    assert rc == 0
