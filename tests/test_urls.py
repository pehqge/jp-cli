"""URL parsing: a browser URL must resolve to (base_url, prefix) robustly."""

from __future__ import annotations

import pytest

from jp.errors import UsageError
from jp.urls import parse_clone_url


@pytest.mark.parametrize(
    "url,base,prefix",
    [
        (
            "https://jupyter.vlab.ufsc.br/user/pedro.gimenez/lab/tree/privado",
            "https://jupyter.vlab.ufsc.br/user/pedro.gimenez",
            "privado",
        ),
        (
            "https://jupyter.vlab.ufsc.br/user/pedro.gimenez/lab/tree/privado/projeto",
            "https://jupyter.vlab.ufsc.br/user/pedro.gimenez",
            "privado/projeto",
        ),
        (
            "https://h/user/bob/doc/tree/data/x",
            "https://h/user/bob",
            "data/x",
        ),
        (
            "https://h/user/bob/tree/data",
            "https://h/user/bob",
            "data",
        ),
        (
            "https://h/user/bob/notebooks/nb/run.ipynb",
            "https://h/user/bob",
            "nb/run.ipynb",
        ),
        (
            "https://h/user/bob/lab/workspaces/auto-x/tree/proj",
            "https://h/user/bob",
            "proj",
        ),
        (
            # query string and fragment are dropped
            "https://h/user/bob/lab/tree/proj?reset&token=SECRET#frag",
            "https://h/user/bob",
            "proj",
        ),
        (
            # percent-encoded path segment
            "https://h/user/bob/lab/tree/minha%20pasta/x",
            "https://h/user/bob",
            "minha pasta/x",
        ),
        (
            # standalone server (no /user/)
            "https://standalone.example/lab/tree/work/sub",
            "https://standalone.example",
            "work/sub",
        ),
        (
            # named server between <name> and lab
            "https://h/user/bob/gpu/lab/tree/proj",
            "https://h/user/bob/gpu",
            "proj",
        ),
        (
            # trailing slash
            "https://h/user/bob/lab/tree/privado/",
            "https://h/user/bob",
            "privado",
        ),
    ],
)
def test_parse_clone_url(url, base, prefix):
    got_base, got_prefix = parse_clone_url(url)
    assert got_base == base
    assert got_prefix == prefix


@pytest.mark.parametrize(
    "bad", ["", "not a url", "ftp://h/x", "/user/bob/lab/tree/x", "h/lab/tree"]
)
def test_parse_clone_url_rejects_non_http(bad):
    with pytest.raises(UsageError):
        parse_clone_url(bad)


def test_parse_clone_url_root_gives_empty_prefix():
    # A URL with no folder resolves to an empty prefix; the caller (clone/init)
    # then refuses it via validate_prefix. parse_clone_url itself does not raise.
    base, prefix = parse_clone_url("https://h/user/bob/lab")
    assert base == "https://h/user/bob"
    assert prefix == ""
