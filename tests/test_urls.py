"""URL parsing: a browser URL must resolve to (base_url, prefix) robustly."""

from __future__ import annotations

import pytest

from jp.errors import UsageError
from jp.urls import origin_of, parse_clone_url, username_of


@pytest.mark.parametrize(
    "url,base,prefix",
    [
        (
            "https://jupyter.example.com/user/you/lab/tree/privado",
            "https://jupyter.example.com/user/you",
            "privado",
        ),
        (
            "https://jupyter.example.com/user/you/lab/tree/privado/projeto",
            "https://jupyter.example.com/user/you",
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


@pytest.mark.parametrize(
    "url,origin",
    [
        ("https://jupyter.example.com/user/you/lab/tree/x", "https://jupyter.example.com"),
        ("http://h/user/bob/tree/data", "http://h"),
        # scheme and host are lowercased
        ("HTTPS://Jupyter.Example.COM/user/you", "https://jupyter.example.com"),
        # an explicit port is part of the origin
        ("https://h:8888/user/bob/lab/tree/x", "https://h:8888"),
        # the path is stripped entirely
        ("https://h/a/b/c?q=1#frag", "https://h"),
        # non-http(s) and empty inputs yield ""
        ("ftp://h/x", ""),
        ("not a url", ""),
        ("", ""),
    ],
)
def test_origin_of(url, origin):
    assert origin_of(url) == origin


@pytest.mark.parametrize(
    "url,user",
    [
        ("https://h/user/alice/lab/tree/x", "alice"),
        ("https://h/user/bob", "bob"),
        # percent-encoded username is unquoted once
        ("https://h/user/al%20ice/lab", "al ice"),
        # no /user/ segment
        ("https://standalone.example/lab/tree/x", ""),
        # /user/ with nothing after it
        ("https://h/user/", ""),
        # empty input
        ("", ""),
    ],
)
def test_username_of(url, user):
    assert username_of(url) == user
