"""pull invariants: no-delete, conflict-abort, dry-run, download containment."""

from __future__ import annotations

from conftest import write_file
from jp import sync
from jp.index import Entry
from jp.sync import Change


def test_pull_downloads_new_remote_file(repo, cfg, fake_api, index, ignore):
    fake_api.seed("users/alice/data.txt", b"from server")
    out = sync.pull(repo, cfg, fake_api, index, ignore)
    assert "data.txt" in out.transferred
    assert (repo / "data.txt").read_bytes() == b"from server"
    assert "data.txt" in index


def test_pull_never_deletes_local(repo, cfg, fake_api, index, ignore):
    # Local file with no remote counterpart: pull must leave it alone.
    write_file(repo, "local_only.txt", b"mine")
    index.set("local_only.txt", Entry(sha256=sync.sha256_bytes(b"mine"), size=4))
    index.save()
    fake_api.seed("users/alice/new_remote.txt", b"r")
    sync.pull(repo, cfg, fake_api, index, ignore)
    assert (repo / "local_only.txt").exists()
    assert (repo / "local_only.txt").read_bytes() == b"mine"


def test_pull_aborts_on_conflict_no_overwrite(repo, cfg, fake_api, index, ignore):
    base = b"base"
    write_file(repo, "c.txt", b"local-edit")
    fake_api.seed("users/alice/c.txt", b"remote-edit")
    index.set("c.txt", Entry(sha256=sync.sha256_bytes(base), size=len(base)))
    index.save()

    out = sync.pull(repo, cfg, fake_api, index, ignore)
    assert "c.txt" in out.conflicts
    # Local content must be UNCHANGED.
    assert (repo / "c.txt").read_bytes() == b"local-edit"


def test_pull_dry_run_writes_nothing(repo, cfg, fake_api, index, ignore):
    fake_api.seed("users/alice/x.txt", b"content")
    out = sync.pull(repo, cfg, fake_api, index, ignore, dry_run=True)
    assert "x.txt" in out.transferred  # would-transfer
    assert not (repo / "x.txt").exists()  # nothing written
    assert "x.txt" not in index


def test_pull_containment_against_malicious_listing(repo, cfg, fake_api, index, ignore):
    # Server tries to smuggle a path OUTSIDE the prefix in its listing.
    # scan_remote must drop it; nothing gets written outside.
    fake_api.files["users/alice/../../etc/evil.txt"] = b"pwn"
    fake_api.files["users/aliceEVIL/sneaky.txt"] = b"pwn2"
    fake_api.seed("users/alice/legit.txt", b"ok")
    out = sync.pull(repo, cfg, fake_api, index, ignore)
    assert "legit.txt" in out.transferred
    # No file created outside the repo tree.
    assert not (repo.parent / "etc").exists()
    assert "legit.txt" in {p.split("/")[-1] for p in index.entries}


def test_pull_updates_modified_remote(repo, cfg, fake_api, index, ignore):
    # base == local, remote changed -> remote_modified -> pull updates local.
    base = b"v1"
    write_file(repo, "f.txt", base)
    index.set(
        "f.txt",
        Entry(sha256=sync.sha256_bytes(base), size=len(base), remote_mtime="2024-01-01T00:00:00Z"),
    )
    index.save()
    fake_api.seed("users/alice/f.txt", b"v2-from-server", mtime="2024-09-09T00:00:00Z")
    states = {s.rel: s for s in sync.diff(repo, cfg, fake_api, index, ignore)}
    assert states["f.txt"].change == Change.REMOTE_MODIFIED
    out = sync.pull(repo, cfg, fake_api, index, ignore)
    assert "f.txt" in out.transferred
    assert (repo / "f.txt").read_bytes() == b"v2-from-server"


def test_pull_per_file_failure_continues(repo, cfg, fake_api, index, ignore, monkeypatch):
    fake_api.seed("users/alice/a.txt", b"a")
    fake_api.seed("users/alice/b.txt", b"b")

    real_get = fake_api.get_file_bytes

    def flaky_get(api_path):
        if api_path.endswith("a.txt"):
            from jp.errors import ApiError

            raise ApiError("boom", status=500)
        return real_get(api_path)

    monkeypatch.setattr(fake_api, "get_file_bytes", flaky_get)
    out = sync.pull(repo, cfg, fake_api, index, ignore)
    assert "b.txt" in out.transferred
    assert any(rel == "a.txt" for rel, _ in out.failures)
    assert "a.txt" not in index


def test_diff_uses_remote_hash_no_download_when_hash_matches(repo, cfg, fake_api, index, ignore):
    """When the cheap server hash matches the local sha, diff must classify the
    file UNCHANGED WITHOUT downloading the body (research §1: content=0&hash=1)."""
    data = b"identical content here"
    write_file(repo, "f.txt", data)
    fake_api.seed("users/alice/f.txt", data, mtime="2024-09-09T00:00:00Z")
    # Base recorded a DIFFERENT mtime so the cheap size+mtime pre-filter is
    # "unsure" and we are forced onto the hash path -- which must use hash(),
    # not a full download.
    index.set(
        "f.txt",
        Entry(
            sha256=sync.sha256_bytes(data),
            size=len(data),
            remote_mtime="2024-01-01T00:00:00Z",
            remote_hash=sync.sha256_bytes(data),
        ),
    )
    index.save()

    states = {s.rel: s for s in sync.diff(repo, cfg, fake_api, index, ignore)}
    assert states["f.txt"].change == Change.UNCHANGED
    # Primary signal was the cheap hash; the body was NEVER downloaded.
    assert "users/alice/f.txt" in fake_api.hash_reads
    assert fake_api.content_reads == []


def test_diff_downloads_only_when_hash_differs(repo, cfg, fake_api, index, ignore):
    """If the remote hash differs from base, diff classifies remote_modified.
    The classification itself still must NOT download the body."""
    base = b"v1"
    write_file(repo, "f.txt", base)
    index.set(
        "f.txt",
        Entry(
            sha256=sync.sha256_bytes(base),
            size=len(base),
            remote_mtime="2024-01-01T00:00:00Z",
            remote_hash=sync.sha256_bytes(base),
        ),
    )
    index.save()
    fake_api.seed("users/alice/f.txt", b"v2-changed", mtime="2024-09-09T00:00:00Z")

    states = {s.rel: s for s in sync.diff(repo, cfg, fake_api, index, ignore)}
    assert states["f.txt"].change == Change.REMOTE_MODIFIED
    # The cheap hash was consulted; the body was not downloaded just to classify.
    assert "users/alice/f.txt" in fake_api.hash_reads
    assert fake_api.content_reads == []  # diff itself never downloads now
