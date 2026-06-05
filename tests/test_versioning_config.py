"""Versioning config keys: serialization, validation, and ``jp config`` wiring.

Covers the five opt-in versioning settings added to :class:`jp.config.Config`:

* round-trip through ``to_json``/``from_json`` for every field;
* the OPT-IN invariant -- a DEFAULT config serializes NO versioning keys, so a
  non-adopter's ``config.json`` never grows them; only a changed key is written,
  under its dotted display key (``versioning.push_prompt`` etc.);
* enum sanitization (an unknown value falls back to the default, mirroring how
  ``color``/``dotfiles`` are sanitized) and ``max_blob_mb`` coercion to a
  positive int (bad -> default 100);
* the dotted versioning keys are treated as KNOWN (never land in ``extra``);
* the scriptable ``jp config get/set/list`` path works for the new keys,
  including the freeform ``versioning.author`` and a rejected bad enum.
"""

from __future__ import annotations

import argparse
import json

import pytest

import jp.commands.config_cmd as config_cmd
from jp.commands._context import RepoContext
from jp.config import Config
from jp.errors import EXIT_OK, UsageError
from jp.ignore import IgnoreSet
from jp.index import Index


# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #
def test_versioning_defaults():
    cfg = Config(base_url="https://h/api", prefix="users/alice")
    assert cfg.versioning_push_prompt == "ask"
    assert cfg.versioning_mirror_history == "ask"
    assert cfg.versioning_notebook_outputs == "hybrid"
    assert cfg.versioning_max_blob_mb == 100
    assert cfg.versioning_author == ""


# --------------------------------------------------------------------------- #
# Opt-in serialization: a default config writes NO versioning keys
# --------------------------------------------------------------------------- #
def test_default_config_serializes_no_versioning_keys(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    cfg = Config(base_url="https://h/api", prefix="users/alice")
    config_mod_save(root, cfg)
    raw = json.loads((root / ".jp" / "config.json").read_text())
    assert not any(k.startswith("versioning.") for k in raw), raw


def test_only_changed_versioning_key_is_written(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    cfg = Config(base_url="https://h/api", prefix="users/alice")
    cfg.versioning_push_prompt = "always"
    config_mod_save(root, cfg)
    raw = json.loads((root / ".jp" / "config.json").read_text())
    # ONLY the changed key is present, under its dotted display key.
    assert raw["versioning.push_prompt"] == "always"
    assert "versioning.mirror_history" not in raw
    assert "versioning.notebook_outputs" not in raw
    assert "versioning.max_blob_mb" not in raw
    assert "versioning.author" not in raw


# --------------------------------------------------------------------------- #
# Round-trip
# --------------------------------------------------------------------------- #
def test_versioning_roundtrip_all_fields(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    cfg = Config(base_url="https://h/api", prefix="users/alice")
    cfg.versioning_push_prompt = "never"
    cfg.versioning_mirror_history = "always"
    cfg.versioning_notebook_outputs = "full"
    cfg.versioning_max_blob_mb = 250
    cfg.versioning_author = "Alice <alice@example.com>"
    config_mod_save(root, cfg)
    loaded = config_mod_load(root)
    assert loaded.versioning_push_prompt == "never"
    assert loaded.versioning_mirror_history == "always"
    assert loaded.versioning_notebook_outputs == "full"
    assert loaded.versioning_max_blob_mb == 250
    assert loaded.versioning_author == "Alice <alice@example.com>"


def test_versioning_keys_are_known_not_extra():
    cfg = Config.from_json(
        {
            "base_url": "https://h/api",
            "prefix": "users/alice",
            "versioning.push_prompt": "always",
            "versioning.author": "X",
        }
    )
    # Dotted versioning keys must be KNOWN -- never leak into the opaque extra dict.
    assert "versioning.push_prompt" not in cfg.extra
    assert "versioning.author" not in cfg.extra
    assert cfg.versioning_push_prompt == "always"
    assert cfg.versioning_author == "X"


# --------------------------------------------------------------------------- #
# Enum sanitization + int coercion (mirrors color/dotfiles handling)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "key,attr,bad,default",
    [
        ("versioning.push_prompt", "versioning_push_prompt", "sometimes", "ask"),
        ("versioning.mirror_history", "versioning_mirror_history", "maybe", "ask"),
        ("versioning.notebook_outputs", "versioning_notebook_outputs", "strip", "hybrid"),
        ("versioning.notebook_outputs", "versioning_notebook_outputs", "bogus", "hybrid"),
    ],
)
def test_unknown_enum_falls_back_to_default(key, attr, bad, default):
    cfg = Config.from_json({"base_url": "https://h/api", "prefix": "users/alice", key: bad})
    assert getattr(cfg, attr) == default


@pytest.mark.parametrize("bad", ["-5", 0, -3, "abc", None, "", 1.5])
def test_bad_max_blob_mb_falls_back_to_100(bad):
    cfg = Config.from_json(
        {"base_url": "https://h/api", "prefix": "users/alice", "versioning.max_blob_mb": bad}
    )
    assert cfg.versioning_max_blob_mb == 100


def test_valid_max_blob_mb_is_kept():
    cfg = Config.from_json(
        {"base_url": "https://h/api", "prefix": "users/alice", "versioning.max_blob_mb": "500"}
    )
    assert cfg.versioning_max_blob_mb == 500


# --------------------------------------------------------------------------- #
# jp config get/set/list wiring
# --------------------------------------------------------------------------- #
def config_mod_save(root, cfg):
    from jp import config as config_mod

    config_mod.save(root, cfg)


def config_mod_load(root):
    from jp import config as config_mod

    return config_mod.load(root)


def _ctx(root) -> RepoContext:
    from jp import config as config_mod

    return RepoContext(
        root=root,
        cfg=config_mod.load(root),
        index=Index.load(root),
        ignore=IgnoreSet.from_root(root),
    )


def _setup(monkeypatch, tmp_path) -> RepoContext:
    root = tmp_path / "r"
    root.mkdir()
    config_mod_save(root, Config(base_url="https://h/api", prefix="users/alice"))
    ctx = _ctx(root)
    monkeypatch.setattr(config_cmd, "load_repo", lambda: ctx)
    return ctx


def _args(action=None, key=None, value=None):
    return argparse.Namespace(action=action, key=key, value=value)


def test_config_set_get_versioning_enum(monkeypatch, tmp_path, capsys):
    ctx = _setup(monkeypatch, tmp_path)
    rc = config_cmd.run(_args("set", "versioning.push_prompt", "always"))
    assert rc == EXIT_OK
    # Persisted to disk.
    raw = json.loads((ctx.root / ".jp" / "config.json").read_text())
    assert raw["versioning.push_prompt"] == "always"
    # And readable back through `get`.
    capsys.readouterr()
    config_cmd.run(_args("get", "versioning.push_prompt"))
    assert "always" in capsys.readouterr().out


def test_config_set_rejects_bad_enum(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    with pytest.raises(UsageError):
        config_cmd.run(_args("set", "versioning.push_prompt", "sometimes"))


def test_config_set_rejects_strip_notebook_outputs(monkeypatch, tmp_path):
    # "strip" is intentionally NOT supported in v1 (lossy) -- reject it on set.
    _setup(monkeypatch, tmp_path)
    with pytest.raises(UsageError):
        config_cmd.run(_args("set", "versioning.notebook_outputs", "strip"))


def test_config_set_author_freeform_roundtrips(monkeypatch, tmp_path):
    ctx = _setup(monkeypatch, tmp_path)
    rc = config_cmd.run(_args("set", "versioning.author", "Pedro <p@e.com>"))
    assert rc == EXIT_OK
    raw = json.loads((ctx.root / ".jp" / "config.json").read_text())
    assert raw["versioning.author"] == "Pedro <p@e.com>"
    from jp import config as config_mod

    assert config_mod.load(ctx.root).versioning_author == "Pedro <p@e.com>"


def test_config_set_max_blob_mb_rejects_nonpositive(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    with pytest.raises(UsageError):
        config_cmd.run(_args("set", "versioning.max_blob_mb", "0"))


def test_config_list_includes_versioning_keys(monkeypatch, tmp_path, capsys):
    _setup(monkeypatch, tmp_path)
    config_cmd.run(_args("list"))
    out = capsys.readouterr().out
    for key in (
        "versioning.push_prompt",
        "versioning.mirror_history",
        "versioning.notebook_outputs",
        "versioning.max_blob_mb",
        "versioning.author",
    ):
        assert key in out


# --------------------------------------------------------------------------- #
# notebook_outputs drives stage_paths: hybrid suppresses a re-run, full does not
# --------------------------------------------------------------------------- #
def _nb_bytes(*, output_text, execution_count):
    nb = {
        "cells": [
            {
                "cell_type": "code",
                "source": ["print('hi')\n"],
                "outputs": (
                    []
                    if output_text is None
                    else [{"output_type": "stream", "name": "stdout", "text": [output_text]}]
                ),
                "execution_count": execution_count,
                "metadata": {},
            }
        ],
        "metadata": {},
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    return json.dumps(nb).encode("utf-8")


def _stage_repo(tmp_path, notebook_outputs):
    from jp import config as config_mod
    from jp.ignore import IgnoreSet
    from jp.versioning.staging import Staging

    root = tmp_path / "w"
    root.mkdir()
    cfg = Config(
        base_url="https://h/api",
        prefix="users/alice",
        versioning_notebook_outputs=notebook_outputs,
    )
    config_mod.save(root, cfg)
    return root, cfg, IgnoreSet.from_root(root), Staging


def test_stage_paths_hybrid_suppresses_pure_rerun(tmp_path):
    from jp.versioning import repo as vrepo
    from jp.versioning.staging import Staging

    root, cfg, ignore, _ = _stage_repo(tmp_path, "hybrid")
    nb = root / "n.ipynb"
    nb.write_bytes(_nb_bytes(output_text=None, execution_count=None))
    vrepo.stage_paths(root, cfg, ignore, ["n.ipynb"], all_files=False, dry_run=False)
    first_sha = Staging.load(root).get("n.ipynb").sha256
    # A pure re-run: same code, new outputs + bumped execution_count.
    nb.write_bytes(_nb_bytes(output_text="result", execution_count=1))
    res = vrepo.stage_paths(root, cfg, ignore, ["n.ipynb"], all_files=False, dry_run=False)
    # Suppressed: not re-staged, the original blob stands.
    assert "n.ipynb" not in res["staged"]
    assert Staging.load(root).get("n.ipynb").sha256 == first_sha


def test_stage_paths_full_versions_every_rerun(tmp_path):
    from jp.versioning import repo as vrepo
    from jp.versioning.staging import Staging

    root, cfg, ignore, _ = _stage_repo(tmp_path, "full")
    nb = root / "n.ipynb"
    nb.write_bytes(_nb_bytes(output_text=None, execution_count=None))
    vrepo.stage_paths(root, cfg, ignore, ["n.ipynb"], all_files=False, dry_run=False)
    e1 = Staging.load(root).get("n.ipynb")
    assert e1.nb_norm_sha == ""  # full mode records NO normalized sha
    # A re-run under "full" IS a real change -> new blob, re-staged.
    nb.write_bytes(_nb_bytes(output_text="result", execution_count=1))
    res = vrepo.stage_paths(root, cfg, ignore, ["n.ipynb"], all_files=False, dry_run=False)
    assert "n.ipynb" in res["staged"]
    assert Staging.load(root).get("n.ipynb").sha256 != e1.sha256
