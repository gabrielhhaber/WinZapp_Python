"""Outside-job recovery for a potentially mixed, in-place installation.

Only success or an explicit pre-copy failure may restart the application.
The recovery note is written before the parent closes, not by damaged code.
"""
import os


def outcome_action(outcome):
    if outcome in ("success", "unchanged"):
        return "relaunch"
    return "abort" if outcome == "aborted" else "recover"


def write_recovery_note(path, template, install_dir, log_path):
    if not template or template == "update_recovery_instructions":
        raise ValueError("missing translated recovery instructions")
    text = template.format(install_dir=install_dir, log_path=log_path)
    temporary = path + ".tmp"
    try:
        with open(temporary, "w", encoding="utf-8-sig", newline="\r\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.remove(temporary)
        except OSError:
            pass


def _dispatch(path, relaunch="RELAUNCH"):
    labels = {"relaunch": relaunch, "abort": "ABORT", "recover": "RECOVERY"}
    return "".join(
        # A recognized line beside contradictory/unknown text is not a
        # complete receipt. First reject lines other than this outcome.
        f'findstr /v /x /c:"{status}" "{path}" >NUL 2>&1\n'
        "if errorlevel 1 (\n"
        f'findstr /x /c:"{status}" "{path}" >NUL 2>&1\n'
        f'if not errorlevel 1 goto {labels[outcome_action(status)]}\n'
        ")\n"
        for status in ("aborted", "failed", "success", "unchanged")
    ) + "goto RECOVERY\n"


def build_guarded_waiter(installer_pid, exe_path, log_path, handoff_path,
                         passes, parent_pid, completion_path, installer_image,
                         recovery_path):
    """Generate text only. Neither the installer nor a viewer runs here."""
    abort_path, done_path = handoff_path + ".abort", handoff_path + ".done"
    checks = f'if exist "{abort_path}" goto ABORT\n'
    if parent_pid:
        checks += (f'tasklist /FI "PID eq {parent_pid}" 2>NUL | find "{parent_pid}" >NUL\n'
                   'if not errorlevel 1 goto ABORT\n')
    note = (
        f'if exist "{recovery_path}" (\n'
        f'    >> "{log_path}" echo recovery instructions: "{recovery_path}"\n'
        '    cmd /c exit 0\n'
        f'    start "" "%SystemRoot%\\System32\\notepad.exe" "{recovery_path}"\n'
        f'    if errorlevel 1 >> "{log_path}" echo recovery instructions could not be opened\n'
        ")\n" if recovery_path else ""
    )
    cleanup = (f'del "{handoff_path}" >NUL 2>&1\n'
               f'del "{done_path}" >NUL 2>&1\n')
    return (
        "@echo off\nsetlocal EnableDelayedExpansion\n"
        f'echo waiting> "{handoff_path}"\n'
        f'if not exist "{handoff_path}" exit /b 1\n'
        "set /a WAITED=0\n:WAIT\n"
        f'if exist "{abort_path}" goto ABORT\n'
        f'findstr /x /c:"aborted" "{completion_path}" >NUL 2>&1\n'
        "if not errorlevel 1 goto ABORT\n"
        f'tasklist /FI "PID eq {installer_pid}" /FI "IMAGENAME eq {installer_image}" 2>NUL | find "{installer_pid}" >NUL\n'
        "if errorlevel 1 goto CHECK_RESULT\n:WAIT_AGAIN\n"
        "set /a WAITED+=1\n"
        f"if !WAITED! GEQ {passes} goto GAVE_UP\n"
        "timeout /t 1 /nobreak >NUL\ngoto WAIT\n:GAVE_UP\n"
        + checks
        + f'>> "{log_path}" echo installer wait limit reached - not relaunching over a running copy\n'
        + note
        # A still-live installer must retain the handoff marker.
        + 'del "%~f0"\ngoto :EOF\n:CHECK_RESULT\n'
        + _dispatch(completion_path)
        + ":RELAUNCH\n" + checks
        + _dispatch(completion_path, "RELAUNCH_CONFIRMED")
        + ":RELAUNCH_CONFIRMED\n"
        + f'>> "{log_path}" echo installer finished - relaunching WinZapp\n'
        + f'if exist "{exe_path}" start "" /normal "{exe_path}"\n'
        + cleanup + 'del "%~f0"\ngoto :EOF\n:RECOVERY\n' + checks
        + f'>> "{log_path}" echo installation may be incomplete - not relaunching WinZapp; repair required\n'
        + note + cleanup + 'del "%~f0"\ngoto :EOF\n:ABORT\n'
        + f'>> "{log_path}" echo installer aborted - not relaunching WinZapp\n'
        + cleanup + f'del "{abort_path}" >NUL 2>&1\n'
        + 'del "%~f0"\nexit /b 1\n'
    )
