"""Exercise the real updater worker with synthetic packages and no processes/UI."""

import types

import pytest

import updater
from core.update_handoff import InstallHandoff
from update_package import UpdatePackage
from update_progress import UpdateProgressLifecycle


@pytest.fixture
def worker(monkeypatch):
    pending, launched, released, discarded, ended = [], [], [], [], []
    token = {"owner_pid": 42, "owner_create_time": 1.0, "owner_token": "attempt"}
    d = types.SimpleNamespace(
        _handoff=InstallHandoff(), _extracted_dir="synthetic-package",
        _install_ok=False, _error_msg="", _zip_url="https://example.invalid/z.zip",
        _sha256sums_url="", _signature_url="", _new_version="9.9.9", _is_alpha=False,
        _main_window=types.SimpleNamespace(i18n=types.SimpleNamespace(t=lambda k: k)),
        _gauge=types.SimpleNamespace(SetValue=lambda n: None),
        _status_label=types.SimpleNamespace(SetLabel=lambda s: None),
        _cancel_btn=types.SimpleNamespace(Disable=lambda: None),
        IsModal=lambda: True, EndModal=lambda code: ended.append(code),
        _quit_other_accounts=lambda: [], _claim_install_slot=lambda: token,
        _end_install_slot=lambda t: released.append(t),
    )
    d._grant_install_handoff = types.MethodType(UpdateProgressLifecycle._grant_install_handoff, d)
    d._request_install_handoff = types.MethodType(UpdateProgressLifecycle._request_install_handoff, d)
    monkeypatch.setattr(updater, "_is_frozen", lambda: True)
    monkeypatch.setattr(updater, "_outer_exe_dir", lambda: "synthetic-install")
    monkeypatch.setattr(updater, "discard_package", lambda path: discarded.append(path))
    monkeypatch.setattr(updater, "download_update_package", lambda *a, **kw: UpdatePackage("downloaded"))
    monkeypatch.setattr(updater, "_run_batch_installer",
                        lambda *a, **kw: launched.append(a) or True)
    # A controlled UI scheduler; no wx.App or worker thread is created.
    monkeypatch.setattr(updater.wx, "CallAfter", lambda fn, *a: pending.append((fn, a)))

    def flush():
        while pending:
            fn, args = pending.pop(0)
            fn(*args)

    d._handoff.decided = types.SimpleNamespace(wait=flush, set=lambda: None)
    return types.SimpleNamespace(dialog=d, launched=launched, released=released,
                                 discarded=discarded, ended=ended, flush=flush, token=token)


def _run(w):
    updater.UpdateProgressDialog._worker(w.dialog)
    assert w.dialog._handoff.settled.is_set()
    w.flush()


class TestWorkerHandoff:
    def test_worker_passes_its_language_to_the_outside_recovery_note(self, worker, monkeypatch):
        captured = []
        worker.dialog._main_window.i18n.t = lambda key: (
            "Translated recovery" if key == "update_recovery_instructions" else key)
        monkeypatch.setattr(updater, "_run_batch_installer",
                            lambda *args, **kwargs: captured.append(kwargs) or True)
        _run(worker)
        assert captured[0]["recovery_text"] == "Translated recovery"

    def test_cancelled_claim_allows_retry_in_the_same_process(self, worker, tmp_path, monkeypatch):
        import functools
        import update_coord
        d = worker.dialog
        d._main_window.global_dir = str(tmp_path)
        d._main_window.account_id = "synthetic"
        original_claim = update_coord.try_begin_update
        monkeypatch.setattr(update_coord, "try_begin_update",
                            functools.partial(original_claim, pid=42, create_time=1.0,
                                              is_alive=lambda pid, ct: True))
        for name in ("_coord", "_claim_install_slot", "_end_install_slot"):
            setattr(d, name, types.MethodType(getattr(updater.UpdateProgressDialog, name), d))
        claim = d._claim_install_slot
        def cancel_after_claim():
            token = claim()
            d._handoff.cancel()
            return token
        d._claim_install_slot = cancel_after_claim
        _run(worker)
        assert worker.launched == []
        retry = original_claim(str(tmp_path), pid=42, create_time=1.0,
                               is_alive=lambda pid, ct: True, explain=True)
        assert retry.token and not retry.reason
        assert update_coord.end_update(str(tmp_path), retry.token)

    def test_launch_retains_token_and_package(self, worker):
        _run(worker)
        assert len(worker.launched) == 1
        assert worker.released == worker.discarded == []
        assert worker.dialog._install_ok
        assert worker.ended == [updater.wx.ID_OK]

    @pytest.mark.parametrize("boundary", ["download", "peers", "claim", "queued_grant"])
    def test_cancel_at_each_prelaunch_boundary_never_spawns(self, worker, monkeypatch, boundary):
        d = worker.dialog
        if boundary == "download":
            d._extracted_dir = ""

            def download(*a, **kw):
                d._handoff.cancel()
                return UpdatePackage("downloaded")

            monkeypatch.setattr(updater, "download_update_package", download)
        elif boundary == "peers":
            d._quit_other_accounts = lambda: d._handoff.cancel() and []
        elif boundary == "claim":
            def claim():
                d._handoff.cancel()
                return worker.token
            d._claim_install_slot = claim
        else:
            grant = d._grant_install_handoff

            def cancel_first():
                d._handoff.cancel()
                grant()

            d._grant_install_handoff = cancel_first
        _run(worker)
        assert worker.launched == []
        assert not d._install_ok
        assert worker.released == ([worker.token] if boundary in ("claim", "queued_grant") else [])
        assert worker.discarded == (["downloaded"] if boundary == "download" else [])

    @pytest.mark.parametrize("failure", ["declined", "not_ready", "exception"])
    def test_failed_launch_releases_only_its_token_and_keeps_prefetched_package(self, worker, monkeypatch, failure):
        def launch(*a, **kw):
            if failure == "exception":
                raise OSError("mock launch failure")
            if failure == "declined":
                kw["install_guard"]["error"] = "update_uac_declined"
            return False

        monkeypatch.setattr(updater, "_run_batch_installer", launch)
        _run(worker)
        assert worker.released == [worker.token]
        assert worker.discarded == []
        assert not worker.dialog._install_ok
        assert worker.ended == [updater.wx.ID_ABORT]
        assert worker.dialog._error_msg == (
            "update_uac_declined" if failure == "declined" else
            "update_coordination_failed" if failure == "not_ready" else "mock launch failure")

    def test_close_after_grant_does_not_undo_a_successful_spawn(self, worker, monkeypatch):
        def launch(*a, **kw):
            assert not worker.dialog._handoff.cancel()
            worker.dialog.IsModal = lambda: False
            return True

        monkeypatch.setattr(updater, "_run_batch_installer", launch)
        _run(worker)
        assert worker.dialog._install_ok
        assert worker.released == worker.discarded == []
        assert worker.ended == [], "late EndModal is safely ignored"

    def test_download_error_settles_without_claim_or_spawn(self, worker, monkeypatch):
        worker.dialog._extracted_dir = ""
        monkeypatch.setattr(updater, "download_update_package",
                            lambda *a, **kw: UpdatePackage(error="bad checksum"))
        _run(worker)
        assert worker.launched == worker.released == []
        assert worker.dialog._error_msg == "bad checksum"
        assert worker.ended == [updater.wx.ID_ABORT]

    def test_refused_claim_uses_specific_reason(self, worker):
        worker.dialog._claim_install_slot = lambda: None
        worker.dialog._claim_error = "update_install_in_progress"
        _run(worker)
        assert worker.launched == worker.released == []
        assert worker.dialog._error_msg == "update_install_in_progress"

    def test_successful_downloaded_package_is_not_discarded(self, worker):
        worker.dialog._extracted_dir = ""
        _run(worker)
        assert worker.dialog._install_ok and worker.discarded == []
