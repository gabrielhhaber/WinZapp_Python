"""Frozen codec failures must not escape or turn corrupt bytes into versions."""
import builtins
import json

import pytest

from core import update_keeps_server, wa_version_refresh, wpp_runtime


READERS = [
    (wpp_runtime.read_homologated_wpp_version, "2.10.48", ""),
    (wpp_runtime._read_json, {"version": "2.10.48"}, {}),
    (update_keeps_server._read_package, {"version": "2.10.48"}, {}),
    (wa_version_refresh._read_json, {"version": "2.10.48"}, None),
]


@pytest.mark.parametrize("reader,value,fallback", READERS)
@pytest.mark.parametrize("bom", [b"", b"\xef\xbb\xbf"])
def test_valid_utf8_keeps_bom_support_without_sig_codec(tmp_path, monkeypatch, reader, value, fallback, bom):
    path = tmp_path / "synthetic.txt"
    body = value if isinstance(value, str) else json.dumps(value)
    path.write_bytes(bom + body.encode("utf-8"))
    real_open = builtins.open

    def without_sig_codec(*args, **kwargs):
        if kwargs.get("encoding") == "utf-8-sig":
            raise LookupError("synthetic unavailable utf_8_sig codec")
        return real_open(*args, **kwargs)

    monkeypatch.setattr(builtins, "open", without_sig_codec)
    assert reader(str(path)) == value


@pytest.mark.parametrize("reader,value,fallback", READERS)
@pytest.mark.parametrize("error", [FileNotFoundError, PermissionError, LookupError])
def test_reader_error_returns_its_existing_fallback(monkeypatch, reader, value, fallback, error):
    def fail(*args, **kwargs):
        raise error("synthetic read error")
    monkeypatch.setattr(builtins, "open", fail)
    assert reader("synthetic") == fallback


@pytest.mark.parametrize("reader,value,fallback", READERS)
def test_corrupt_utf8_is_rejected_strictly(tmp_path, reader, value, fallback):
    path = tmp_path / "corrupt.txt"
    path.write_bytes(b"2.10.48\xff")
    assert reader(str(path)) == fallback


@pytest.mark.parametrize("reader", [wpp_runtime._read_json, update_keeps_server._read_package])
def test_package_readers_keep_the_dictionary_contract(tmp_path, reader):
    path = tmp_path / "package.json"
    path.write_bytes(b"\xef\xbb\xbf[]")
    assert reader(str(path)) == {}
