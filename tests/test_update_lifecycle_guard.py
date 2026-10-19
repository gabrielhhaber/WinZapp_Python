"""No real processes, wx.App or installer: ownership survives the parent.

State uses pytest temporary files; Windows locking/process primitives are
injected. Generated native scripts are inspected as structural artifacts.
"""
import os
import types

import pytest
import update_coord as coord
from core import update_install_guard as guard
from core import update_file_lease as lease
from core import update_guard_launch as launch
from core import update_guard_process as process
from core.update_outcome import previous_update_failure


def claim(root):
    return coord.try_begin_update(str(root), pid=111, create_time=1.0,
                                  is_alive=lambda *args: True)


class TestInstallerOwnership:
    def test_dead_parent_does_not_open_the_slot_while_the_guard_is_held(self, tmp_path, monkeypatch):
        token = claim(tmp_path)
        coord.arm_installer_guard(str(tmp_path), token)
        monkeypatch.setattr(guard, "lease_busy", lambda path: True)
        assert coord.is_update_in_progress(str(tmp_path), is_alive=lambda *args: False)
        assert coord.try_create_runtime_lease(str(tmp_path), pid=222, create_time=2,
                                               is_alive=lambda *args: False) is None
        refusal = coord.try_begin_update(str(tmp_path), pid=222, create_time=2,
                                          is_alive=lambda *args: False, explain=True)
        assert refusal.token is None

    @pytest.mark.parametrize("outcome", ["success", "unchanged", "failed", "aborted"])
    def test_finished_copy_unblocks_start_even_before_guardian_exit(self, tmp_path, monkeypatch, outcome):
        token = claim(tmp_path)
        paths = coord.arm_installer_guard(str(tmp_path), token)
        monkeypatch.setattr(guard, "lease_busy", lambda path: True)
        guard.write_signal(paths["done"], outcome)
        assert not coord.is_update_in_progress(str(tmp_path), is_alive=lambda *args: True)

    def test_dead_parent_and_dead_guard_recover(self, tmp_path, monkeypatch):
        token = claim(tmp_path)
        coord.arm_installer_guard(str(tmp_path), token)
        monkeypatch.setattr(guard, "lease_busy", lambda path: False)
        assert not coord.is_update_in_progress(str(tmp_path), is_alive=lambda *args: False)

    def test_legacy_owner_identity_is_transferred_before_parent_exit(self, tmp_path):
        token = claim(tmp_path)
        coord.arm_installer_guard(str(tmp_path), token)
        coord.transfer_installer_guard(str(tmp_path), token, 333,
                                       proc_create_time=lambda pid: 3.0)
        state = coord._read_state(str(tmp_path))
        assert state["owner_pid"] == 333 and state["owner_create_time"] == 3.0
        assert state["owner_token"] == token["owner_token"]

    @pytest.mark.parametrize("pid,created", [(0, 3.0), (333, 0.0), (333, None)])
    def test_unverifiable_guardian_cannot_take_ownership(self, tmp_path, pid, created):
        token = claim(tmp_path)
        coord.arm_installer_guard(str(tmp_path), token)
        with pytest.raises(ValueError):
            coord.transfer_installer_guard(str(tmp_path), token, pid,
                                           proc_create_time=lambda pid: created)
        assert coord._read_state(str(tmp_path))["owner_pid"] == 111

    def test_transfer_cannot_overwrite_another_owners_state(self, tmp_path):
        token = claim(tmp_path)
        coord.arm_installer_guard(str(tmp_path), token)
        stale = dict(token, owner_token="b" * 32)
        with pytest.raises(ValueError):
            coord.transfer_installer_guard(str(tmp_path), stale, 333,
                                           proc_create_time=lambda pid: 3.0)
        assert coord._read_state(str(tmp_path))["owner_pid"] == 111

    def test_parent_cannot_revoke_a_committed_guard(self, tmp_path, monkeypatch):
        token = claim(tmp_path)
        paths = coord.arm_installer_guard(str(tmp_path), token)
        monkeypatch.setattr(guard, "lease_busy", lambda path: True)
        guard.write_signal(paths["commit"], "commit")
        assert not coord.end_update(str(tmp_path), token)
        guard.write_signal(paths["done"], "failed")
        assert coord.end_update(str(tmp_path), token)

    def test_pregrant_failure_can_release_its_own_slot(self, tmp_path, monkeypatch):
        token = claim(tmp_path)
        paths = coord.arm_installer_guard(str(tmp_path), token)
        monkeypatch.setattr(guard, "lease_busy", lambda path: True)
        assert not coord.end_update(str(tmp_path), token)
        guard.write_signal(paths["abort"], "abort")
        assert coord.end_update(str(tmp_path), token)

    def test_wrong_token_cannot_arm_or_release_a_newer_installation(self, tmp_path):
        token = claim(tmp_path)
        stale = dict(token, owner_token="b" * 32)
        with pytest.raises(ValueError):
            coord.arm_installer_guard(str(tmp_path), stale)
        assert not coord.end_update(str(tmp_path), stale)

    def test_malformed_guard_state_fails_closed(self, tmp_path):
        claim(tmp_path)
        state = coord._read_state(str(tmp_path))
        state["installer_guard"] = "true"
        coord._write_state(str(tmp_path), state)
        assert coord.is_update_in_progress(str(tmp_path), is_alive=lambda *args: False)


class TestGrantProtocol:
    @pytest.mark.parametrize("change", ["lock_released", "finished", "identity_changed", "aborted"])
    def test_guardian_is_rechecked_after_waiter_preparation(self, tmp_path, monkeypatch, change):
        paths = guard.guard_paths(str(tmp_path), "a" * 32)
        guard.write_signal(paths["ready"], "ready:333")
        held = [True]
        monkeypatch.setattr(guard, "lease_busy", lambda path: held[0])
        def prepare_waiter(pid):
            assert pid == 333
            if change == "lock_released":
                held[0] = False
            elif change == "finished":
                guard.write_signal(paths["done"], "aborted")
            elif change == "identity_changed":
                guard.write_signal(paths["ready"], "ready:444")
            else:
                guard.write_signal(paths["abort"], "abort")
            return True
        assert not guard.await_ready(paths, before_commit=prepare_waiter)
        assert not os.path.exists(paths["commit"])
        assert guard.read_signal(paths["abort"]) == "abort"

    @pytest.mark.parametrize("signal,value", [("done", "aborted"), ("abort", "abort")])
    def test_finished_or_aborted_guardian_never_prepares_a_waiter(self, tmp_path, monkeypatch, signal, value):
        paths = guard.guard_paths(str(tmp_path), "a" * 32)
        guard.write_signal(paths["ready"], "ready:333")
        guard.write_signal(paths[signal], value)
        monkeypatch.setattr(guard, "lease_busy", lambda path: True)
        prepared = []
        assert not guard.await_ready(paths, before_commit=lambda pid: prepared.append(pid) or True)
        assert not prepared and not os.path.exists(paths["commit"])

    @pytest.mark.parametrize("ready", ["ready", "ready:0", "ready:-1", "ready:abc",
                                       "ready:2147483648", "ready:٣٣٣"])
    def test_malformed_ready_identity_never_grants_a_copy(self, tmp_path, monkeypatch, ready):
        paths = guard.guard_paths(str(tmp_path), "a" * 32)
        guard.write_signal(paths["ready"], ready) if ready.isascii() else (
            tmp_path / os.path.basename(paths["ready"])).write_text(ready, encoding="utf-8")
        monkeypatch.setattr(guard, "lease_busy", lambda path: True)
        ticks = iter([0, 0, 2])
        assert not guard.await_ready(paths, timeout=1, clock=lambda: next(ticks), sleep=lambda _: None)
        assert not os.path.exists(paths["commit"])

    def test_waiter_refusal_prevents_grant(self, tmp_path, monkeypatch):
        paths = guard.guard_paths(str(tmp_path), "a" * 32)
        guard.write_signal(paths["ready"], "ready:333")
        monkeypatch.setattr(guard, "lease_busy", lambda path: True)
        assert not guard.await_ready(paths, before_commit=lambda pid: False)
        assert guard.read_signal(paths["abort"]) == "abort"
        assert not os.path.exists(paths["commit"])

    def test_only_ready_and_locked_guardian_gets_a_commit(self, tmp_path, monkeypatch):
        paths = guard.guard_paths(str(tmp_path), "a" * 32)
        guard.write_signal(paths["ready"], "ready:222")
        monkeypatch.setattr(guard, "lease_busy", lambda path: True)
        assert guard.await_ready(paths)
        assert guard.read_signal(paths["commit"]) == "commit"
        assert not os.path.exists(paths["abort"])

    @pytest.mark.parametrize("ready,locked", [(False, True), (True, False), (False, False)])
    def test_incomplete_handoff_aborts_without_commit(self, tmp_path, monkeypatch, ready, locked):
        paths = guard.guard_paths(str(tmp_path), "a" * 32)
        if ready:
            guard.write_signal(paths["ready"], "ready:222")
        monkeypatch.setattr(guard, "lease_busy", lambda path: locked)
        ticks = iter([0, 0, 2])
        assert not guard.await_ready(paths, timeout=1, clock=lambda: next(ticks), sleep=lambda _: None)
        assert not os.path.exists(paths["commit"])
        assert guard.read_signal(paths["abort"]) == "abort"

    def test_visible_commit_is_not_revoked_on_late_io_error(self, tmp_path, monkeypatch):
        paths = guard.guard_paths(str(tmp_path), "a" * 32)
        guard.write_signal(paths["ready"], "ready:222")
        monkeypatch.setattr(guard, "lease_busy", lambda path: True)
        real = guard.write_signal
        def ambiguous(path, value):
            real(path, value)
            raise OSError("reported after replace")
        monkeypatch.setattr(guard, "write_signal", ambiguous)
        assert guard.await_ready(paths)


class TestLifetimeFiles:
    @pytest.mark.parametrize("failure,busy", [(FileNotFoundError(), False),
                                               (PermissionError(), True), (OSError(), True)])
    def test_unknown_access_is_not_proof_of_an_unused_lock(self, monkeypatch, failure, busy):
        def fail(*args, **kwargs):
            raise failure
        monkeypatch.setattr(lease, "open_lease", fail)
        assert lease.lease_busy("synthetic") is busy

    def test_shared_package_reader_is_reused_then_released(self, tmp_path, monkeypatch):
        events = []
        monkeypatch.setattr(lease, "_packages", {})
        monkeypatch.setattr(lease, "open_lease", lambda path: events.append("open") or
                            (lambda: events.append("close")))
        root = str(tmp_path / "winzapp_ext_ready")
        lease.hold_package(root)
        lease.hold_package(root)
        assert events == ["open"] and lease.package_busy(root)
        lease.release_package(root)
        assert events == ["open", "close"]

    def test_old_active_package_is_not_removed(self, tmp_path, monkeypatch):
        import update_temp_sweep as sweep
        root = tmp_path / "winzapp_ext_ready"
        root.mkdir()
        (root / "WinZapp.exe").write_text("payload")
        os.utime(root, (0, 0))
        monkeypatch.setattr(sweep, "package_busy", lambda path: True)
        assert sweep.sweep_stale_update_temporaries(str(tmp_path), now=10 * 86400) == 0
        assert root.is_dir()

    def test_discard_does_not_remove_a_package_taken_over_by_the_guardian(self, tmp_path, monkeypatch):
        import update_package
        root = tmp_path / "winzapp_ext_guarded"
        root.mkdir()
        payload = root / "WinZapp.exe"
        payload.write_text("synthetic")
        released = []
        monkeypatch.setattr(update_package, "release_package", released.append)
        monkeypatch.setattr(update_package, "package_busy", lambda path: True)
        update_package.discard_package(str(root))
        assert released == [str(root)] and payload.read_text() == "synthetic"


class TestOutcomeReceipts:
    @pytest.mark.parametrize("receipt,marker_ns,result_ns,expected", [
        ("success:" + "a" * 32, 1_000_000_000, 2_000_000_000, None),
        ("success:" + "a" * 32, 2_000_000_000, 1_000_000_000, "update failed"),
        ("success:" + "a" * 32, 1_000_000_000, 1_000_000_000, "update failed"),
        ("pending:" + "a" * 32, 1_000_000_000, 2_000_000_000, "update failed"),
        ("failed:" + "a" * 32, 1_000_000_000, 2_000_000_000, "update failed"),
        ("success:malformed", 1_000_000_000, 2_000_000_000, "update failed"),
    ])
    def test_only_a_newer_valid_success_supersedes_the_old_warning(
            self, tmp_path, receipt, marker_ns, result_ns, expected):
        marker = tmp_path / "update_failed.marker"
        marker.write_text("update failed", encoding="utf-8")
        result = tmp_path / "update_result.marker"
        result.write_text(receipt, encoding="ascii")
        os.utime(marker, ns=(marker_ns, marker_ns))
        os.utime(result, ns=(result_ns, result_ns))
        assert previous_update_failure(str(tmp_path)) == expected

    def test_no_failure_marker_is_not_an_error(self, tmp_path):
        assert previous_update_failure(str(tmp_path)) is None


class TestGuardedProcessPreparation:
    @pytest.mark.parametrize("admin", [True, False])
    def test_waiter_and_ready_guard_precede_the_commit(self, tmp_path, monkeypatch, admin):
        paths = guard.guard_paths(str(tmp_path), "a" * 32)
        events = []
        monkeypatch.setattr(launch, "powershell_path", lambda: r"C:\Windows\powershell.exe")
        def ready(paths, before_commit):
            assert before_commit(333)
            events.append("commit")
            return True
        monkeypatch.setattr(launch, "await_ready", ready)
        def waiter(*args, **kwargs):
            events.append("waiter")
            assert args[1] == 333
            assert kwargs["completion_path"] == paths["done"]
            assert kwargs["installer_image"] == "powershell.exe"
            assert kwargs["recovery_path"] == str(tmp_path / "handoff.recovery.txt")
            assert os.path.isfile(kwargs["recovery_path"])
            return True
        ok = launch.launch_guarded(str(tmp_path / "run.bat"), paths, "", str(tmp_path / "handoff"), "exe",
                                    "log", 111, admin, lambda *args: True,
                                    lambda *args: events.append("spawn") or (True, 222),
                                    lambda *args, **kwargs: events.append("spawn") or
                                    types.SimpleNamespace(pid=222), waiter, {}, recovery_text="Recovery")
        assert ok and events == ["spawn", "waiter", "commit"]

    @pytest.mark.parametrize("failure", ["uac", "pid", "waiter", "ready"])
    def test_no_commit_when_preconditions_fail(self, tmp_path, monkeypatch, failure):
        paths = guard.guard_paths(str(tmp_path), "a" * 32)
        monkeypatch.setattr(launch, "powershell_path", lambda: r"C:\Windows\powershell.exe")
        monkeypatch.setattr(launch, "await_ready", lambda paths, before_commit:
                            failure != "ready" and before_commit(333))
        assert not launch.launch_guarded(
            str(tmp_path / "run.bat"), paths, "", str(tmp_path / "handoff"), "exe", "log", 111,
            True, lambda *args: True,
            lambda *args: (failure != "uac", None if failure == "pid" else 222),
            None, lambda *args, **kwargs: failure != "waiter", {}, recovery_text="Recovery")
        assert not os.path.exists(paths["commit"])

    def test_guardian_job_is_assigned_before_ready_and_kills_its_copy_children(self):
        # Structural native-source contract; no Win32 calls or child processes.
        script = process.guardian_script("batch", guard.guard_paths("root", "a" * 32))
        assert "0x08000004" in script and "0x2000" in script
        assert script.index("Check(AssignProcessToJobObject(job, pi.process))") < script.index(
            "Check(ResumeThread(pi.thread)") < script.index('File.WriteAllText(ready + ".tmp"')
        assert "TerminateJobObject(job, 1)" in script
        assert "accounting.active > 0" in script

    @pytest.mark.parametrize("path", ['C:\\bad!\\guard.ps1', 'C:\\%TEMP%\\guard.ps1'])
    def test_unrepresentable_batch_path_is_refused(self, path):
        with pytest.raises(ValueError):
            process.launcher_script(path, "powershell.exe")

    def test_native_scripts_quote_apostrophes_and_reject_control_characters(self):
        paths = guard.guard_paths("root", "a" * 32)
        assert "O''Brien" in process.guardian_script("O'Brien", paths)
        with pytest.raises(ValueError):
            process.guardian_script("line\nbreak", paths)


def test_generated_guarded_batch_never_relaunches_as_a_job_child():
    import updater
    paths = guard.guard_paths(r"C:\global", "a" * 32)
    script = updater._build_installer_script(
        r"C:\stage", r"C:\install", r"C:\install\WinZapp.exe", r"C:\logs\install.log",
        r"C:\install\update_failed.marker", 111, 6300, relaunch_handoff=r"C:\temp\waiter",
        install_guard=paths, result_path=r"C:\install\update_result.marker", run_id="b" * 32)
    assert 'start ""' not in script
    assert script.index(":INSTALL_GRANTED") < script.index("xcopy /E")
    assert 'if not "!XCOPY_RESULT!"=="0"' in script
    assert script.index("echo success:") < script.index("echo success\n")
    assert "echo xcopy exit code:" in script


def test_guarded_waiter_cannot_relaunch_on_timeout_or_unknown_completion():
    from update_relaunch import build_relaunch_waiter_script
    script = build_relaunch_waiter_script(222, "exe", "log", "handoff", completion_path="result")
    gave_up = script.split(":GAVE_UP\n", 1)[1].split(":CHECK_RESULT\n", 1)[0]
    assert 'start ""' not in gave_up
    relaunch = script.split(":RELAUNCH\n", 1)[1].split(":ABORT\n", 1)[0]
    assert relaunch.index('findstr /x /c:"success"') < relaunch.index('start ""')
    waiting = script.split(":WAIT\n", 1)[1].split(":WAIT_AGAIN\n", 1)[0]
    assert 'if exist "handoff.done" goto RELAUNCH' not in waiting
    assert "if errorlevel 1 goto CHECK_RESULT" in waiting


def test_guarded_waiter_stops_on_a_pregrant_abort_receipt():
    from update_relaunch import build_relaunch_waiter_script
    script = build_relaunch_waiter_script(222, "exe", "log", "handoff", completion_path="result")
    waiting = script.split(":WAIT\n", 1)[1].split(":WAIT_AGAIN\n", 1)[0]
    # Generated batch contract only; no command interpreter/process is run.
    stop = 'findstr /x /c:"aborted" "result" >NUL 2>&1\nif not errorlevel 1 goto ABORT'
    assert stop in waiting and waiting.index(stop) < waiting.index("tasklist")
    legacy = build_relaunch_waiter_script(222, "exe", "log", "handoff")
    assert '/c:"aborted"' not in legacy


def test_production_batch_preparation_passes_the_guard_and_requires_an_outside_waiter(
        tmp_path, monkeypatch):
    import updater
    install = tmp_path / "install"
    install.mkdir()
    package = tmp_path / "winzapp_ext_prepared"
    package.mkdir()
    paths = guard.guard_paths(str(tmp_path), "a" * 32)
    monkeypatch.setattr(updater, "_keep_newer_installed_server", lambda *args: None)
    monkeypatch.setattr(updater, "own_node_pids", lambda *args: [])
    monkeypatch.setattr(updater, "_needs_admin", lambda: False)
    monkeypatch.setattr(updater, "_is_elevated", lambda: False)
    monkeypatch.setattr(updater, "_console_safe_path", lambda path: path)
    monkeypatch.setattr(updater, "resource_path", lambda *parts: str(tmp_path.joinpath(*parts)))
    monkeypatch.setattr(updater, "log_path", lambda: str(tmp_path / "logs"))
    monkeypatch.setattr(updater.tempfile, "tempdir", str(tmp_path))
    written, launched = [], []
    monkeypatch.setattr(updater, "_write_installer_script",
                        lambda path, text: written.append(text) or True)
    monkeypatch.setattr(launch, "launch_guarded", lambda *args, **kwargs: launched.append(args) or True)
    assert updater._run_batch_installer(str(package), str(install), "WinZapp.exe", 111,
                                         install_guard=paths)
    assert launched[0][1] is paths and launched[0][2] == str(package)
    assert launched[0][3].endswith(".relaunch")
    assert 'start ""' not in written[0]
