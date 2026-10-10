"""After an update installed with administrator rights, WinZapp comes back
without them.

The elevated installer used to end with `start "" WinZapp.exe`, so WinZapp,
its Node and every npm under it ran as Administrator after the update — the
report where an update broke the user's own npm. The relaunch now goes
through a waiter the non-elevated WinZapp starts. See client/update_relaunch.py.

_run_batch_installer() is driven with ShellExecuteExW, Popen and the script
writer replaced, so nothing is launched.
"""

import os
import re

import pytest

import update_relaunch
import updater
from update_relaunch import (
    WINZAPP_NODE_VARIABLES,
    build_relaunch_waiter_script,
    relaunch_environment,
    relaunch_lines,
)

EXE = r"C:\WinZapp\WinZapp.exe"
LOG = r"C:\L\update_install.log"
HANDOFF = r"C:\T\winzapp_upd_x.bat.relaunch"
START = f'if exist "{EXE}" start "" "{EXE}"'


def _script(**kw):
    return updater._build_installer_script(
        r"C:\tmp\ext", r"C:\WinZapp", EXE, LOG, r"C:\WinZapp\update_failed.marker",
        pid=4242, api_port=6300, **kw,
    )


class TestTheNonElevatedScriptIsUnchanged:
    def test_every_relaunch_is_the_original_line(self):
        s = _script(extra_pids=[5151])
        assert s.count(START) == 3  # xcopy failed, success, other account timed out
        assert ".relaunch" not in s
        assert relaunch_lines(EXE, LOG) == START + "\n"

    def test_no_handoff_is_the_default(self):
        assert _script() == _script(relaunch_handoff="")


class TestTheElevatedScriptLeavesTheRelaunchToTheWaiter:
    def test_every_relaunch_is_conditional_on_the_marker(self):
        s = _script(extra_pids=[5151], relaunch_handoff=HANDOFF)
        assert s.count(f'if exist "{HANDOFF}" (') == 3
        assert s.count(START) == 3, "each one still relaunches by itself when no waiter runs"
        for block in s.split(f'if exist "{HANDOFF}" (')[1:]:
            waiter_side, own_side = block.split(") else (", 1)
            assert "start" not in waiter_side
            assert own_side.lstrip().startswith(START)

    def test_every_exit_path_tells_the_waiter_it_is_done(self):
        """Written after the relaunch decision on all three exits, so a
        waiter whose pid check was fooled by a reused pid still stops — and
        only on the waiter's side, so no waiter leaves no .done behind."""
        s = _script(extra_pids=[5151], relaunch_handoff=HANDOFF)
        done = f'echo done> "{HANDOFF}.done"'
        assert s.count(done) == 3
        for block in s.split(f'if exist "{HANDOFF}" (')[1:]:
            waiter_side, own_side = block.split(") else (", 1)
            assert done in waiter_side
            assert done not in own_side.split(")\n", 1)[0]

    def test_the_failure_paths_still_mark_the_failure_first(self):
        s = _script(extra_pids=[5151], relaunch_handoff=HANDOFF)
        assert s.index('echo update failed >') < s.index(f'if exist "{HANDOFF}"')
        assert "exit /b 1" in s.split("xcopy FAILED", 1)[1].split("xcopy OK", 1)[0]


class TestTheWaiter:
    def test_creating_the_marker_is_its_first_action(self):
        w = build_relaunch_waiter_script(9001, EXE, LOG, HANDOFF)
        lines = w.splitlines()
        assert lines[2] == f'echo waiting> "{HANDOFF}"'
        assert lines[3] == f'if not exist "{HANDOFF}" exit /b 1'

    def test_it_stops_on_done_on_the_pid_or_on_its_limit(self):
        w = build_relaunch_waiter_script(9001, EXE, LOG, HANDOFF, passes=120)
        loop = w.split(":WAIT\n", 1)[1].split(":GAVE_UP", 1)[0]
        assert f'if exist "{HANDOFF}.done" goto RELAUNCH' in loop
        assert 'tasklist /FI "PID eq 9001" /FI "IMAGENAME eq cmd.exe"' in loop
        assert "if errorlevel 1 goto RELAUNCH" in loop
        assert "if !WAITED! GEQ 120 goto GAVE_UP" in loop
        # Each pass sleeps a second: at least 30 minutes, honestly named.
        assert update_relaunch.WAITER_PASSES == 30 * 60
        assert loop.index("tasklist") < loop.index("timeout /t 1 /nobreak")

    def test_giving_up_relaunches_but_keeps_the_marker(self):
        """A script still running then sees the marker and does not start a
        second copy."""
        w = build_relaunch_waiter_script(9001, EXE, LOG, HANDOFF)
        gave_up = w.split(":GAVE_UP", 1)[1].split(":RELAUNCH", 1)[0]
        assert START in gave_up
        assert HANDOFF not in gave_up

    def test_a_normal_end_relaunches_and_cleans_up(self):
        w = build_relaunch_waiter_script(9001, EXE, LOG, HANDOFF)
        end = w.split(":RELAUNCH\n", 1)[1]
        assert f'>> "{LOG}" echo' in end, "the relaunch is recorded in update_install.log"
        assert end.index(START) < end.index(f'del "{HANDOFF}"')
        assert f'del "{HANDOFF}.done"' in end and 'del "%~f0"' in end


class TestTheRelaunchedEnvironment:
    def test_it_is_what_a_fresh_start_gets(self):
        env = {"PATH": "p", "USERPROFILE": "u", "_PYI_APPLICATION_HOME_DIR": "x",
               "_pyi_archive_file": "y", "AUTHENTICATION_API_KEY": "secret",
               "PORT": "6300", "Winzapp_Instance_Id": "abc"}
        child = relaunch_environment(env)
        assert child == {"PATH": "p", "USERPROFILE": "u", "PYINSTALLER_RESET_ENVIRONMENT": "1"}
        assert "AUTHENTICATION_API_KEY" in env, "the caller's mapping is never modified"

    def test_every_variable_winzapp_puts_in_its_environment_is_dropped(self):
        """Structural guard: a new os.environ[...] = in wpp_server must be
        added to WINZAPP_NODE_VARIABLES too, or a relaunch inherits it."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "client", "main_window", "wpp_server.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        assigned = set(re.findall(r'os\.environ\["([A-Z_]+)"\]\s*=', src))
        assert assigned and assigned <= set(WINZAPP_NODE_VARIABLES)


@pytest.fixture
def install(tmp_path, monkeypatch):
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    monkeypatch.setattr(updater, "log_path", lambda *p: str(tmp_path / "logs" / os.path.join(*p)))
    monkeypatch.setattr(updater.sys, "platform", "win32")
    monkeypatch.setattr(updater, "own_node_pids", lambda port, paths: [])
    monkeypatch.setattr(updater, "_is_elevated", lambda: False)
    monkeypatch.setattr(updater.tempfile, "tempdir", str(tmp_path))
    written = {}

    def _write(path, script):
        written[path] = script
        with open(path, "w", encoding="utf-8") as fh:  # so cleanup can be checked
            fh.write(script)
        return True

    monkeypatch.setattr(updater, "_write_installer_script", _write)
    popen = []

    class _Proc:
        killed = False

        def kill(self):
            _Proc.killed = True

        def wait(self, timeout=None):
            return 0

    state = {"waiter_creates_marker": True, "proc": _Proc}

    def _popen(cmd, **kw):
        popen.append((cmd, kw))
        if cmd[2].endswith("_relaunch.bat") and state["waiter_creates_marker"]:
            # What the waiter's first line does.
            handoff = re.search(r'echo waiting> "([^"]+)"', written[cmd[2]]).group(1)
            with open(handoff, "w") as fh:
                fh.write("waiting\n")
        return _Proc()

    monkeypatch.setattr(update_relaunch.subprocess, "Popen", _popen)
    monkeypatch.setattr(updater.subprocess, "Popen", _popen)
    monkeypatch.setattr(update_relaunch.time, "sleep", lambda s: None)
    elevated = []

    def run(needs_admin=True, result=(True, 9001), waiter_creates_marker=True):
        state["waiter_creates_marker"] = waiter_creates_marker
        monkeypatch.setattr(updater, "_needs_admin", lambda: needs_admin)

        def _run_elevated(bat):
            elevated.append(bat)
            return result

        monkeypatch.setattr(updater, "run_elevated", _run_elevated)
        ok = updater._run_batch_installer(str(extracted), str(install_dir), "WinZapp.exe", pid=1234)
        return ok, written, popen, elevated

    run.state = state
    return run


class TestRunBatchInstaller:
    def test_elevated_hands_the_relaunch_to_a_waiter_that_created_the_marker(self, install):
        ok, written, popen, elevated = install()
        assert ok is True
        bat = elevated[0]
        waiter = bat[:-len(".bat")] + "_relaunch.bat"
        assert [c for c, kw in popen] == [["cmd.exe", "/c", waiter]]
        assert popen[0][1]["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
        assert "PID eq 9001" in written[waiter]
        assert os.path.isfile(bat + ".relaunch")
        assert f'if exist "{bat}.relaunch" (' in written[bat]
        assert not install.state["proc"].killed

    def test_a_waiter_that_never_creates_the_marker_is_stopped(self, install, monkeypatch):
        """Then the script relaunches by itself: never zero, never two."""
        monkeypatch.setattr(update_relaunch.time, "monotonic",
                            iter(range(0, 10_000, 5)).__next__)
        ok, written, popen, elevated = install(waiter_creates_marker=False)
        assert ok is True, "the installer itself is running"
        assert install.state["proc"].killed
        assert not os.path.exists(elevated[0] + ".relaunch")
        assert not os.path.exists(elevated[0][:-len(".bat")] + "_relaunch.bat")

    def test_the_marker_path_in_the_script_is_console_safe(self, install, tmp_path, monkeypatch):
        """The marker is the one path written into the script that lives
        under the user's profile (%TEMP%), which can be non-ASCII (issue
        #83). Stand-in for the 8.3 form: another spelling of the same dir."""
        real = updater._console_safe_path
        safe_tmp = os.path.join(str(tmp_path), ".")
        monkeypatch.setattr(updater, "_console_safe_path",
                            lambda p: safe_tmp if p == str(tmp_path) else real(p))
        ok, written, popen, elevated = install()
        marker = os.path.join(safe_tmp, os.path.basename(elevated[0]) + ".relaunch")
        assert f'if exist "{marker}" (' in written[elevated[0]]
        assert os.path.isfile(marker)

    def test_a_temp_folder_with_no_ascii_path_falls_back_to_the_old_relaunch(
            self, install, tmp_path, monkeypatch):
        """No 8.3 names and a profile like "Paweł": the marker path cannot go
        into the script, so there is no waiter — the update still runs."""
        real = updater._console_safe_path
        monkeypatch.setattr(updater, "_console_safe_path",
                            lambda p: "C:\\Users\\Pawe\u0142\\Temp" if p == str(tmp_path) else real(p))
        ok, written, popen, elevated = install()
        assert ok is True
        assert popen == [], "no waiter"
        script = written[elevated[0]]
        assert ".relaunch" not in script
        assert script.count('start ""') == 2, "the script relaunches by itself, as before"

    def test_a_declined_uac_prompt_launches_nothing(self, install):
        ok, written, popen, elevated = install(result=(False, None))
        assert ok is False
        assert popen == []
        assert not os.path.exists(elevated[0] + ".relaunch")

    def test_no_installer_pid_leaves_the_relaunch_to_the_script(self, install):
        ok, written, popen, elevated = install(result=(True, None))
        assert ok is True
        assert popen == [], "nothing to wait on, so no waiter"
        assert not os.path.exists(elevated[0] + ".relaunch")

    def test_a_waiter_that_cannot_start_leaves_the_relaunch_to_the_script(self, install, monkeypatch):
        def _fail(cmd, **kw):
            raise OSError("no cmd.exe")

        monkeypatch.setattr(update_relaunch.subprocess, "Popen", _fail)
        ok, written, popen, elevated = install()
        assert ok is True, "the installer itself is running"
        assert not os.path.exists(elevated[0] + ".relaunch")

    def test_not_elevated_runs_the_script_directly_with_a_fresh_environment(self, install):
        ok, written, popen, elevated = install(needs_admin=False)
        assert ok is True
        assert elevated == []
        assert len(popen) == 1 and popen[0][0][:2] == ["cmd.exe", "/c"]
        assert popen[0][1]["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
        script = written[popen[0][0][2]]
        assert ".relaunch" not in script
        assert script.count('start ""') == 2

    def test_an_already_elevated_winzapp_is_logged_and_left_elevated(self, install, monkeypatch, caplog):
        """Not de-elevated: some people run WinZapp as administrator on
        purpose (update_relaunch.py)."""
        monkeypatch.setattr(updater, "_is_elevated", lambda: True)
        with caplog.at_level("INFO"):
            ok, written, popen, elevated = install(needs_admin=False)
        assert ok is True and elevated == []
        assert "running elevated" in caplog.text


@pytest.mark.skipif(os.name != "nt", reason="wintypes only has Windows sizes on Windows")
def test_shellexecuteinfo_matches_the_windows_layout():
    """run_elevated() cannot be run in a test (it raises a UAC prompt), so
    at least the structure it hands to ShellExecuteExW is checked against
    shellapi.h: a wrong size makes the call fail as if UAC were declined."""
    import ctypes

    from update_relaunch import SHELLEXECUTEINFOW

    if ctypes.sizeof(ctypes.c_void_p) == 8:
        assert ctypes.sizeof(SHELLEXECUTEINFOW) == 112
        assert SHELLEXECUTEINFOW.hProcess.offset == 104
    else:
        assert ctypes.sizeof(SHELLEXECUTEINFOW) == 60
