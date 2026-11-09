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
    monkeypatch.setattr(bn, "_prepared_time",
                        lambda tree: times.get("work:" + os.path.basename(tree), 0))
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


def test_a_new_commit_on_a_stale_workspace_does_not_hide_it(tmp_path, monkeypatch):
    # 2026-11-08: the workspace from before #732 got a new commit, newer than
    # every patch; export then rewrote 0002 without the call hook.
    import subprocess
    tree = tmp_path / "neonize"
    tree.mkdir()

    def git(*args, when):
        env = dict(os.environ, GIT_COMMITTER_DATE=f"@{when} +0000", GIT_AUTHOR_DATE=f"@{when} +0000")
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args], cwd=tree,
                       check=True, capture_output=True, env=env)
    git("init", "-q", "-b", bn.BRANCH, when=1_700_001_000)
    git("commit", "-q", "--allow-empty", "-m", "upstream", when=1_700_001_000)
    git("tag", bn.BASE, when=1_700_001_000)
    git("commit", "-q", "--allow-empty", "-m", "0001 applied by prepare", when=1_700_002_000)
    git("commit", "-q", "--allow-empty", "-m", "a new change", when=1_700_009_000)
    assert bn._prepared_time(str(tree)) == 1_700_002_000

    monkeypatch.setattr(bn, "tree_path", lambda name: str(tmp_path / name))
    monkeypatch.setattr(bn, "_commit_time", lambda cwd, *args: 1_700_005_000 if cwd == bn.HERE else 0)
    monkeypatch.setattr(bn, "matches_patches", lambda name: False)
    assert bn.patches_moved("neonize")
