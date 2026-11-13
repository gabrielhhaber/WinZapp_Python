"""An update installed on exit says how it ended (issue #769): the app stays
closed, so a message box the screen reader reads at once tells whether it was
installed, instead of people guessing when they may open WinZapp again."""

import base64
import re

import updater
from core.update_exit_notice import ExitNotice, exit_notice, notice_line
from core.update_install_guard import guard_paths
from core.update_recovery import build_guarded_waiter
from update_relaunch import build_relaunch_waiter_script

EXE = r"C:\WinZapp\WinZapp.exe"


def _script_of(line):
    encoded = re.search(r"-EncodedCommand (\S+)", line).group(1)
    return base64.b64decode(encoded).decode("utf-16-le")


class _Words:
    def t(self, key):
        return {"update_on_exit_notice_title": "Atualização do WinZapp",
                "update_on_exit_installed": "A versão {version} foi instalada.",
                "update_on_exit_not_installed": "A versão {version} não foi instalada."}[key]


def test_the_notice_is_an_ascii_batch_line_that_does_not_wait():
    line = notice_line("Atualização", "Já pode abrir o WinZapp d'água")
    assert line.isascii() and line.endswith("\n") and line.count("\n") == 1
    assert line.startswith('start "" /min powershell.exe ')       # the script goes on
    script = _script_of(line)
    assert "'Já pode abrir o WinZapp d''água'" in script           # quoted, accents kept
    assert "'Atualização'" in script
    assert "DefaultDesktopOnly" in script                          # comes to the front
    assert "'Information'" in script


def test_a_failure_warns_and_nothing_in_the_text_escapes_its_quotes():
    script = _script_of(notice_line("T", "a';Remove-Item x;'\r\n" + "z" * 900, failed=True))
    assert "'Warning'" in script
    assert "a'';Remove-Item x;''" in script and "\r" not in script and "\n" not in script
    assert "z" * 380 in script and "z" * 400 not in script        # cut to fit a batch line


def test_the_notices_are_in_the_users_language_with_the_version():
    notice = exit_notice(_Words(), "2.5.1")
    assert "A versão 2.5.1 foi instalada." in _script_of(notice.installed)
    assert "A versão 2.5.1 não foi instalada." in _script_of(notice.not_installed)
    assert "Atualização do WinZapp" in _script_of(notice.installed)


NOTICE = ExitNotice("NOTICE-INSTALLED\n", "NOTICE-NOT-INSTALLED\n")


def test_the_unguarded_script_says_installed_or_not_installed():
    script = updater._build_installer_script(
        "stage", "install", EXE, "log", "failed.marker", 111, 6300,
        extra_pids=[222], relaunch_app=False, exit_notice=NOTICE)
    success = script.index(":OTHER_ACCOUNT_TIMEOUT")
    copy_failed = script.index("xcopy FAILED")
    assert script.index("NOTICE-INSTALLED") < success
    assert copy_failed < script.index("NOTICE-NOT-INSTALLED") < script.index("xcopy OK")
    assert "NOTICE-NOT-INSTALLED" in script[success:]              # another account kept it open
    # still running WinZapp: no notice that it may be opened
    parent_timeout = script[script.index(":PARENT_TIMEOUT"):]
    assert "NOTICE" not in parent_timeout


def test_a_normal_update_or_a_guarded_script_shows_no_notice_of_its_own():
    normal = updater._build_installer_script(
        "stage", "install", EXE, "log", "failed.marker", 111, 6300, exit_notice=NOTICE)
    assert "NOTICE" not in normal                                   # it relaunches instead
    guarded = updater._build_installer_script(
        "stage", "install", EXE, "log", "failed.marker", 111, 6300,
        relaunch_handoff="handoff", install_guard=guard_paths("root", "a" * 32),
        result_path="result.marker", run_id="b" * 32, relaunch_app=False, exit_notice=NOTICE)
    assert "NOTICE" not in guarded                                  # its waiter says it


def test_the_guarded_waiter_says_installed_only_on_success():
    waiter = build_guarded_waiter(1, EXE, "log", "handoff", 5, 0, "done.txt", "powershell.exe",
                                  "note.txt", relaunch=False, notice=NOTICE)
    block = waiter[waiter.index(":RELAUNCH_CONFIRMED"):waiter.index(":RECOVERY")]
    assert 'findstr /x /c:"success" "done.txt"' in block
    assert block.index("NOTICE-INSTALLED") < block.index(") else (") < block.index("NOTICE-NOT-INSTALLED")
    assert "NOTICE" not in waiter[waiter.index(":RECOVERY"):]       # the recovery note says it
    same = build_relaunch_waiter_script(1, EXE, "log", "handoff", completion_path="done.txt",
                                        installer_image="powershell.exe", recovery_path="note.txt",
                                        relaunch=False, notice=NOTICE)
    assert "NOTICE-INSTALLED" in same


def test_a_waiter_that_relaunches_or_has_no_notice_is_unchanged():
    plain = build_guarded_waiter(1, EXE, "log", "handoff", 5, 0, "done.txt", "powershell.exe",
                                 "note.txt", relaunch=False)
    assert build_guarded_waiter(1, EXE, "log", "handoff", 5, 0, "done.txt", "powershell.exe",
                                "note.txt", relaunch=False, notice=None) == plain
    relaunching = build_guarded_waiter(1, EXE, "log", "handoff", 5, 0, "done.txt",
                                       "powershell.exe", "note.txt", notice=NOTICE)
    assert "NOTICE" not in relaunching
