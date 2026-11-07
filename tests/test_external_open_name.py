import os

from core import external_open as eo
from core.transcription import temp_sweep


def test_real_name_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(eo.tempfile, "tempdir", str(tmp_path))
    path = eo.write_named_temp_file(b"x", "Relatório final.pdf")
    assert os.path.basename(path) == "Relatório final.pdf"
    assert open(path, "rb").read() == b"x"


def test_same_name_twice_does_not_collide(tmp_path, monkeypatch):
    monkeypatch.setattr(eo.tempfile, "tempdir", str(tmp_path))
    a = eo.write_named_temp_file(b"1", "a.txt")
    b = eo.write_named_temp_file(b"2", "a.txt")
    assert a != b and open(a, "rb").read() == b"1"


def test_invalid_characters_and_paths_are_cleaned():
    assert eo.safe_file_name("../../evil:name?.txt") == "evil_name_.txt"
    assert eo.safe_file_name("   ", "fb.bin") == "fb.bin"
    assert eo.safe_file_name("CON.txt") == "_CON.txt"
    assert len(eo.safe_file_name("a" * 400 + ".pdf")) <= 150
    assert eo.safe_file_name("a" * 400 + ".pdf").endswith(".pdf")


def test_swept_prefix():
    assert eo.PREFIX in temp_sweep.PREFIXES
