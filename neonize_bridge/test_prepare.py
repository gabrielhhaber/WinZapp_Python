"""prepare.py: the build's zip can only replace files of the neonize package."""
import io
import os
import zipfile

import pytest

import prepare


def zip_with(*members, links=()):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name in members:
            z.writestr(name, b"x")
        for name in links:
            info = zipfile.ZipInfo(name)
            info.external_attr = 0o120777 << 16
            z.writestr(info, b"target")
    buf.seek(0)
    return zipfile.ZipFile(buf)


def problems(*members, **kw):
    return prepare.check_members(zip_with(*members, **kw).infolist())


def test_an_ordinary_neonize_package_passes():
    assert problems("neonize/__init__.py", "neonize/client.py", "neonize/client.pyi",
                    "neonize/proto/Neonize_pb2.py", "neonize/neonize-windows-amd64.dll",
                    "neonize/__pycache__/client.cpython-313.pyc", "neonize/py.typed") == []


@pytest.mark.parametrize("name", [
    "evil.pth", "sitecustomize.py", "usercustomize.py", "other_package/__init__.py",
    "neonize/../evil.pth", "/etc/x.py", "C:/x.py", "neonize\\..\\evil.py", "../neonize/x.py",
    "neonize/run.bat", "neonize/x.exe", "neonize/readme.txt", "neonize/data.json",
])
def test_what_is_not_from_the_neonize_package_is_refused(name):
    assert problems(name)


def test_symbolic_link_is_refused():
    assert any("symbolic" in p for p in problems(links=["neonize/link.py"]))


def test_too_many_members_are_refused(monkeypatch):
    monkeypatch.setattr(prepare, "MAX_MEMBERS", 2)
    assert any("members" in p for p in problems("neonize/a.py", "neonize/b.py", "neonize/c.py"))


def test_extracts_only_inside_the_destination_folder(tmp_path):
    destination = tmp_path / "site-packages"
    destination.mkdir()
    prepare.extract_checked(zip_with("neonize/__init__.py", "neonize/proto/x.py"), str(destination))
    assert (destination / "neonize" / "__init__.py").read_bytes() == b"x"
    assert (destination / "neonize" / "proto" / "x.py").exists()
    assert not (tmp_path / "evil.pth").exists()


def test_refused_archive_extracts_nothing(tmp_path):
    destination = tmp_path / "site-packages"
    destination.mkdir()
    with pytest.raises(SystemExit):
        prepare.extract_checked(zip_with("neonize/ok.py", "evil.pth"), str(destination))
    assert list(destination.iterdir()) == []


def test_the_real_size_counts_even_without_the_header_check(tmp_path, monkeypatch):
    monkeypatch.setattr(prepare, "MAX_UNPACKED_BYTES", 10)
    monkeypatch.setattr(prepare, "check_members", lambda infos: [])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("neonize/big.py", b"x" * 100)
    buf.seek(0)
    destination = tmp_path / "sp"
    destination.mkdir()
    with pytest.raises(SystemExit):
        prepare.extract_checked(zipfile.ZipFile(buf), str(destination))


def test_the_declared_size_above_the_limit_is_refused(monkeypatch):
    monkeypatch.setattr(prepare, "MAX_UNPACKED_BYTES", 10)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("neonize/big.py", b"x" * 100)
    buf.seek(0)
    assert any("too large" in p for p in prepare.check_members(zipfile.ZipFile(buf).infolist()))


@pytest.mark.parametrize("url,expected", [
    ("https://github.com/reffehcsoibaf/neonize/releases/download/x/y.zip", True),
    ("https://objects.githubusercontent.com/abc", True),
    ("http://github.com/x", False),
    ("https://evil.com/github.com", False),
    ("https://github.com.evil.com/x", False),
    ("https://notgithub.com/x", False),
])
def test_only_downloads_from_github(url, expected):
    assert prepare.host_allowed(url) is expected


# -- which lock and which build, by system ------------------------------------

def test_each_system_installs_its_own_lock():
    """python-magic-bin is Windows-only; python-magic (Homebrew's libmagic) is
    what macOS needs: the wrong lock fails the hash install on the user's Mac."""
    import neonize_platform
    windows = prepare.runtime_lock(neonize_platform.Target("windows", "amd64"))
    mac = prepare.runtime_lock(neonize_platform.Target("darwin", "arm64"))
    assert os.path.basename(windows) == "requirements.lock"
    assert os.path.basename(mac) == "requirements-macos.lock"
    assert os.path.isfile(windows) and os.path.isfile(mac)


def test_macos_never_downloads_the_published_windows_build(monkeypatch):
    import neonize_platform
    release = {"tag": "neonize-0.12.4-winzapp.1", "asset": "a.zip", "sha256": "0" * 64,
               "identity": "1" * 64}
    monkeypatch.setattr(prepare.neonize_upstream, "load", lambda path: {"release": release})
    monkeypatch.setattr(prepare.neonize_upstream, "checkout_identity", lambda here: "1" * 64)
    assert prepare.usable_release(neonize_platform.Target("darwin", "arm64")) is None
    assert prepare.usable_release(neonize_platform.Target("windows", "amd64")) is release
