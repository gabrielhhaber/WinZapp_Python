"""build_neonize.py never exports or builds a workspace older than the
repository's patches (seen after pulling #732: export put the old 0002 back
and the DLL had no calls)."""
import os

import pytest

import build_neonize as bn


@pytest.fixture
def trees(tmp_path, monkeypatch):
    for name in bn.TREES:
        os.makedirs(tmp_path / name / ".git")
    monkeypatch.setattr(bn, "tree_path", lambda name: str(tmp_path / name))
    times = {}

    def commit_time(cwd, *args):
        key = "repo:" + os.path.basename(args[-1]) if cwd == bn.HERE else "work:" + os.path.basename(cwd)
        return times.get(key, 0)
    monkeypatch.setattr(bn, "_commit_time", commit_time)
    matching = set(bn.TREES)
    monkeypatch.setattr(bn, "matches_patches", lambda name: name in matching)
    return times, matching


def test_a_pull_newer_than_the_workspace_is_seen(trees):
    times, matching = trees
    times.update({"repo:neonize": 200, "work:neonize": 100})
    matching.discard("neonize")
    assert bn.moved_trees() == ["neonize"]


def test_a_workspace_that_already_holds_the_new_patches_is_fine(trees):
    times, _matching = trees
    times.update({"repo:neonize": 200, "work:neonize": 100})
    assert bn.moved_trees() == []


def test_own_commits_after_the_pull_are_exported(trees):
    times, matching = trees
    times.update({"repo:neonize": 100, "work:neonize": 200})
    matching.discard("neonize")
    assert bn.moved_trees() == []


def test_no_git_history_never_counts_as_moved(trees):
    _times, matching = trees
    matching.clear()
    assert bn.moved_trees() == []


def test_export_refuses_and_writes_nothing(trees, monkeypatch, tmp_path):
    times, matching = trees
    times.update({"repo:neonize": 200, "work:neonize": 100})
    matching.discard("neonize")
    written = []
    monkeypatch.setattr(bn, "format_patches", lambda name, target: written.append(name))
    with pytest.raises(SystemExit) as stop:
        bn.export()
    assert stop.value.code == bn.PATCHES_MOVED and written == []
