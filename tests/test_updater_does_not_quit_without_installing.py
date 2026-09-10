"""The updater may only quit the app when an installer is actually running.

Reported live, in dev mode: the app found a newer release, downloaded it,
extracted it, logged "Dev mode detected. Skipping real installation." — and
then exited, taking the WPPConnect server with it. A pairing was in progress at
the time, so the symptom was "Nenhum código de pareamento foi recebido": the
process generating the code had been killed by its own updater.

The cause is a lie in the dialog's answer. `_install_ok` means "a batch
installer is running and will relaunch this process", which is the caller's one
licence to call real_exit(). The dev-mode branch set it True while launching
nothing at all. The declined-UAC path used to tell the same lie and was fixed
the same way — see the ShellExecuteW comment in updater.py.

UpdateProgressDialog is a wx.Dialog and UpdateChecker wants a real MainWindow,
so _do_install() is exercised as a plain function against stubs, the same way
tests/test_unread_reread_race.py drives MainWindow methods.
"""

import types

import pytest

import updater
from updater import UpdateChecker


class _Dialog:
    """Stands in for UpdateProgressDialog: whatever run() was told to answer."""

    def __init__(self, result, install_ok):
        self._result = result
        self._install_ok = install_ok
        self.destroyed = False
        self._error_msg = "boom"

    def run(self):
        return self._result

    def Destroy(self):
        self.destroyed = True


class _MainWindow:
    def __init__(self):
        self.exits = 0

    def real_exit(self):
        self.exits += 1


def _checker(monkeypatch, result, install_ok):
    """UpdateChecker with _do_install() bound, and its dialog replaced."""
    mw = _MainWindow()
    stub = types.SimpleNamespace(
        _mw=mw,
        _do_install=None,
        retries=0,
    )
    stub._schedule_retry = lambda: setattr(stub, "retries", stub.retries + 1)
    # Every exit from _do_install() that leaves the app running hands the
    # cross-account prompt claim back, so the next account may be offered the
    # update. Counted here so a branch that stops doing it is visible.
    stub.releases = 0
    stub._release_prompt = lambda: setattr(stub, "releases", stub.releases + 1)
    stub._do_install = types.MethodType(UpdateChecker._do_install, stub)

    dialog = _Dialog(result, install_ok)
    monkeypatch.setattr(updater, "UpdateProgressDialog",
                        lambda *a, **kw: dialog)
    return stub, mw, dialog


class TestItOnlyExitsForAnInstallerThatIsActuallyRunning:
    def test_a_real_install_still_quits_the_app(self, monkeypatch):
        """The frozen path: the batch script is running and is waiting for this
        process to exit before it can overwrite the files."""
        stub, mw, _dialog = _checker(monkeypatch, updater.wx.ID_OK, True)

        stub._do_install("9.9.9", "http://example/z.zip")

        assert mw.exits == 1

    def test_dev_mode_does_not_quit(self, monkeypatch):
        """Nothing was installed and nothing will relaunch us."""
        stub, mw, _dialog = _checker(monkeypatch, updater.wx.ID_OK, False)

        stub._do_install("9.9.9", "http://example/z.zip")

        assert mw.exits == 0

    def test_dev_mode_does_not_schedule_a_retry_either(self, monkeypatch):
        """There is nothing to retry: the download succeeded, the install is
        simply not applicable here. Re-prompting would reopen the dialog over
        and over for the rest of the dev session."""
        stub, mw, _dialog = _checker(monkeypatch, updater.wx.ID_OK, False)

        stub._do_install("9.9.9", "http://example/z.zip")

        assert stub.retries == 0

    def test_the_dialog_is_destroyed_either_way(self, monkeypatch):
        stub, _mw, dialog = _checker(monkeypatch, updater.wx.ID_OK, False)

        stub._do_install("9.9.9", "http://example/z.zip")

        assert dialog.destroyed

    def test_a_cancelled_update_retries_and_does_not_quit(self, monkeypatch):
        stub, mw, _dialog = _checker(monkeypatch, updater.wx.ID_CANCEL, False)

        stub._do_install("9.9.9", "http://example/z.zip")

        assert (mw.exits, stub.retries) == (0, 1)
