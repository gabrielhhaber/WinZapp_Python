"""Copy outcomes and standalone recovery notes; no processes or UI run."""
from pathlib import Path

import pytest


@pytest.mark.parametrize("outcome,action", [
    ("success", "relaunch"), ("unchanged", "relaunch"), ("aborted", "abort"),
    ("failed", "recover"), ("", "recover"), (None, "recover"),
    ("pending", "recover"), ("SUCCESS", "recover"), ("success\nfailed", "recover"),
])
def test_only_confirmed_success_or_unchanged_payload_can_relaunch(outcome, action):
    from core.update_recovery import outcome_action
    assert outcome_action(outcome) == action


def test_recovery_note_is_prepared_as_unicode_text_without_running_a_viewer(tmp_path):
    from core.update_recovery import write_recovery_note
    note = tmp_path / "recovery.txt"
    write_recovery_note(str(note), "Kapalı tutun.\n{install_dir}\n{log_path}",
                        "C:\\Örnek\\WinZapp", "C:\\Örnek\\log.txt")
    assert note.read_bytes().startswith(b"\xef\xbb\xbf")
    assert note.read_text(encoding="utf-8-sig") == (
        "Kapalı tutun.\nC:\\Örnek\\WinZapp\nC:\\Örnek\\log.txt")
    assert not Path(str(note) + ".tmp").exists()


def test_failed_note_preparation_does_not_leave_a_partial_report(tmp_path, monkeypatch):
    from core import update_recovery as recovery
    note = tmp_path / "recovery.txt"
    def fail(*args):
        raise OSError("synthetic disk failure")
    monkeypatch.setattr(recovery.os, "replace", fail)
    with pytest.raises(OSError):
        recovery.write_recovery_note(str(note), "repair {install_dir}", "install", "log")
    assert not note.exists() and not Path(str(note) + ".tmp").exists()


@pytest.mark.parametrize("failure", ["missing_translation", "disk"])
def test_recovery_preparation_failure_never_spawns_an_installer(tmp_path, monkeypatch, failure):
    from core import update_guard_launch as launch
    from core import update_install_guard as guard
    paths = guard.guard_paths(str(tmp_path), "a" * 32)
    spawned = []
    if failure == "disk":
        def fail(*args):
            raise OSError("synthetic disk failure")
        monkeypatch.setattr(launch, "write_recovery_note", fail)
    assert not launch.launch_guarded(
        str(tmp_path / "run.bat"), paths, "", str(tmp_path / "handoff"), "exe", "log", 111,
        True, lambda *args: True, lambda *args: spawned.append("uac") or (True, 222),
        lambda *args, **kwargs: spawned.append("child"), None, {},
        recovery_text="" if failure == "missing_translation" else "Recovery")
    assert spawned == [] and not Path(paths["commit"]).exists()
    assert guard.read_signal(paths["abort"]) == "abort"


@pytest.mark.parametrize("locale", ["pt-BR", "pt-PT", "en-US", "es-ES", "pl", "tr-TR", "ro"])
def test_every_locale_can_prepare_the_standalone_recovery_note(tmp_path, locale):
    from tests.locales import load_strings
    from core.update_recovery import write_recovery_note
    messages = load_strings(locale)
    note = tmp_path / "recovery.txt"
    write_recovery_note(str(note), messages["update_recovery_instructions"], "INSTALL", "LOG")
    text = note.read_text(encoding="utf-8-sig")
    assert "INSTALL" in text and "LOG" in text and "{install_dir}" not in text
    assert "https://github.com/gabrielhhaber/WinZapp_Python/releases" in text


def test_waiter_does_not_authorize_failed_or_unknown_payload_relaunch():
    from update_relaunch import build_relaunch_waiter_script
    script = build_relaunch_waiter_script(222, "WinZapp.exe", "log", "handoff",
                                          completion_path="result", recovery_path="recovery.txt")
    checked = script.split(":CHECK_RESULT\n", 1)[1].split(":RELAUNCH\n", 1)[0]
    assert '/c:"failed" "result" >NUL 2>&1\nif not errorlevel 1 goto RECOVERY' in checked
    assert checked.endswith("goto RECOVERY\n")
    for status in ("success", "unchanged"):
        # Unknown/contradictory lines must not authorize a restart simply
        # because a recognized success line appears somewhere in the file.
        reject_other_lines = f'findstr /v /x /c:"{status}" "result" >NUL 2>&1'
        assert reject_other_lines in checked
        assert checked.index(reject_other_lines) < checked.index(f'findstr /x /c:"{status}"')
    launch = script.split(":RELAUNCH\n", 1)[1].split(":RECOVERY\n", 1)[0]
    assert '/c:"failed" "result" >NUL 2>&1\nif not errorlevel 1 goto RECOVERY' in launch
    assert launch.index('/c:"success"') < launch.index('start "" /normal "WinZapp.exe"')
    assert launch.index('/c:"unchanged"') < launch.index('start "" /normal "WinZapp.exe"')
    for label, end in ((":GAVE_UP\n", ":CHECK_RESULT\n"), (":RECOVERY\n", ":ABORT\n")):
        branch = script.split(label, 1)[1].split(end, 1)[0]
        assert 'start "" /normal "WinZapp.exe"' not in branch
        assert '"%SystemRoot%\\System32\\notepad.exe" "recovery.txt"' in branch
        assert 'echo recovery instructions: "recovery.txt"' in branch


def test_batch_distinguishes_pre_copy_failure_from_uncertain_copy_result():
    import updater
    from core.update_install_guard import guard_paths
    script = updater._build_installer_script(
        "stage", "install", "WinZapp.exe", "log", "failed.marker", 111, 6300,
        extra_pids=[222], relaunch_handoff="handoff", install_guard=guard_paths("root", "a" * 32),
        result_path="result.marker", run_id="b" * 32)
    assert script.index("set WZ_COPY_STARTED=0") < script.index(":WAIT\n")
    assert script.index("set WZ_COPY_STARTED=1") < script.index("xcopy /E")
    peer = script.split(":OTHER_ACCOUNT_TIMEOUT\n", 1)[1].split(":PARENT_TIMEOUT\n", 1)[0]
    assert "echo unchanged" in peer and "echo failed\n" not in peer
    receipt_error = script.split(":UPDATE_RESULT_FAILED\n", 1)[1]
    assert 'if "!WZ_COPY_STARTED!"=="0" (' in receipt_error
    assert "echo unchanged" in receipt_error and "echo failed" in receipt_error
