"""Stop WinZapp's Node server on quit, as the Windows build does.

_stop_wpp_server() ends with `taskkill /F /T` on Windows. Off Windows it
only proc.terminate()s — which Node ignores while start.js shuts down —
and when this run reused a server a previous run left behind, it finds that
server through a netstat lookup that is Windows-only, so nothing is killed
at all and Node lingers after Quit.

Mac equivalents: find the listener with lsof, and after upstream's own
shutdown (which has already flushed and released the WhatsApp profile)
make sure that process and its children are gone.
"""

import logging
import os
import signal
import subprocess
import time


def find_pid_listening_on_port(self, port):
    try:
        out = subprocess.run(["lsof", "-nP", f"-iTCP:{int(port)}", "-sTCP:LISTEN", "-t"],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None
    pids = [int(p) for p in out.split() if p.isdigit()]
    return pids[0] if pids else None


def _descendants(pid):
    out, todo = [], [pid]
    while todo:
        p = todo.pop()
        try:
            kids = subprocess.run(["pgrep", "-P", str(p)], capture_output=True, text=True,
                                  timeout=5).stdout.split()
        except Exception:
            kids = []
        for k in kids:
            if k.isdigit():
                out.append(int(k))
                todo.append(int(k))
    return out


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def kill_tree(pid, grace=0.0):
    """taskkill /F /T: the process and everything under it. Called only
    after upstream's shutdown has flushed and released the WhatsApp profile,
    so — like /F — no grace period by default (Node ignores SIGTERM while
    start.js winds down, which only delayed quitting)."""
    tree = [pid] + _descendants(pid)
    for p in reversed(tree):
        try:
            os.kill(p, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and any(_alive(p) for p in tree):
        time.sleep(0.1)
    for p in tree:
        if _alive(p):
            try:
                os.kill(p, signal.SIGKILL)
            except OSError:
                pass


def owns_port_listener(window):
    """Whether what listens on the account's port is this account's own Node,
    to be killed on quit. Not on a custom API: the user's own server, or the
    Neonize bridge, which every account shares and which
    _stop_wpp_server() already leaves (it asks the bridge to close its
    sessions; the last account out stops it, neonize_bridge_process.py).
    The same rule as profile_is_local(): both mean "the bundled WPPConnect".
    """
    from core.profile_recovery import profile_is_local
    return profile_is_local(window)


def install():
    from main_window import wpp_server
    cls = wpp_server.WppServerMixin
    cls._find_pid_listening_on_port = find_pid_listening_on_port
    orig_stop = cls._stop_wpp_server

    def stop(self, *args, **kwargs):
        # A Mac quit (Dock, Cmd-Q via the app menu, logout) arrives as
        # QUERY_END_SESSION *and* END_SESSION, and upstream tears down on
        # both; the second pass only waits on a server that is already gone.
        if getattr(self, "_mac_server_stopped", False):
            logging.info("[server_mac] server already stopped — skipping the second teardown")
            return None
        from . import lifecycle_mac
        lifecycle_mac.leave_dock()
        pid = None
        if owns_port_listener(self):
            proc = getattr(self, "wpp_process", None)
            pid = proc.pid if proc is not None and proc.poll() is None else None
            pid = pid or find_pid_listening_on_port(self, getattr(self, "wpp_port", 6300))
        try:
            return orig_stop(self, *args, **kwargs)
        finally:
            # Each account runs its own Node on its own port (upstream,
            # _stop_wpp_server's docstring), so this one is ours to stop.
            self._mac_server_stopped = True
            if pid and _alive(pid):
                logging.info("[server_mac] stopping Node pid %s and its children", pid)
                kill_tree(pid)

    cls._stop_wpp_server = stop

