"""_read_wpp_minimum_version() replaced a WPP_MINIMUM_VERSION key inside a
bundled client/.env file with its own dedicated plain-text file
(wpp_minimum_version.txt) — the .env file was the only thing build.py /
build-windows.yml ever shipped alongside the exe, and it existed purely to
carry this one value. See the method's own docstring for the full reasoning;
this test just pins the new file's read behaviour.

MainWindow is a wx.Frame and cannot be instantiated without a running
wx.App, so the method is bound onto a plain stub.
"""

from main import MainWindow


class _Stub:
    _read_wpp_minimum_version = MainWindow._read_wpp_minimum_version


class TestReadWppMinimumVersion:
    def test_reads_the_bundled_file(self, tmp_path, monkeypatch):
        version_file = tmp_path / "wpp_minimum_version.txt"
        version_file.write_text("2.10.16\n", encoding="utf-8")
        monkeypatch.setattr(
            "main.resource_path",
            lambda *parts: str(tmp_path.joinpath(*parts)),
        )

        assert _Stub()._read_wpp_minimum_version() == "2.10.16"

    def test_returns_empty_string_when_the_file_is_absent(self, tmp_path, monkeypatch):
        """The normal case for any local/dev build — only build-windows.yml
        ever writes this file."""
        monkeypatch.setattr(
            "main.resource_path",
            lambda *parts: str(tmp_path.joinpath(*parts)),
        )

        assert _Stub()._read_wpp_minimum_version() == ""

    def test_strips_surrounding_whitespace(self, tmp_path, monkeypatch):
        version_file = tmp_path / "wpp_minimum_version.txt"
        version_file.write_text("  2.10.16  \r\n", encoding="utf-8")
        monkeypatch.setattr(
            "main.resource_path",
            lambda *parts: str(tmp_path.joinpath(*parts)),
        )

        assert _Stub()._read_wpp_minimum_version() == "2.10.16"
