"""Notebook-aware hybrid normalization for the versioning store.

The DECISION (locked): notebooks are stored HYBRID. The object store always holds
the ORIGINAL ``.ipynb`` bytes (exactly what ``add`` snapshots), so a checkout
restores the faithful original -- outputs, widget state and all. But CHANGE
DETECTION and human-readable DIFF use a NORMALIZED form derived from those bytes,
which deliberately discards the volatile parts of a notebook that churn without a
real semantic change. The single load-bearing property is *suppression*: a pure
re-run -- same code, fresh outputs and a bumped ``execution_count`` -- must hash
identically under :func:`normalized_sha`, so it never creates a new version.

What normalization STRIPS (and why)
-----------------------------------
* every cell's ``outputs``  -> set to ``[]`` (outputs are a function of running,
  not of the source; a re-run regenerates them);
* every cell's ``execution_count`` -> ``None`` (a monotonic run counter -- pure
  churn);
* ``metadata.widgets`` -> dropped (ipywidgets persist a *huge* base64 state blob
  here that changes on every interaction but is not source);
* ``metadata.language_info.version`` -> dropped (the interpreter patch version,
  e.g. ``"3.11.4"``, varies by machine/kernel and is not a code change).

Everything else -- cell ``source``/``cell_type``, cell order, notebook structure,
and all other metadata -- is preserved, so a real code edit DOES change the
normalized form.

nbformat support
----------------
nbformat v4 (top-level ``cells`` list) is handled fully. v3 stored cells under
``worksheets`` and has NO top-level ``cells`` list; such a file is treated as an
opaque binary blob (``None``) rather than mis-normalized -- we never crash on it.
Anything that is not valid JSON, or is JSON but not a dict with a ``cells`` list,
is likewise opaque (``None``): the caller stores/diffs it as a plain binary blob.

Defensive throughout: a structurally-odd-but-JSON notebook (cells missing keys, a
string ``source`` instead of a list, unexpected types) must NEVER raise -- only a
non-notebook returns ``None``. Standard library only (``json``, ``hashlib``); we
deliberately do NOT import ``nbformat``/``nbconvert`` (zero third-party deps).
"""

from __future__ import annotations

import hashlib
import json

# The ``.ipynb`` suffix, matched case-insensitively.
_NB_SUFFIX = ".ipynb"

# Notebook-level metadata keys we drop wholesale (volatile, non-source).
_DROP_METADATA_KEYS = ("widgets",)
# Nested ``metadata.language_info`` sub-keys we drop (interpreter patch version).
_DROP_LANGUAGE_INFO_KEYS = ("version",)


def is_notebook(rel: str) -> bool:
    """True iff ``rel`` has a case-insensitive ``.ipynb`` suffix.

    A pure name check (it does not read the file); the bytes are only inspected
    by :func:`normalize_notebook` and friends, which fall back to opaque (``None``)
    if a ``.ipynb``-named file turns out not to be a real notebook.
    """
    return isinstance(rel, str) and rel.lower().endswith(_NB_SUFFIX)


def _parse_notebook(data: bytes) -> dict | None:
    """Parse ``data`` as a notebook object, or return ``None`` if it is not one.

    A notebook is, for our purposes, a JSON *object* (dict) carrying a ``cells``
    *list*. Parse errors, non-dict JSON, and a missing/non-list ``cells`` (e.g. a
    v3 ``worksheets`` notebook) all return ``None`` so the caller treats the bytes
    as an opaque binary blob. Never raises.
    """
    try:
        obj = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    if not isinstance(obj.get("cells"), list):
        return None
    return obj


def normalize_notebook(data: bytes) -> bytes | None:
    """Return a CANONICAL, change-detection form of the notebook, or ``None``.

    ``None`` means ``data`` is not a notebook (not JSON, not a dict, or has no
    ``cells`` list) and should be handled as an opaque blob. Otherwise every cell
    has its ``outputs`` cleared and ``execution_count`` nulled, the volatile
    notebook-level metadata (``widgets`` and ``language_info.version``) is removed,
    and the result is re-serialized as deterministic canonical JSON bytes (sorted
    keys, no whitespace) so two semantically-identical notebooks normalize to the
    SAME bytes. Defensive: a structurally-odd notebook never raises.
    """
    obj = _parse_notebook(data)
    if obj is None:
        return None

    norm: dict = {}
    for key, value in obj.items():
        if key == "cells":
            norm["cells"] = [_normalize_cell(cell) for cell in value]
        elif key == "metadata":
            norm["metadata"] = _normalize_metadata(value)
        else:
            norm[key] = value

    return json.dumps(norm, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _normalize_cell(cell: object) -> object:
    """Strip a single cell's volatile fields. Non-dict cells pass through as-is."""
    if not isinstance(cell, dict):
        return cell
    out = dict(cell)
    # Clear outputs and execution_count ONLY when the cell actually carries them
    # (markdown/raw cells have neither). We force them to the canonical "empty"
    # values rather than deleting so a code cell that gained outputs on a re-run
    # normalizes identically to the same cell with no outputs.
    if "outputs" in out:
        out["outputs"] = []
    if "execution_count" in out:
        out["execution_count"] = None
    return out


def _normalize_metadata(metadata: object) -> object:
    """Drop volatile notebook-level metadata. Non-dict metadata passes through."""
    if not isinstance(metadata, dict):
        return metadata
    out = {k: v for k, v in metadata.items() if k not in _DROP_METADATA_KEYS}
    lang = out.get("language_info")
    if isinstance(lang, dict):
        out["language_info"] = {k: v for k, v in lang.items() if k not in _DROP_LANGUAGE_INFO_KEYS}
    return out


def normalized_sha(data: bytes) -> str | None:
    """sha256 hexdigest of :func:`normalize_notebook`, or ``None`` if not a notebook.

    This is the hybrid change-detection key: two notebooks with identical code but
    different outputs/execution counts/widget state hash to the SAME value, so a
    pure re-run is never re-staged or committed.
    """
    norm = normalize_notebook(data)
    if norm is None:
        return None
    return hashlib.sha256(norm).hexdigest()


def notebook_code_text(data: bytes) -> str | None:
    """Render a human-readable, outputs-free view of a notebook for diffing.

    Concatenates cells in order; each cell is preceded by a ``# %% [<cell_type>]``
    header (the Jupyter "percent" cell marker) and followed by its source. Outputs
    and execution counts are ignored entirely, so the rendering is STABLE across a
    pure re-run -- which is exactly what ``jp show`` / ``jp diff`` diff for
    notebooks. Returns ``None`` if ``data`` is not a notebook. Never raises on an
    odd cell (a string ``source`` or a missing ``cell_type`` is tolerated).
    """
    obj = _parse_notebook(data)
    if obj is None:
        return None

    lines: list[str] = []
    for cell in obj["cells"]:
        cell_type = "unknown"
        source: object = ""
        if isinstance(cell, dict):
            ct = cell.get("cell_type", "unknown")
            cell_type = str(ct) if ct is not None else "unknown"
            source = cell.get("source", "")
        lines.append(f"# %% [{cell_type}]")
        lines.append(_source_to_text(source))
    # Trailing newline gives unified_diff clean per-line records.
    return "\n".join(lines) + "\n"


def _source_to_text(source: object) -> str:
    """Coerce a cell ``source`` (a list of strings, a string, or odd) to text.

    nbformat stores ``source`` as a list of line-strings (each typically ending in
    ``\\n``), but some tools emit a single string. Either is accepted; anything
    else is rendered via ``str`` so we never raise on a malformed cell.
    """
    if isinstance(source, list):
        return "".join(str(part) for part in source)
    if isinstance(source, str):
        return source
    return str(source)
