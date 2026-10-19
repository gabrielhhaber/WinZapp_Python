"""CI-only OS acceptance with synthetic files, never the installed application.

Real process/lock tests deliberately run only on GitHub's Windows runner.
The local default skips before any native call or process creation. Polling
here is bounded integration synchronization, not a unit-test timeout delay.
"""
import contextlib
import os
import subprocess
import time

import pytest

from core import update_file_lease as lease
from core.update_guard_process import guardian_script, powershell_path
from core.update_install_guard import batch_done, batch_gate, guard_paths, read_signal, write_signal


def _on_runner():
    return (os.name == "nt" and os.environ.get("GITHUB_ACTIONS") == "true"
            and os.environ.get("RUNNER_OS") == "Windows")


pytestmark = pytest.mark.skipif(not _on_runner(), reason="native updater acceptance is GitHub Windows CI only")


def _close_owned_child(kernel, handle):
    """Teardown must also work when the job-under-test failed to kill it."""
    try:
        if kernel.WaitForSingleObject(handle, 0) != 0:
            assert kernel.TerminateProcess(handle, 1), "could not stop the synthetic owned child"
            assert kernel.WaitForSingleObject(handle, 10000) == 0, "owned child did not stop"
    finally:
        kernel.CloseHandle(handle)


def _until(condition):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.05)
    pytest.fail("synthetic guardian did not reach the expected state within 30 seconds")


@contextlib.contextmanager
def _guardian(root, body, package=""):
    if not _on_runner():
        raise RuntimeError("native helper is restricted to GitHub Windows CI")
    paths = guard_paths(str(root), "a" * 32)
    log = str(root / "installer.log")
    batch = root / "installer.bat"
    batch.write_text("@echo off\nsetlocal EnableDelayedExpansion\n"
                     + batch_gate(paths, log) + body + "\nexit /b 0\n", encoding="ascii")
    script = root / "guardian.ps1"
    script.write_text(guardian_script(str(batch), paths, package), encoding="utf-8-sig")
    with (root / "guardian-output.txt").open("wb") as output:
        proc = subprocess.Popen([powershell_path(), "-NoProfile", "-NonInteractive",
                                 "-ExecutionPolicy", "Bypass", "-File", str(script)],
                                stdout=output, stderr=subprocess.STDOUT,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            _until(lambda: read_signal(paths["ready"]).startswith("ready:") or proc.poll() is not None)
            assert proc.poll() is None, (root / "guardian-output.txt").read_text(errors="replace")
            yield proc, paths
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)


@pytest.mark.parametrize("decision", ["commit", "abort"])
def test_native_child_cannot_write_before_a_grant_and_reports_completion(tmp_path, decision):
    destination = tmp_path / "synthetic-payload.txt"
    paths = guard_paths(str(tmp_path), "a" * 32)
    body = f'> "{destination}" echo synthetic\n' + batch_done(paths, "success")
    with _guardian(tmp_path, body) as (proc, actual):
        assert lease.lease_busy(actual["lock"])
        assert not destination.exists()
        write_signal(actual[decision], decision)
        assert proc.wait(timeout=15) == (0 if decision == "commit" else 1)
        assert destination.exists() is (decision == "commit")
        assert read_signal(actual["done"]) == ("success" if decision == "commit" else "aborted")
        assert read_signal(actual["diagnostic"]) == ("completed:0" if decision == "commit" else "completed:1")
        assert not lease.lease_busy(actual["lock"])


def test_native_guardian_takes_the_package_lease_before_the_parent_releases_it(tmp_path):
    package = tmp_path / "winzapp_ext_synthetic"
    package.mkdir()
    lease.hold_package(str(package))
    paths = guard_paths(str(tmp_path), "a" * 32)
    try:
        with _guardian(tmp_path, batch_done(paths, "success"), lease.package_lock_path(str(package))) as (proc, actual):
            lease.release_package(str(package))
            assert lease.package_busy(str(package))
            write_signal(actual["commit"], "commit")
            assert proc.wait(timeout=15) == 0
            assert not lease.package_busy(str(package))
    finally:
        lease.release_package(str(package))


def test_native_guardian_death_terminates_the_owned_grandchild(tmp_path):
    import ctypes
    from ctypes import wintypes
    identity = tmp_path / "owned-child.pid"
    child = tmp_path / "owned-child.ps1"
    child.write_text(f"[IO.File]::WriteAllText('{identity}', [string]$PID)\n"
                      "while ($true) { Start-Sleep -Milliseconds 50 }\n", encoding="utf-8-sig")
    body = f'"{powershell_path()}" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{child}"\n'
    with _guardian(tmp_path, body) as (proc, paths):
        write_signal(paths["commit"], "commit")
        _until(lambda: read_signal(str(identity)).isdecimal())
        child_pid = int(identity.read_text())
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
        kernel.TerminateProcess.restype = wintypes.BOOL
        # Hold this exact child's handle before killing the guardian: no PID
        # scanning/reuse assumptions, no foreign processes or installations.
        handle = kernel.OpenProcess(0x00100000 | 0x0001, False, child_pid)
        assert handle
        try:
            proc.kill()
            proc.wait(timeout=10)
            assert kernel.WaitForSingleObject(handle, 10000) == 0
        finally:
            _close_owned_child(kernel, handle)
        assert not lease.lease_busy(paths["lock"])
