"""bridge_bundle.py: the Neonize bridge inside WinZapp.app. Nothing is
frozen here: the freezer and the tools are faked."""
import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path[:0] = [os.path.join(ROOT, "macos"), os.path.join(ROOT, "client")]

import bridge_bundle  # noqa: E402
from winzapp_mac import paths_mac  # noqa: E402


def test_the_dylib_has_the_name_neonize_loads_on_each_mac():
    assert bridge_bundle.library_name("arm64") == "neonize-darwin-arm64.dylib"
    assert bridge_bundle.library_name("x86_64") == "neonize-darwin-amd64.dylib"


def test_the_app_finds_the_bridge_where_the_build_puts_it(tmp_path):
    app = tmp_path / "WinZapp.app"
    meipass = app / "Contents" / "Frameworks"
    assert paths_mac.bundled_resource(str(meipass), "neonize_bridge") == bridge_bundle.bundled_dir(str(app))
    assert paths_mac.BRIDGE_FOLDER == bridge_bundle.FOLDER


def test_the_executable_is_the_one_the_client_starts():
    sys.path.insert(0, os.path.join(ROOT, "client"))
    from core import neonize_bridge_process
    if sys.platform != "win32":
        assert neonize_bridge_process.PACKAGED_EXE == bridge_bundle.EXECUTABLE


class _Freezer:
    """bridge_build stand-in: build() makes a frozen bridge, stage() copies it."""

    def __init__(self, tmp_path, arch="arm64", dylib=True, executable=True):
        self.tmp_path, self.arch, self.dylib, self.executable = tmp_path, arch, dylib, executable

    def build(self, root, out, base_python, log):
        frozen = Path(out) / "dist" / "neonize_bridge"
        (frozen / "_internal" / "neonize").mkdir(parents=True)
        exe = frozen / bridge_bundle.EXECUTABLE
        exe.write_text("#!")
        if self.executable:
            exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
        if self.dylib:
            (frozen / "_internal" / "neonize" / bridge_bundle.library_name(self.arch)).write_text("")
        return frozen

    def stage(self, frozen, staging):
        import shutil
        shutil.copytree(frozen, Path(staging) / "neonize_bridge", symlinks=True)
        return 2


def _bundle(tmp_path, freezer, which=lambda name: "/usr/local/bin/go"):
    app = tmp_path / "WinZapp.app"
    (app / "Contents" / "Resources").mkdir(parents=True)
    return bridge_bundle.bundle(ROOT, str(app), str(tmp_path / "build"), "arm64",
                                log=lambda *_: None, which=which, freezer=freezer)


@pytest.mark.skipif(sys.platform == "win32", reason="execute bits")
def test_a_working_bridge_lands_in_resources(tmp_path):
    bridge = _bundle(tmp_path, _Freezer(tmp_path))
    assert bridge == str(tmp_path / "WinZapp.app" / "Contents" / "Resources" / "neonize_bridge")
    assert bridge_bundle.problems(bridge, "arm64") == []


def test_without_go_the_build_stops_and_says_how_to_skip_the_bridge(tmp_path):
    with pytest.raises(SystemExit, match="--no-bridge"):
        _bundle(tmp_path, _Freezer(tmp_path), which=lambda name: None)


def test_a_bridge_without_the_dylib_for_this_mac_stops_the_build(tmp_path):
    """An Intel dylib in an Apple Silicon app would offer the engine and fail
    at its first start."""
    with pytest.raises(SystemExit, match="neonize-darwin-arm64.dylib is missing"):
        _bundle(tmp_path, _Freezer(tmp_path, arch="x86_64"))


@pytest.mark.skipif(sys.platform == "win32", reason="execute bits")
def test_a_bridge_that_is_not_executable_stops_the_build(tmp_path):
    with pytest.raises(SystemExit, match="not executable"):
        _bundle(tmp_path, _Freezer(tmp_path, executable=False))


class _Process:
    """subprocess.Popen stand-in for the background freeze."""

    pid = 4242

    def __init__(self, code=0, running=False):
        self.code, self.running, self.terminated = code, running, False

    def wait(self, timeout=None):
        self.running = False
        return self.code

    def poll(self):
        return None if self.running else self.code

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True


def test_the_freeze_runs_this_script_in_a_child_with_its_output_in_a_log(tmp_path):
    started = {}

    def popen(command, stdout, stderr, env, start_new_session):
        started.update(command=command, log=stdout.name, env=env, session=start_new_session)
        return _Process()

    registered = []
    freezing = bridge_bundle.start(str(tmp_path / "build"), python="/py", popen=popen,
                                   at_exit=lambda *call: registered.append(call))
    assert started["command"] == ["/py", os.path.abspath(bridge_bundle.__file__), "freeze",
                                  str(tmp_path / "build")]
    assert started["log"] == freezing.log_path == str(tmp_path / "build" / "bridge.log")
    assert freezing.frozen == str(tmp_path / "build" / "bridge" / "dist" / "neonize_bridge")
    # its own process group, so stop() reaches the Go build and PyInstaller
    assert started["session"] is True
    assert registered == [(bridge_bundle.stop, freezing.process)]


def test_the_freeze_has_its_own_pyinstaller_cache(tmp_path):
    """PyInstaller's macOS binary cache is shared per Python and unlocked; the
    app's freeze runs at the same time."""
    env = {}
    bridge_bundle.start(str(tmp_path / "build"), popen=lambda *a, **kw: env.update(kw["env"]) or _Process(),
                        at_exit=lambda *call: None)
    cache = env["PYINSTALLER_CONFIG_DIR"]
    assert cache == str(tmp_path / "build" / "bridge-pyinstaller")
    # outside bridge/, which every freeze deletes
    assert not cache.startswith(str(tmp_path / "build" / "bridge") + os.sep)


@pytest.mark.skipif(sys.platform == "win32", reason="execute bits")
def test_a_finished_freeze_lands_in_resources(tmp_path):
    freezer = _Freezer(tmp_path)
    frozen = freezer.build(ROOT, str(tmp_path / "build" / "bridge"), None, None)
    app = tmp_path / "WinZapp.app"
    (app / "Contents" / "Resources").mkdir(parents=True)
    freezing = bridge_bundle.Freezing(_Process(), str(tmp_path / "bridge.log"), str(frozen))
    bridge = bridge_bundle.finish(freezing, str(app), "arm64", log=lambda *_: None, freezer=freezer)
    assert bridge_bundle.problems(bridge, "arm64") == []


def test_a_failed_freeze_stops_the_build_and_shows_its_output(tmp_path):
    log_path = tmp_path / "bridge.log"
    log_path.write_text("go: build failed\n")
    shown = []
    freezing = bridge_bundle.Freezing(_Process(code=2), str(log_path), str(tmp_path / "none"))
    with pytest.raises(SystemExit, match="exit 2"):
        bridge_bundle.finish(freezing, str(tmp_path / "WinZapp.app"), "arm64",
                             log=shown.append, freezer=_Freezer(tmp_path))
    assert "go: build failed" in shown[0]


def test_a_build_that_stops_early_ends_the_freeze_and_its_children():
    running, done = _Process(running=True), _Process()
    signals = []
    bridge_bundle.stop(running, killpg=lambda pid, sig: signals.append((pid, sig)))
    bridge_bundle.stop(done, killpg=lambda pid, sig: signals.append((pid, sig)))
    assert signals == [(4242, bridge_bundle.signal.SIGTERM)]


def test_the_child_only_accepts_freeze_and_a_folder():
    with pytest.raises(SystemExit, match="usage"):
        bridge_bundle.main(["bundle"])
