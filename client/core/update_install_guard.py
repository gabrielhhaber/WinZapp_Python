"""A token-specific OS lifetime lock across application -> batch handoff.

Only the guardian holds the lock. Relaunch needs a final batch receipt and
guardian exit. No PID guessing or timeout grants permission to write
installation files.
"""
import os
import time
from core.update_file_lease import lease_busy

FINISHED = frozenset(("success", "unchanged", "failed", "aborted"))


class GuardPaths(dict):
    owner_root = None
    owner_token = None


def guard_paths(root, token):
    if len(token) != 32 or any(c not in "0123456789abcdef" for c in token):
        raise ValueError("invalid installer token")
    base = os.path.join(root, "update-installer-" + token)
    return GuardPaths((name, base + suffix) for name, suffix in
            (("lock", ".lock"), ("ready", ".ready"), ("commit", ".commit"),
             ("abort", ".abort"), ("done", ".done"), ("diagnostic", ".diagnostic")))


def read_signal(path):
    try:
        with open(path, encoding="ascii") as stream:
            return stream.read(64).strip()
    except (OSError, UnicodeError):
        return ""


def write_signal(path, value):
    temporary = path + ".tmp"
    try:
        with open(temporary, "w", encoding="ascii") as stream:
            stream.write(value + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.remove(temporary)
        except OSError:
            pass


def active_guard(root, state):
    paths = guard_paths(root, state["owner_token"])
    if read_signal(paths["done"]) in FINISHED:
        return False
    return True if lease_busy(paths["lock"]) else None


def await_ready(paths, timeout=15.0, clock=time.monotonic, sleep=time.sleep,
                before_commit=lambda pid: True):
    deadline = clock() + timeout
    while clock() < deadline:
        if (read_signal(paths["done"]) in FINISHED
                or read_signal(paths["abort"]) == "abort"):
            break
        ready = read_signal(paths["ready"])
        pid = ready.removeprefix("ready:")
        if (ready.startswith("ready:") and pid.isascii() and pid.isdecimal()
                and 0 < int(pid) < 2 ** 31 and lease_busy(paths["lock"])):
            if before_commit(int(pid)) is not True:
                break
            # Waiter preparation can take seconds. Do not grant from the
            # readiness snapshot observed before that fallible operation.
            if (read_signal(paths["ready"]) != ready
                    or read_signal(paths["done"]) in FINISHED
                    or read_signal(paths["abort"]) == "abort"
                    or not lease_busy(paths["lock"])):
                break
            try:
                write_signal(paths["commit"], "commit")
            except OSError:
                # An atomic replace may have become visible. Never revoke a
                # grant the batch could already have consumed.
                if read_signal(paths["commit"]) == "commit":
                    return True
                break
            return True
        sleep(0.05)
    # No commit means no file writes, even if abort cannot be recorded.
    try:
        write_signal(paths["abort"], "abort")
    except OSError:
        pass
    return False


def batch_gate(paths, log_path):
    if not paths:
        return ""
    return (
        "set WZ_GRANT_WAIT=0\n:WAIT_INSTALL_GRANT\n"
        f'if exist "{paths["abort"]}" goto INSTALL_GRANT_ABORT\n'
        f'findstr /x /c:"commit" "{paths["commit"]}" >NUL 2>&1\n'
        "if not errorlevel 1 goto INSTALL_GRANTED\n"
        "set /a WZ_GRANT_WAIT+=1\n"
        "if !WZ_GRANT_WAIT! GEQ 300 goto INSTALL_GRANT_ABORT\n"
        "timeout /t 1 /nobreak >NUL\ngoto WAIT_INSTALL_GRANT\n"
        ":INSTALL_GRANT_ABORT\n"
        f'>> "{log_path}" echo installer handoff not committed - no files changed\n'
        f'> "{paths["done"]}" echo aborted\n'
        "exit /b 1\n:INSTALL_GRANTED\n"
    )


def batch_done(paths, status, indent=""):
    return f'{indent}> "{paths["done"]}" echo {status}\n' if paths else ""
