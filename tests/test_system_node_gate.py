"""setup_api.py refuses a system Node.js it has no evidence about.

setup_api.py prefers client/node/node.exe and silently fell back to whatever
`node` was on PATH when that folder was absent — which is every fresh
checkout, since only build.py and CI provision it. Node 26 turns that
fallback into a dead end that never names Node: puppeteer's extract-zip@2.0.1
leaves the promisified stream.pipeline of the first multi-chunk zip entry
unsettled, so the Chromium download stops two files in, throws nothing and
resolves nothing; puppeteer then finds a browser folder with no chrome.exe,
refuses to re-download, and every later run fails on that stub.

The decision is in winzapp_tools.build_env so it is reachable without running
the installer, and _gate_system_node() is the one thing in setup_api.py that
acts on it. Both are pinned here: a gate that is right and unreachable is the
same bug as no gate.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import setup_api  # noqa: E402
from node_download_config import NODE_VERSION  # noqa: E402
from winzapp_tools import build_env  # noqa: E402


class TestNodeMajor:
    def test_reads_the_major_of_an_ordinary_version(self):
        assert build_env.node_major("22.22.2") == 22
        assert build_env.node_major("26.7.0") == 26

    def test_tolerates_the_v_prefix_node_itself_prints(self):
        assert build_env.node_major("v22.22.2") == 22

    def test_is_none_for_anything_it_cannot_read(self):
        assert build_env.node_major("") is None
        assert build_env.node_major("   ") is None
        assert build_env.node_major("not-a-version") is None
        assert build_env.node_major(None) is None


class TestSystemNodeIsRefused:
    def test_the_homologated_major_is_accepted(self):
        assert build_env.system_node_is_refused("22.22.2", "22.22.2") is False

    def test_another_patch_of_the_same_major_is_accepted(self):
        """Compared by major, not exactly: 22.x is the verified runtime line,
        and refusing 22.20.0 would block a working setup for nothing."""
        assert build_env.system_node_is_refused("22.20.0", "22.22.2") is False
        assert build_env.system_node_is_refused("22.0.0", "22.22.2") is False

    def test_the_major_that_broke_this_is_refused(self):
        """Node 26.7.0, measured: Chromium extraction stopped after 2 of 290
        entries and the install.mjs run reported success."""
        assert build_env.system_node_is_refused("26.7.0", "22.22.2") is True

    def test_any_other_major_is_refused_in_both_directions(self):
        assert build_env.system_node_is_refused("24.15.0", "22.22.2") is True
        assert build_env.system_node_is_refused("20.11.0", "22.22.2") is True

    def test_only_an_answer_refuses(self):
        """A probe that could not speak is not a verdict. portable_node_version()
        returns "" for a missing exe AND for a --version that timed out on a
        cold machine, and a check must not be more fatal than the thing it
        stands in for — the same rule the npm health probe above it was taught
        after a 10s timeout failed a release build."""
        assert build_env.system_node_is_refused("", "22.22.2") is False
        assert build_env.system_node_is_refused("garbage", "22.22.2") is False
        assert build_env.system_node_is_refused("22.22.2", "") is False


class _Gate:
    """_gate_system_node() with its two lookups scripted.

    It resolves a bare command through shutil.which() and then asks
    portable_node_version() — neither of which may touch the real machine, or
    the test would assert on whatever Node the developer happens to have.
    """

    def __init__(self, monkeypatch, version, *, on_path=True, allow=None):
        monkeypatch.setattr(
            setup_api.shutil,
            "which",
            lambda cmd: r"C:\Program Files\nodejs\node.exe" if on_path else None,
        )
        monkeypatch.setattr(setup_api, "portable_node_version", lambda exe: version)
        monkeypatch.delenv(setup_api.ALLOW_SYSTEM_NODE_ENV, raising=False)
        if allow is not None:
            monkeypatch.setenv(setup_api.ALLOW_SYSTEM_NODE_ENV, allow)

    def run(self, node_bin="node"):
        setup_api._gate_system_node(node_bin)


class TestGateSystemNode:
    def test_a_refused_major_exits_nonzero(self, monkeypatch, capsys):
        gate = _Gate(monkeypatch, "26.7.0")
        with pytest.raises(SystemExit) as exit_info:
            gate.run()
        assert exit_info.value.code == 1
        out = capsys.readouterr().out
        assert "26.7.0" in out
        assert NODE_VERSION in out

    def test_the_refusal_names_a_way_out(self, monkeypatch, capsys):
        """A gate that only says no strands whoever hits it. It has to name
        the override, since nothing else in the output points at this file."""
        gate = _Gate(monkeypatch, "26.7.0")
        with pytest.raises(SystemExit):
            gate.run()
        assert setup_api.ALLOW_SYSTEM_NODE_ENV in capsys.readouterr().out

    def test_the_homologated_major_passes_through(self, monkeypatch):
        _Gate(monkeypatch, NODE_VERSION).run()

    def test_an_unreadable_version_warns_and_continues(self, monkeypatch, capsys):
        _Gate(monkeypatch, "").run()
        assert "WARNING" in capsys.readouterr().out

    def test_node_missing_from_path_is_not_a_verdict(self, monkeypatch, capsys):
        """`npm install` fails loudly and legibly a moment later; this gate
        must not pre-empt it with a message about versions."""
        _Gate(monkeypatch, "", on_path=False).run()
        assert "WARNING" in capsys.readouterr().out

    def test_the_override_lets_a_refused_major_through(self, monkeypatch, capsys):
        _Gate(monkeypatch, "26.7.0", allow="1").run()
        assert "WARNING" in capsys.readouterr().out

    def test_an_empty_override_is_not_set(self, monkeypatch):
        """`set WINZAPP_ALLOW_SYSTEM_NODE=` on Windows leaves an empty string
        behind, which means "unset" to the person who typed it."""
        gate = _Gate(monkeypatch, "26.7.0", allow="   ")
        with pytest.raises(SystemExit):
            gate.run()

    def test_the_override_says_nothing_when_the_version_is_already_right(
        self, monkeypatch, capsys
    ):
        """Checking the override before the verdict told somebody who had left
        it set that they were using v22.22.2 "instead of the homologated
        v22.22.2"."""
        _Gate(monkeypatch, NODE_VERSION, allow="1").run()
        assert capsys.readouterr().out.count("WARNING") == 0

    def test_off_windows_the_refusal_does_not_send_you_after_a_windows_zip(
        self, monkeypatch, capsys
    ):
        """client/node/ is a win-x64 archive (client/node_download_config.py),
        so it can never exist on Linux — where this gate runs on EVERY run,
        since npm is never the portable one there. `build-onefile` is not a
        remedy available to that reader."""
        monkeypatch.setattr(setup_api.sys, "platform", "linux")
        gate = _Gate(monkeypatch, "26.7.0")
        with pytest.raises(SystemExit):
            gate.run()
        out = capsys.readouterr().out
        assert "build-onefile" not in out
        assert "client/node/" not in out
        assert "nvm" in out and setup_api.ALLOW_SYSTEM_NODE_ENV in out

    def test_on_windows_the_refusal_offers_the_portable_runtime(self, monkeypatch, capsys):
        monkeypatch.setattr(setup_api.sys, "platform", "win32")
        gate = _Gate(monkeypatch, "26.7.0")
        with pytest.raises(SystemExit):
            gate.run()
        assert "build-onefile" in capsys.readouterr().out

    def test_the_refusal_names_the_line_rather_than_one_patch(self, monkeypatch, capsys):
        """"is not the homologated major (v22.22.2)" conflated the two: 22.22.2
        is a version, 22.x is the line the gate actually compares."""
        gate = _Gate(monkeypatch, "26.7.0")
        with pytest.raises(SystemExit):
            gate.run()
        assert f"{build_env.node_major(NODE_VERSION)}.x" in capsys.readouterr().out

    def test_an_absolute_node_is_not_resolved_through_path(self, monkeypatch):
        """The unhealthy-portable-npm fallback hands over an absolute path
        shutil.which() already produced; resolving it again could pick a
        different Node than the one about to be run."""
        seen = []
        monkeypatch.setattr(
            setup_api.shutil, "which", lambda cmd: seen.append(cmd) or r"C:\other\node.exe"
        )
        monkeypatch.setattr(setup_api, "portable_node_version", lambda exe: NODE_VERSION)
        monkeypatch.delenv(setup_api.ALLOW_SYSTEM_NODE_ENV, raising=False)
        setup_api._gate_system_node(r"C:\Program Files\nodejs\node.exe")
        assert seen == []


class TestNpmRunsUnderPortableNode:
    """The one predicate deciding both how npm is invoked and whether the gate
    applies. An earlier version tracked ``client/node/node.exe`` instead, which
    got the mixed toolchain below exactly backwards."""

    def test_the_portable_npm_cli_is_recognised(self):
        assert setup_api.npm_runs_under_portable_node(
            r"C:\src\WinZapp\client\node\node_modules\npm\bin\npm-cli.js"
        ) is True

    def test_a_bare_npm_is_not_the_portable_one(self):
        """`npm` reaches _run(), which resolves it through shutil.which() to
        the system npm.cmd — and that launches the Node.js it was installed
        beside, not client/node/node.exe. Measured on the reporting machine:
        `which npm` -> C:\\Program Files\\nodejs\\npm.CMD, npm 11.19.0, the
        Node 26 install."""
        assert setup_api.npm_runs_under_portable_node("npm") is False

    def test_the_system_npm_shim_is_not_the_portable_one(self):
        assert setup_api.npm_runs_under_portable_node(r"C:\Program Files\nodejs\npm.CMD") is False
        assert setup_api.npm_runs_under_portable_node("/usr/bin/npm") is False


class TestGateIsWired:
    """setup_api.py's toolchain block cannot be called from a test — it is
    inline in a 200-line function that clones a repository first. These read
    its source, so each one has to assert something a plausible edit could
    actually get wrong, not merely that a line is spelled a certain way."""

    def _block(self):
        source = (ROOT / "setup_api.py").read_text(encoding="utf-8")
        start = source.index('print("[INFO] Automating Node.js dependency')
        return source, source[start:source.index('print("[INFO] Running npm install...")', start)]

    def test_the_portable_node_is_adopted_only_together_with_its_npm(self):
        """The defect this replaced: node_bin was set from node.exe alone, so
        a client/node/ with no npm tree left npm_bin as the bare "npm" while
        every local signal said the toolchain was portable — and the gate,
        keyed on node.exe, waved through a run that used the system Node."""
        _, block = self._block()
        adopt_node = block.index("node_bin = win_node")
        adopt_npm = block.index("npm_bin = win_npm")
        guard = block.index("if os.path.isfile(win_npm):")
        assert guard < adopt_node and guard < adopt_npm, (
            "node_bin = win_node must sit inside the branch that found the "
            "portable npm, or the two halves can come from different runtimes"
        )

    def test_the_unhealthy_npm_fallback_lands_on_the_gate(self):
        """The second, easy-to-miss path: a portable npm that fails its health
        probe swaps in shutil.which("node") deep inside the branch above. It
        reaches the gate only because npm_bin stops being npm-cli.js in the
        same breath — assert that ordered pair, since asserting either line
        alone passes with the other deleted."""
        _, block = self._block()
        fallback = block.index('system_node = shutil.which("node")')
        assert block.index("npm_bin = system_npm", fallback) > fallback
        assert block.index("node_bin = system_node", fallback) > fallback

    def test_the_gate_guards_exactly_what_the_invocation_keys_on(self):
        """Both must ask the same question. When they disagree, the toolchain
        that runs is not the toolchain that was checked."""
        source, block = self._block()
        assert "if not npm_runs_under_portable_node(npm_bin):\n            _gate_system_node(node_bin)" in block
        assert 'if npm_runs_under_portable_node(npm_bin):\n            _run([node_bin, npm_bin, "install"' in source

    def test_the_gate_runs_before_npm_install(self):
        """Behind it, `npm install` alone is 300+ packages and minutes of
        network before the Chromium download the gate exists to protect."""
        source, _ = self._block()
        assert source.index("_gate_system_node(node_bin)") < source.index(
            'print("[INFO] Running npm install...")'
        )

    def test_the_homologated_version_is_not_restated_here(self):
        """client/node_download_config.py is the single source of truth for
        the Node version, and a gate carrying its own copy would go on
        refusing the right runtime the day that pin moves.

        Matched version-SHAPED, the way tests/test_node_version_single_source.py
        does it, not against the current value: asserting `NODE_VERSION not in
        source` fails the day somebody writes the number into an explanatory
        comment, which is this repository's house style and nothing to do with
        the invariant."""
        source = (ROOT / "setup_api.py").read_text(encoding="utf-8")
        assert "from node_download_config import NODE_VERSION" in source
        offenders = [
            f"{number}: {line.strip()}"
            for number, line in enumerate(source.splitlines(), 1)
            if not line.strip().startswith("#")
            and re.search(r"""["']\d+\.\d+\.\d+["']|node-v\d+\.\d+\.\d+""", line)
        ]
        assert not offenders, f"read the version from node_download_config instead: {offenders}"


class TestSetupApiStillRunsStandalone:
    def test_it_imports_from_a_foreign_working_directory(self, tmp_path):
        """`python setup_api.py` from anywhere has to keep working — the new
        imports reach winzapp_tools/ and client/, neither of which is on the
        path by default. Run out-of-process: an import that only works because
        pytest.ini already set pythonpath would prove nothing."""
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                f"import sys; sys.path.insert(0, r'{ROOT}'); import setup_api; "
                "print(setup_api.ALLOW_SYSTEM_NODE_ENV)",
            ],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": ""},
        )
        assert completed.returncode == 0, completed.stderr
        assert "WINZAPP_ALLOW_SYSTEM_NODE" in completed.stdout
