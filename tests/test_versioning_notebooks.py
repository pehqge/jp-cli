"""Tests for the notebook-hybrid normalization layer (jp.versioning.notebooks).

The HYBRID decision: the stored blob is the ORIGINAL .ipynb bytes (faithful for
checkout), but CHANGE-DETECTION and DIFF use a NORMALIZED form that drops volatile
state -- cell outputs, ``execution_count``, ``metadata.widgets`` (giant base64
widget state), and ``metadata.language_info.version``. The load-bearing property
proved here is *suppression*: a pure re-run (same code, fresh outputs/counts)
yields an IDENTICAL ``normalized_sha`` so it never creates a new version, while a
real code edit yields a DIFFERENT one. Non-notebooks / corrupt JSON are treated as
opaque blobs (None), never crashing.
"""

from __future__ import annotations

import json

from jp.versioning import notebooks


# --------------------------------------------------------------------------- #
# Helpers: build small but realistic notebooks.
# --------------------------------------------------------------------------- #
def _nb(cells: list[dict], *, metadata: dict | None = None, nbformat: int = 4) -> bytes:
    obj: dict = {
        "cells": cells,
        "metadata": metadata or {},
        "nbformat": nbformat,
        "nbformat_minor": 5,
    }
    return json.dumps(obj).encode("utf-8")


def _code_cell(source, *, outputs=None, execution_count=None) -> dict:
    return {
        "cell_type": "code",
        "source": source,
        "outputs": outputs if outputs is not None else [],
        "execution_count": execution_count,
        "metadata": {},
    }


def _md_cell(source) -> dict:
    return {"cell_type": "markdown", "source": source, "metadata": {}}


# --------------------------------------------------------------------------- #
# is_notebook
# --------------------------------------------------------------------------- #
def test_is_notebook_suffix_case_insensitive():
    assert notebooks.is_notebook("a.ipynb")
    assert notebooks.is_notebook("dir/sub/Analysis.IPYNB")
    assert notebooks.is_notebook("X.IpyNb")
    assert not notebooks.is_notebook("a.py")
    assert not notebooks.is_notebook("a.ipynb.txt")
    assert not notebooks.is_notebook("ipynb")
    assert not notebooks.is_notebook("")


# --------------------------------------------------------------------------- #
# normalize_notebook / normalized_sha: structure + stripping
# --------------------------------------------------------------------------- #
def test_normalize_drops_outputs_and_execution_count():
    data = _nb([_code_cell(["print(1)\n"], outputs=[{"text": "1"}], execution_count=7)])
    norm = notebooks.normalize_notebook(data)
    assert norm is not None
    obj = json.loads(norm)
    cell = obj["cells"][0]
    assert cell["outputs"] == []
    assert cell["execution_count"] is None
    # The actual code is preserved.
    assert cell["source"] == ["print(1)\n"]


def test_normalize_drops_metadata_widgets_and_language_version():
    data = _nb(
        [_code_cell(["x = 1\n"])],
        metadata={
            "widgets": {"state": "A" * 5000},
            "language_info": {"name": "python", "version": "3.11.4"},
            "kernelspec": {"name": "python3"},
        },
    )
    norm = notebooks.normalize_notebook(data)
    assert norm is not None
    obj = json.loads(norm)
    assert "widgets" not in obj["metadata"]
    assert "version" not in obj["metadata"]["language_info"]
    # Non-volatile metadata survives.
    assert obj["metadata"]["language_info"]["name"] == "python"
    assert obj["metadata"]["kernelspec"] == {"name": "python3"}


def test_normalized_sha_is_hex_and_stable():
    data = _nb([_code_cell(["a = 2\n"])])
    sha1 = notebooks.normalized_sha(data)
    sha2 = notebooks.normalized_sha(data)
    assert sha1 == sha2
    assert sha1 is not None
    assert len(sha1) == 64
    int(sha1, 16)  # valid hex


# --------------------------------------------------------------------------- #
# The suppression proof: a re-run must NOT change the normalized sha.
# --------------------------------------------------------------------------- #
def test_pure_rerun_same_code_different_outputs_yields_same_normalized_sha():
    before = _nb([_code_cell(["print('hi')\n"], outputs=[], execution_count=None)])
    # Same code; new outputs + a bumped execution_count (a re-run).
    after = _nb(
        [
            _code_cell(
                ["print('hi')\n"],
                outputs=[{"output_type": "stream", "text": "hi\n"}],
                execution_count=42,
            )
        ]
    )
    assert before != after  # raw bytes differ
    assert notebooks.normalized_sha(before) == notebooks.normalized_sha(after)


def test_widget_state_churn_does_not_change_normalized_sha():
    a = _nb([_code_cell(["w()\n"])], metadata={"widgets": {"state": "X" * 100}})
    b = _nb([_code_cell(["w()\n"])], metadata={"widgets": {"state": "Y" * 9999}})
    assert a != b
    assert notebooks.normalized_sha(a) == notebooks.normalized_sha(b)


def test_code_change_yields_different_normalized_sha():
    before = _nb([_code_cell(["x = 1\n"])])
    after = _nb([_code_cell(["x = 2\n"])])  # a real edit
    assert notebooks.normalized_sha(before) != notebooks.normalized_sha(after)


def test_adding_a_cell_changes_normalized_sha():
    before = _nb([_code_cell(["x = 1\n"])])
    after = _nb([_code_cell(["x = 1\n"]), _md_cell(["# notes"])])
    assert notebooks.normalized_sha(before) != notebooks.normalized_sha(after)


# --------------------------------------------------------------------------- #
# Defensive: non-notebooks and odd structures never crash.
# --------------------------------------------------------------------------- #
def test_non_json_returns_none():
    assert notebooks.normalize_notebook(b"\x00\x01 not json at all") is None
    assert notebooks.normalized_sha(b"\x00\x01 not json at all") is None
    assert notebooks.notebook_code_text(b"\x00\x01 not json") is None


def test_json_but_not_a_notebook_returns_none():
    assert notebooks.normalize_notebook(b'{"hello": "world"}') is None
    assert notebooks.normalize_notebook(b"[1, 2, 3]") is None
    # "cells" present but not a list.
    assert notebooks.normalize_notebook(b'{"cells": {"a": 1}}') is None


def test_v3_worksheets_structure_does_not_crash():
    # v3 puts cells under "worksheets"; no top-level "cells" list -> opaque (None).
    v3 = json.dumps(
        {"worksheets": [{"cells": [{"cell_type": "code", "input": ["x=1"]}]}], "nbformat": 3}
    ).encode("utf-8")
    assert notebooks.normalize_notebook(v3) is None
    assert notebooks.normalized_sha(v3) is None
    assert notebooks.notebook_code_text(v3) is None


def test_structurally_odd_notebook_does_not_crash():
    # cells lacking keys: no cell_type, no source, an output-less cell, weird types.
    odd = json.dumps(
        {
            "cells": [
                {},  # no keys at all
                {"cell_type": "code"},  # no source / outputs / execution_count
                {"source": "a string not a list", "cell_type": "code"},
                {"cell_type": "raw", "source": ["raw\n"]},
            ],
            "metadata": {},
            "nbformat": 4,
        }
    ).encode("utf-8")
    norm = notebooks.normalize_notebook(odd)
    assert norm is not None  # JSON notebook with a cells list -> normalizable
    sha = notebooks.normalized_sha(odd)
    assert sha is not None and len(sha) == 64
    # And it is stable.
    assert notebooks.normalize_notebook(odd) == norm


# --------------------------------------------------------------------------- #
# notebook_code_text: readable, outputs-free, stable across re-runs.
# --------------------------------------------------------------------------- #
def test_code_text_concatenates_cells_in_order_with_headers():
    data = _nb([_code_cell(["a = 1\n", "b = 2\n"]), _md_cell(["# Title\n"])])
    text = notebooks.notebook_code_text(data)
    assert text is not None
    assert "# %% [code]" in text
    assert "# %% [markdown]" in text
    assert "a = 1" in text
    assert "b = 2" in text
    assert "# Title" in text
    # Order: code cell appears before markdown cell.
    assert text.index("a = 1") < text.index("# Title")


def test_code_text_ignores_outputs_and_is_stable_across_reruns():
    before = _nb([_code_cell(["compute()\n"], outputs=[], execution_count=None)])
    after = _nb(
        [
            _code_cell(
                ["compute()\n"],
                outputs=[{"output_type": "stream", "text": "noise\n"}],
                execution_count=99,
            )
        ]
    )
    t1 = notebooks.notebook_code_text(before)
    t2 = notebooks.notebook_code_text(after)
    assert t1 == t2
    assert t1 is not None
    assert "noise" not in t1  # outputs never appear


def test_code_text_handles_string_source():
    # source as a plain string (not a list) must not crash.
    data = json.dumps(
        {"cells": [{"cell_type": "code", "source": "single = 1\n"}], "nbformat": 4}
    ).encode("utf-8")
    text = notebooks.notebook_code_text(data)
    assert text is not None
    assert "single = 1" in text
