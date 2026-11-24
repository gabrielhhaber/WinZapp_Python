"""The whatsmeow tree comes from Go's read-only module cache: build_neonize.py
makes it writable without cutting the way into its folders (on macOS a folder
set to 0600 lost its search bit and the walk stopped at the first one,
PR #900's first macOS run)."""
import os
import shutil
import stat
import sys

import pytest

import build_neonize as bn

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")


def _read_only_tree(tmp_path):
    root = tmp_path / "whatsmeow"
    (root / "types" / "richresponse").mkdir(parents=True)
    (root / "types" / "richresponse" / "x.go").write_text("package x\n")
    (root / "go.mod").write_text("module x\n")
    for path in (root / "types" / "richresponse" / "x.go", root / "go.mod"):
        os.chmod(path, 0o444)
    for folder in (root / "types" / "richresponse", root / "types", root):
        os.chmod(folder, 0o555)
    return root


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


@posix_only
def test_the_tree_becomes_writable_and_every_folder_stays_searchable(tmp_path):
    root = _read_only_tree(tmp_path)
    bn.make_tree_writable(str(root))
    for folder in (root, root / "types", root / "types" / "richresponse"):
        assert _mode(folder) & 0o700 == 0o700
        assert _mode(folder) & 0o055 == 0o055          # what it had is kept
    for path in (root / "go.mod", root / "types" / "richresponse" / "x.go"):
        assert _mode(path) & 0o600 == 0o600
        assert not _mode(path) & stat.S_IXUSR          # a file does not become executable


@posix_only
def test_a_folder_left_without_its_search_bit_is_repaired(tmp_path):
    """What a failed run of the old code left behind (folders at 0600)."""
    folder = tmp_path / "types"
    folder.mkdir()
    os.chmod(folder, 0o600)
    bn.make_writable(str(folder))
    assert _mode(folder) & 0o700 == 0o700


def test_a_read_only_tree_can_be_removed(tmp_path):
    root = _read_only_tree(tmp_path)
    shutil.rmtree(root, onexc=bn._writable_retry)
    assert not root.exists()
