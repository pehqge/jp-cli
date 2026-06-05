"""Mirror-mode safety: push/pull never delete unless mirror is on AND confirmed.

Also: Task 8 -- the REMOTE HISTORY MIRROR. The mirror lives at ``<prefix>/__jp/``
on the remote; these tests prove the single most important invariant (``__jp`` is
EXCLUDED from sync so pull never downloads it and a mirror-mode push never lists
it as deletable), plus the write-once / ref-last / CAS / hex-guard / size-guard /
ask-once contract of :mod:`jp.versioning.mirror`.
"""

from __future__ import annotations

import argparse

import pytest

import conftest
from jp import sync
from jp.commands import _mirror
from jp.commands._context import RepoContext
from jp.ignore import IgnoreSet
from jp.index import Entry, Index
from jp.versioning import mirror as mirror_mod
from jp.versioning import refs


def _ctx(repo, cfg):
    return RepoContext(root=repo, cfg=cfg, index=Index.load(repo), ignore=IgnoreSet.from_root(repo))


def test_push_marks_remote_only_files_as_deletable(repo, cfg, fake_api, index, ignore):
    # Remote has a file with no local counterpart -> it's a deletion candidate,
    # but the engine itself NEVER deletes.
    fake_api.seed("users/alice/remote_only.txt", b"keep me")
    out = sync.push(repo, cfg, fake_api, index, ignore)
    assert "remote_only.txt" in out.deletable
    assert fake_api.deletes == []  # engine issued zero DELETEs
    assert "users/alice/remote_only.txt" in fake_api.files


def test_pull_marks_local_only_files_as_deletable(repo, cfg, fake_api, index, ignore):
    conftest.write_file(repo, "local_only.txt", b"mine")
    out = sync.pull(repo, cfg, fake_api, index, ignore)
    assert "local_only.txt" in out.deletable
    # engine never removed the local file
    assert (repo / "local_only.txt").exists()


def test_mirror_handle_deletes_only_confirmed_remote(repo, cfg, fake_api, monkeypatch):
    cfg.mirror = True
    fake_api.seed("users/alice/old.txt", b"x")
    fake_api.seed("users/alice/keep.txt", b"y")
    ctx = _ctx(repo, cfg)
    out = sync.Outcome(deletable=["old.txt", "keep.txt"])

    # Simulate the user choosing to delete only "old.txt" (monkeypatch auto-restores).
    import jp.tui as tui

    monkeypatch.setattr(tui, "interactive", lambda *a, **k: True)
    monkeypatch.setattr(tui, "confirm_deletions", lambda paths, where, _reader=None: ["old.txt"])
    _mirror.handle("remote", ctx, fake_api, out, yes=False, dry_run=False)

    assert "users/alice/old.txt" not in fake_api.files  # deleted
    assert "users/alice/keep.txt" in fake_api.files  # kept
    assert out.deleted == ["old.txt"]


def test_mirror_handle_noninteractive_without_yes_deletes_nothing(repo, cfg, fake_api, monkeypatch):
    cfg.mirror = True
    fake_api.seed("users/alice/old.txt", b"x")
    ctx = _ctx(repo, cfg)
    out = sync.Outcome(deletable=["old.txt"])

    import jp.tui as tui

    monkeypatch.setattr(tui, "interactive", lambda *a, **k: False)
    _mirror.handle("remote", ctx, fake_api, out, yes=False, dry_run=False)

    assert "users/alice/old.txt" in fake_api.files  # NOT deleted
    assert out.deleted == []


def test_mirror_handle_yes_deletes_all_remote(repo, cfg, fake_api):
    cfg.mirror = True
    fake_api.seed("users/alice/a.txt", b"x")
    fake_api.seed("users/alice/b.txt", b"y")
    ctx = _ctx(repo, cfg)
    out = sync.Outcome(deletable=["a.txt", "b.txt"])
    _mirror.handle("remote", ctx, fake_api, out, yes=True, dry_run=False)
    assert "users/alice/a.txt" not in fake_api.files
    assert "users/alice/b.txt" not in fake_api.files
    assert sorted(out.deleted) == ["a.txt", "b.txt"]


def test_mirror_handle_dry_run_deletes_nothing(repo, cfg, fake_api):
    cfg.mirror = True
    fake_api.seed("users/alice/a.txt", b"x")
    ctx = _ctx(repo, cfg)
    out = sync.Outcome(deletable=["a.txt"])
    _mirror.handle("remote", ctx, fake_api, out, yes=True, dry_run=True)
    assert "users/alice/a.txt" in fake_api.files  # dry-run never deletes
    assert out.deleted == []


def test_mirror_local_delete_after_confirm(repo, cfg, fake_api, monkeypatch):
    cfg.mirror = True
    conftest.write_file(repo, "scratch.txt", b"junk")
    # record it in the index so removal also drops the index entry
    idx = Index.load(repo)
    idx.set("scratch.txt", Entry(sha256=sync.sha256_bytes(b"junk"), size=4))
    idx.save()
    ctx = _ctx(repo, cfg)
    out = sync.Outcome(deletable=["scratch.txt"])

    import jp.tui as tui

    monkeypatch.setattr(tui, "interactive", lambda *a, **k: True)
    monkeypatch.setattr(
        tui, "confirm_deletions", lambda paths, where, _reader=None: ["scratch.txt"]
    )
    _mirror.handle("local", ctx, fake_api, out, yes=False, dry_run=False)

    assert not (repo / "scratch.txt").exists()  # local file removed
    assert out.deleted == ["scratch.txt"]
    assert "scratch.txt" not in Index.load(repo)  # index entry dropped


# =========================================================================== #
# Task 8 helpers
# =========================================================================== #
def _commit(repo, cfg, files: dict[str, bytes], message: str = "c") -> str:
    """Write files into the working tree and commit them; return the commit sha."""
    from jp.versioning import repo as vrepo

    for rel, data in files.items():
        conftest.write_file(repo, rel, data)
    return vrepo.create_commit(
        repo, cfg, message=message, stage_all=True, allow_empty=False, dry_run=False
    )["sha"]


def _obj_puts(fake_api) -> list[str]:
    """Every PUT path under __jp/objects, in call order."""
    return [p for (m, p) in fake_api.calls if m == "PUT" and "/__jp/objects/" in p]


def _ref_puts(fake_api) -> list[str]:
    """Every PUT path under __jp/refs, in call order."""
    return [p for (m, p) in fake_api.calls if m == "PUT" and "/__jp/refs/" in p]


# =========================================================================== #
# CRITICAL: scan_remote / pull / deletable EXCLUDE __jp and jp-tmp
# =========================================================================== #
def test_scan_remote_excludes_meta_dirs(cfg, fake_api):
    """The single most important test: jp's own meta dirs are NEVER user content.

    A fake remote carrying a mirror (``__jp/objects/...`` + ``__jp/refs/heads/main``)
    and a ``jp-tmp`` temp dir must yield ZERO such entries from scan_remote, so:
      (a) pull never downloads the object store into the working tree, and
      (b) a mirror-mode push never lists them as deletion candidates.
    A real user file is still returned, AND a deep ``__jp`` nested inside a user
    dir is NOT excluded (only the top-level meta dir directly under the prefix).
    """
    fake_api.seed("users/alice/__jp/objects/ab/cdef0123", b"obj")
    fake_api.seed("users/alice/__jp/refs/heads/main", b"a" * 64 + b"\n")
    fake_api.seed("users/alice/jp-tmp/scratch", b"tmp")
    fake_api.seed("users/alice/real.txt", b"hello")
    # A user directory that merely CONTAINS a child named __jp deeper down: kept.
    fake_api.seed("users/alice/proj/__jp/notes.txt", b"mine")

    remote = sync.scan_remote(fake_api, cfg)

    assert all("__jp" not in k.split("/")[:1] for k in remote)  # no top-level __jp
    assert not any(k == "jp-tmp" or k.startswith("jp-tmp/") for k in remote)
    assert "real.txt" in remote
    # The deep __jp under a user dir survives (it is NOT a top-level meta dir).
    assert "proj/__jp/notes.txt" in remote
    # And no mirror artifact leaked through.
    assert not any(k.startswith("__jp/") for k in remote)


def test_pull_never_downloads_mirror(repo, cfg, fake_api, index, ignore):
    """A pull against a remote holding a mirror downloads zero mirror files."""
    fake_api.seed("users/alice/__jp/objects/ab/cdef0123", b"obj")
    fake_api.seed("users/alice/__jp/refs/heads/main", b"a" * 64 + b"\n")
    fake_api.seed("users/alice/real.txt", b"hello")

    out = sync.pull(repo, cfg, fake_api, index, ignore)
    assert "real.txt" in out.transferred
    assert not any("__jp" in t for t in out.transferred)
    assert not (repo / "__jp").exists()  # never materialized locally


def test_mirror_dir_never_listed_as_deletable(repo, cfg, fake_api, index, ignore):
    """A mirror-mode push never offers to delete the user's history."""
    fake_api.seed("users/alice/__jp/objects/ab/cdef0123", b"obj")
    fake_api.seed("users/alice/__jp/refs/heads/main", b"a" * 64 + b"\n")
    fake_api.seed("users/alice/jp-tmp/scratch", b"tmp")
    # No local files -> without the exclusion, every remote file would be deletable.
    out = sync.push(repo, cfg, fake_api, index, ignore)
    assert out.deletable == []
    assert fake_api.deletes == []


def test_scan_remote_excludes_meta_name_reported_as_file(cfg, fake_api):
    """FIX 3: a remote *file* named exactly __jp/jp-tmp at the prefix root is excluded.

    The server could report a meta name as a plain file rather than a directory;
    the exclusion runs BEFORE the type branch, so pull never materializes it.
    """
    fake_api.seed("users/alice/__jp", b"a stray file literally named __jp")
    fake_api.seed("users/alice/jp-tmp", b"a stray file literally named jp-tmp")
    fake_api.seed("users/alice/real.txt", b"hello")

    remote = sync.scan_remote(fake_api, cfg)
    assert "real.txt" in remote
    assert "__jp" not in remote
    assert "jp-tmp" not in remote


def test_scan_local_reserves_top_level_meta_dirs(repo, ignore):
    """FIX 1: a LOCAL top-level __jp/ or jp-tmp/ is reserved and never scanned.

    A deep ``proj/__jp/x`` maps to a distinct remote path and IS still scanned.
    """
    conftest.write_file(repo, "__jp/objects/ab/" + "c" * 62, b"obj")
    conftest.write_file(repo, "__jp/refs/heads/main", b"a" * 64 + b"\n")
    conftest.write_file(repo, "jp-tmp/scratch", b"tmp")
    conftest.write_file(repo, "real.txt", b"hello")
    conftest.write_file(repo, "proj/__jp/x", b"mine")

    local = sync.scan_local(repo, ignore)
    assert "real.txt" in local
    assert "proj/__jp/x" in local  # deep meta name is NOT reserved
    assert not any(k == "__jp" or k.startswith("__jp/") for k in local)
    assert not any(k == "jp-tmp" or k.startswith("jp-tmp/") for k in local)


def test_local_meta_dir_never_uploaded_over_remote_mirror(repo, cfg, fake_api, index, ignore):
    """FIX 1 (the BLOCKER): a normal push NEVER uploads a local __jp/ over the mirror.

    A pre-seeded remote mirror object must survive untouched -- a local top-level
    __jp/ is reserved, so push uploads only the real user file and issues ZERO
    PUTs under <prefix>/__jp/, so the content-addressed store is never clobbered.
    A deep proj/__jp/x is a normal user file and IS uploaded.
    """
    # A pre-existing remote mirror object whose bytes must NOT be overwritten.
    fake_api.seed("users/alice/__jp/objects/ab/cdef0123", b"sacred mirror bytes")

    conftest.write_file(repo, "__jp/objects/ab/" + "c" * 62, b"would corrupt the mirror")
    conftest.write_file(repo, "__jp/refs/heads/main", b"a" * 64 + b"\n")
    conftest.write_file(repo, "jp-tmp/scratch", b"local tmp junk")
    conftest.write_file(repo, "real.txt", b"hello")
    conftest.write_file(repo, "proj/__jp/x", b"a normal deep file")

    out = sync.push(repo, cfg, fake_api, index, ignore)

    # The user file (and the deep meta-named file) uploaded; NOTHING under __jp/.
    puts = {p for (m, p) in fake_api.calls if m == "PUT"}
    assert "users/alice/real.txt" in puts
    assert "users/alice/proj/__jp/x" in puts
    assert not any(p.startswith("users/alice/__jp/") for p in puts)
    assert not any(p.startswith("users/alice/jp-tmp/") for p in puts)
    # The pre-seeded mirror object is byte-for-byte intact (never clobbered).
    assert fake_api.files["users/alice/__jp/objects/ab/cdef0123"] == b"sacred mirror bytes"
    assert "real.txt" in out.transferred
    assert "proj/__jp/x" in out.transferred


# =========================================================================== #
# mirror_history: layout, REF LAST, incremental, write-once
# =========================================================================== #
def test_mirror_pushes_objects_then_ref_last(repo, cfg, fake_api):
    _commit(repo, cfg, {"a.txt": b"alpha", "b.txt": b"beta"})
    result = mirror_mod.mirror_history(repo, cfg, fake_api)

    assert result.ran is True
    assert result.complete is True
    assert result.pushed > 0
    # Objects went to __jp/objects/<2>/<62>; the ref to __jp/refs/heads/main.
    for p in _obj_puts(fake_api):
        rel = p[len("users/alice/__jp/objects/") :]
        two, rest = rel.split("/", 1)
        assert len(two) == 2 and len(rest) == 62
    assert "users/alice/__jp/refs/heads/main" in _ref_puts(fake_api)

    # REF LAST: every object PUT precedes the ref PUT.
    put_paths = [p for (m, p) in fake_api.calls if m == "PUT"]
    ref_idx = put_paths.index("users/alice/__jp/refs/heads/main")
    obj_idxs = [i for i, p in enumerate(put_paths) if "/__jp/objects/" in p]
    assert all(i < ref_idx for i in obj_idxs)

    # The ref body is the tip sha + newline.
    tip = refs.resolve_head(repo)
    assert fake_api.files["users/alice/__jp/refs/heads/main"] == (tip + "\n").encode("ascii")


def test_mirror_object_bytes_are_exact_on_disk_file(repo, cfg, fake_api):
    """The remote object stores the EXACT local object-file bytes (marker+body)."""
    from jp.versioning.objects import ObjectStore

    _commit(repo, cfg, {"a.txt": b"alpha"})
    mirror_mod.mirror_history(repo, cfg, fake_api)
    store = ObjectStore(repo)
    for p in _obj_puts(fake_api):
        rel = p[len("users/alice/__jp/objects/") :]
        sha = rel.replace("/", "")
        assert fake_api.files[p] == store.path_for(sha).read_bytes()


def test_mirror_incremental_second_run_pushes_nothing(repo, cfg, fake_api):
    _commit(repo, cfg, {"a.txt": b"alpha"})
    first = mirror_mod.mirror_history(repo, cfg, fake_api)
    assert first.pushed > 0

    # Second mirror with NO new commits: the remote tip already equals ours, so
    # ZERO objects are pushed and the ref is not re-PUT (already up to date).
    fake_api.calls.clear()
    second = mirror_mod.mirror_history(repo, cfg, fake_api)
    assert second.pushed == 0
    assert _obj_puts(fake_api) == []
    assert _ref_puts(fake_api) == []  # tip unchanged -> no ref churn
    assert second.complete is True


def test_mirror_incremental_only_new_objects_after_commit(repo, cfg, fake_api):
    _commit(repo, cfg, {"a.txt": b"alpha"})
    mirror_mod.mirror_history(repo, cfg, fake_api)
    # A second commit adds one file -> only the NEW objects (blob+tree+commit) push.
    fake_api.calls.clear()
    _commit(repo, cfg, {"b.txt": b"beta"}, message="c2")
    result = mirror_mod.mirror_history(repo, cfg, fake_api)
    assert result.pushed > 0
    # None of the previously-mirrored objects are PUT again as NEW (created).
    # (We assert the delta is small: 3 objects -- new blob, new tree, new commit.)
    assert result.pushed == 3
    assert "users/alice/__jp/refs/heads/main" in _ref_puts(fake_api)


def test_mirror_write_once_already_present_is_skipped(repo, cfg, fake_api):
    """An object already on the remote (created=False) counts skipped, not new."""
    from jp.versioning.fsck import reachable_objects
    from jp.versioning.objects import ObjectStore

    _commit(repo, cfg, {"a.txt": b"alpha"})
    store = ObjectStore(repo)
    # Pre-seed ALL reachable objects on the remote so every PUT is an overwrite.
    for sha in reachable_objects(repo, store, include_staged=False):
        rel = f"users/alice/__jp/objects/{sha[:2]}/{sha[2:]}"
        fake_api.seed(rel, store.path_for(sha).read_bytes())

    result = mirror_mod.mirror_history(repo, cfg, fake_api)
    assert result.pushed == 0
    assert result.skipped > 0
    assert result.complete is True  # ref still advances


# =========================================================================== #
# FIX 4: an oversized object in the store is hard-skipped (never PUT)
# =========================================================================== #
def test_mirror_oversized_object_is_skipped_and_withholds_ref(repo, cfg, fake_api, monkeypatch):
    """An object whose on-disk file exceeds the cap is NOT PUT (a failure).

    A blob committed before the stage guard existed (or via another path) could be
    huge; the mirror enforces the cap a SECOND time so it never reads a giant body
    into memory and hangs near the timeout. The over-cap object is recorded as a
    failure -> the branch is incomplete and the ref is withheld.
    """
    _commit(repo, cfg, {"a.txt": b"alpha"})
    # Force a cap below the (tiny) object files so the guard fires deterministically.
    monkeypatch.setattr(mirror_mod, "_max_blob_bytes", lambda cfg: 2)

    result = mirror_mod.mirror_history(repo, cfg, fake_api)

    assert result.failed > 0
    assert result.pushed == 0  # nothing under the cap got through either (all > 2 B)
    assert _ref_puts(fake_api) == []  # ref withheld -- not yet safe
    assert result.complete is False
    assert any("max_blob_mb" in w and "not mirrored" in w for w in result.warnings)
    # No oversized body was ever PUT under __jp/objects.
    assert _obj_puts(fake_api) == []


def test_mirror_under_cap_object_still_pushes(repo, cfg, fake_api, monkeypatch):
    """A cap comfortably above the object files lets them through normally."""
    _commit(repo, cfg, {"a.txt": b"alpha"})
    monkeypatch.setattr(mirror_mod, "_max_blob_bytes", lambda cfg: 100 * 1024 * 1024)
    result = mirror_mod.mirror_history(repo, cfg, fake_api)
    assert result.pushed > 0
    assert result.complete is True


# =========================================================================== #
# FIX 5: the per-PUT timeout bump is restored afterwards (no session leak)
# =========================================================================== #
def test_mirror_timeout_restored_after_large_put(repo, cfg, fake_api):
    """A large-object PUT may bump api.timeout, but it MUST be restored after."""
    # A >1 MiB blob triggers the large-object path (_put_with_timeout). The
    # stage/mirror caps default to 100 MiB so a ~2 MiB blob passes the guard.
    big = b"x" * (2 * 1024 * 1024)
    _commit(repo, cfg, {"big.bin": big})
    fake_api.timeout = 30.0
    before = fake_api.timeout

    result = mirror_mod.mirror_history(repo, cfg, fake_api)

    assert result.pushed > 0
    # The inflated per-call timeout did NOT leak into the session.
    assert fake_api.timeout == before


# =========================================================================== #
# CAS: a concurrently-advanced remote ref is NOT clobbered
# =========================================================================== #
def test_mirror_cas_aborts_ref_update_when_remote_advanced(repo, cfg, fake_api, monkeypatch):
    _commit(repo, cfg, {"a.txt": b"alpha"})

    other_tip = "f" * 64
    state = {"first": True}
    real_read = mirror_mod._read_remote_ref

    def flaky_read(api, prefix, branch):
        # First read (baseline) -> absent; second read (the CAS re-check) -> a
        # DIFFERENT sha, simulating another machine advancing the remote ref.
        if state["first"]:
            state["first"] = False
            return None
        return other_tip

    monkeypatch.setattr(mirror_mod, "_read_remote_ref", flaky_read)
    result = mirror_mod.mirror_history(repo, cfg, fake_api)
    monkeypatch.setattr(mirror_mod, "_read_remote_ref", real_read)

    # Objects were uploaded, but the ref was NOT moved (CAS conflict recorded).
    assert result.pushed > 0
    assert _ref_puts(fake_api) == []  # ref never PUT
    assert result.complete is False
    assert any("advanced" in w for w in result.warnings)
    br = result.branches[0]
    assert br.ref_advanced is False


def test_mirror_unresolvable_remote_never_overwrites_ref(repo, cfg, fake_api, monkeypatch):
    """FIX 2: a remote ref pointing at history we lack is NEVER overwritten.

    The remote sha is not in our store, so our tip is NOT a provable descendant.
    Advancing would be a non-fast-forward overwrite of unknown history, so the
    objects may be pushed but the ref is WITHHELD entirely (zero ref PUTs) and the
    branch is reported incomplete -- the user must run jp fetch first (Task 9).
    """
    _commit(repo, cfg, {"a.txt": b"alpha"})
    unknown = "c" * 64  # not in our store

    monkeypatch.setattr(mirror_mod, "_read_remote_ref", lambda api, prefix, br: unknown)
    result = mirror_mod.mirror_history(repo, cfg, fake_api)

    assert result.pushed > 0  # objects still uploaded (harmless, resumable)
    assert _ref_puts(fake_api) == []  # but the ref is NEVER PUT
    assert result.complete is False
    br = result.branches[0]
    assert br.ref_advanced is False
    assert "refusing to overwrite" in br.ref_skipped_reason
    assert any("doesn't have" in w for w in result.warnings)


# =========================================================================== #
# HEX GUARD + path jail
# =========================================================================== #
def test_hex_guard_rejects_bogus_sha():
    from jp.errors import JpError

    for bad in ("", "xyz", "AB" * 32, "../etc", "g" * 64, "a" * 63, "a" * 65):
        with pytest.raises(JpError):
            mirror_mod._validate_object_sha(bad)
    # A valid sha composes a clean prefix-relative path.
    good = "a" * 64
    assert mirror_mod._object_remote_rel(good) == f"__jp/objects/aa/{'a' * 62}"


def test_mirror_object_path_is_within_prefix(repo, cfg, fake_api):
    """Every mirror write stays under the prefix (assert_within_prefix honored)."""
    _commit(repo, cfg, {"a.txt": b"alpha"})
    mirror_mod.mirror_history(repo, cfg, fake_api)
    for _m, p in fake_api.calls:
        if "/__jp/" in p:
            assert p.startswith("users/alice/__jp/")


def test_mirror_assert_within_prefix_refuses_escape(cfg, monkeypatch):
    """A composed path that would escape the prefix is refused before any PUT."""
    from jp import paths
    from jp.errors import SafetyError

    # __jp under a DIFFERENT prefix must not validate against users/alice.
    escape = paths.remote_path_for("users/mallory", "__jp/refs/heads/main")
    with pytest.raises(SafetyError):
        paths.assert_within_prefix(escape, "users/alice")


# =========================================================================== #
# mirror is best-effort: a PUT failure never raises out
# =========================================================================== #
def test_mirror_put_failure_is_recorded_not_raised(repo, cfg, fake_api):
    _commit(repo, cfg, {"a.txt": b"alpha"})

    from jp.errors import ApiError

    orig_put = fake_api.put_file

    def boom(api_path, data):
        if "/__jp/objects/" in api_path:
            raise ApiError("boom", status=500)
        return orig_put(api_path, data)

    fake_api.put_file = boom  # type: ignore[assignment]
    result = mirror_mod.mirror_history(repo, cfg, fake_api)  # must NOT raise

    assert result.failed > 0
    assert result.complete is False
    assert _ref_puts(fake_api) == []  # ref NOT advanced when objects failed
    assert result.branches[0].ref_advanced is False


def test_mirror_no_history_is_noop(repo, cfg, fake_api):
    """A repo with no committed history mirrors nothing and reports not-run."""
    result = mirror_mod.mirror_history(repo, cfg, fake_api)
    assert result.ran is False
    assert result.pushed == 0
    assert fake_api.calls == []


# =========================================================================== #
# SIZE GUARD (repo.stage_paths): oversized files are not versioned
# =========================================================================== #
def test_size_guard_skips_oversized_file(repo, fake_api):
    from jp.config import Config
    from jp.ignore import IgnoreSet
    from jp.versioning import repo as vrepo
    from jp.versioning.staging import Staging

    cfg = Config(base_url="https://hub.example/api", prefix="users/alice", versioning_max_blob_mb=1)
    conftest.write_file(repo, "small.txt", b"tiny")
    conftest.write_file(repo, "big.bin", b"x" * (2 * 1024 * 1024))  # 2 MiB > 1 MiB cap

    summary = vrepo.stage_paths(
        repo, cfg, IgnoreSet.from_root(repo), [], all_files=True, dry_run=False
    )
    assert "small.txt" in summary["staged"]
    assert "big.bin" not in summary["staged"]
    # The oversized file is absent from the staging tree entirely.
    staging = Staging.load(repo)
    assert "small.txt" in staging.entries
    assert "big.bin" not in staging.entries


def test_size_guard_named_path_errors(repo):
    from jp.config import Config
    from jp.ignore import IgnoreSet
    from jp.versioning import repo as vrepo

    cfg = Config(base_url="https://hub.example/api", prefix="users/alice", versioning_max_blob_mb=1)
    conftest.write_file(repo, "big.bin", b"x" * (2 * 1024 * 1024))
    summary = vrepo.stage_paths(
        repo, cfg, IgnoreSet.from_root(repo), ["big.bin"], all_files=False, dry_run=False
    )
    assert summary["staged"] == []
    assert summary["errors"]  # named oversized path -> per-path error (non-zero exit)


def test_size_guard_under_limit_stages_normally(repo):
    from jp.config import Config
    from jp.ignore import IgnoreSet
    from jp.versioning import repo as vrepo

    cfg = Config(base_url="https://hub.example/api", prefix="users/alice", versioning_max_blob_mb=1)
    conftest.write_file(repo, "ok.txt", b"y" * 1024)  # well under 1 MiB
    summary = vrepo.stage_paths(
        repo, cfg, IgnoreSet.from_root(repo), [], all_files=True, dry_run=False
    )
    assert "ok.txt" in summary["staged"]


# =========================================================================== #
# push.py wiring: the ask-once prompt + flag + config persistence
# =========================================================================== #
def _push_args(**kw) -> argparse.Namespace:
    base = {
        "path": [],
        "dry_run": False,
        "mirror": None,
        "yes": False,
        "raw": False,
        "backup_history": False,
    }
    base.update(kw)
    return argparse.Namespace(**base)


def _wire_push(monkeypatch, repo, cfg, fake_api):
    import jp.commands.push as push_cmd

    ctx = _ctx(repo, cfg)
    monkeypatch.setattr(push_cmd, "load_repo", lambda: ctx)
    monkeypatch.setattr(push_cmd._context, "build_api", lambda c: fake_api)
    return push_cmd, ctx


def _set_tty(monkeypatch, value: bool):
    import sys

    monkeypatch.setattr(sys.stdin, "isatty", lambda: value)


def test_push_ask_once_always_persists_and_mirrors(repo, fake_api, monkeypatch):
    from jp.config import Config

    cfg = Config(base_url="https://hub.example/api", prefix="users/alice")  # mirror_history=ask
    _commit(repo, cfg, {"a.txt": b"alpha"})
    push_cmd, ctx = _wire_push(monkeypatch, repo, cfg, fake_api)
    _set_tty(monkeypatch, True)
    monkeypatch.setattr(push_cmd.ui, "ask_line", lambda p: "a")

    rc = push_cmd.run(_push_args())
    assert rc == 0
    # Persisted + mirrored now.
    assert config_mod_load(repo).versioning_mirror_history == "always"
    assert "users/alice/__jp/refs/heads/main" in _ref_puts(fake_api)


def test_push_ask_once_never_persists_and_skips(repo, fake_api, monkeypatch):
    from jp.config import Config

    cfg = Config(base_url="https://hub.example/api", prefix="users/alice")
    _commit(repo, cfg, {"a.txt": b"alpha"})
    push_cmd, ctx = _wire_push(monkeypatch, repo, cfg, fake_api)
    _set_tty(monkeypatch, True)
    monkeypatch.setattr(push_cmd.ui, "ask_line", lambda p: "n")

    rc = push_cmd.run(_push_args())
    assert rc == 0
    assert config_mod_load(repo).versioning_mirror_history == "never"
    assert _ref_puts(fake_api) == []  # NOT mirrored


def test_push_ask_once_once_mirrors_and_sets_never(repo, fake_api, monkeypatch):
    from jp.config import Config

    cfg = Config(base_url="https://hub.example/api", prefix="users/alice")
    _commit(repo, cfg, {"a.txt": b"alpha"})
    push_cmd, ctx = _wire_push(monkeypatch, repo, cfg, fake_api)
    _set_tty(monkeypatch, True)
    monkeypatch.setattr(push_cmd.ui, "ask_line", lambda p: "o")

    rc = push_cmd.run(_push_args())
    assert rc == 0
    # Mirrored once AND set never (so it won't nag again).
    assert "users/alice/__jp/refs/heads/main" in _ref_puts(fake_api)
    assert config_mod_load(repo).versioning_mirror_history == "never"


def test_push_non_tty_ask_never_prompts_nor_mirrors(repo, fake_api, monkeypatch):
    from jp.config import Config

    cfg = Config(base_url="https://hub.example/api", prefix="users/alice")
    _commit(repo, cfg, {"a.txt": b"alpha"})
    push_cmd, ctx = _wire_push(monkeypatch, repo, cfg, fake_api)
    _set_tty(monkeypatch, False)
    asked = {"v": False}
    monkeypatch.setattr(push_cmd.ui, "ask_line", lambda p: asked.__setitem__("v", True) or "")

    rc = push_cmd.run(_push_args())
    assert rc == 0
    assert asked["v"] is False  # never prompted
    assert _ref_puts(fake_api) == []  # never mirrored
    # Config unchanged (still ask).
    assert config_mod_load(repo).versioning_mirror_history == "ask"


def test_push_backup_history_flag_mirrors_without_changing_config(repo, fake_api, monkeypatch):
    from jp.config import Config

    cfg = Config(base_url="https://hub.example/api", prefix="users/alice")
    _commit(repo, cfg, {"a.txt": b"alpha"})
    push_cmd, ctx = _wire_push(monkeypatch, repo, cfg, fake_api)
    _set_tty(monkeypatch, False)  # even non-tty: the flag forces it

    rc = push_cmd.run(_push_args(backup_history=True))
    assert rc == 0
    assert "users/alice/__jp/refs/heads/main" in _ref_puts(fake_api)
    assert config_mod_load(repo).versioning_mirror_history == "ask"  # UNCHANGED


def test_push_always_mode_mirrors(repo, fake_api, monkeypatch):
    from jp.config import Config

    cfg = Config(
        base_url="https://hub.example/api",
        prefix="users/alice",
        versioning_mirror_history="always",
    )
    _commit(repo, cfg, {"a.txt": b"alpha"})
    push_cmd, ctx = _wire_push(monkeypatch, repo, cfg, fake_api)
    _set_tty(monkeypatch, False)

    rc = push_cmd.run(_push_args())
    assert rc == 0
    assert "users/alice/__jp/refs/heads/main" in _ref_puts(fake_api)


def test_push_never_mode_skips(repo, fake_api, monkeypatch):
    from jp.config import Config

    cfg = Config(
        base_url="https://hub.example/api",
        prefix="users/alice",
        versioning_mirror_history="never",
    )
    _commit(repo, cfg, {"a.txt": b"alpha"})
    push_cmd, ctx = _wire_push(monkeypatch, repo, cfg, fake_api)
    _set_tty(monkeypatch, True)

    rc = push_cmd.run(_push_args())
    assert rc == 0
    assert _ref_puts(fake_api) == []  # never mode -> no mirror


def test_push_dry_run_never_mirrors(repo, fake_api, monkeypatch):
    from jp.config import Config

    cfg = Config(
        base_url="https://hub.example/api",
        prefix="users/alice",
        versioning_mirror_history="always",
    )
    _commit(repo, cfg, {"a.txt": b"alpha"})
    push_cmd, ctx = _wire_push(monkeypatch, repo, cfg, fake_api)
    _set_tty(monkeypatch, True)

    rc = push_cmd.run(_push_args(dry_run=True))
    assert rc == 0
    assert _ref_puts(fake_api) == []  # dry-run never mirrors


def test_push_mirror_failure_does_not_change_exit_code(repo, fake_api, monkeypatch):
    """A mirror failure is a warning only -- push still returns its normal code."""
    from jp.config import Config
    from jp.errors import ApiError

    cfg = Config(
        base_url="https://hub.example/api",
        prefix="users/alice",
        versioning_mirror_history="always",
    )
    _commit(repo, cfg, {"a.txt": b"alpha"})
    push_cmd, ctx = _wire_push(monkeypatch, repo, cfg, fake_api)
    _set_tty(monkeypatch, True)

    orig_put = fake_api.put_file

    def boom(api_path, data):
        if "/__jp/objects/" in api_path:
            raise ApiError("boom", status=500)
        return orig_put(api_path, data)

    fake_api.put_file = boom  # type: ignore[assignment]

    rc = push_cmd.run(_push_args())
    assert rc == 0  # data push succeeded; mirror failure never changes the code
    assert _ref_puts(fake_api) == []  # ref NOT advanced


def config_mod_load(repo):
    from jp import config as config_mod

    return config_mod.load(repo)


# --------------------------------------------------------------------------- #
# FIX 3: _ref_remote_rel self-validates the branch name (defense-in-depth).
# --------------------------------------------------------------------------- #
def test_ref_remote_rel_composes_safe_branch():
    rel = mirror_mod._ref_remote_rel("main")
    assert rel == "__jp/refs/heads/main"


def test_ref_remote_rel_rejects_traversal_branch():
    from jp.versioning.objects import VersioningError

    # A crafted name must never be composed into a traversal-y remote ref path,
    # regardless of caller: _ref_remote_rel now re-validates it itself.
    for bad in ("../escape", "a/b", ".", "..", "-dash"):
        with pytest.raises(VersioningError):
            mirror_mod._ref_remote_rel(bad)
