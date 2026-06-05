"""Unit tests for os_mount command construction (no subprocess, no real mount)."""

import pytest

from jp.mount.os_mount import (
    MountError,
    _dav_url_to_scheme,
    _prepare_dir_target,
    auto_mount_target,
    build_mount_plan,
    build_unmount_plan,
    first_free_drive_letter,
    gvfs_dav_path,
)

URL = "http://127.0.0.1:9/"


# ---------------------------------------------------------------------------
# build_mount_plan
# ---------------------------------------------------------------------------


def test_mount_plan_darwin():
    plan = build_mount_plan(URL, "/Volumes/jp", "darwin")
    assert plan.argv == ["mount_webdav", "-S", URL, "/Volumes/jp"]
    assert plan.needs_existing_dir is True


def test_mount_plan_darwin_note():
    plan = build_mount_plan(URL, "/Volumes/jp", "darwin")
    assert "/Volumes/jp" in plan.note


def test_mount_plan_win32():
    plan = build_mount_plan(URL, "Z:", "win32")
    assert plan.argv == ["net", "use", "*", URL]
    assert plan.needs_existing_dir is False


def test_mount_plan_linux():
    plan = build_mount_plan(URL, "/x", "linux")
    assert plan.argv == ["gio", "mount", "dav://127.0.0.1:9/"]
    assert plan.needs_existing_dir is False


def test_mount_plan_linux_other_platform():
    """Any non-darwin, non-win platform falls through to the GVfs path."""
    plan = build_mount_plan(URL, "/mnt/jp", "freebsd")
    assert plan.argv[0] == "gio"


# ---------------------------------------------------------------------------
# build_unmount_plan
# ---------------------------------------------------------------------------


def test_unmount_darwin():
    assert build_unmount_plan(URL, "/x", "darwin") == ["umount", "/x"]


def test_unmount_win32():
    assert build_unmount_plan(URL, "X:", "win32") == ["net", "use", "X:", "/delete", "/y"]


def test_unmount_linux():
    assert build_unmount_plan(URL, "/x", "linux") == [
        "gio",
        "mount",
        "-u",
        "dav://127.0.0.1:9/",
    ]


# ---------------------------------------------------------------------------
# _dav_url_to_scheme
# ---------------------------------------------------------------------------


def test_dav_scheme_rewrite_http():
    assert _dav_url_to_scheme("http://127.0.0.1:9/", "dav://") == "dav://127.0.0.1:9/"


def test_dav_scheme_rewrite_https():
    assert _dav_url_to_scheme("https://host/path", "davs://") == "davs://host/path"


def test_dav_scheme_passthrough():
    assert _dav_url_to_scheme("dav://already/", "dav://") == "dav://already/"


# ---------------------------------------------------------------------------
# auto_mount_target: leaf -> target per platform (explicit platform= arg)
# ---------------------------------------------------------------------------


def test_auto_target_darwin_folder():
    assert auto_mount_target("jp-live-test", "/home/me", "darwin") == "/home/me/jp-live-test"


def test_auto_target_linux_folder():
    assert auto_mount_target("proj", "/work", "linux") == "/work/proj"


def test_auto_target_other_unix_folder():
    assert auto_mount_target("proj", "/work", "freebsd") == "/work/proj"


def test_auto_target_windows_drive_letter():
    # Windows ignores cwd/leaf and returns the first free drive letter; the used
    # set is injected so no ctypes call happens.
    target = auto_mount_target("proj", "/work", "win32", used_drive_letters={"C", "D"})
    assert target == "E:"


# ---------------------------------------------------------------------------
# _prepare_dir_target: create / reuse-empty / refuse-non-empty (darwin rule)
# ---------------------------------------------------------------------------


def test_prepare_creates_missing_dir(tmp_path):
    target = tmp_path / "jp-live-test"
    _prepare_dir_target(str(target))
    assert target.is_dir()


def test_prepare_reuses_empty_dir(tmp_path):
    target = tmp_path / "empty"
    target.mkdir()
    _prepare_dir_target(str(target))  # no raise
    assert target.is_dir()


def test_prepare_refuses_non_empty_dir(tmp_path):
    target = tmp_path / "full"
    target.mkdir()
    (target / "keep.txt").write_text("data")
    with pytest.raises(MountError):
        _prepare_dir_target(str(target))
    # User data is untouched.
    assert (target / "keep.txt").read_text() == "data"


def test_prepare_refuses_existing_file(tmp_path):
    target = tmp_path / "afile"
    target.write_text("x")
    with pytest.raises(MountError):
        _prepare_dir_target(str(target))


# ---------------------------------------------------------------------------
# gvfs_dav_path: pure GVfs mount-path computation
# ---------------------------------------------------------------------------


def test_gvfs_path_basic():
    assert (
        gvfs_dav_path("127.0.0.1", 8080, uid=1000)
        == "/run/user/1000/gvfs/dav:host=127.0.0.1,port=8080,ssl=false"
    )


def test_gvfs_path_ssl():
    assert (
        gvfs_dav_path("example.com", 443, uid=501, ssl=True)
        == "/run/user/501/gvfs/dav:host=example.com,port=443,ssl=true"
    )


# ---------------------------------------------------------------------------
# first_free_drive_letter: pure Windows free-letter logic
# ---------------------------------------------------------------------------


def test_first_free_letter_skips_used():
    assert first_free_drive_letter({"C", "D"}) == "E:"


def test_first_free_letter_accepts_colon_forms():
    assert first_free_drive_letter({"C:", "D:", "E:"}) == "F:"


def test_first_free_letter_all_taken_raises():
    every = {chr(c) for c in range(ord("A"), ord("Z") + 1)}
    with pytest.raises(MountError):
        first_free_drive_letter(every)
