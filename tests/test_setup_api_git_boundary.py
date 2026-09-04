from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_partial_nested_git_directory_cannot_escape_to_parent_repository():
    source = (ROOT / "setup_api.py").read_text(encoding="utf-8")
    assert 'os.path.isfile(os.path.join(git_dir, "HEAD"))' in source
    assert 'os.path.isfile(os.path.join(git_dir, "config"))' in source
    assert "if tag and managed_git_clone:" in source
    assert "elif not tag and managed_git_clone:" in source
