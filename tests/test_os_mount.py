"""Unit tests for os_mount command construction (no subprocess, no real mount)."""

from jp.mount.os_mount import _dav_url_to_scheme, build_mount_plan, build_unmount_plan

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
