"""Downloading an update no longer needs the progress dialog.

Download, signature check and extraction used to live inside
UpdateProgressDialog._worker(), so an update could not be fetched without the
modal window that keeps the user out of the app. They are
update_package.download_update_package() now: the dialog runs it with its
gauge and Cancel, a background download runs the same function with neither.
One function on purpose — both paths must hand the installer exactly the same
verified bytes (docs/traps/release-integrity.md).
"""

import os
import zipfile

import pytest

import update_package
import updater
from update_package import download_update_package


class _I18n:
    def t(self, key):
        return key + " {detail}" if key == "update_checksum_mismatch" else key


def _archive(tmp_path, files):
    path = tmp_path / "WinZapp.zip"
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return path.read_bytes()


class _Response:
    def __init__(self, payload, chunks=4):
        self._payload = payload
        self._chunks = chunks
        self.headers = {"content-length": str(len(payload))}

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        size = max(1, len(self._payload) // self._chunks)
        for start in range(0, len(self._payload), size):
            yield self._payload[start:start + size]


@pytest.fixture
def temp_files(tmp_path, monkeypatch):
    """Every temporary file and directory lands under tmp_path."""
    monkeypatch.setattr(update_package.tempfile, "tempdir", str(tmp_path / "tmp"))
    os.makedirs(tmp_path / "tmp")
    return tmp_path / "tmp"


@pytest.fixture
def release(tmp_path, monkeypatch):
    state = {"payload": _archive(tmp_path, {"WinZapp.exe": "new build"}), "verify": (True, ""),
             "seen": []}
    monkeypatch.setattr(update_package.requests, "get",
                        lambda url, **kw: _Response(state["payload"]))

    def _verify(zip_path, filename, sha256sums_url, **kw):
        state["seen"].append((filename, sha256sums_url, kw))
        return state["verify"]

    monkeypatch.setattr(updater, "_verify_sha256sums", _verify)
    return state


def _fetch(**kw):
    return download_update_package(
        "https://example.invalid/WinZapp.zip?token=1", "https://example.invalid/SHA256SUMS.txt",
        "https://example.invalid/SHA256SUMS.txt.sig", "2.0.0.1", False, _I18n(), **kw)


class TestAFetchThatWorks:
    def test_it_returns_the_directory_to_install_from(self, release, temp_files):
        package = _fetch()

        assert package.error == "" and package.cancelled is False
        assert open(os.path.join(package.extract_dir, "WinZapp.exe")).read() == "new build"

    def test_the_zip_does_not_stay_behind(self, release, temp_files):
        _fetch()
        assert not [n for n in os.listdir(temp_files) if n.endswith(".zip")]

    def test_a_single_top_level_folder_is_what_gets_installed(self, release, temp_files, tmp_path):
        release["payload"] = _archive(tmp_path, {"WinZapp/WinZapp.exe": "nested"})

        package = _fetch()

        assert os.path.basename(package.extract_dir) == "WinZapp"
        assert os.path.isfile(os.path.join(package.extract_dir, "WinZapp.exe"))

    def test_the_download_reports_progress_and_never_claims_to_be_done(self, release, temp_files):
        seen = []
        _fetch(on_progress=seen.append)
        assert seen and seen == sorted(seen) and max(seen) <= 99


class TestNothingIsTrustedBeforeTheCheck:
    def test_the_release_is_checked_against_its_own_version_and_channel(self, release, temp_files):
        _fetch()
        (filename, sums_url, kw), = release["seen"]
        assert filename == "WinZapp.zip"                  # the query string is not the name
        assert sums_url.endswith("SHA256SUMS.txt")
        assert kw == {"signature_url": "https://example.invalid/SHA256SUMS.txt.sig",
                      "expected_version": "2.0.0.1", "is_alpha": False}

    def test_a_refused_package_is_reported_and_removed(self, release, temp_files):
        release["verify"] = (False, "signature does not match")

        package = _fetch()

        assert package.extract_dir == ""
        assert package.error == "update_checksum_mismatch signature does not match"
        assert os.listdir(temp_files) == []               # no ZIP, and nothing extracted


class TestStoppingIt:
    def test_a_cancel_during_the_download_keeps_nothing(self, release, temp_files):
        calls = []

        def _cancelled():
            calls.append(1)
            return len(calls) > 2

        package = _fetch(is_cancelled=_cancelled)

        assert package.cancelled is True and package.extract_dir == ""
        assert release["seen"] == []                      # never got as far as the check
        assert os.listdir(temp_files) == []

    def test_a_network_fault_is_the_callers_to_report(self, release, temp_files, monkeypatch):
        def _down(url, **kw):
            raise ConnectionError("no route")

        monkeypatch.setattr(update_package.requests, "get", _down)

        with pytest.raises(ConnectionError):
            _fetch()
