"""Which process the update installer may kill to free the API port.

The batch installer (updater._build_installer_script) used to free the port
with

    for /f "tokens=5" %%a in ('netstat -aon ^| findstr :6300 ^| findstr LISTENING') do taskkill /F /PID %%a

and the same line for :5433. Two problems, both about killing what is not
ours:

* findstr matches a substring, so ":6300" also matched a program listening
  on 63000-63009 (and ":5433" on 54330-54339), and the PID was killed
  without ever asking what program it was.
* 5433 was the port of a PostgreSQL that the gateway WinZapp used before
  WPPConnect Server depended on. Nothing in WinZapp has started one since
  that gateway was dropped, so whatever listens there now belongs to
  someone else (a developer's own PostgreSQL, quite often). That kill is
  gone, not narrowed.

What is WinZapp's to stop is decided here, in Python, while WinZapp is still
running: a process listening on exactly api_port whose executable is
WinZapp's own node.exe — the same file, compared by the filesystem rather
than by name, so an 8.3 or differently-cased path still matches and a
node.exe anywhere else never does. A batch script cannot answer that last
question without PowerShell and 8.3 path gymnastics. The script then kills
those PIDs only if, by the time it runs, each one is still a node.exe still
listening on exactly that port: WinZapp's own shutdown normally stops Node
first, and Windows reuses PIDs quickly.

Nothing depends on the state column. netstat translates LISTENING on
localized Windows (so the old findstr LISTENING most likely never matched
on a pt-BR or German system, and the kill is in effect new there), and a
translation can be two words, which shifts every column after it. So
"listening" is read from the foreign address (0.0.0.0:0 or [::]:0), and the
PID is always the LAST field: parts[-1] here, and in the script the last
word of everything after the third column.
"""

import logging
import os
import subprocess
import sys

_LISTENING_FOREIGN = ("0.0.0.0:0", "[::]:0")


def listening_pids(netstat_output: str, port: int) -> list:
    """PIDs of the TCP sockets in ``netstat -ano`` output listening on
    exactly *port* (IPv4 or IPv6), in first-seen order, without repeats."""
    pids = []
    for line in (netstat_output or "").splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[0].upper() != "TCP":
            continue
        if parts[1].rpartition(":")[2] != str(port) or parts[2] not in _LISTENING_FOREIGN:
            continue
        try:
            pid = int(parts[-1])
        except ValueError:
            continue
        if pid > 0 and pid not in pids:
            pids.append(pid)
    return pids


def _image_path(pid: int) -> str:
    """Full path of *pid*'s executable, or "" when it cannot be read.
    PROCESS_QUERY_LIMITED_INFORMATION is enough, and is granted for a
    process at a higher integrity level of the same user too."""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.QueryFullProcessImageNameW.argtypes = (
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return ""
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buffer))
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return buffer.value
            return ""
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return ""


def _same_file(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def own_node_pids(port: int, node_paths, netstat_output: "str | None" = None,
                  image_path=_image_path) -> list:
    """PIDs listening on exactly *port* whose executable is one of
    *node_paths* (WinZapp's own node.exe). Never raises; [] off Windows or
    when netstat cannot be read."""
    if netstat_output is None:
        if sys.platform != "win32":
            return []
        try:
            netstat_output = subprocess.check_output(
                ["netstat", "-ano"],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                text=True,
                errors="replace",
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
        except Exception as exc:
            logging.warning("Auto-updater: netstat failed (%s); the installer "
                            "will not stop any Node on port %s", exc, port)
            return []
    ours = []
    for pid in listening_pids(netstat_output, port):
        path = image_path(pid)
        if path and any(_same_file(path, p) for p in node_paths if p):
            ours.append(pid)
        else:
            logging.info("Auto-updater: pid %s listens on port %s but is not "
                         "WinZapp's node.exe (%s) — the installer leaves it alone",
                         pid, port, os.path.basename(path) or "unknown")
    return ours


def node_kill_lines(pids, port: int, log_path: str) -> str:
    """Batch text that stops each of *pids* if it is still a node.exe
    listening on exactly *port*. Empty for no pids."""
    suffix = f":{port}"
    blocks = []
    for pid in pids:
        blocks.append(
            "set WZ_LISTENS=0\n"
            # %%d is the rest of the line (state, if any, and PID); the
            # inner for leaves its last word in WZ_PID.
            "for /f \"tokens=1-3,*\" %%a in ('netstat -ano') do if /i \"%%a\"==\"TCP\" (\n"
            '    set "WZ_PID="\n'
            '    for %%p in (%%d) do set "WZ_PID=%%p"\n'
            f'    if "!WZ_PID!"=="{pid}" (\n'
            '        set "WZ_LOCAL=%%b"\n'
            f'        if "!WZ_LOCAL:~-{len(suffix)}!"=="{suffix}" (\n'
            '            if "%%c"=="0.0.0.0:0" set WZ_LISTENS=1\n'
            '            if "%%c"=="[::]:0" set WZ_LISTENS=1\n'
            "        )\n"
            "    )\n"
            ")\n"
            'if "!WZ_LISTENS!"=="1" (\n'
            f'    tasklist /FI "PID eq {pid}" /FI "IMAGENAME eq node.exe" 2>NUL | find "{pid}" >NUL\n'
            "    if not errorlevel 1 (\n"
            f'        >> "{log_path}" echo stopping WinZapp node.exe {pid} still listening on port {port}\n'
            f"        taskkill /F /PID {pid} >NUL 2>&1\n"
            "    )\n"
            ")\n"
        )
    return "".join(blocks)
