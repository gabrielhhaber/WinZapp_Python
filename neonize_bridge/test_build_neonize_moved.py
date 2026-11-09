"""build_neonize.py never exports or builds a workspace older than the
repository's patches (seen after pulling #732: export put the old 0002 back
and the DLL had no calls), and never refuses a workspace that only adds to them."""
import os

import pytest

import build_neonize as bn


@pytest.fixture
def trees(tmp_path, monkeypatch):
    """patch file -> when the repository last changed it; tree -> when it was
    prepared; tree -> the files an export would overwrite."""
    for name in bn.TREES:
        os.makedirs(tmp_path / name / ".git")
    monkeypatch.setattr(bn, "tree_path", lambda name: str(tmp_path / name))
    changed, prepared, overwritten = {}, {}, {}
    monkeypatch.setattr(bn, "_commit_time",
                        lambda cwd, *args: changed.get(os.path.basename(args[-1]), 0))
    monkeypatch.setattr(bn, "_prepared_time",
                        lambda tree: prepared.get(os.path.basename(tree), 0))
    monkeypatch.setattr(bn, "overwritten_patches", lambda name: overwritten.get(name, []))
    return changed, prepared, overwritten


def test_a_patch_pulled_after_the_prepare_is_not_overwritten(trees):
    changed, prepared, overwritten = trees
    changed["0002-calls.patch"] = 200          # #732, pulled after the prepare
    prepared["neonize"] = 100
    overwritten["neonize"] = ["0002-calls.patch"]
    assert bn.moved_trees() == ["neonize"]


def test_new_patches_and_more_commits_on_them_export(trees):
    # 0018 committed to the repository after the prepare, then a new commit:
    # the export only adds 0019 and leaves every file as it is
    changed, prepared, _overwritten = trees
    changed["0018-media.patch"] = 200
    prepared["neonize"] = 100
    assert bn.moved_trees() == []


def test_the_workspaces_own_edit_of_an_older_patch_exports(trees):
    changed, prepared, overwritten = trees
    changed["0005-raw.patch"] = 50             # last changed before the prepare
    prepared["neonize"] = 100
    overwritten["neonize"] = ["0005-raw.patch"]
    assert bn.moved_trees() == []


def test_no_git_history_never_counts_as_moved(trees):
    changed, prepared, overwritten = trees
    changed["0002-calls.patch"] = 200
    overwritten["neonize"] = ["0002-calls.patch"]
    assert bn.moved_trees() == []              # never prepared: time 0


def test_export_refuses_and_writes_nothing(trees, monkeypatch):
    changed, prepared, overwritten = trees
    changed["0002-calls.patch"], prepared["neonize"] = 200, 100
    overwritten["neonize"] = ["0002-calls.patch"]
    written = []
    monkeypatch.setattr(bn, "format_patches", lambda name, target: written.append(name))
    with pytest.raises(SystemExit) as stop:
        bn.export()
    assert stop.value.code == bn.PATCHES_MOVED and written == []


def test_the_overwritten_files_are_the_ones_the_export_changes_or_drops(tmp_path, monkeypatch):
    patches = tmp_path / "patches" / "neonize"
    patches.mkdir(parents=True)
    (patches / "0001-same.patch").write_bytes(b"same")
    (patches / "0002-old.patch").write_bytes(b"old")
    (patches / "0003-gone.patch").write_bytes(b"gone")
    monkeypatch.setattr(bn, "PATCHES", str(tmp_path / "patches"))
    monkeypatch.setattr(bn, "WORK", str(tmp_path / "work"))

    def export_to(name, target):
        os.makedirs(target, exist_ok=True)
        for file, data in (("0001-same.patch", b"same"), ("0002-old.patch", b"new"),
                           ("0004-added.patch", b"added")):
            with open(os.path.join(target, file), "wb") as f:
                f.write(data)
    monkeypatch.setattr(bn, "format_patches", export_to)
    assert bn.overwritten_patches("neonize") == ["0002-old.patch", "0003-gone.patch"]


def test_a_new_commit_on_a_stale_workspace_does_not_hide_it(tmp_path, monkeypatch):
    # 2026-11-08: the workspace from before #732 got a new commit, newer than
    # every patch; export then rewrote 0002 without the call hook.
    import subprocess
    tree = tmp_path / "neonize"
    tree.mkdir()

    def git(*args, when):
        env = dict(os.environ, GIT_COMMITTER_DATE=f"@{when} +0000", GIT_AUTHOR_DATE=f"@{when} +0000")
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                        *args], cwd=tree, check=True, capture_output=True, env=env)
    git("init", "-q", "-b", bn.BRANCH, when=1_700_001_000)
    git("commit", "-q", "--allow-empty", "-m", "upstream", when=1_700_001_000)
    git("tag", bn.BASE, when=1_700_001_000)
    git("commit", "-q", "--allow-empty", "-m", "0001 applied by prepare", when=1_700_002_000)
    git("commit", "-q", "--allow-empty", "-m", "a new change", when=1_700_009_000)
    assert bn._prepared_time(str(tree)) == 1_700_002_000

    monkeypatch.setattr(bn, "tree_path", lambda name: str(tmp_path / name))
    monkeypatch.setattr(bn, "_commit_time", lambda cwd, *args: 1_700_005_000 if cwd == bn.HERE else 0)
    monkeypatch.setattr(bn, "overwritten_patches", lambda name: ["0002-calls.patch"])
    assert bn.patches_moved("neonize")
