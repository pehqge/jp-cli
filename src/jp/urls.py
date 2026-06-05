"""Parse JupyterHub/Jupyter-server URLs into a (base_url, prefix) pair.

A user copies a URL straight from their browser, e.g.::

    https://jupyter.example.com/user/you/lab/tree/privado/projeto

and ``jp clone`` should "just work". This module turns such a URL into:

  * ``base_url`` -- the Contents API root, i.e. everything up to and including
    ``/user/<name>`` (or the server origin for a standalone server), with no
    trailing slash. ``/api/contents`` is appended by the API client.
  * ``prefix``   -- the remote sub-path the workspace is rooted at
    (``privado/projeto`` above), validated by :func:`paths.validate_prefix`.

Supported shapes (query string and fragment are dropped first)::

    .../user/<name>/lab/tree/<path>
    .../user/<name>/doc/tree/<path>
    .../user/<name>/tree/<path>
    .../user/<name>/notebooks/<path>
    .../user/<name>/edit/<path>
    .../user/<name>/files/<path>
    .../user/<name>/lab/workspaces/<ws>/tree/<path>
    .../user/<name>/<servername>/lab/tree/<path>   (named servers)
    .../lab/tree/<path>                             (standalone, no /user/)
    .../tree/<path>                                 (standalone)

The path may be percent-encoded (``%20`` etc.); it is unquoted exactly once.
A URL with no ``<path>`` resolves to an EMPTY prefix, which the caller must
reject (jp never operates on the server root) -- so the error is raised by
``validate_prefix`` downstream, with a clear message.
"""

from __future__ import annotations

import re
import urllib.parse

from .errors import UsageError

# View segments that introduce the file path in a Jupyter URL. ``tree`` covers
# both the classic tree view and the JupyterLab ``lab/tree`` form once we have
# stripped the leading ``lab``/``doc`` segment.
_VIEW_SEGMENTS = {"tree", "notebooks", "edit", "view", "files"}


def parse_clone_url(url: str) -> tuple[str, str]:
    """Return ``(base_url, prefix)`` parsed from a Jupyter URL.

    Raises :class:`UsageError` if the input is not an http(s) URL. The returned
    ``prefix`` is the raw remote sub-path (possibly empty); the caller validates
    it with :func:`paths.validate_prefix`.
    """
    raw = (url or "").strip()
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise UsageError(
            f"not a valid http(s) URL: {url!r}. "
            "Copy the address bar URL of your Jupyter folder, e.g. "
            "https://host/user/<name>/lab/tree/<folder>"
        )

    origin = f"{parts.scheme}://{parts.netloc}"
    # Drop query (?token=..., ?reset) and fragment; split the path into segments.
    segments = [s for s in parts.path.split("/") if s != ""]

    # 1) Locate the single-user server base: ".../user/<name>" if present.
    base_segments: list[str] = []
    rest: list[str]
    if "user" in segments:
        idx = segments.index("user")
        if idx + 1 >= len(segments):
            raise UsageError(f"malformed /user/ URL (missing username): {url!r}")
        base_segments = segments[: idx + 2]  # include 'user' and '<name>'
        rest = segments[idx + 2 :]
    else:
        rest = list(segments)  # standalone server, base is the origin

    # 2) A named server may sit between <name> and the view segment:
    #    /user/<name>/<servername>/lab/tree/...  -> treat <servername> as base.
    if rest and rest[0] not in _VIEW_SEGMENTS and rest[0] not in ("lab", "doc"):
        base_segments.append(rest[0])
        rest = rest[1:]

    # 3) Strip a leading 'lab' or 'doc' (JupyterLab) and optional workspace:
    #    lab/tree/... | doc/tree/... | lab/workspaces/<ws>/tree/...
    if rest and rest[0] in ("lab", "doc"):
        rest = rest[1:]
        if rest and rest[0] == "workspaces":
            # drop 'workspaces' and the workspace name
            rest = rest[2:] if len(rest) >= 2 else []

    # 4) Strip the view segment (tree/notebooks/edit/...); whatever follows is
    #    the remote path. If there is no view segment, treat the remainder as
    #    the path directly (lenient).
    if rest and rest[0] in _VIEW_SEGMENTS:
        rest = rest[1:]

    base_url = origin if not base_segments else origin + "/" + "/".join(base_segments)
    prefix = urllib.parse.unquote("/".join(rest))
    # Defensive: a stray scheme-like or backslash content should not survive.
    prefix = re.sub(r"\\", "/", prefix).strip("/")
    return base_url.rstrip("/"), prefix


def origin_of(url: str) -> str:
    """Return the lowercased ``scheme://host[:port]`` of ``url``, no path/slash.

    Used to identify the *site* a credential belongs to: a token is scoped to a
    whole hub, so only the origin matters. Returns ``""`` for an empty input or
    anything that is not an http(s) URL.
    """
    parts = urllib.parse.urlsplit((url or "").strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    # netloc already carries the optional ``:port``; lowercase the whole origin.
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


def username_of(url: str) -> str:
    """Return the segment right after ``/user/`` in a JupyterHub URL, else ``""``.

    The value is unquoted once (matching how the rest of the path is decoded).
    """
    parts = urllib.parse.urlsplit((url or "").strip())
    segments = [s for s in parts.path.split("/") if s != ""]
    if "user" in segments:
        idx = segments.index("user")
        if idx + 1 < len(segments):
            return urllib.parse.unquote(segments[idx + 1])
    return ""
