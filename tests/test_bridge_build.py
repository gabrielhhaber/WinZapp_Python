"""Freezing the Neonize bridge for a compiled build: the rules, without PyInstaller."""

import re
from pathlib import Path

import pytest

from winzapp_tools import bridge_build as bb

ROOT = Path(__file__).resolve().parent.parent
HASH = "ab" * 32


def _repo(tmp_path, venv="current"):
    """venv: "current" (marker matches), "stale" (marker differs), "none"."""
    root = tmp_path / "repo"
    bridge = root / "neonize_bridge"
    bridge.mkdir(parents=True)
    (bridge / "server.py").write_text("")
    (bridge / "prepare.py").write_text("")
    # the identity the checkout calls for (the real one hashes the manifest and patches)
    (bridge / "neonize_upstream.py").write_text(
        f'MARKER = ".neonize-build-sha256"\ndef checkout_identity(folder):\n    return "{HASH}"\n')
    (bridge / "requirements-build.lock").write_text("")
    if venv != "none":
        (bridge / ".venv" / "Scripts").mkdir(parents=True)
        (bridge / ".venv" / "Scripts" / "python.exe").write_text("")
        (bridge / ".venv" / ".neonize-build-sha256").write_text(HASH if venv == "current" else "old")
    return root


class _Runner:
    """Records the commands; prepare.py makes the venv and its marker, PyInstaller
    drops the exe."""

    def __init__(self, root, produce_exe=True):
        self.root, self.produce_exe, self.calls = root, produce_exe, []

    def __call__(self, command, check=True, cwd=None):
        self.calls.append((list(command), cwd))
        if command[-1].endswith("prepare.py"):
            venv = Path(self.root) / "neonize_bridge" / ".venv"
            (venv / "Scripts").mkdir(parents=True, exist_ok=True)
            (venv / "Scripts" / "python.exe").write_text("")
            (venv / ".neonize-build-sha256").write_text(HASH)
        if "PyInstaller" in command and self.produce_exe:
            out = Path(command[command.index("--distpath") + 1]) / "neonize_bridge"
            out.mkdir(parents=True)
            (out / bb.EXE_NAME).write_text("exe")
            (out / "_internal").mkdir()
            (out / "_internal" / "neonize.dll").write_text("dll")


def _build(root, tmp_path, run, **kw):
    return bb.build(root, tmp_path / "out", run=run, log=lambda *_: None, **kw)


def test_the_command_freezes_server_py_with_what_pyinstaller_cannot_see(tmp_path):
    root = _repo(tmp_path)
    command = bb.freeze_command(bb.venv_python(root), root, tmp_path / "out")
    assert command[1:3] == ["-m", "PyInstaller"] and "--onedir" in command
    assert command[command.index("--name") + 1] == "neonize_bridge"
    assert command[command.index("--collect-all") + 1] == "neonize"
    assert "engineio.async_drivers.aiohttp" in command
    assert command[-1].endswith("server.py")
    # the bridge's modules are found next to server.py
    assert command[command.index("--paths") + 1] == str(bb.bridge_dir(root))


def test_the_build_tools_come_from_the_hashed_lock_inside_the_bridge_environment(tmp_path):
    root = _repo(tmp_path)
    run = _Runner(root)
    _build(root, tmp_path, run)
    install = next(c for c, _ in run.calls if "pip" in c)
    assert install[0] == str(bb.venv_python(root))
    assert "--require-hashes" in install
    assert install[install.index("-r") + 1] == str(bb.build_lock(root))


def test_a_missing_bridge_environment_is_prepared_first(tmp_path):
    root = _repo(tmp_path, venv="none")
    run = _Runner(root)
    result = _build(root, tmp_path, run, base_python="build-python")
    assert run.calls[0][0][0] == "build-python" and run.calls[0][0][1].endswith("prepare.py")
    assert (result / bb.EXE_NAME).is_file()


def test_an_environment_built_for_another_neonize_build_is_prepared_again(tmp_path):
    root = _repo(tmp_path, venv="stale")
    run = _Runner(root)
    _build(root, tmp_path, run)
    assert any(c[-1].endswith("prepare.py") for c, _ in run.calls)


def test_a_current_environment_is_not_prepared_again(tmp_path):
    root = _repo(tmp_path)
    run = _Runner(root)
    _build(root, tmp_path, run)
    assert not any(c[-1].endswith("prepare.py") for c, _ in run.calls)


def test_an_environment_that_prepare_cannot_bring_up_to_date_is_a_failure(tmp_path):
    root = _repo(tmp_path, venv="stale")
    with pytest.raises(bb.BridgeBuildError):
        _build(root, tmp_path, lambda command, check=True, cwd=None: None)


def test_a_build_without_the_exe_is_a_failure_not_a_silent_skip(tmp_path):
    root = _repo(tmp_path)
    with pytest.raises(bb.BridgeBuildError):
        _build(root, tmp_path, _Runner(root, produce_exe=False))


def test_staging_copies_the_exe_and_its_runtime_under_neonize_bridge(tmp_path):
    root = _repo(tmp_path)
    frozen = _build(root, tmp_path, _Runner(root))
    staging = tmp_path / "staging"
    (staging / "neonize_bridge").mkdir(parents=True)
    (staging / "neonize_bridge" / "stale.txt").write_text("old")
    assert bb.stage(frozen, staging) == 2
    assert (staging / "neonize_bridge" / bb.EXE_NAME).is_file()
    assert (staging / "neonize_bridge" / "_internal" / "neonize.dll").is_file()
    assert not (staging / "neonize_bridge" / "stale.txt").exists()


# -- the locks that decide what gets frozen into every release ---------------

def _pins(path):
    return dict(re.findall(r"^([A-Za-z0-9_.\-]+)==([^\s\\]+)", path.read_text(encoding="utf-8"), re.M))


def _requirements_for(windows):
    """requirements.txt's pins whose environment marker holds on Windows (or not)."""
    pins = {}
    for line in (ROOT / "neonize_bridge" / "requirements.txt").read_text(encoding="utf-8").splitlines():
        found = re.match(r"^([A-Za-z0-9_.\-]+)==([^\s;]+)\s*(?:;\s*(.*))?$", line)
        if not found:
            continue
        marker = (found.group(3) or "").replace(" ", "")
        if marker and ("=='win32'" in marker) != windows:
            continue
        pins[found.group(1)] = found.group(2)
    return pins


@pytest.mark.parametrize("lock_name, windows", [("requirements.lock", True),
                                                ("requirements-macos.lock", False)])
def test_the_runtime_lock_covers_the_requirements_with_hashes(lock_name, windows):
    lock = ROOT / "neonize_bridge" / lock_name
    locked = {k.lower(): v for k, v in _pins(lock).items()}
    wanted = _requirements_for(windows)
    assert ("python-magic-bin" in wanted) == windows and ("python-magic" in wanted) != windows
    for name, version in wanted.items():
        assert locked[name.lower()] == version
    text = lock.read_text(encoding="utf-8")
    assert text.count("--hash=sha256:") >= len(locked)
    assert "--index-url" not in text and "--extra-index-url" not in text


def test_the_build_tools_are_the_ones_the_client_build_uses():
    pins = {k.lower(): v for k, v in _pins(ROOT / "neonize_bridge" / "requirements-build.lock").items()}
    client = {k.lower(): v for k, v in _pins(ROOT / "requirements.txt").items()}
    assert pins["pyinstaller"] == client["pyinstaller"]
    assert pins["pyinstaller-hooks-contrib"] == client["pyinstaller-hooks-contrib"]
    assert "--hash=sha256:" in (ROOT / "neonize_bridge" / "requirements-build.lock").read_text(encoding="utf-8")


def test_prepare_installs_from_the_lock_and_leaves_its_marker():
    source = (ROOT / "neonize_bridge" / "prepare.py").read_text(encoding="utf-8")
    assert '"--require-hashes"' in source and "requirements.lock" in source
    assert "MARKER" in source


def test_the_expected_identity_is_the_checkouts_manifest_and_patches():
    import importlib.util
    spec = importlib.util.spec_from_file_location("nu", ROOT / "neonize_bridge" / "neonize_upstream.py")
    nu = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(nu)
    assert bb.expected_build_identity(ROOT) == nu.checkout_identity(str(ROOT / "neonize_bridge"))
    assert re.fullmatch(r"[0-9a-f]{64}", bb.expected_build_identity(ROOT))
