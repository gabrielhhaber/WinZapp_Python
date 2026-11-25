"""A Neonize build without Rust keeps the NetEq sidecar it already had.

On 2026-11-25 a local `all --install` without Rust wiped dist/ and the venv's
neonize folder: the sidecar copied from a CI artifact was gone, group calls
fell back to the Go mixer and nothing said so."""
import json
import os

import pytest

import build_neonize as bn
import build_neteq
import neonize_platform as platforms

WINDOWS = platforms.Target("windows", "amd64")


def _sidecar(folder, stamp=None):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / build_neteq.DLL).write_bytes(b"neteq dll")
    (folder / "NETEQ_NOTICES.txt").write_text("notices")
    (folder / "neteq_rust_licenses" / "licenses").mkdir(parents=True)
    (folder / "neteq_rust_licenses" / "licenses" / "MIT").write_text("mit")
    if stamp is not None:
        (folder / build_neteq.STAMP).write_text(stamp + "\n")


def _source(tmp_path, lib=b"fn x() {}"):
    source = tmp_path / "call_neteq"
    (source / "src").mkdir(parents=True)
    for name in ("Cargo.toml", "Cargo.lock", "THIRD_PARTY_NOTICES.txt"):
        (source / name).write_text(name)
    (source / "src" / "lib.rs").write_bytes(lib)
    return source


def test_a_folder_without_the_dll_holds_no_sidecar(tmp_path):
    (tmp_path / "NETEQ_NOTICES.txt").write_text("orphan notices")
    assert build_neteq.read_sidecar(tmp_path) == {}
    assert build_neteq.read_sidecar(tmp_path / "missing") == {}


def test_the_kept_sidecar_comes_back_with_its_notices_and_licenses(tmp_path):
    digest = build_neteq.source_digest(_source(tmp_path))
    _sidecar(tmp_path / "old", stamp=digest)
    kept = build_neteq.read_sidecar(tmp_path / "old")
    assert build_neteq.restore_sidecar(tmp_path / "new", kept, digest) is True
    assert (tmp_path / "new" / build_neteq.DLL).read_bytes() == b"neteq dll"
    assert (tmp_path / "new" / "neteq_rust_licenses" / "licenses" / "MIT").read_text() == "mit"


def test_a_sidecar_built_from_other_sources_stays_out(tmp_path, capsys):
    old = build_neteq.source_digest(_source(tmp_path / "a", lib=b"old"))
    new = build_neteq.source_digest(_source(tmp_path / "b", lib=b"new"))
    assert old != new
    _sidecar(tmp_path / "old", stamp=old)
    assert build_neteq.restore_sidecar(
        tmp_path / "new", build_neteq.read_sidecar(tmp_path / "old"), new) is False
    assert not (tmp_path / "new").exists()
    assert "other sources" in capsys.readouterr().out


def test_an_unstamped_sidecar_copied_from_ci_is_kept_with_a_warning(tmp_path, capsys):
    _sidecar(tmp_path / "old")
    assert build_neteq.restore_sidecar(
        tmp_path / "new", build_neteq.read_sidecar(tmp_path / "old"), "any") is True
    assert "no source stamp" in capsys.readouterr().out


def test_the_digest_ignores_line_endings(tmp_path):
    unix = _source(tmp_path / "unix", lib=b"a\nb\n")
    windows = _source(tmp_path / "windows", lib=b"a\r\nb\r\n")
    assert build_neteq.source_digest(unix) == build_neteq.source_digest(windows)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    tree = tmp_path / "tree"
    (tree / "neonize").mkdir(parents=True)
    (tree / "neonize" / "__init__.py").write_text("")
    monkeypatch.setattr(bn, "TARGET", WINDOWS)
    monkeypatch.setattr(bn, "DIST", str(dist))
    monkeypatch.setattr(bn, "tree_path", lambda name: str(tree))
    monkeypatch.setattr(bn, "check_compilers", lambda: None)
    monkeypatch.setattr(bn, "_uncommitted", lambda tree: True)
    monkeypatch.setattr(bn, "git", lambda tree, *args, capture=False: "0.12.4")
    monkeypatch.setattr(build_neteq, "build", lambda package, system: False)   # no Rust

    def go_build(cmd, cwd=None, env=None, capture=False):
        with open(cmd[cmd.index("-o") + 1], "wb") as library:
            library.write(b"go dll")
    monkeypatch.setattr(bn, "run", go_build)
    return dist


INFO = {"whatsmeow": {"version": "v0"}, "meowcaller": {"commit": "c0"}}


def test_a_build_without_rust_keeps_the_previous_dist_sidecar(workspace):
    _sidecar(workspace / "neonize")
    bn.build(INFO)
    package = workspace / "neonize"
    assert (package / build_neteq.DLL).read_bytes() == b"neteq dll"
    assert (package / platforms.library_name(WINDOWS)).read_bytes() == b"go dll"
    sums = (workspace / "SHA256SUMS.txt").read_text()
    assert f"neonize/{build_neteq.DLL}" in sums       # it is part of what was built


def test_a_first_build_without_rust_simply_has_no_sidecar(workspace):
    bn.build(INFO)
    assert not (workspace / "neonize" / build_neteq.DLL).exists()


def test_install_keeps_the_venv_sidecar_when_the_build_has_none(tmp_path, monkeypatch):
    dist, venv, site = tmp_path / "dist", tmp_path / "venv", tmp_path / "site"
    library = platforms.library_name(WINDOWS)
    (dist / "neonize").mkdir(parents=True)
    (dist / "neonize" / library).write_bytes(b"new go dll")
    (dist / "BUILD_INFO.json").write_text(json.dumps({"identity": ""}))
    _sidecar(site / "neonize")
    (site / "neonize" / library).write_bytes(b"old go dll")
    python = platforms.venv_python(str(venv), WINDOWS)
    os.makedirs(os.path.dirname(python))
    open(python, "w").close()
    monkeypatch.setattr(bn, "TARGET", WINDOWS)
    monkeypatch.setattr(bn, "DIST", str(dist))
    monkeypatch.setattr(bn, "run", lambda *a, **k: str(site))
    bn.install(str(venv))
    assert (site / "neonize" / library).read_bytes() == b"new go dll"
    assert (site / "neonize" / build_neteq.DLL).read_bytes() == b"neteq dll"


def test_install_takes_the_build_sidecar_over_the_installed_one(tmp_path, monkeypatch):
    dist, venv, site = tmp_path / "dist", tmp_path / "venv", tmp_path / "site"
    library = platforms.library_name(WINDOWS)
    _sidecar(site / "neonize")
    (dist / "neonize").mkdir(parents=True)
    (dist / "neonize" / library).write_bytes(b"go dll")
    (dist / "neonize" / build_neteq.DLL).write_bytes(b"fresh neteq")
    (dist / "BUILD_INFO.json").write_text(json.dumps({"identity": ""}))
    python = platforms.venv_python(str(venv), WINDOWS)
    os.makedirs(os.path.dirname(python))
    open(python, "w").close()
    monkeypatch.setattr(bn, "TARGET", WINDOWS)
    monkeypatch.setattr(bn, "DIST", str(dist))
    monkeypatch.setattr(bn, "run", lambda *a, **k: str(site))
    bn.install(str(venv))
    assert (site / "neonize" / build_neteq.DLL).read_bytes() == b"fresh neteq"
    assert not (site / "neonize" / "NETEQ_NOTICES.txt").exists()
