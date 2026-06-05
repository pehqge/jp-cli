"""Task 9 -- ``jp fetch`` / ``jp restore`` / ``clone --history``.

This is the ONLY task that reads remote history bytes BACK, so the remote is
treated as POSSIBLY HOSTILE. The strongest test is a ROUND-TRIP: commit locally,
mirror to a fake remote (Task 8), wipe the local ``.jp`` objects+refs, then fetch
and assert every object is back, byte-verified, and log/show work again.

The security tests prove the verify-before-place gateway rejects a tampered
object, a decompression bomb, a hostile tree key, and never hangs on a forged
parents cycle. Reachability tests prove fetch issues O(reachable) GETs and never
enumerates the whole remote ``__jp/objects`` tree.
"""

from __future__ import annotations

import hashlib
import shutil
import zlib

import pytest

import conftest
from jp.errors import ApiError, NetworkError
from jp.versioning import fetch as fetch_mod
from jp.versioning import mirror as mirror_mod
from jp.versioning import refs
from jp.versioning.objects import ObjectStore, VersioningError


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _commit(repo, cfg, files: dict[str, bytes], message: str = "c") -> str:
    """Write files into the working tree and commit them; return the commit sha."""
    from jp.versioning import repo as vrepo

    for rel, data in files.items():
        conftest.write_file(repo, rel, data)
    return vrepo.create_commit(
        repo, cfg, message=message, stage_all=True, allow_empty=False, dry_run=False
    )["sha"]


def _mirror(repo, cfg, fake_api):
    """Mirror committed history to the fake remote (Task 8)."""
    return mirror_mod.mirror_history(repo, cfg, fake_api)


def _wipe_local_history(repo) -> None:
    """Delete the LOCAL object store + refs (simulate losing .jp/objects + refs).

    Leaves the working tree intact -- the common restore scenario. HEAD is also
    removed so the repo looks freshly-uninitialized to the versioning layer.
    """
    jp = repo / ".jp"
    shutil.rmtree(jp / "objects", ignore_errors=True)
    shutil.rmtree(jp / "refs", ignore_errors=True)
    (jp / "HEAD").unlink(missing_ok=True)
    (jp / "staged.json").unlink(missing_ok=True)


def _content_reads_under_objects(fake_api) -> list[str]:
    """Every get_file_bytes() that hit an __jp/objects path (a real download)."""
    return [p for p in fake_api.content_reads if "/__jp/objects/" in p]


# --------------------------------------------------------------------------- #
# ROUND-TRIP: the strongest test
# --------------------------------------------------------------------------- #
def test_fetch_round_trip_restores_every_object(repo, cfg, fake_api):
    """commit -> mirror -> wipe local -> fetch: every object is back + verified."""
    from jp.versioning import repo as vrepo

    _commit(repo, cfg, {"a.txt": b"alpha", "b.txt": b"beta"})
    _commit(repo, cfg, {"a.txt": b"alpha2"}, message="c2")
    tip = refs.resolve_head(repo)
    mr = _mirror(repo, cfg, fake_api)
    assert mr.complete

    # Capture the full reachable set BEFORE wiping, to compare after.
    from jp.versioning.fsck import reachable_objects

    store = ObjectStore(repo)
    expected = reachable_objects(repo, store, include_staged=False)
    assert expected

    _wipe_local_history(repo)
    store = ObjectStore(repo)
    assert not any(store.has(s) for s in expected)  # truly gone

    result = fetch_mod.fetch_history(repo, cfg, fake_api)
    assert result.failed == 0
    assert result.downloaded > 0

    # Every reachable object is back and re-hashes correctly (store.read verifies).
    store = ObjectStore(repo)
    for sha in expected:
        assert store.has(sha)
        store.read(sha)  # raises if it does not re-hash to sha

    # The local ref was set to the remote tip (it was absent -> first fetch).
    assert refs.read_ref(repo, "main") == tip
    assert refs.resolve_head(repo) == tip

    # log/show work again: iter_history walks the full chain.
    seen = [sha for sha, _ in vrepo.iter_history(store, tip)]
    assert len(seen) == 2  # two commits


def test_fetch_is_idempotent_second_run_downloads_nothing(repo, cfg, fake_api):
    _commit(repo, cfg, {"a.txt": b"alpha"})
    _mirror(repo, cfg, fake_api)
    _wipe_local_history(repo)

    first = fetch_mod.fetch_history(repo, cfg, fake_api)
    assert first.downloaded > 0
    # Second fetch: everything is already local -> all skipped, zero downloads.
    second = fetch_mod.fetch_history(repo, cfg, fake_api)
    assert second.downloaded == 0
    assert second.skipped > 0
    assert second.failed == 0


# --------------------------------------------------------------------------- #
# REACHABILITY: O(reachable) GETs, never a full __jp enumeration
# --------------------------------------------------------------------------- #
def test_fetch_only_downloads_reachable_objects(repo, cfg, fake_api):
    """Fetch GETs exactly the reachable objects -- and never lists __jp/objects."""
    from jp.versioning.fsck import reachable_objects

    _commit(repo, cfg, {"a.txt": b"alpha"})
    store = ObjectStore(repo)
    reachable = reachable_objects(repo, store, include_staged=False)
    _mirror(repo, cfg, fake_api)

    # Seed a DECOY object on the remote that is NOT reachable from the ref. If
    # fetch walked the whole __jp/objects listing it would try to download it.
    decoy_sha = "d" * 64
    fake_api.seed(f"users/alice/__jp/objects/{decoy_sha[:2]}/{decoy_sha[2:]}", b"\x00decoy")

    _wipe_local_history(repo)
    fake_api.content_reads.clear()
    list_calls_before = list(fake_api.calls)

    result = fetch_mod.fetch_history(repo, cfg, fake_api)
    assert result.failed == 0

    downloaded = _content_reads_under_objects(fake_api)
    # Exactly the reachable objects were downloaded -- the decoy was never touched.
    fetched_shas = {p.split("/__jp/objects/")[1].replace("/", "") for p in downloaded}
    assert fetched_shas == reachable
    assert decoy_sha not in fetched_shas
    # Each reachable object was downloaded exactly once (no redundant GETs).
    assert len(downloaded) == len(reachable)

    # No directory LISTING of __jp was performed (reachability-driven, not a walk).
    # The FakeApi records list_dir only via scan_remote-style calls; fetch never
    # calls list_dir, so its calls log has no entries touching __jp listings.
    new_calls = [c for c in fake_api.calls if c not in list_calls_before]
    assert not any("__jp" in p and m in ("LIST", "MKDIR") for (m, p) in new_calls)


# --------------------------------------------------------------------------- #
# SECURITY: tampered object, bomb, hostile tree key, parents cycle
# --------------------------------------------------------------------------- #
def test_fetch_rejects_tampered_object(repo, cfg, fake_api):
    """Flipping a byte in a served object -> fetch aborts, places NOTHING corrupt."""
    _commit(repo, cfg, {"a.txt": b"x" * 4096})  # compressible blob
    store = ObjectStore(repo)
    from jp.versioning import repo as vrepo

    # Tamper the BLOB object on the remote (its tree entry will fail to re-hash).
    commit = vrepo.read_commit(store, refs.resolve_head(repo))
    tree = vrepo.read_tree(store, commit["tree"])
    blob_sha = next(iter(tree.values()))["sha256"]
    _mirror(repo, cfg, fake_api)
    remote_blob = f"users/alice/__jp/objects/{blob_sha[:2]}/{blob_sha[2:]}"
    tampered = bytearray(fake_api.files[remote_blob])
    tampered[-1] ^= 0xFF
    fake_api.files[remote_blob] = bytes(tampered)

    _wipe_local_history(repo)
    with pytest.raises(VersioningError):
        fetch_mod.fetch_history(repo, cfg, fake_api)

    # The tampered blob was never placed locally; the ref was never advanced.
    store = ObjectStore(repo)
    assert not store.has(blob_sha)
    assert refs.read_ref(repo, "main") is None


def test_fetch_rejects_tampered_ref_tip(repo, cfg, fake_api):
    """A remote ref pointing at a sha that does not exist on the remote -> aborts.

    The ref names a commit; if the remote does not actually serve that object, the
    GET 404s -> a NetworkError abort. Nothing is placed and the ref is not moved.
    """
    _commit(repo, cfg, {"a.txt": b"alpha"})
    _mirror(repo, cfg, fake_api)
    # Point the remote ref at a bogus (absent) commit sha.
    fake_api.files["users/alice/__jp/refs/heads/main"] = (("e" * 64) + "\n").encode("ascii")

    _wipe_local_history(repo)
    with pytest.raises((VersioningError, NetworkError, ApiError)):
        fetch_mod.fetch_history(repo, cfg, fake_api)
    assert refs.read_ref(repo, "main") is None


def test_fetch_rejects_decompression_bomb(repo, cfg, fake_api):
    """A remote object whose zlib body inflates beyond its size cap is refused.

    We replace a real blob's served bytes with a "bomb" body that claims a small
    size in its tree entry but decompresses to far more -- import_object's
    max_size cap (the tree-entry size) refuses it before any OOM, and fetch aborts.
    """
    payload_small = b"s" * 64  # the committed blob is tiny -> tree size is small
    _commit(repo, cfg, {"a.txt": payload_small})
    store = ObjectStore(repo)
    from jp.versioning import repo as vrepo

    commit = vrepo.read_commit(store, refs.resolve_head(repo))
    tree = vrepo.read_tree(store, commit["tree"])
    blob_sha = next(iter(tree.values()))["sha256"]
    _mirror(repo, cfg, fake_api)

    # Serve a bomb in place of the real blob bytes (50 MiB of zeros, tiny on wire).
    bomb_plain = b"\x00" * (50 * 1024 * 1024)
    remote_blob = f"users/alice/__jp/objects/{blob_sha[:2]}/{blob_sha[2:]}"
    fake_api.files[remote_blob] = b"\x01" + zlib.compress(bomb_plain)
    assert len(fake_api.files[remote_blob]) < 100 * 1024  # the served file stays tiny

    _wipe_local_history(repo)
    with pytest.raises(VersioningError):
        fetch_mod.fetch_history(repo, cfg, fake_api)
    assert not ObjectStore(repo).has(blob_sha)


def test_fetch_hostile_tree_size_cannot_lift_bomb_ceiling(repo, cfg, fake_api, monkeypatch):
    """FIX 1: a tree declaring a HUGE size cannot raise the per-blob bomb ceiling.

    We forge a self-consistent commit -> tree -> blob where the tree entry declares
    ``size`` far above the module cap and the blob is a zlib bomb. The tree's own
    bytes hash honestly, so it is placed -- but the declared size is attacker chosen.
    With the module cap patched tiny (CI-safe), import_object must CLAMP the per-blob
    cap to the module ceiling and REJECT the bomb. Nothing corrupt is placed.
    """
    import json

    import jp.versioning.objects as objmod

    _commit(repo, cfg, {"a.txt": b"alpha"})  # ensures the store + repo exist
    store = ObjectStore(repo)

    # Patch the module ceiling tiny so the clamp is observable without GB allocs.
    monkeypatch.setattr(objmod, "_MAX_DECOMPRESSED_BYTES", 1024)

    # A bomb blob: ~1 MiB of zeros, tiny on the wire.
    bomb_plain = b"\x00" * (1024 * 1024)
    blob_sha = hashlib.sha256(bomb_plain).hexdigest()
    bomb_payload = b"\x01" + zlib.compress(bomb_plain)

    # A tree that declares a 10 GiB size for that blob (attacker-authored, self-
    # consistent: the tree's BYTES hash fine; the NUMBER is a lie).
    huge_size = 10 * 1024 * 1024 * 1024  # 10 GiB -- the attacker-chosen lie
    hostile_tree = json.dumps(
        {
            "version": 1,
            "entries": {"big.bin": {"sha256": blob_sha, "size": huge_size, "mode": "file"}},
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    tree_sha = store.write(hostile_tree)
    commit_obj = json.dumps(
        {
            "version": 1,
            "tree": tree_sha,
            "parents": [],
            "message": "evil",
            "author": "m",
            "time": "t",
            "epoch": 0,
            "jp": "x",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    commit_sha = store.write(commit_obj)

    refs.write_ref(repo, "main", commit_sha)
    _mirror(repo, cfg, fake_api)
    # Serve the bomb body in place of the (placeholder) blob the mirror pushed.
    blob_path = f"users/alice/__jp/objects/{blob_sha[:2]}/{blob_sha[2:]}"
    fake_api.files[blob_path] = bomb_payload
    fake_api.files["users/alice/__jp/refs/heads/main"] = (commit_sha + "\n").encode("ascii")

    _wipe_local_history(repo)
    with pytest.raises(VersioningError):
        fetch_mod.fetch_history(repo, cfg, fake_api)
    assert not ObjectStore(repo).has(blob_sha)  # the bomb was never placed


def test_fetch_oversized_meta_object_is_refused(repo, cfg, fake_api, monkeypatch):
    """FIX 2: a COMMIT/TREE object beyond the meta cap is refused (bounded alloc).

    Commits/trees are imported with max_size=_FETCH_META_MAX. We patch that constant
    tiny and serve a commit object larger than it (an honest but oversized blob of
    bytes that hashes to its own name) -> import_object's cap rejects it. This proves
    meta-object allocation is bounded, with no large allocation in CI.
    """
    _commit(repo, cfg, {"a.txt": b"alpha"})
    store = ObjectStore(repo)

    # Patch the fetch meta cap absurdly small so a normal-ish object exceeds it.
    monkeypatch.setattr(fetch_mod, "_FETCH_META_MAX", 16)

    # A "commit" object whose RAW content is > 16 bytes: it hashes to its own sha
    # (honest bytes), but exceeds the patched meta cap, so import refuses it.
    big_meta = b"z" * 4096
    meta_sha = hashlib.sha256(big_meta).hexdigest()
    store_other = ObjectStore(repo)
    store_other.write(big_meta)  # place it locally so we can grab its on-disk bytes
    payload = store_other.path_for(meta_sha).read_bytes()

    refs.write_ref(repo, "main", meta_sha)  # point the ref at the oversized "commit"
    _mirror(repo, cfg, fake_api)
    fake_api.files[f"users/alice/__jp/objects/{meta_sha[:2]}/{meta_sha[2:]}"] = payload
    fake_api.files["users/alice/__jp/refs/heads/main"] = (meta_sha + "\n").encode("ascii")

    _wipe_local_history(repo)
    with pytest.raises(VersioningError):
        fetch_mod.fetch_history(repo, cfg, fake_api)
    assert not ObjectStore(repo).has(meta_sha)  # the oversized meta object was refused
    assert store  # silence lint


def test_fetch_rejects_hostile_tree_key(repo, cfg, fake_api):
    """A served tree object carrying a ``"../x"`` key is rejected by read_tree.

    We forge a malicious tree object (valid JSON, traversal-y key), store it so its
    bytes hash to a name, and point a forged commit at it. fetch downloads + places
    the commit/tree (they re-hash fine -- the BYTES are honest) but read_tree
    rejects the unsafe key, so the fetch aborts before any blob is trusted.
    """
    import json

    # A real commit gives us a baseline; we then forge a hostile tree + commit.
    _commit(repo, cfg, {"a.txt": b"alpha"})
    store = ObjectStore(repo)

    hostile_tree = json.dumps(
        {
            "version": 1,
            "entries": {"../escape.txt": {"sha256": "a" * 64, "size": 1, "mode": "file"}},
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    tree_sha = store.write(hostile_tree)  # bytes are honest; the KEY is hostile
    commit_obj = json.dumps(
        {
            "version": 1,
            "tree": tree_sha,
            "parents": [],
            "message": "evil",
            "author": "m",
            "time": "2024-01-01T00:00:00+00:00",
            "epoch": 0,
            "jp": "x",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    commit_sha = store.write(commit_obj)

    # Mirror everything (including the forged objects), then point the ref at the
    # hostile commit and wipe local history.
    refs.write_ref(repo, "main", commit_sha)
    _mirror(repo, cfg, fake_api)
    fake_api.files["users/alice/__jp/refs/heads/main"] = (commit_sha + "\n").encode("ascii")

    _wipe_local_history(repo)
    with pytest.raises(VersioningError):
        fetch_mod.fetch_history(repo, cfg, fake_api)
    # The escape path was never written anywhere outside the repo.
    assert not (repo.parent / "escape.txt").exists()


def test_fetch_repeated_parent_fetched_once(repo, cfg, fake_api):
    """A merge-shaped commit listing the SAME parent twice fetches it once (dedup).

    A genuine sha-based parents CYCLE is impossible to forge (a commit's sha depends
    on its parents), so the realistic shape the visited guard defends against is a
    parent referenced multiple times -- which must be downloaded exactly once and
    never re-walked. (A truly injected cycle is exercised in
    ``test_fetch_walk_visited_guard_stops_injected_cycle`` below.)
    """
    import json

    _commit(repo, cfg, {"a.txt": b"alpha"})
    store = ObjectStore(repo)
    empty_tree = json.dumps(
        {"version": 1, "entries": {}}, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    tree_sha = store.write(empty_tree)

    c1 = json.dumps(
        {
            "version": 1,
            "tree": tree_sha,
            "parents": [],
            "message": "root",
            "author": "m",
            "time": "t",
            "epoch": 0,
            "jp": "x",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    c1_sha = store.write(c1)
    c2 = json.dumps(
        {
            "version": 1,
            "tree": tree_sha,
            "parents": [c1_sha, c1_sha],
            "message": "merge",
            "author": "m",
            "time": "t",
            "epoch": 0,
            "jp": "x",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    c2_sha = store.write(c2)

    refs.write_ref(repo, "main", c2_sha)
    _mirror(repo, cfg, fake_api)
    fake_api.files["users/alice/__jp/refs/heads/main"] = (c2_sha + "\n").encode("ascii")

    _wipe_local_history(repo)
    fake_api.content_reads.clear()
    result = fetch_mod.fetch_history(repo, cfg, fake_api)  # must terminate
    assert result.failed == 0
    assert ObjectStore(repo).has(c1_sha)
    # c1 was downloaded EXACTLY once despite being named twice as a parent.
    c1_path = f"users/alice/__jp/objects/{c1_sha[:2]}/{c1_sha[2:]}"
    assert fake_api.content_reads.count(c1_path) == 1


def test_fetch_walk_visited_guard_stops_injected_cycle(repo, cfg, fake_api, monkeypatch):
    """Inject a true commit-DAG cycle via a patched reader: the walk must NOT hang.

    A real cycle cannot be content-addressed, so we patch ``read_commit`` to model a
    hostile store where A.parents = [B] and B.parents = [A]. The visited set in
    ``_fetch_reachable`` must terminate the walk rather than loop forever.
    """
    from jp.versioning import fetch as fmod

    sha_a = "a" * 64
    sha_b = "b" * 64
    tree = "c" * 64
    cyclic = {
        sha_a: {"version": 1, "tree": tree, "parents": [sha_b]},
        sha_b: {"version": 1, "tree": tree, "parents": [sha_a]},
    }

    # Model a hostile store: nothing is local yet (so the walk DESCENDS each
    # commit), the "download"+"import" are no-ops, and reads come from the cyclic
    # map. read_tree returns no blobs. Without the visited guard this loops forever.
    monkeypatch.setattr(ObjectStore, "has", lambda self, s: False)
    monkeypatch.setattr(fmod, "_download_object", lambda api, prefix, sha: b"")
    monkeypatch.setattr(ObjectStore, "import_object", lambda self, sha, raw, **kw: None)
    monkeypatch.setattr(fmod, "read_commit", lambda store, s: cyclic[s])
    monkeypatch.setattr(fmod, "read_tree", lambda store, s: {})

    br = fmod.BranchFetch(branch="main")
    # Returns promptly: A and B are each visited once (each pulls its commit object
    # + shared tree), then the walk terminates -- no infinite loop. The bounded
    # download count proves we did not re-walk a node.
    fmod._fetch_reachable(None, ObjectStore(repo), "users/alice", sha_a, br)
    assert br.downloaded == 4  # commit A, commit B, + the shared tree pulled per commit


# --------------------------------------------------------------------------- #
# FAST-FORWARD only ref policy
# --------------------------------------------------------------------------- #
def test_fetch_sets_ref_when_absent(repo, cfg, fake_api):
    _commit(repo, cfg, {"a.txt": b"alpha"})
    tip = refs.resolve_head(repo)
    _mirror(repo, cfg, fake_api)
    _wipe_local_history(repo)

    fetch_mod.fetch_history(repo, cfg, fake_api)
    assert refs.read_ref(repo, "main") == tip


def test_fetch_fast_forwards_when_local_behind(repo, cfg, fake_api):
    """Local ref at C1, remote at C2 (descendant): fetch fast-forwards to C2."""
    c1 = _commit(repo, cfg, {"a.txt": b"alpha"})
    c2 = _commit(repo, cfg, {"a.txt": b"alpha2"}, message="c2")
    _mirror(repo, cfg, fake_api)

    # Roll the LOCAL ref back to C1 (local is now behind the remote tip C2).
    refs.write_ref(repo, "main", c1)
    assert refs.read_ref(repo, "main") == c1

    result = fetch_mod.fetch_history(repo, cfg, fake_api)
    assert result.failed == 0
    assert refs.read_ref(repo, "main") == c2  # fast-forwarded
    assert result.branches[0].ref_updated is True


def test_fetch_does_not_move_ref_when_remote_behind(repo, cfg, fake_api):
    """Remote at C1, local at C2 (local ahead): fetch leaves the local ref at C2."""
    c1 = _commit(repo, cfg, {"a.txt": b"alpha"})
    _mirror(repo, cfg, fake_api)  # remote ref = C1
    c2 = _commit(repo, cfg, {"a.txt": b"alpha2"}, message="c2")  # local now ahead

    result = fetch_mod.fetch_history(repo, cfg, fake_api)
    assert result.failed == 0
    assert refs.read_ref(repo, "main") == c2  # unchanged (remote behind)
    assert result.branches[0].ref_updated is False
    assert "behind" in result.branches[0].ref_skipped_reason
    assert c1  # silence lint


def test_fetch_does_not_move_ref_when_diverged(repo, cfg, fake_api):
    """Diverged history: fetch downloads objects but never moves the local ref."""
    base = _commit(repo, cfg, {"a.txt": b"alpha"})
    # Remote branch advances to a commit the local repo will NOT have on its line.
    remote_only = _commit(repo, cfg, {"a.txt": b"remote-change"}, message="remote")
    _mirror(repo, cfg, fake_api)  # remote ref = remote_only

    # Locally, roll back to base and make a DIFFERENT commit (a divergent sibling).
    refs.write_ref(repo, "main", base)
    from jp.versioning import repo as vrepo

    # Re-checkout base content then commit a different change to diverge.
    conftest.write_file(repo, "a.txt", b"local-change")
    local_only = vrepo.create_commit(
        repo, cfg, message="local", stage_all=True, allow_empty=False, dry_run=False
    )["sha"]
    assert local_only != remote_only

    result = fetch_mod.fetch_history(repo, cfg, fake_api)
    assert result.failed == 0
    # The remote objects were downloaded (available) but the ref was NOT moved.
    assert refs.read_ref(repo, "main") == local_only
    assert result.branches[0].ref_updated is False
    assert any("diverged" in w for w in result.warnings)
    assert ObjectStore(repo).has(remote_only)  # objects are present


def test_fetch_absent_remote_branch_warns_not_errors(repo, cfg, fake_api):
    """Fetching a branch with no remote ref is a warning, not a failure."""
    _commit(repo, cfg, {"a.txt": b"alpha"})
    # Nothing mirrored -> the remote has no __jp/refs/heads/main.
    result = fetch_mod.fetch_history(repo, cfg, fake_api)
    assert result.failed == 0
    assert result.downloaded == 0
    assert any("absent or corrupt" in w for w in result.warnings)


# --------------------------------------------------------------------------- #
# CLEAN ABORT on a mid-walk network failure
# --------------------------------------------------------------------------- #
def test_fetch_network_failure_mid_walk_aborts_cleanly(repo, cfg, fake_api):
    """A network error fetching a blob aborts: ref not moved; only verified objects."""
    _commit(repo, cfg, {"a.txt": b"alpha", "b.txt": b"beta"})
    store = ObjectStore(repo)
    from jp.versioning import repo as vrepo

    commit = vrepo.read_commit(store, refs.resolve_head(repo))
    tree = vrepo.read_tree(store, commit["tree"])
    blob_shas = [m["sha256"] for m in tree.values()]
    _mirror(repo, cfg, fake_api)
    _wipe_local_history(repo)

    # Make ONE specific blob GET explode with a transport error.
    victim = blob_shas[0]
    victim_path = f"users/alice/__jp/objects/{victim[:2]}/{victim[2:]}"
    orig = fake_api.get_file_bytes

    def boom(api_path):
        if api_path == victim_path:
            raise NetworkError("connection reset")
        return orig(api_path)

    fake_api.get_file_bytes = boom  # type: ignore[assignment]

    with pytest.raises(NetworkError):
        fetch_mod.fetch_history(repo, cfg, fake_api)

    # The ref was never advanced; the victim blob was not placed. The fetch is
    # resumable (other verified objects may be present -- harmless orphans).
    assert refs.read_ref(repo, "main") is None
    assert not ObjectStore(repo).has(victim)


# --------------------------------------------------------------------------- #
# restore: rebuild objects+refs AND check out HEAD into the working tree
# --------------------------------------------------------------------------- #
def test_restore_rebuilds_and_checks_out_into_empty_tree(repo, cfg, fake_api):
    """After wiping local .jp AND the working files, restore rebuilds both."""
    _commit(repo, cfg, {"a.txt": b"alpha", "sub/b.txt": b"beta"})
    tip = refs.resolve_head(repo)
    _mirror(repo, cfg, fake_api)

    # Wipe local history AND the working files (a true "lost everything" recovery).
    _wipe_local_history(repo)
    (repo / "a.txt").unlink()
    shutil.rmtree(repo / "sub", ignore_errors=True)

    result = fetch_mod.restore(repo, cfg, fake_api)
    assert result.fetch.failed == 0
    assert result.head_resolved is True
    assert refs.resolve_head(repo) == tip

    # The working tree was materialized from HEAD's tree.
    assert (repo / "a.txt").read_bytes() == b"alpha"
    assert (repo / "sub" / "b.txt").read_bytes() == b"beta"


def test_restore_respects_dirty_working_files_without_force(repo, cfg, fake_api):
    """restore must NOT clobber a dirty working file (checkout safety gate)."""
    from jp.errors import SafetyError

    _commit(repo, cfg, {"a.txt": b"alpha"})
    _mirror(repo, cfg, fake_api)
    _wipe_local_history(repo)

    # The working file now has UNCOMMITTED edits relative to the (to-be-restored)
    # HEAD. Because local history was wiped, HEAD will be absent during the
    # classify -> but once fetched, HEAD resolves and the file differs from target
    # with no HEAD entry == untracked collision -> BLOCKED without --force.
    (repo / "a.txt").write_bytes(b"my uncommitted local edits")

    with pytest.raises(SafetyError):
        fetch_mod.restore(repo, cfg, fake_api, force=False)
    # The dirty file is untouched (the checkout aborted before any write).
    assert (repo / "a.txt").read_bytes() == b"my uncommitted local edits"


def test_restore_force_overwrites_dirty(repo, cfg, fake_api):
    _commit(repo, cfg, {"a.txt": b"alpha"})
    _mirror(repo, cfg, fake_api)
    _wipe_local_history(repo)
    (repo / "a.txt").write_bytes(b"dirty")

    result = fetch_mod.restore(repo, cfg, fake_api, force=True)
    assert result.head_resolved is True
    assert (repo / "a.txt").read_bytes() == b"alpha"  # forced overwrite


def test_restore_no_remote_history_is_noop_checkout(repo, cfg, fake_api):
    """With nothing on the remote, restore fetches nothing and HEAD stays unborn."""
    _wipe_local_history(repo)
    result = fetch_mod.restore(repo, cfg, fake_api)
    assert result.head_resolved is False
    assert result.checkout is None
    assert result.fetch.downloaded == 0


# --------------------------------------------------------------------------- #
# FIX 4: _update_local_ref advances via a compare-and-swap (defense-in-depth).
# --------------------------------------------------------------------------- #
def test_update_local_ref_fast_forward_advances(repo, cfg):
    """A clean fast-forward advances the local ref (happy path under the lock)."""
    from jp.versioning.fetch import _update_local_ref

    base = _commit(repo, cfg, {"a.txt": b"v1"})
    tip = _commit(repo, cfg, {"a.txt": b"v2"}, message="c2")
    assert base != tip
    # Pin the local ref at the older commit, then ask to fast-forward to tip.
    refs.write_ref(repo, refs.DEFAULT_BRANCH, base)
    store = ObjectStore(repo)
    br = fetch_mod.BranchFetch(branch=refs.DEFAULT_BRANCH)
    _update_local_ref(repo, store, refs.DEFAULT_BRANCH, tip, br)
    assert br.ref_updated is True
    assert refs.read_ref(repo, refs.DEFAULT_BRANCH) == tip


def test_update_local_ref_cas_raises_on_concurrent_advance(repo, cfg, monkeypatch):
    """If the on-disk ref changed since it was read, the CAS raises (no blind write).

    _update_local_ref reads the local sha, decides a fast-forward, then advances via
    update_ref(expected=<that local sha>). We simulate a racer that moved the ref
    between the read and the CAS by patching the read fetch USES to report the old
    ancestor while the real on-disk ref holds a different (newer) sha -- so
    update_ref's own re-read mismatches and raises rather than overwriting.
    """
    from jp.versioning.fetch import _update_local_ref

    base = _commit(repo, cfg, {"a.txt": b"v1"})
    tip = _commit(repo, cfg, {"a.txt": b"v2"}, message="c2")
    racer = _commit(repo, cfg, {"a.txt": b"v3"}, message="c3")
    # The on-disk ref is at ``racer`` (what a concurrent fetch advanced it to)...
    refs.write_ref(repo, refs.DEFAULT_BRANCH, racer)
    # ...but fetch's read sees the stale ``base`` (its pre-race snapshot), so it
    # decides a fast-forward to ``tip`` with expected=base. update_ref re-reads the
    # REAL on-disk value (racer != base) -> CAS mismatch -> raise.
    monkeypatch.setattr(fetch_mod, "read_ref", lambda root, branch: base)
    store = ObjectStore(repo)
    br = fetch_mod.BranchFetch(branch=refs.DEFAULT_BRANCH)
    with pytest.raises(VersioningError, match="advanced concurrently"):
        _update_local_ref(repo, store, refs.DEFAULT_BRANCH, tip, br)
    # The blind write never happened: the on-disk ref is still the racer's value.
    assert refs.read_ref(repo, refs.DEFAULT_BRANCH) == racer
