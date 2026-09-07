"""The portable-npm health probe must not be able to fail a build on its own.

setup_api.py runs `npm install --help` before the real install to check the
bundled Node runtime can execute npm at all. It ran with a 10-second timeout
and no handler, so `subprocess.TimeoutExpired` escaped into the outer
except-block that reports

    [ERROR] Node.js dependencies installation/build failed: Command
    '[... npm-cli.js, install, --help]' timed out after 10 seconds

and exits 1. On 2026-09-07 (run 34162896892) that stopped an alpha from being
published, on a commit whose two immediate predecessors had built fine — the
signature of a threshold sitting too close to the normal cost of the operation
rather than of a broken runtime.

The principle this pins: **a health probe must never be more fatal than the
operation it stands in for.** The real `npm install` a few lines below has no
timeout at all and fails loudly and informatively when npm is genuinely broken,
so a probe that merely fails to answer in time has learned nothing and must not
decide anything.
"""

import importlib.util
import inspect
import re
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _setup_api():
    spec = importlib.util.spec_from_file_location(
        "winzapp_setup_api", ROOT / "setup_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _probe_source():
    """The block around the health probe, read out of main()."""
    source = inspect.getsource(_setup_api().main)
    start = source.index('"install", "--help"')
    return source[max(0, start - 2000):start + 2000]


class TestATimeoutIsNotAVerdict:
    def test_the_probe_is_wrapped(self):
        block = _probe_source()
        assert "except subprocess.TimeoutExpired:" in block, (
            "an unhandled TimeoutExpired here reaches the outer handler and "
            "aborts the whole build"
        )

    def test_a_timeout_does_not_fall_into_the_unhealthy_branch(self):
        """The unhealthy branch can raise RuntimeError when no system Node is
        available. A probe that timed out has not shown npm to be unhealthy."""
        block = _probe_source()
        assert "npm_probe is not None and npm_probe.returncode != 0" in block

    def test_it_says_what_it_did(self):
        block = _probe_source()
        assert "[WARNING]" in block and "timed out" in block


class TestTheBudgetIsNotSetToTheCostOfTheOperation:
    def test_the_probe_gets_a_realistic_budget(self):
        """A cold `npm install --help` on a CI runner routinely exceeds ten
        seconds; it is printing a help page, so a slow answer says something
        about the machine, not about npm."""
        block = _probe_source()
        timeout = re.search(r"timeout=(\d+)", block)
        assert timeout, "the probe no longer passes a timeout"
        assert int(timeout.group(1)) >= 60, (
            f"timeout={timeout.group(1)}s is close enough to the normal cost of "
            "this call to fail on an ordinary slow runner"
        )


class TestTheRealInstallStillDecides:
    def test_npm_install_itself_is_not_given_a_timeout(self):
        """It is the operation the probe stands in for. Capping it would move
        the same flake onto the step that actually matters."""
        source = inspect.getsource(_setup_api().main)
        install = source.index('"install", "--no-audit"')
        line_start = source.rindex("\n", 0, install)
        line_end = source.index("\n", install)
        assert "timeout" not in source[line_start:line_end]


class TestTheProbeStillCatchesARealFailure:
    """Softening the timeout must not soften the case it exists for."""

    def test_a_nonzero_exit_with_no_system_node_still_aborts(self, monkeypatch):
        module = _setup_api()
        block = _probe_source()
        assert "Portable npm is unhealthy" in block
        assert "raise RuntimeError(" in block

    def test_a_nonzero_exit_falls_back_to_system_node_when_there_is_one(self):
        block = _probe_source()
        assert "using the system" in block
        assert "node_bin = system_node" in block
