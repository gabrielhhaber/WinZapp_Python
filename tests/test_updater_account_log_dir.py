"""update_install.log used to be written next to the exe (install_dir),
loose alongside WinZapp's own program files — the only writable-data file
in that whole directory, since everything else the app writes (settings,
database, and log.log/shutdown_audit.log themselves) lives under
data/accounts/<id>/. Reported live as confusing: looking in the account's
own logs/ folder for the update installer's log never found it.

_run_batch_installer() now resolves the log path through app_paths.
log_path() (the same function log.log/shutdown_audit.log go through) so
it lands in the current account's logs/ folder instead. update_failed.
marker deliberately stays in the install dir — main.py reads it at the
very start of __init__, before an account is even chosen.
"""

import os

import pytest

import updater


class TestBatchInstallerLogGoesToTheAccountLogsFolder:
    def test_the_log_path_passed_to_the_script_is_the_account_logs_dir(
        self, tmp_path, monkeypatch
    ):
        account_logs = tmp_path / "data" / "accounts" / "abc123" / "logs"
        install_dir = tmp_path / "install"
        install_dir.mkdir()
        extracted_dir = tmp_path / "extracted"
        extracted_dir.mkdir()

        monkeypatch.setattr(updater, "log_path", lambda *parts: os.path.join(str(account_logs), *parts))
        monkeypatch.setattr(updater, "_needs_admin", lambda: False)
        monkeypatch.setattr(updater.sys, "platform", "win32")

        captured = {}

        def _fake_write(bat_path, script):
            captured["script"] = script
            return True

        monkeypatch.setattr(updater, "_write_installer_script", _fake_write)
        monkeypatch.setattr(updater.subprocess, "Popen", lambda *a, **kw: None)

        ok = updater._run_batch_installer(
            str(extracted_dir), str(install_dir), "WinZapp.exe", pid=1234,
        )

        assert ok is True
        assert account_logs.is_dir(), "the account logs dir must be created if missing"
        script = captured["script"]
        assert str(account_logs) in script
        assert os.path.join(str(account_logs), "update_install.log") in script
        # The failure marker stays install-dir-side — read before any
        # account is chosen at startup.
        assert os.path.join(str(install_dir), "update_failed.marker") in script

    def test_falls_back_to_the_install_dir_if_no_account_is_active(
        self, tmp_path, monkeypatch
    ):
        """log_path() raises when app_paths has no active account set — the
        update must still proceed rather than crash the whole flow."""
        install_dir = tmp_path / "install"
        install_dir.mkdir()
        extracted_dir = tmp_path / "extracted"
        extracted_dir.mkdir()

        def _raise(*a, **kw):
            raise RuntimeError("no active account")

        monkeypatch.setattr(updater, "log_path", _raise)
        monkeypatch.setattr(updater, "_needs_admin", lambda: False)
        monkeypatch.setattr(updater.sys, "platform", "win32")

        captured = {}

        def _fake_write(bat_path, script):
            captured["script"] = script
            return True

        monkeypatch.setattr(updater, "_write_installer_script", _fake_write)
        monkeypatch.setattr(updater.subprocess, "Popen", lambda *a, **kw: None)

        ok = updater._run_batch_installer(
            str(extracted_dir), str(install_dir), "WinZapp.exe", pid=1234,
        )

        assert ok is True
        assert os.path.join(str(install_dir), "update_install.log") in captured["script"]
