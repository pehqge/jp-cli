"""push invariants: no-delete, conflict-abort, dotfile-skip, dry-run, per-file resilience."""

from __future__ import annotations

from conftest import write_file
from jp import sync
from jp.index import Entry
from jp.sync import Change


def test_push_uploads_new_local_file(repo, cfg, fake_api, index, ignore):
    write_file(repo, "hello.txt", b"hi there")
    out = sync.push(repo, cfg, fake_api, index, ignore)
    assert "hello.txt" in out.transferred
    assert fake_api.files["users/alice/hello.txt"] == b"hi there"
    # Index recorded only after success.
    assert "hello.txt" in index
    assert index.get("hello.txt").sha256 == sync.sha256_bytes(b"hi there")


def test_push_never_deletes_remote(repo, cfg, fake_api, index, ignore):
    # Remote has a file with no local counterpart; push must NOT delete it.
    fake_api.seed("users/alice/remote_only.txt", b"keep me")
    index.set("remote_only.txt", Entry(sha256=sync.sha256_bytes(b"keep me"), size=7))
    index.save()
    write_file(repo, "new.txt", b"x")
    sync.push(repo, cfg, fake_api, index, ignore)
    assert fake_api.deletes == []
    assert "users/alice/remote_only.txt" in fake_api.files


def test_push_aborts_on_conflict_no_overwrite(repo, cfg, fake_api, index, ignore):
    # base = "base", local = "local-edit", remote = "remote-edit" -> CONFLICT.
    base = b"base"
    fake_api.seed("users/alice/c.txt", b"remote-edit")
    index.set("c.txt", Entry(sha256=sync.sha256_bytes(base), size=len(base)))
    index.save()
    write_file(repo, "c.txt", b"local-edit")

    out = sync.push(repo, cfg, fake_api, index, ignore)
    assert "c.txt" in out.conflicts
    assert "c.txt" not in out.transferred
    # Remote content must be UNCHANGED (no last-writer-wins).
    assert fake_api.files["users/alice/c.txt"] == b"remote-edit"
    assert ("PUT", "users/alice/c.txt") not in fake_api.calls


def test_push_skips_dotfiles_and_does_not_abort(repo, cfg, fake_api, index, ignore):
    write_file(repo, ".env", b"SECRET=1")  # hidden -> server would 400
    write_file(repo, "ok.txt", b"fine")
    out = sync.push(repo, cfg, fake_api, index, ignore)
    # Dotfile is reported as skipped, NOT a failure, and run still succeeds.
    assert ".env" in out.skipped_hidden
    assert out.failures == []
    assert "ok.txt" in out.transferred
    # We never even attempted to PUT the dotfile.
    assert all(not p.endswith("/.env") for (_, p) in fake_api.calls)


def test_push_skips_dotdir_contents(repo, cfg, fake_api, index, ignore):
    write_file(repo, ".config/key.txt", b"k")
    write_file(repo, "visible.txt", b"v")
    out = sync.push(repo, cfg, fake_api, index, ignore)
    assert ".config/key.txt" in out.skipped_hidden
    assert "visible.txt" in out.transferred


def test_push_never_writes_dotted_remote_paths(repo, cfg, fake_api, index, ignore):
    write_file(repo, "a/b.txt", b"data")
    sync.push(repo, cfg, fake_api, index, ignore)
    for _, path in fake_api.calls:
        if "->" in path:
            path = path.split("->", 1)[1]
        for part in path.split("/"):
            assert not part.startswith("."), f"jp wrote a dotted remote path: {path}"


def test_push_dry_run_writes_nothing(repo, cfg, fake_api, index, ignore):
    write_file(repo, "x.txt", b"content")
    out = sync.push(repo, cfg, fake_api, index, ignore, dry_run=True)
    assert "x.txt" in out.transferred  # reported as "would transfer"
    assert fake_api.calls == []  # NO remote mutation
    assert "x.txt" not in index  # index untouched


def test_push_uploads_directly_to_final_path(repo, cfg, fake_api, index, ignore):
    # Upload is a direct whole-file PUT to the final path -- no temp+rename
    # (PATCH cannot overwrite an existing target: it 409s) and never a dotted
    # temp dir (the server rejects hidden names).
    write_file(repo, "doc.txt", b"hello")
    sync.push(repo, cfg, fake_api, index, ignore)
    methods = [m for (m, _) in fake_api.calls]
    assert "RENAME" not in methods
    assert ("PUT", "users/alice/doc.txt") in fake_api.calls
    assert all("jp-tmp" not in p for (_, p) in fake_api.calls)


def test_push_creates_prefix_and_parent_dirs(repo, cfg, fake_api, index, ignore):
    # The server does not auto-create parents and the prefix may not exist yet,
    # so push must mkdir the prefix tree and each intermediate dir.
    write_file(repo, "dados/sub/peso.bin", b"\x00\x01\x02")
    sync.push(repo, cfg, fake_api, index, ignore)
    mkdirs = [p for (m, p) in fake_api.calls if m == "MKDIR"]
    assert "users/alice" in mkdirs  # the prefix itself
    assert "users/alice/dados" in mkdirs
    assert "users/alice/dados/sub" in mkdirs
    assert fake_api.files["users/alice/dados/sub/peso.bin"] == b"\x00\x01\x02"


def test_push_per_file_failure_does_not_abort_run(repo, cfg, fake_api, index, ignore, monkeypatch):
    write_file(repo, "good1.txt", b"a")
    write_file(repo, "bad.txt", b"b")
    write_file(repo, "good2.txt", b"c")

    real_put = fake_api.put_file_bytes

    def flaky_put(api_path, data):
        if api_path.endswith("/bad.txt") or "bad.txt" in api_path:
            from jp.errors import ApiError

            raise ApiError("server hiccup", status=500)
        return real_put(api_path, data)

    monkeypatch.setattr(fake_api, "put_file_bytes", flaky_put)
    out = sync.push(repo, cfg, fake_api, index, ignore)

    assert "good1.txt" in out.transferred
    assert "good2.txt" in out.transferred
    assert any(rel == "bad.txt" for rel, _ in out.failures)
    # Index has the successes but NOT the failed file.
    assert "good1.txt" in index and "good2.txt" in index
    assert "bad.txt" not in index


def test_push_unchanged_file_not_reuploaded(repo, cfg, fake_api, index, ignore):
    data = b"same"
    write_file(repo, "same.txt", data)
    fake_api.seed("users/alice/same.txt", data, mtime="2024-01-01T00:00:00Z")
    index.set(
        "same.txt",
        Entry(sha256=sync.sha256_bytes(data), size=len(data), remote_mtime="2024-01-01T00:00:00Z"),
    )
    index.save()
    out = sync.push(repo, cfg, fake_api, index, ignore)
    assert "same.txt" not in out.transferred
    assert ("PUT", "users/alice/same.txt") not in fake_api.calls


def test_classify_local_new(repo, cfg, fake_api, index, ignore):
    write_file(repo, "n.txt", b"x")
    states = {s.rel: s for s in sync.diff(repo, cfg, fake_api, index, ignore)}
    assert states["n.txt"].change == Change.LOCAL_NEW


def test_push_unchanged_detected_by_hash_without_download(repo, cfg, fake_api, index, ignore):
    """Even when the remote mtime no longer matches the base (cheap pre-filter is
    'unsure'), an unchanged file is detected via the cheap server hash and is
    NOT re-uploaded and NOT downloaded (research §1)."""
    data = b"same content"
    write_file(repo, "same.txt", data)
    fake_api.seed("users/alice/same.txt", data, mtime="2024-12-31T00:00:00Z")
    index.set(
        "same.txt",
        Entry(
            sha256=sync.sha256_bytes(data),
            size=len(data),
            remote_mtime="2024-01-01T00:00:00Z",  # differs from remote now
            remote_hash=sync.sha256_bytes(data),
        ),
    )
    index.save()

    out = sync.push(repo, cfg, fake_api, index, ignore)
    assert "same.txt" not in out.transferred
    assert ("PUT", "users/alice/same.txt") not in fake_api.calls
    # Decision used the cheap hash, never a body download.
    assert "users/alice/same.txt" in fake_api.hash_reads
    assert fake_api.content_reads == []
