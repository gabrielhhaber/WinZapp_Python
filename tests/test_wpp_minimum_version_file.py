"""wpp_minimum_version.txt replaced a WPP_MINIMUM_VERSION key inside a
bundled client/.env file with its own dedicated plain-text file — the .env
file was the only thing build.py / build-windows.yml ever shipped alongside
the exe, and it existed purely to carry this one value. See
_read_wpp_minimum_version()'s docstring for the full reasoning; this file
pins the read behaviour.

The read itself is core/wpp_runtime.py, module-level and stdlib-only, because
four call sites need it — the startup version gate, the in-app update checker,
the end-user reinstall dialog and setup_api.py (which runs before any
client-side dependency is installed, so it cannot import core/utils.py). They
each used to carry their own copy, with their own idea of which exceptions to
swallow.

MainWindow is a wx.Frame and cannot be instantiated without a running
wx.App, so its thin wrapper is bound onto a plain stub.
"""

from core.wpp_runtime import homologated_wpp_tag, read_homologated_wpp_version
from main import MainWindow


#: Not valid UTF-8, which is what a truncated or half-written file looks like.
_CORRUPT = bytes([0xFF, 0xFE, 0x00]) + b"broken" + bytes([0x80])


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


class TestHomologatedWppTag:
    """The three install paths all want the *git tag*, not the bare version."""

    def test_the_version_becomes_the_tag_setup_checks_out(self, tmp_path):
        version_file = tmp_path / "wpp_minimum_version.txt"
        version_file.write_text("2.10.16\n", encoding="utf-8")

        assert homologated_wpp_tag(str(version_file)) == "v2.10.16"

    def test_an_already_prefixed_value_is_not_double_prefixed(self, tmp_path):
        version_file = tmp_path / "wpp_minimum_version.txt"
        version_file.write_text("v2.10.16", encoding="utf-8")

        assert homologated_wpp_tag(str(version_file)) == "v2.10.16"

    def test_a_byte_order_mark_does_not_end_up_inside_the_tag(self, tmp_path):
        """The file is committed and hand-edited on Windows, where plenty of
        editors write a BOM. Read as plain utf-8 it becomes part of the
        version string, and the tag setup_api.py checks out is a tag no
        repository has."""
        version_file = tmp_path / "wpp_minimum_version.txt"
        version_file.write_text("2.10.16\n", encoding="utf-8-sig")

        assert read_homologated_wpp_version(str(version_file)) == "2.10.16"
        assert homologated_wpp_tag(str(version_file)) == "v2.10.16"

    def test_an_absent_file_yields_no_tag(self, tmp_path):
        """Every caller reads this as "nothing pinned" and falls back to
        resolving a tag over the network — never as an empty tag."""
        assert homologated_wpp_tag(str(tmp_path / "absent.txt")) == ""

    def test_homologated_tag_survives_a_corrupt_file(self, tmp_path):
        """A truncated or half-written file raises UnicodeDecodeError, which
        is a ValueError and not an OSError — the distinction that made the
        startup gate's own copy of this read able to abort __init__ before
        ensure_wpp_running() ever ran."""
        version_file = tmp_path / "wpp_minimum_version.txt"
        version_file.write_bytes(_CORRUPT)

        assert read_homologated_wpp_version(str(version_file)) == ""
        assert homologated_wpp_tag(str(version_file)) == ""

    def test_a_directory_in_place_of_the_file_yields_no_tag(self, tmp_path):
        assert homologated_wpp_tag(str(tmp_path)) == ""
