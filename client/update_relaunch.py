"""After an update installed with administrator rights, WinZapp comes back
without them.

When the install folder is not writable by the user (_needs_admin(), e.g.
a copy put in Program Files by hand), updater._run_batch_installer() runs
the installer script through a UAC prompt. That script ended with
`start "" WinZapp.exe`, and a process started by an elevated process is
elevated too — so the WinZapp that came back after the update, its Node
and every npm under it ran as Administrator, at a level the user never ran
it at. That is how an update broke the user's own npm (see
core/npm_environment.py for the cache half of that report). When the UAC
prompt was answered with another administrator's password (a standard
account), that `start ""` even ran WinZapp as that other account.

The elevated script now leaves the relaunch to a small waiter script that
the non-elevated WinZapp starts before it exits. The waiter inherits what
the user ran WinZapp with — same account, same level — waits for the
elevated script to finish, however it finishes (copied, xcopy failed,
another account timed out, or killed), and starts WinZapp.exe the way the
script used to. Rejected alternatives:

* `explorer.exe WinZapp.exe` from the elevated script relies on Explorer
  being the running shell and handing the launch to its existing,
  non-elevated instance, and from another administrator's account it would
  not reach the user's Explorer at all.
* `runas /trustlevel:0x20000` produces a restricted token, not the user's
  normal one, and is known to fail on current Windows 10/11 builds.
* De-elevating Node itself (win32_helpers._spawn_delevated) leaves the
  whole WinZapp elevated; wpp_server._start_wpp_background explains why
  Node is deliberately not de-elevated, and that stays true.

Exactly one relaunch, never zero or two. The waiter's first action is to
create the handoff marker, so the marker exists exactly when a waiter got
that far; start_relaunch_waiter() waits for it to appear before this
process goes on to exit, and kills a waiter that never creates it. The
elevated script reaches its relaunch only after this process has exited
(its first loop waits for that), and relaunches by itself exactly when the
marker is missing. So: no waiter, a waiter that could not start, or one
killed before the marker — the script relaunches as it always did; a marker
— the waiter does, and the script does not.

The waiter stops waiting at whichever comes first: the script's
`<marker>.done`, written after its relaunch decision on every exit path
(only when the marker is there, so no waiter means no leftover file); the
script's cmd.exe gone (by pid and image name — the path where the script
was killed); or WAITER_PASSES checks, so a reused pid cannot hold it
forever. Giving up leaves the marker in place, so a script still running
does not start a second copy (both small files then stay in %TEMP%).

What this cannot do: a WinZapp that is ALREADY elevated when it updates
(run as administrator on purpose, or relaunched elevated by an older
build's installer) can write the install folder, so _needs_admin() is
False, the script is not elevated by us, and its plain relaunch inherits
the elevation. That is left alone — some people run WinZapp as
administrator deliberately, and nothing an older build leaves behind tells
the two apart — and logged by updater._run_batch_installer().
docs/traps/updater-channels.md has the details.
"""

import ctypes
import logging
import os
import subprocess
import time
from ctypes import wintypes

#: How many times the waiter checks on the elevated script before
#: relaunching anyway. Each check runs tasklist and then sleeps one second,
#: so this is at least 30 minutes, somewhat more on a slow machine (a batch
#: script has no clock it can do arithmetic on without parsing the
#: locale-formatted %TIME%). An update finishes in minutes; this only bounds
#: the case where the script was killed and its pid went to another cmd.exe.
WAITER_PASSES = 30 * 60

#: Variables WinZapp itself puts into os.environ for its Node
#: (wpp_server._start_wpp_background). A fresh start has none of them, so
#: the relaunched WinZapp does not get them either.
WINZAPP_NODE_VARIABLES = (
    "AUTHENTICATION_API_KEY",
    "WPP_LID_MODE",
    "PORT",
    "PUPPETEER_CACHE_DIR",
    "WINZAPP_INSTALLATION_ID",
    "WINZAPP_INSTANCE_ID",
    "WINZAPP_USER_DATA_DIR",
    "WINZAPP_TOKEN_STORE_DIR",
)


class SHELLEXECUTEINFOW(ctypes.Structure):
    """shellapi.h's SHELLEXECUTEINFOW (112 bytes on 64-bit Windows,
    pinned by tests/test_update_relaunch.py there)."""
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", wintypes.ULONG),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hIconOrMonitor", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


def relaunch_environment(environment) -> dict:
    """The environment a relaunched WinZapp starts from: WinZapp's own,
    minus what this run of it added.

    The old elevated relaunch got a fresh environment from UAC; a child of
    this process would otherwise carry the PyInstaller bootloader's
    _PYI_* state, which a onefile build reads as "I am a child of that
    run" and would reuse its extraction folder, deleted by then.
    PYINSTALLER_RESET_ENVIRONMENT=1 (PyInstaller 6.9+, the build pins
    6.21) tells the bootloader to start as a new top-level instance; the
    _PYI_* names are dropped as well, in case it is ignored."""
    drop = {name.upper() for name in WINZAPP_NODE_VARIABLES}
    child = {
        name: value for name, value in environment.items()
        if not name.upper().startswith("_PYI_") and name.upper() not in drop
    }
    child["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return child


def relaunch_lines(exe_path: str, log_path: str, handoff_path: str = "",
                   indent: str = "") -> str:
    """The installer script's relaunch of WinZapp.

    Without *handoff_path* (the non-elevated install) it is the original
    single line. With it, the script leaves the relaunch to the waiter when
    the marker exists, relaunches by itself otherwise, and then tells the
    waiter it is done."""
    start = f'if exist "{exe_path}" start "" "{exe_path}"\n'
    if not handoff_path:
        return indent + start
    return (
        f'{indent}if exist "{handoff_path}" (\n'
        f'{indent}    >> "{log_path}" echo relaunch left to the waiter running without administrator rights\n'
        f'{indent}    echo done> "{handoff_path}.done"\n'
        f"{indent}) else (\n"
        f"{indent}    {start}"
        f"{indent})\n"
    )


def build_relaunch_waiter_script(installer_pid: int, exe_path: str, log_path: str,
                                 handoff_path: str,
                                 passes: int = WAITER_PASSES) -> str:
    """The waiter's batch text. Pure — every path is already console-safe."""
    done_path = handoff_path + ".done"
    return (
        "@echo off\n"
        "setlocal EnableDelayedExpansion\n"
        # First, before anything else: the marker is what tells both this
        # process and the elevated script that a waiter is running.
        f'echo waiting> "{handoff_path}"\n'
        f'if not exist "{handoff_path}" exit /b 1\n'
        "set /a WAITED=0\n"
        ":WAIT\n"
        f'if exist "{done_path}" goto RELAUNCH\n'
        f'tasklist /FI "PID eq {installer_pid}" /FI "IMAGENAME eq cmd.exe" 2>NUL | find "{installer_pid}" >NUL\n'
        "if errorlevel 1 goto RELAUNCH\n"
        "set /a WAITED+=1\n"
        f"if !WAITED! GEQ {passes} goto GAVE_UP\n"
        "timeout /t 1 /nobreak >NUL\n"
        "goto WAIT\n"
        ":GAVE_UP\n"
        # The marker stays: a script still running must not relaunch too.
        f'>> "{log_path}" echo gave up waiting for the installer after {passes} checks - relaunching WinZapp\n'
        f'if exist "{exe_path}" start "" "{exe_path}"\n'
        'del "%~f0"\n'
        "goto :EOF\n"
        ":RELAUNCH\n"
        f'>> "{log_path}" echo installer finished - relaunching WinZapp without administrator rights\n'
        f'if exist "{exe_path}" start "" "{exe_path}"\n'
        f'del "{handoff_path}" >NUL 2>&1\n'
        f'del "{done_path}" >NUL 2>&1\n'
        'del "%~f0"\n'
    )


def start_relaunch_waiter(bat_path: str, installer_pid, exe_path: str, log_path: str,
                          handoff_path: str, write_script, popen=None,
                          appear_timeout: float = 10.0) -> bool:
    """Start the non-elevated waiter and wait for it to take the relaunch
    over (its marker). Returns True when it did. Every other outcome leaves
    no marker and no waiter, so the elevated script relaunches WinZapp
    itself, as it always did.

    *write_script* is updater._write_installer_script (OEM code page)."""
    if not installer_pid:
        logging.warning("Auto-updater: no pid for the elevated installer; it "
                        "relaunches WinZapp itself")
        return False
    waiter_path = bat_path[:-len(".bat")] + "_relaunch.bat"
    script = build_relaunch_waiter_script(installer_pid, exe_path, log_path, handoff_path)
    if not write_script(waiter_path, script):
        return False
    popen = popen or subprocess.Popen
    try:
        # Same flags as the non-elevated installer, for the same code-page
        # reason; this process is the user's own, so the waiter and the
        # WinZapp it starts are too.
        proc = popen(["cmd.exe", "/c", waiter_path],
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                     env=relaunch_environment(os.environ))
    except OSError:
        logging.exception("Auto-updater: could not start the relaunch waiter; "
                          "the elevated installer relaunches WinZapp itself")
        _remove_quietly(waiter_path)
        return False
    deadline = time.monotonic() + appear_timeout
    while time.monotonic() < deadline:
        if os.path.exists(handoff_path):
            logging.info("Auto-updater: relaunch waiter started for installer pid %s",
                         installer_pid)
            return True
        time.sleep(0.05)
    logging.error("Auto-updater: the relaunch waiter did not start in %ss; "
                  "stopping it, the elevated installer relaunches WinZapp itself",
                  appear_timeout)
    try:
        proc.kill()
        proc.wait(timeout=5)
    except Exception:
        logging.exception("Auto-updater: could not stop the relaunch waiter")
    # It may have created the marker between the last look and the kill.
    if not _remove_quietly(handoff_path):
        logging.error("Auto-updater: could not remove the relaunch marker; "
                      "WinZapp may not be relaunched after the update")
    _remove_quietly(waiter_path)
    return False


def _remove_quietly(path: str) -> bool:
    """Remove *path*; True when it is gone (or never existed)."""
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError:
        logging.exception("Auto-updater: could not remove %s", os.path.basename(path))
        return False
    return True


def run_elevated(bat_path: str):
    """Run ``cmd.exe /c <bat_path>`` through UAC. Returns ``(launched, pid)``.

    ShellExecuteExW rather than ShellExecuteW because only the Ex form hands
    back the process, and the waiter needs its pid. launched is False when
    the user declined the prompt (ERROR_CANCELLED) or the call failed; pid is
    None when Windows returned no process handle, and then there is no
    waiter and the script relaunches WinZapp itself."""
    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SEE_MASK_NOASYNC = 0x00000100
    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(SHELLEXECUTEINFOW)
    info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
    info.lpVerb = "runas"
    info.lpFile = "cmd.exe"
    info.lpParameters = f'/c "{bat_path}"'
    info.nShow = 0  # SW_HIDE, as the ShellExecuteW call this replaces

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = (ctypes.POINTER(SHELLEXECUTEINFOW),)
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        logging.warning(
            "Auto-updater: ShellExecuteExW('runas', ...) failed or was "
            "declined by the user (error=%s); batch installer was not launched.",
            ctypes.get_last_error(),
        )
        return False, None
    if not info.hProcess:
        return True, None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetProcessId.argtypes = (wintypes.HANDLE,)
    kernel32.GetProcessId.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    try:
        return True, (kernel32.GetProcessId(info.hProcess) or None)
    finally:
        kernel32.CloseHandle(info.hProcess)
