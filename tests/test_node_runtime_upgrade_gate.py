"""The portable Node.js runtime is replaced only when it is really broken.

node_runtime_needs_download() decides whether WinZapp throws away the whole
client/node/ folder and re-downloads it behind a modal dialog. It has to say
yes for a node.exe older than the homologated build (npm only *warns* on an
engines mismatch, then WPPConnect fails much later and far less legibly), and
for a versioned node.exe sitting on a broken npm tree — but it must not say
yes merely because a probe took too long. A slow first launch after boot (cold
file cache, an antivirus inspecting node.exe) used to be swallowed by a
blanket `except Exception` that then reported "unhealthy", so a perfectly good
runtime was discarded.

It lives module-level in main.py for exactly this: MainWindow is a wx.Frame,
and the gate used to be reachable only by scanning main.py's source for
substrings — which asserts the code is spelled a certain way, not that it
decides anything.
"""

import subprocess
from pathlib import Path

import pytest

from main import (
    NPM_HEALTH_MARKER_NAME,
    node_runtime_needs_download,
    npm_health_recorded,
    record_npm_health,
)
from node_download_config import NODE_VERSION


ROOT = Path(__file__).resolve().parents[1]


class _Completed:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


class _Runtime:
    """A portable Node.js install on disk, plus a scriptable subprocess.run.

    node.exe answers `--version` with `node_version`; the npm probe answers
    with `npm_result`, which may also be an exception instance to raise.
    """

    def __init__(self, tmp_path, node_version=NODE_VERSION, npm_result=None):
        self.node_exe = str(tmp_path / "node.exe")
        self.npm_cli = str(tmp_path / "npm-cli.js")
        self.marker_path = str(tmp_path / NPM_HEALTH_MARKER_NAME)
        Path(self.node_exe).write_text("", encoding="utf-8")
        Path(self.npm_cli).write_text("", encoding="utf-8")
        self.node_version = node_version
        self.npm_result = npm_result if npm_result is not None else _Completed(0)
        self.npm_probes = 0

    def run(self, cmd, **kwargs):
        if cmd[1] == "--version":
            return _Completed(0, f"v{self.node_version}\n")
        self.npm_probes += 1
        if isinstance(self.npm_result, BaseException):
            raise self.npm_result
        return self.npm_result

    def decide(self):
        return node_runtime_needs_download(
            self.node_exe, self.npm_cli, self.marker_path
        )


@pytest.fixture
def patched_run(monkeypatch):
    def _install(runtime):
        import main

        monkeypatch.setattr(main.subprocess, "run", runtime.run)
        return runtime

    return _install


class TestNpmProbe:
    def test_a_timed_out_npm_probe_keeps_the_installed_runtime(
        self, tmp_path, patched_run
    ):
        """The regression this gate exists for. An unknown answer changes
        nothing: the runtime stays, the marker stays unwritten, and the next
        launch asks again."""
        runtime = patched_run(
            _Runtime(
                tmp_path,
                npm_result=subprocess.TimeoutExpired(cmd="npm", timeout=10),
            )
        )

        needs_download, installed = runtime.decide()

        assert needs_download is False
        assert installed == NODE_VERSION
        assert not Path(runtime.marker_path).exists()

    def test_an_npm_that_cannot_be_launched_keeps_the_installed_runtime(
        self, tmp_path, patched_run
    ):
        runtime = patched_run(
            _Runtime(tmp_path, npm_result=OSError("access denied"))
        )

        assert runtime.decide()[0] is False

    def test_a_failing_npm_probe_replaces_the_runtime(self, tmp_path, patched_run):
        """A probe that *does* answer is believed in both directions."""
        runtime = patched_run(_Runtime(tmp_path, npm_result=_Completed(1)))

        assert runtime.decide()[0] is True
        assert not Path(runtime.marker_path).exists()

    def test_a_missing_npm_cli_replaces_the_runtime(self, tmp_path, patched_run):
        runtime = patched_run(_Runtime(tmp_path))
        Path(runtime.npm_cli).unlink()

        assert runtime.decide()[0] is True
        assert runtime.npm_probes == 0

    def test_a_healthy_runtime_is_probed_once_and_remembered(
        self, tmp_path, patched_run
    ):
        """Booting npm costs 1-2 s on the UI thread, on the critical path of
        every launch — which is the whole reason for the marker."""
        runtime = patched_run(_Runtime(tmp_path))

        assert runtime.decide() == (False, NODE_VERSION)
        assert runtime.decide() == (False, NODE_VERSION)
        assert runtime.npm_probes == 1


class TestNodeVersionGate:
    def test_a_missing_node_exe_needs_a_download(self, tmp_path):
        needs_download, installed = node_runtime_needs_download(
            str(tmp_path / "node.exe"),
            str(tmp_path / "npm-cli.js"),
            str(tmp_path / NPM_HEALTH_MARKER_NAME),
        )

        assert needs_download is True
        assert installed == ""

    def test_an_older_node_is_replaced_before_npm_is_even_probed(
        self, tmp_path, patched_run
    ):
        runtime = patched_run(_Runtime(tmp_path, node_version="18.0.0"))

        assert runtime.decide() == (True, "18.0.0")
        assert runtime.npm_probes == 0

    def test_an_unreadable_node_version_is_replaced(self, tmp_path, patched_run):
        """`Version("")` raises; the old code reported that as "could not
        validate" and downloaded, which is still the right answer."""
        runtime = patched_run(_Runtime(tmp_path, node_version=""))

        assert runtime.decide()[0] is True


class TestNpmHealthMarker:
    def test_a_marker_from_another_node_build_is_not_an_answer(self, tmp_path):
        marker = tmp_path / NPM_HEALTH_MARKER_NAME
        record_npm_health(str(marker), "22.22.2")

        assert npm_health_recorded(str(marker), "22.22.2") is True
        assert npm_health_recorded(str(marker), "24.0.0") is False

    def test_a_missing_marker_is_not_an_answer(self, tmp_path):
        assert npm_health_recorded(str(tmp_path / "absent"), "22.22.2") is False

    def test_a_corrupt_marker_is_not_an_answer_and_does_not_raise(self, tmp_path):
        """UnicodeDecodeError is a ValueError, not an OSError. The call site
        sits outside any local try block, so an escaping exception climbs to
        MainWindow.__init__'s blanket handler and skips ensure_wpp_version()
        *and* ensure_wpp_running(): the app opens, the Node server never
        starts, and nothing is said out loud."""
        marker = tmp_path / NPM_HEALTH_MARKER_NAME
        marker.write_bytes(b"\xff\xfe\x00broken\x80")

        assert npm_health_recorded(str(marker), "22.22.2") is False

    def test_an_unwritable_marker_only_costs_another_probe(self, tmp_path):
        """Best effort by design: an install directory that cannot be written
        to just re-probes next launch, which is slow but never wrong."""
        record_npm_health(str(tmp_path / "no-such-dir" / "marker"), "22.22.2")


def test_node_upgrade_replaces_instead_of_overlaying_the_npm_tree():
    source = (ROOT / "client/ui/dialogs/node_download.py").read_text(
        encoding="utf-8"
    )
    extract = source[source.index("def _extract_node") : source.index("def _run_download")]

    assert 'tempfile.mkdtemp(prefix=".node-staging-"' in extract
    assert "os.replace(node_dir, backup_dir)" in extract
    assert "os.replace(staging_dir, node_dir)" in extract


def test_setup_rejects_a_versioned_but_broken_portable_npm():
    source = (ROOT / "setup_api.py").read_text(encoding="utf-8")

    assert '[node_bin, npm_bin, "install", "--help"]' in source
    assert "Portable npm is unhealthy" in source
