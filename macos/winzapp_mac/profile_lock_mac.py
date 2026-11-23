"""Wait for the Mac's WhatsApp browser to let go of its profile.

Before a session starts again (pairing, re-pairing, recovery), WinZapp waits
until the previous browser has released the session's userDataDir, and stops
it if it will not. ConnectionMixin does that with PowerShell and taskkill, so
on every other platform wait_for_profile_release() returned True at once and
the process list always read empty.

On the Mac that meant /start-session ran 60 ms after /close-session, while the
old chrome-headless-shell still held the profile. The new browser then died
on launch ("Failed to launch the browser process: Code: null"), no pairing
code was ever made, and the connect dialog gave up after 90 s (2026-11-23).

Same contract as the Windows versions: the list is an answer (possibly empty)
or None when the process list could not be read, and only a browser whose
--user-data-dir names THIS session is ever touched.
"""

import logging
import os
import signal
import subprocess
import time

_SINGLETON_FILES = ("lockfile", "SingletonLock", "SingletonCookie", "SingletonSocket")


def owning_pids(ps_output, session_name):
    """PIDs in `ps -axww -o pid=,command=` output whose command line is a
    browser holding *session_name*'s profile."""
    import connection_state as cs
    pids = []
    for line in ps_output.splitlines():
        pid, _, cmdline = line.strip().partition(" ")
        if not pid.isdigit():
            continue
        if cs.chrome_cmdline_owns_session(cmdline, session_name):
            pids.append(int(pid))
    return pids


def chrome_pids_owning_session(self, session_name):
    if not session_name:
        return []
    try:
        out = subprocess.check_output(
            ["/bin/ps", "-axww", "-o", "pid=,command="],
            text=True, stderr=subprocess.DEVNULL, timeout=10)
    except Exception as e:
        logging.warning("[profile-lock] could not list browser processes: %s", e)
        return None
    own = os.getpid()
    return [p for p in owning_pids(out, session_name) if p != own]


def _remove_stale_locks(self, session_name):
    gd = getattr(self, "global_dir", None)
    if not gd:
        return
    udd = os.path.join(gd, "api", "userDataDir", session_name)
    for name in _SINGLETON_FILES:
        p = os.path.join(udd, name)
        # lexists: the Singleton* markers are symlinks that dangle once
        # their process is gone (upstream issue #414).
        if os.path.lexists(p):
            try:
                os.remove(p)
                logging.info("[profile-lock] removed stale %s", name)
            except Exception:
                pass


def kill_orphaned_chrome_for_session(self, session_name=None):
    from core.profile_recovery import profile_is_local
    if not profile_is_local(self):
        return
    session_name = session_name or (getattr(self, "token", "") or "").split(":")[0]
    if not session_name:
        return
    pids = chrome_pids_owning_session(self, session_name) or []
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except Exception as e:
            logging.info("[profile-lock] SIGTERM pid=%s failed: %s", pid, e)
    deadline = time.monotonic() + 3.0
    while pids and time.monotonic() < deadline:
        time.sleep(0.25)
        pids = [p for p in pids if _alive(p)]
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
            logging.warning("[profile-lock] killed browser pid=%s holding session %s",
                            pid, session_name[:12])
        except ProcessLookupError:
            pass
        except Exception as e:
            logging.info("[profile-lock] SIGKILL pid=%s failed: %s", pid, e)
    _remove_stale_locks(self, session_name)


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def wait_for_profile_release(self, session_name, timeout=20.0):
    from core.profile_recovery import profile_is_local
    if not profile_is_local(self) or not session_name:
        return True
    started = time.monotonic()
    deadline = started + timeout
    polls = unreadable = 0
    while time.monotonic() < deadline:
        polls += 1
        holders = chrome_pids_owning_session(self, session_name)
        if holders is None:
            unreadable += 1
            time.sleep(0.5)
            continue
        if not holders:
            try:
                self._shutdown_audit(
                    "profile released after %d poll(s) in %.1fs (%d unreadable)"
                    % (polls, time.monotonic() - started, unreadable))
            except Exception:
                pass
            if polls > 1:
                logging.info("[profile-lock] %s released after %d poll(s)",
                             session_name[:12], polls)
            return True
        time.sleep(0.5)
    logging.warning("[profile-lock] %s still held after %.0fs — stopping the holder(s)",
                    session_name[:12], timeout)
    kill_orphaned_chrome_for_session(self, session_name)
    if chrome_pids_owning_session(self, session_name) == []:
        return True
    logging.warning("[profile-lock] %s is STILL held after the kill — starting the "
                    "session anyway; it will likely be refused.", session_name[:12])
    return False


def install():
    from main_window.connection import ConnectionMixin
    ConnectionMixin._chrome_pids_owning_session = chrome_pids_owning_session
    ConnectionMixin._kill_orphaned_chrome_for_session = kill_orphaned_chrome_for_session
    ConnectionMixin.wait_for_profile_release = wait_for_profile_release
