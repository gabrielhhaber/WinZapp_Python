"""Real pinned-handle mutations use CI-owned fixtures, never the local desktop."""
import os

import pytest

from core.storage_manager.fileops import ChangedFile, pinned
from core.storage_manager.scan import scan
from core.storage_manager.types import Root


pytestmark = pytest.mark.skipif(os.name != "nt" or os.environ.get("CI") != "true",
                                reason="native file provider acceptance is CI-only")


def fixture_entry(tmp_path):
    file = tmp_path / "source"
    file.write_bytes(b"fixture")
    return scan([Root("fixture", str(tmp_path), "compile_cache", managed=True)]).entries[0]


def test_native_disposition_removes_the_previewed_identity(tmp_path):
    entry = fixture_entry(tmp_path)
    with pinned(entry) as file:
        file.remove()
    assert not os.path.exists(entry.path)


def test_native_rename_preserves_content_without_overwrite(tmp_path):
    entry = fixture_entry(tmp_path)
    target = tmp_path / "held"
    with pinned(entry) as file:
        file.move(str(target))
    assert target.read_bytes() == b"fixture"
    target.write_bytes(b"new")
    replacement = fixture_entry(tmp_path)
    # Scan includes target too; select the source explicitly.
    replacement = next(e for e in scan([Root("fixture", str(tmp_path))]).entries if e.relative == "source")
    with pinned(replacement) as file:
        with pytest.raises(OSError):
            file.move(str(target))
    assert target.read_bytes() == b"new"


def test_native_provider_rejects_changed_metadata(tmp_path):
    entry = fixture_entry(tmp_path)
    with open(entry.path, "ab") as stream:
        stream.write(b"changed")
    with pytest.raises(ChangedFile):
        with pinned(entry):
            raise AssertionError("changed file was accepted")
