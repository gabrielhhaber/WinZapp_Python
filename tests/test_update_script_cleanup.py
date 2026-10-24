"""The installer script removes the extracted update when the copy worked.

Every update left a full copy of the release in the user's %TEMP% because
nothing ever deleted it: the script removed itself and nothing else. The line
that does is placed where it cannot hurt — after a successful copy, after the
relaunch (the user is waiting for the app, not for a few thousand deletes), and
never on the failure paths, where the folder is evidence and the retry's source.
"""

import os

import updater

EXE = r"C:\WinZapp\WinZapp.exe"
LOG = r"C:\L\update_install.log"
EXTRACTED = r"C:\Users\me\AppData\Local\Temp\winzapp_ext_ab12"
REMOVE = f'rmdir /S /Q "{EXTRACTED}"'


def _script(**kw):
    return updater._build_installer_script(
        EXTRACTED + r"\WinZapp", r"C:\WinZapp", EXE, LOG,
        r"C:\WinZapp\update_failed.marker", pid=4242, api_port=6300, **kw,
    )


class TestTheCleanupLine:
    def test_it_is_in_the_script_when_a_folder_is_given(self):
        assert REMOVE in _script(cleanup_dir=EXTRACTED)

    def test_without_a_folder_the_script_is_what_it_was(self):
        assert "rmdir" not in _script()
        assert _script() == _script(cleanup_dir="")

    def test_a_directory_that_is_already_gone_is_not_an_error(self):
        assert f'if exist "{EXTRACTED}" {REMOVE}' in _script(cleanup_dir=EXTRACTED)

    def test_it_comes_after_the_copy_succeeded_and_the_app_was_relaunched(self):
        s = _script(cleanup_dir=EXTRACTED)
        copied = s.index("echo xcopy OK")
        # The failure branch relaunches too, earlier in the script: it is the
        # relaunch after the success marker that has to come first.
        assert copied < s.index(f'start "" /normal "{EXE}"', copied) < s.index(REMOVE)

    def test_it_comes_before_the_script_deletes_itself(self):
        s = _script(cleanup_dir=EXTRACTED)
        assert s.index(REMOVE) < s.index('del "%~f0"')

    def test_a_failed_copy_keeps_the_folder(self):
        s = _script(cleanup_dir=EXTRACTED)
        failure = s[s.index("echo xcopy FAILED"):s.index("echo xcopy OK")]
        assert "rmdir" not in failure

    def test_timing_out_on_another_account_keeps_it_too(self):
        s = _script(extra_pids=[5151], cleanup_dir=EXTRACTED)
        timeout = s[s.index(":OTHER_ACCOUNT_TIMEOUT"):]
        assert "rmdir" not in timeout

    def test_the_log_says_it_was_attempted(self):
        assert "temporary update folder cleaned up" in _script(cleanup_dir=EXTRACTED)

    def test_the_elevated_script_cleans_up_after_handing_the_relaunch_over(self):
        handoff = r"C:\T\winzapp_upd_x.bat.relaunch"
        s = _script(cleanup_dir=EXTRACTED, relaunch_handoff=handoff)
        assert s.index(f'echo done> "{handoff}.done"') < s.index(REMOVE)


class TestWhatTheInstallerPassesIt:
    """_run_batch_installer itself, stopped where the script would be written:
    nothing is launched and nothing outside tmp_path is touched."""

    def _cleanup_dir_for(self, monkeypatch, tmp_path, extracted):
        seen = {}

        def build(*args, **kwargs):
            seen.update(kwargs)
            return "script"

        def mkstemp(suffix="", prefix=""):
            path = tmp_path / f"{prefix}x{suffix}"
            path.write_text("")
            return os.open(path, os.O_RDONLY), str(path)

        monkeypatch.setattr(updater, "_build_installer_script", build)
        monkeypatch.setattr(updater, "_write_installer_script", lambda path, script: False)
        monkeypatch.setattr(updater, "_keep_newer_installed_server", lambda *a: None)
        monkeypatch.setattr(updater, "own_node_pids", lambda *a: [])
        monkeypatch.setattr(updater, "log_path", lambda: str(tmp_path / "logs"))
        monkeypatch.setattr(updater, "_console_safe_path", lambda path: path)
        # _needs_admin() probes the real install folder by writing a file.
        monkeypatch.setattr(updater, "_needs_admin", lambda: False)
        monkeypatch.setattr(updater, "_is_elevated", lambda: False)
        monkeypatch.setattr(updater.tempfile, "mkstemp", mkstemp)
        assert updater._run_batch_installer(str(extracted), str(tmp_path / "install"),
                                            "WinZapp.exe", 4242) is False
        return seen["cleanup_dir"]

    def test_it_is_handed_the_root_not_the_folder_inside_it(self, monkeypatch, tmp_path):
        root = tmp_path / "winzapp_ext_ab12"
        (root / "WinZapp").mkdir(parents=True)
        assert self._cleanup_dir_for(monkeypatch, tmp_path, root / "WinZapp") == str(root)

    def test_a_folder_not_named_like_ours_is_never_handed_over(self, monkeypatch, tmp_path):
        other = tmp_path / "Downloads" / "WinZapp"
        other.mkdir(parents=True)
        assert self._cleanup_dir_for(monkeypatch, tmp_path, other) == ""
