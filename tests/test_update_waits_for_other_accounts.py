"""An update with two accounts open used to loop: "atualiza e não muda".

Every account is its own process on the same install directory. The batch
installer waited only for the PID that accepted the update and killed only
that account's Node, then xcopy'd over WinZapp.exe and its DLLs while the
other account still had them mapped — a sharing violation, the old build
relaunched, and the next check offered the same release again. updater.py's
own docstring said the install was "gated by try_begin_update()", but nothing
called it, and as written it could never have succeeded anyway: the caller's
own runtime lease counted as "another account is open".

UpdateProgressDialog._worker runs on a thread and needs wx, so the pieces
are exercised as unbound methods against a stub, the way the other updater
tests do.
"""

import os
import time
import types
import zipfile

import updater
from core.update_handoff import InstallHandoff


def _script(extra_pids=()):
    return updater._build_installer_script(
        r"C:\tmp\ext", r"C:\WinZapp",
        r"C:\WinZapp\WinZapp.exe",
        r"C:\WinZapp\update_install.log",
        r"C:\WinZapp\update_failed.marker",
        pid=4242, api_port=6300, extra_pids=extra_pids,
    )


class TestInstallerScriptWaitsForEveryAccount:
    def test_each_other_account_pid_gets_its_own_wait_loop(self):
        s = _script(extra_pids=[5151, 6161])
        assert '"PID eq 4242"' in s
        assert '"PID eq 5151"' in s
        assert '"PID eq 6161"' in s
        # Every wait loop must come before the copy, or it protects nothing.
        assert s.index('"PID eq 6161"') < s.index("xcopy")
        # Distinct labels, each with its own goto — a shared label would
        # make cmd jump back to the first loop for every pid.
        assert s.count(":WAIT1\n") == 1 and "goto WAIT1" in s
        assert s.count(":WAIT2\n") == 1 and "goto WAIT2" in s

    def test_waiting_for_another_account_has_a_bounded_failure_path(self):
        s = _script(extra_pids=[5151])
        assert "setlocal EnableDelayedExpansion" in s
        assert "if !WAIT1_SECONDS! GEQ 60 goto OTHER_ACCOUNT_TIMEOUT" in s
        assert ":OTHER_ACCOUNT_TIMEOUT" in s
        assert "another WinZapp account did not exit" in s
        assert s.index(":OTHER_ACCOUNT_TIMEOUT") > s.index("xcopy")

    def test_no_other_account_means_the_script_is_unchanged(self):
        assert _script() == _script(extra_pids=[])


class _Label:
    def __init__(self):
        self.label = ""

    def SetLabel(self, v):
        self.label = v


def _stub(main_window):
    stub = types.SimpleNamespace(_main_window=main_window, _status_label=_Label(),
                                 _handoff=InstallHandoff())
    for name in ("_coord", "_quit_other_accounts", "_claim_install_slot", "_end_install_slot"):
        setattr(stub, name, getattr(updater.UpdateProgressDialog, name).__get__(stub))
    return stub


def _mw(global_dir=None, account_id=None):
    return types.SimpleNamespace(
        global_dir=global_dir, account_id=account_id,
        i18n=types.SimpleNamespace(t=lambda k: k),
    )


class TestQuitOtherAccounts:
    def test_single_account_run_asks_nobody_and_installs(self, monkeypatch):
        stub = _stub(_mw())
        assert stub._quit_other_accounts() == []
        assert stub._claim_install_slot() == {}

    def test_other_accounts_are_asked_to_quit_and_their_pids_returned(self, monkeypatch, tmp_path):
        import ipc
        import node_coord
        import update_coord

        gd = str(tmp_path)
        asked = []
        monkeypatch.setattr(wx_call_after_module(), "CallAfter", lambda fn, *a: fn(*a))
        monkeypatch.setattr(update_coord, "other_live_leases",
                            lambda _gd: [{"pid": 5151}, {"pid": 6161}, {"pid": None, "_corrupt": True}])
        monkeypatch.setattr(node_coord, "live_node_leases",
                            lambda _gd, is_alive: [{"account_id": "me"}, {"account_id": "b"}, {"account_id": "c"}])
        monkeypatch.setattr(ipc, "request_quit", lambda _gd, acc: asked.append(acc) or True)

        stub = _stub(_mw(gd, "me"))
        pids = stub._quit_other_accounts()

        assert pids == [5151, 6161]
        assert asked == ["b", "c"], "every peer but ourselves, in order"
        assert stub._status_label.label == "update_closing_other_accounts"

    def test_a_peer_that_fails_to_answer_does_not_abort_the_walk(self, monkeypatch, tmp_path):
        import ipc
        import node_coord
        import update_coord

        asked = []

        def _quit(_gd, acc):
            asked.append(acc)
            if acc == "b":
                raise OSError("pipe gone")
            return True

        monkeypatch.setattr(wx_call_after_module(), "CallAfter", lambda fn, *a: fn(*a))
        monkeypatch.setattr(update_coord, "other_live_leases", lambda _gd: [])
        monkeypatch.setattr(node_coord, "live_node_leases",
                            lambda _gd, is_alive: [{"account_id": "b"}, {"account_id": "c"}])
        monkeypatch.setattr(ipc, "request_quit", _quit)

        stub = _stub(_mw(str(tmp_path), "me"))
        stub._quit_other_accounts()
        assert asked == ["b", "c"]

    def test_a_peer_named_only_by_its_node_lease_is_still_waited_on(self, monkeypatch, tmp_path):
        """Reported 2026-10-18: two accounts open, the update ran "also waiting
        on no other account" while the second account (pid in its node lease)
        was still running, so the installer did not wait for it."""
        import ipc
        import node_coord
        import update_coord

        monkeypatch.setattr(wx_call_after_module(), "CallAfter", lambda fn, *a: fn(*a))
        monkeypatch.setattr(update_coord, "other_live_leases", lambda _gd: [])
        monkeypatch.setattr(node_coord, "live_node_leases", lambda _gd, is_alive: [
            {"account_id": "me", "pid": os.getpid()}, {"account_id": "b", "pid": 43216}])
        monkeypatch.setattr(ipc, "request_quit", lambda _gd, acc: True)

        assert _stub(_mw(str(tmp_path), "me"))._quit_other_accounts() == [43216]


class TestPeerPids:
    def test_both_sources_are_merged_without_repeating_a_pid(self):
        import update_coord
        runtime = [{"pid": 5151}, {"pid": 6161}]
        node = [{"account_id": "b", "pid": 6161}, {"account_id": "c", "pid": 7171}]
        assert update_coord.peer_pids(runtime, node, "me", 4242) == [5151, 6161, 7171]

    def test_our_own_account_pid_and_corrupt_entries_are_never_waited_on(self):
        import update_coord
        runtime = [{"pid": 4242}, {"pid": None, "_corrupt": True}, {"pid": True}]
        node = [{"account_id": "me", "pid": 9999}, {"account_id": "b", "_corrupt": True},
                {"account_id": "c", "pid": "123"}]
        assert update_coord.peer_pids(runtime, node, "me", 4242) == []

    def test_nothing_to_wait_on(self):
        import update_coord
        assert update_coord.peer_pids([], None, "me", 4242) == []


class TestClaimInstallSlot:
    def test_refuses_while_another_account_is_still_alive(self, monkeypatch, tmp_path):
        import functools
        import update_coord

        gd = str(tmp_path)
        alive = lambda pid, ct: True
        update_coord.try_create_runtime_lease(gd, pid=os.getpid(), create_time=0.0, is_alive=alive)
        update_coord.try_create_runtime_lease(gd, pid=999_999, create_time=1.0, is_alive=alive)
        # The real lease_alive() sees pid 999999 as dead, so pin liveness.
        monkeypatch.setattr(update_coord, "try_begin_update",
                            functools.partial(update_coord.try_begin_update, is_alive=alive))
        # A peer that never exits: the grace period is spent, then the same refusal.
        monkeypatch.setattr(updater, "_PEER_EXIT_GRACE_SECONDS", 0.05)
        monkeypatch.setattr(updater, "_PEER_EXIT_POLL_SECONDS", 0.01)
        stub = _stub(_mw(gd, "me"))
        assert stub._claim_install_slot() is None
        assert stub._claim_error == "update_other_accounts_running"

    def test_a_peer_that_is_still_exiting_is_waited_for_instead_of_refused(self, monkeypatch, tmp_path):
        """Reported 2026-11-11: with two accounts open the update failed with
        \"another account is running\" and only worked after answering Yes to
        retry. The peer had confirmed RELEASED but its process was still alive
        for a moment, and the claim ran at once."""
        import functools
        import update_coord

        gd = str(tmp_path)
        always = lambda pid, ct: True
        update_coord.try_create_runtime_lease(gd, pid=os.getpid(), create_time=0.0, is_alive=always)
        update_coord.try_create_runtime_lease(gd, pid=999_999, create_time=1.0, is_alive=always)
        polls = {"n": 0}

        def peer_alive(pid, ct):
            if pid == 999_999:
                polls["n"] += 1
                return polls["n"] <= 3  # dies on the fourth look
            return True

        monkeypatch.setattr(update_coord, "try_begin_update",
                            functools.partial(update_coord.try_begin_update, is_alive=peer_alive))
        monkeypatch.setattr(updater, "_PEER_EXIT_POLL_SECONDS", 0.01)
        stub = _stub(_mw(gd, "me"))
        token = stub._claim_install_slot()
        assert token and token.get("owner_token")
        assert polls["n"] >= 4, "it must have polled until the peer was gone"
        stub._end_install_slot(token)

    def test_cancelling_stops_the_wait_for_a_peer(self, monkeypatch, tmp_path):
        import functools
        import update_coord

        gd = str(tmp_path)
        alive = lambda pid, ct: True
        update_coord.try_create_runtime_lease(gd, pid=os.getpid(), create_time=0.0, is_alive=alive)
        update_coord.try_create_runtime_lease(gd, pid=999_999, create_time=1.0, is_alive=alive)
        monkeypatch.setattr(update_coord, "try_begin_update",
                            functools.partial(update_coord.try_begin_update, is_alive=alive))
        monkeypatch.setattr(updater, "_PEER_EXIT_GRACE_SECONDS", 60.0)
        monkeypatch.setattr(updater, "_PEER_EXIT_POLL_SECONDS", 0.01)
        stub = _stub(_mw(gd, "me"))
        stub._handoff.cancelled.set()
        started = time.monotonic()
        assert stub._claim_install_slot() is None
        assert time.monotonic() - started < 5, "a cancelled update must not sit out the grace period"

    def test_other_refusals_are_not_waited_out(self, monkeypatch, tmp_path):
        import update_coord

        stub = _stub(_mw(str(tmp_path), "me"))
        token = stub._claim_install_slot()
        monkeypatch.setattr(updater, "_PEER_EXIT_GRACE_SECONDS", 60.0)
        started = time.monotonic()
        try:
            assert stub._claim_install_slot() is None
            assert stub._claim_error == "update_install_in_progress"
        finally:
            stub._end_install_slot(token)
        assert time.monotonic() - started < 5

    def test_own_lease_alone_does_not_block_and_the_slot_is_released(self, tmp_path):
        import update_coord

        gd = str(tmp_path)
        update_coord.try_create_runtime_lease(gd, pid=os.getpid())
        stub = _stub(_mw(gd, "me"))
        token = stub._claim_install_slot()
        assert token and token.get("owner_token")
        assert update_coord.is_update_in_progress(gd) is True
        stub._end_install_slot(token)
        assert update_coord.is_update_in_progress(gd) is False

    def test_a_coordination_error_blocks_installation(self, monkeypatch, tmp_path):
        import update_coord

        def _boom(_gd, **kwargs):
            raise RuntimeError("lock dir vanished")

        monkeypatch.setattr(update_coord, "try_begin_update", _boom)
        stub = _stub(_mw(str(tmp_path), "me"))
        assert stub._claim_install_slot() is None
        assert stub._claim_error == "update_coordination_failed"

    def test_own_live_install_has_an_install_message_not_an_account_message(self, tmp_path):
        import update_coord
        stub = _stub(_mw(str(tmp_path), "me"))
        token = stub._claim_install_slot()
        assert token
        try:
            assert stub._claim_install_slot() is None
            assert stub._claim_error == "update_install_in_progress"
        finally:
            stub._end_install_slot(token)


class TestWorkerReleasesFailedInstallClaim:
    def test_an_installer_launch_exception_releases_the_claim(self, monkeypatch, tmp_path):
        archive = tmp_path / "update.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("WinZapp.exe", "replacement")

        class _Response:
            headers = {"content-length": str(archive.stat().st_size)}

            def raise_for_status(self):
                pass

            def iter_content(self, chunk_size):
                yield archive.read_bytes()

        token = {"owner_token": "token"}
        released = []
        dialog = types.SimpleNamespace(
            _zip_url="https://example.invalid/update.zip",
            _sha256sums_url="",
            _signature_url="",
            _new_version="1.2.3.4",
            _is_alpha=False,
            _cancelled=False,
            _install_ok=False,
            _handoff=InstallHandoff(),
            _request_install_handoff=lambda: True,
            _error_msg="",
            _gauge=types.SimpleNamespace(SetValue=lambda value: None),
            _status_label=_Label(),
            _main_window=types.SimpleNamespace(
                i18n=types.SimpleNamespace(t=lambda key: key), wpp_port=6300,
            ),
            _quit_other_accounts=lambda: [],
            _claim_install_slot=lambda: token,
            _end_install_slot=lambda value: released.append(value),
            EndModal=lambda result: None,
            IsModal=lambda: True,
        )
        dialog._worker = updater.UpdateProgressDialog._worker.__get__(dialog)

        monkeypatch.setattr(updater, "_is_frozen", lambda: True)
        monkeypatch.setattr(updater.tempfile, "tempdir", str(tmp_path))
        monkeypatch.setattr(updater.tls_trust, "get", lambda *args, **kwargs: _Response())
        monkeypatch.setattr(updater, "_verify_sha256sums", lambda *args, **kwargs: (True, ""))
        monkeypatch.setattr(updater, "_run_batch_installer",
                            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("launch failed")))
        monkeypatch.setattr(updater.wx, "CallAfter", lambda callback, *args: callback(*args))

        dialog._worker()

        assert released == [token]
        assert dialog._install_ok is False


def wx_call_after_module():
    return updater.wx
