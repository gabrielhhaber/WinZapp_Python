"""Tests for updater.py's release-asset integrity verification.

Context: updater.py downloads a ZIP from a GitHub release and hands it
elevated write access to the install directory via a generated batch
script. Nothing verified that download in any way before this — a MITM'd
or hijacked release could get arbitrary code executed as an admin.
_verify_sha256sums() checks the download against a SHA256SUMS.txt manifest
CI now publishes alongside every release (.github/workflows/release.yml).
"""

import hashlib
import os
import tempfile

import pytest

import updater


class _FakeResponse:
    def __init__(self, text="", status_code=200):
        self.text = text
        # The manifest is read as bytes now: its signature covers the exact
        # bytes served, which decoding and re-encoding would not preserve.
        self.content = text.encode("utf-8")
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


@pytest.fixture
def tmp_file():
    fd, path = tempfile.mkstemp()
    os.close(fd)
    with open(path, "wb") as f:
        f.write(b"pretend this is a WinZapp.zip release asset")
    yield path
    try:
        os.remove(path)
    except OSError:
        pass


def _sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


class TestFindSha256sumsAsset:
    def test_finds_asset_case_insensitively(self):
        assets = [
            {"name": "WinZapp.zip", "browser_download_url": "https://x/WinZapp.zip"},
            {"name": "SHA256SUMS.txt", "browser_download_url": "https://x/SHA256SUMS.txt"},
        ]
        assert updater._find_sha256sums_asset(assets) == "https://x/SHA256SUMS.txt"

    def test_returns_empty_when_absent(self):
        assets = [{"name": "WinZapp.zip", "browser_download_url": "https://x/WinZapp.zip"}]
        assert updater._find_sha256sums_asset(assets) == ""

    def test_empty_asset_list(self):
        assert updater._find_sha256sums_asset([]) == ""


class TestVerifySha256sums:
    def test_no_manifest_url_fails_open(self, tmp_file):
        """Older releases published before this feature existed have no
        manifest at all — must not permanently block updating from them."""
        ok, detail = updater._verify_sha256sums(tmp_file, "WinZapp.zip", "")
        assert ok is True
        assert detail == ""

    def test_matching_checksum_passes(self, tmp_file, monkeypatch):
        expected = _sha256_of(tmp_file)
        manifest = f"{expected}  WinZapp.zip\nsomeotherhash  WinZappInstaller.exe\n"
        monkeypatch.setattr(updater.requests, "get", lambda *a, **kw: _FakeResponse(manifest))

        ok, detail = updater._verify_sha256sums(tmp_file, "WinZapp.zip", "https://x/SHA256SUMS.txt")

        assert ok is True
        assert detail == ""

    def test_mismatched_checksum_fails_closed(self, tmp_file, monkeypatch):
        manifest = "0000000000000000000000000000000000000000000000000000000000000000  WinZapp.zip\n"
        monkeypatch.setattr(updater.requests, "get", lambda *a, **kw: _FakeResponse(manifest))

        ok, detail = updater._verify_sha256sums(tmp_file, "WinZapp.zip", "https://x/SHA256SUMS.txt")

        assert ok is False
        assert "mismatch" in detail.lower()

    def test_missing_entry_for_filename_fails_closed(self, tmp_file, monkeypatch):
        """The manifest exists (so this ISN'T an old pre-feature release)
        but doesn't mention our filename at all — suspicious, not silently
        accepted."""
        manifest = "abc123  SomeOtherFile.zip\n"
        monkeypatch.setattr(updater.requests, "get", lambda *a, **kw: _FakeResponse(manifest))

        ok, detail = updater._verify_sha256sums(tmp_file, "WinZapp.zip", "https://x/SHA256SUMS.txt")

        assert ok is False
        assert "no checksum entry" in detail.lower()

    def test_manifest_fetch_failure_fails_closed(self, tmp_file, monkeypatch):
        def _raise(*a, **kw):
            raise Exception("network error")
        monkeypatch.setattr(updater.requests, "get", _raise)

        ok, detail = updater._verify_sha256sums(tmp_file, "WinZapp.zip", "https://x/SHA256SUMS.txt")

        assert ok is False
        assert "failed to download" in detail.lower()

    def test_ignores_asterisk_binary_mode_marker(self, tmp_file, monkeypatch):
        """sha256sum's own output format prefixes the filename with '*' for
        binary mode (e.g. "<hash> *WinZapp.zip") — must still match."""
        expected = _sha256_of(tmp_file)
        manifest = f"{expected} *WinZapp.zip\n"
        monkeypatch.setattr(updater.requests, "get", lambda *a, **kw: _FakeResponse(manifest))

        ok, _ = updater._verify_sha256sums(tmp_file, "WinZapp.zip", "https://x/SHA256SUMS.txt")

        assert ok is True
