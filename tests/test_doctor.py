"""``jp doctor`` health-probe behaviour (research §8).

The doctor must distinguish, via a redirect-free GET /api/status:
  * server up (200),
  * server stopped (3xx -> /hub) -> an ACTIONABLE "start your server" message,
  * bad token (403),
without following redirects (which would hide the stopped-server signal).
"""

from __future__ import annotations

import conftest
import jp.commands.doctor as doctor
from jp.commands import _context
from jp.errors import EXIT_OK, EXIT_PARTIAL


class _Args:
    no_network = False


def _setup(repo, monkeypatch, fake):
    monkeypatch.chdir(repo)
    monkeypatch.setattr(_context, "build_api", lambda cfg: fake)
    # Token is loaded by doctor via config.load_token; short-circuit it.
    from jp import config as config_mod

    monkeypatch.setattr(config_mod, "load_token", lambda cfg: "tok-1234567890abcdef")


def test_doctor_reports_server_up(repo, monkeypatch, capsys):
    fake = conftest.FakeApi()
    fake.status_mode = "up"
    fake.seed("users/alice/x.txt", b"x")
    _setup(repo, monkeypatch, fake)
    rc = doctor.run(_Args())
    out = capsys.readouterr().out
    assert "server: up" in out
    assert rc == EXIT_OK


def test_doctor_server_down_gives_actionable_error(repo, monkeypatch, capsys):
    fake = conftest.FakeApi()
    fake.status_mode = "down"  # raises ServerDownError, mirroring a 3xx -> /hub
    _setup(repo, monkeypatch, fake)
    rc = doctor.run(_Args())
    captured = capsys.readouterr()
    combined = (captured.out + captured.err).lower()
    # Actionable: tell the user to start the server from the Hub UI.
    assert "start" in combined and "server" in combined
    assert rc == EXIT_PARTIAL


def test_doctor_bad_token_reports_auth(repo, monkeypatch, capsys):
    fake = conftest.FakeApi()
    fake.status_mode = "auth"  # raises AuthError (403 JSON on a running server)
    _setup(repo, monkeypatch, fake)
    rc = doctor.run(_Args())
    # Reported as a problem (token/auth), distinct from a clean pass.
    assert rc == EXIT_PARTIAL
