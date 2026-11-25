"""restart_mac.py: the Mac restart behind the pairing dialog's engine switch."""
import os
import subprocess
import sys
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path[:0] = [os.path.join(ROOT, "macos"), os.path.join(ROOT, "client")]

from core import app_restart  # noqa: E402
from winzapp_mac import restart_mac  # noqa: E402

TRANSIENT = app_restart.TRANSIENT_ARGUMENTS


def _bundle(tmp_path, name="WinZapp.app"):
    macos_dir = tmp_path / "Somewhere Else" / name / "Contents" / "MacOS"
    macos_dir.mkdir(parents=True)
    exe = macos_dir / "WinZapp"
    exe.write_text("")
    return str(tmp_path / "Somewhere Else" / name), str(exe)


def test_a_frozen_build_reopens_its_own_bundle_by_absolute_path(tmp_path):
    app, exe = _bundle(tmp_path)
    command = restart_mac.restart_command(
        exe, ["main.py", "--account", "a1", "--startup-source", "user"], True,
        "/x/launcher.py", TRANSIENT)
    assert command == ["/usr/bin/open", "-n", os.path.realpath(app),
                       "--args", "--account", "a1", "--startup-source", "user"]
    assert os.path.isabs(command[2])
    assert "-a" not in command          # never by name: LaunchServices may pick another copy


def test_transient_and_system_arguments_do_not_follow(tmp_path):
    app, exe = _bundle(tmp_path)
    command = restart_mac.restart_command(
        exe, ["main.py", "--post-update", "--account", "a1", "--background", "-psn_0_12345"],
        True, "/x/launcher.py", TRANSIENT)
    assert command == ["/usr/bin/open", "-n", os.path.realpath(app), "--args", "--account", "a1"]


def test_no_arguments_means_no_args_flag(tmp_path):
    app, exe = _bundle(tmp_path)
    assert restart_mac.restart_command(exe, ["main.py"], True, "/x/l.py", TRANSIENT) == [
        "/usr/bin/open", "-n", os.path.realpath(app)]


def test_an_executable_outside_a_bundle_is_run_directly(tmp_path):
    exe = tmp_path / "WinZapp"
    exe.write_text("")
    assert restart_mac.restart_command(str(exe), ["main.py", "--account", "a"], True,
                                       "/x/l.py", TRANSIENT) == [
        os.path.realpath(str(exe)), "--account", "a"]


def test_a_development_run_restarts_through_the_launcher():
    """launcher.py installs the Mac layer; client/main.py alone would not."""
    command = restart_mac.restart_command(
        "/usr/bin/python3", ["/repo/client/main.py", "--account", "a"], False,
        "/repo/macos/launcher.py", TRANSIENT)
    assert command == ["/usr/bin/python3", "/repo/macos/launcher.py", "--account", "a"]


def test_the_helper_waits_for_the_pid_bounded_and_logs():
    argv = restart_mac.helper_command(4242, ["/usr/bin/open", "-n", "/A/WinZapp.app"],
                                      "/logs/restart.log", 240)
    assert argv[:4] == ["/bin/sh", "-c", restart_mac.RESTART_SCRIPT, "winzapp-restart"]
    assert argv[4:] == ["4242", "240", "/logs/restart.log", "/usr/bin/open", "-n",
                        "/A/WinZapp.app"]
    assert 'kill -0 "$pid"' in restart_mac.RESTART_SCRIPT
    assert '-ge "$limit"' in restart_mac.RESTART_SCRIPT


def _run_helper(tmp_path, pid, passes):
    out = tmp_path / "ran"
    log = tmp_path / "restart.log"
    helper = subprocess.Popen(restart_mac.helper_command(
        pid, ["/bin/sh", "-c", f'echo ran > "{out}"'], str(log), passes))
    return helper, out, log


def test_the_script_runs_the_command_once_the_process_is_gone(tmp_path):
    waited = subprocess.Popen(["/bin/sleep", "1"])
    helper, out, log = _run_helper(tmp_path, waited.pid, 30)
    time.sleep(0.3)
    assert not out.exists()             # still waiting on the live pid
    waited.wait()
    assert helper.wait(timeout=10) == 0
    assert out.read_text().strip() == "ran"
    assert "restarting WinZapp" in log.read_text()


def test_the_script_gives_up_on_a_process_that_does_not_end(tmp_path):
    stuck = subprocess.Popen(["/bin/sleep", "30"])
    try:
        helper, out, log = _run_helper(tmp_path, stuck.pid, 2)
        assert helper.wait(timeout=10) == 1
        assert not out.exists()
        assert "not restarting" in log.read_text()
    finally:
        stuck.kill()
        stuck.wait()


def test_the_helper_is_detached_with_a_clean_environment(monkeypatch):
    calls = []
    monkeypatch.setenv("WINZAPP_INSTANCE_ID", "x")
    monkeypatch.setenv("_PYI_APPLICATION_HOME_DIR", "/gone")
    assert restart_mac.restart_this_program("ignored.bat", "/l/restart.log",
                                            popen=lambda argv, **kw: calls.append((argv, kw)))
    argv, kw = calls[0]
    assert argv[4] == str(os.getpid())
    assert argv[5] == str(app_restart.WAIT_PASSES)
    assert kw["start_new_session"] is True
    assert "WINZAPP_INSTANCE_ID" not in kw["env"]
    assert "_PYI_APPLICATION_HOME_DIR" not in kw["env"]


def test_a_helper_that_cannot_start_is_reported():
    def refuse(argv, **kw):
        raise OSError("no /bin/sh")
    assert restart_mac.restart_this_program("x.bat", "", popen=refuse) is False


def test_install_replaces_the_batch_restart(monkeypatch):
    monkeypatch.setattr(app_restart, "restart_this_program", app_restart.restart_this_program)
    restart_mac.install()
    assert app_restart.restart_this_program is restart_mac.restart_this_program


def test_the_pairing_dialog_switch_no_longer_reports_a_failure(monkeypatch):
    """connect.py passes app_restart.restart_this_program, read at call time,
    so the Mac one is what switch_and_restart runs."""
    from ui.dialogs import connect_engine

    monkeypatch.setattr(app_restart, "restart_this_program", app_restart.restart_this_program)
    restart_mac.install()
    started = []
    monkeypatch.setattr(restart_mac.subprocess, "Popen",
                        lambda argv, **kw: started.append(argv))
    monkeypatch.setattr(connect_engine, "data_path", lambda name: "/d/" + name)
    monkeypatch.setattr(connect_engine, "log_path", lambda name: "/l/" + name)
    boxes = []
    monkeypatch.setattr(connect_engine.wx, "MessageBox", lambda *a, **k: boxes.append(a))
    saved = []
    window = types.SimpleNamespace(settings={"connection": {"wpp_engine": "wppconnect"}},
                                   save_settings=lambda: saved.append(1))
    i18n = types.SimpleNamespace(t=lambda key: key)

    assert connect_engine.switch_and_restart(window, i18n, "neonize",
                                             app_restart.restart_this_program)
    assert boxes == []
    assert window.settings["connection"]["wpp_engine"] == "neonize"
    assert started and started[0][:2] == ["/bin/sh", "-c"]
    assert started[0][6] == "/l/restart.log"
    source = open(os.path.join(ROOT, "client", "ui", "dialogs", "connect.py"),
                  encoding="utf-8").read()
    assert "app_restart.restart_this_program)" in source
