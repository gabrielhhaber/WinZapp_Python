"""
Account-scoped IPC for WinZapp multi-account (client/ipc.py)
============================================================

Lets one WinZapp process ask another (identified by account_id) to come to the
foreground (``activate``) or shut down (``quit``). Chosen for correctness over
the WM_COPYDATA idea (plan Zad. 2.0, GPT r5 #3):

  * Transport = a NAMED PIPE with a DACL restricted to the current user on
    Windows; an AF_UNIX socket (0600) on other platforms (dev / CI / tests).
    Both speak the SAME line protocol, so the logic layer is unit-tested on
    Linux and the Windows transport only swaps the wire.
  * Channel name keyed by ``hash(canonical global_dir) + account_id`` — never a
    window title, so "Praca" can't be confused with "Praca 2".
  * ``activate`` carries a ``startup_source`` (user vs autostart) so a technical
    activation is never mistaken for a conscious switch that would move
    last_foreground.
  * ``quit`` distinguishes ACK ("received") from real termination: the requester
    waits until the target actually releases (released_predicate) — not merely
    the ACK — with a timeout.
  * Readiness handshake: the requester connects with retries; a listener that
    hasn't bound yet simply isn't connectable → request returns False.
  * A request that arrives before the wx window exists is queued and flushed
    once ``window_ready_predicate`` turns true; delivery to wx must be marshalled
    by the caller's callback via wx.CallAfter.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import sys
import threading
import time
from typing import Callable, Optional

from coord_locks import canonical_dir

_IS_WINDOWS = sys.platform == "win32"

# How long the LISTENER (the process being asked to quit) polls
# released_predicate() before giving up and answering with whatever it says
# at that moment — this decides the "released" reply's truthfulness,
# independent of the CALLER's own request_quit() timeout below. Must stay >=
# the target's own worst-case graceful-teardown time (kept as a plain
# duplicated constant, not a cross-module import, since ipc.py must stay
# importable before a MainWindow/wx.App exists):
#   MessageQueue._STOP_DRAIN_SECONDS                  (4s:  in-flight send)
# + MainWindow._SELF_RESTART_YIELD_SECONDS            (5s:  yield to an
#   in-progress recovery/in-place session restart)
# + MainWindow._WPP_GRACEFUL_STOP_SECONDS             (10s: close-session POST)
# + MainWindow._SHUTDOWN_FLUSH_TIMEOUT                (15s: poll for CLOSED)
# + MainWindow._TOKEN_PERSIST_GRACE_SECONDS           (5s:  token-file wait)
# + ~2s taskkill-confirm poll
# + _flush_pending_debounced_saves()                  (~3s)
# + DatabaseBridge.close()'s own bounds                (12s)
# = ~56s realistic worst case; 75s leaves real margin. If any of the budgets
# above changes, this number and request_quit()'s own default (below) both
# need re-checking together — raising only one silently reintroduces a
# "gave up early, answered released:False for a session still flushing" bug.
_QUIT_RELEASE_POLL_SECONDS = 75.0


# ── channel identity ─────────────────────────────────────────────────────────
def _channel_key(global_dir: str, account_id: str) -> str:
    h = hashlib.md5(canonical_dir(global_dir).encode("utf-8")).hexdigest()[:16]
    return f"WinZapp_{h}_{account_id}"


def _pipe_name(global_dir: str, account_id: str) -> str:
    return r"\\.\pipe\%s" % _channel_key(global_dir, account_id)


def _unix_path(global_dir: str, account_id: str) -> str:
    # Keep well under the ~108-char sun_path limit: hash the key.
    key = _channel_key(global_dir, account_id)
    digest = hashlib.md5(key.encode()).hexdigest()[:16]
    return os.path.join(_ipc_dir(global_dir), f"ipc_{digest}.sock")


def _ipc_dir(global_dir: str) -> str:
    d = os.path.join(global_dir, "ipc")
    os.makedirs(d, exist_ok=True)
    return d


# ── protocol ─────────────────────────────────────────────────────────────────
# One JSON object per line each way. Request: {cmd, request_id, source?}.
# Reply(s): {request_id, ack: True} then, for quit, {request_id, released: True}.

def _make_request(cmd: str, source: Optional[str] = None) -> dict:
    req = {"cmd": cmd, "request_id": os.urandom(8).hex()}
    if source is not None:
        req["source"] = source
    return req


class IpcListener:
    """Serves one account's IPC channel on a background thread."""

    def __init__(
        self,
        global_dir: str,
        account_id: str,
        on_activate: Callable[[str], None],
        on_quit: Callable[[], None],
        released_predicate: Optional[Callable[[], bool]] = None,
        window_ready_predicate: Optional[Callable[[], bool]] = None,
    ):
        self.global_dir = global_dir
        self.account_id = account_id
        self.on_activate = on_activate
        self.on_quit = on_quit
        self.released_predicate = released_predicate or (lambda: True)
        self.window_ready_predicate = window_ready_predicate or (lambda: True)
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._srv_sock: Optional[socket.socket] = None
        self._queue: list = []
        self._queue_lock = threading.Lock()

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self) -> None:
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def wait_ready(self, timeout: float = 5.0) -> bool:
        return self._ready.wait(timeout)

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._srv_sock is not None:
                self._srv_sock.close()
        except OSError:
            pass
        # Nudge a blocking accept() by self-connecting.
        try:
            if not _IS_WINDOWS:
                path = _unix_path(self.global_dir, self.account_id)
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.settimeout(0.2)
                    s.connect(path)
        except OSError:
            pass
        if self._thread:
            self._thread.join(timeout=2.0)

    # ── queue-before-window ──────────────────────────────────────────────
    def flush_queue(self) -> None:
        with self._queue_lock:
            pending, self._queue = self._queue, []
        for source in pending:
            self.on_activate(source)

    def _dispatch_activate(self, source: str) -> None:
        if self.window_ready_predicate():
            self.on_activate(source)
        else:
            with self._queue_lock:
                self._queue.append(source)

    # ── server loop (AF_UNIX fallback; Windows uses a pipe variant) ───────
    def _serve(self) -> None:
        if _IS_WINDOWS:
            self._serve_windows()
        else:
            self._serve_unix()

    def _serve_unix(self) -> None:
        path = _unix_path(self.global_dir, self.account_id)
        try:
            os.unlink(path)
        except OSError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        os.chmod(path, 0o600)  # current user only
        srv.listen(8)
        srv.settimeout(0.5)
        self._srv_sock = srv
        self._ready.set()
        while not self._stop.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            # Handed off to its own thread for the same reason as the
            # Windows pipe loop below: a "quit" reply waits here for up to
            # _QUIT_RELEASE_POLL_SECONDS for released_predicate(), and
            # handling it inline would stall accept() from picking up any
            # other request (e.g. a concurrent "activate") for that whole
            # window.
            threading.Thread(target=self._handle_conn, args=(conn,), daemon=True).start()
        try:
            srv.close()
            os.unlink(path)
        except OSError:
            pass

    @staticmethod
    def _current_user_pipe_sa():
        """SECURITY_ATTRIBUTES with a DACL granting access to this Windows
        user's SID only — nobody else, not even other local administrators.

        A bare ``win32security.SECURITY_ATTRIBUTES()`` (the previous code
        here) has no security descriptor of its own, so ``CreateNamedPipe``
        falls back to the default DACL for a kernel object created by this
        process: verified directly against a real pipe, that default grants
        full control to the current user AND to ``BUILTIN\\Administrators``
        AND ``NT AUTHORITY\\SYSTEM`` — not "current user only" the way the
        module docstring above has always claimed. Any other account able to
        reach that pipe name (any local admin, not just this user) could send
        it an "activate"/"quit" command for a WinZapp instance it does not
        own. This SID-scoped DACL is what actually delivers on that claim.
        """
        import win32security
        import win32api
        import ntsecuritycon

        token = win32security.OpenProcessToken(
            win32api.GetCurrentProcess(), win32security.TOKEN_QUERY
        )
        user_sid, _ = win32security.GetTokenInformation(token, win32security.TokenUser)

        dacl = win32security.ACL()
        # FILE_ALL_ACCESS rather than just read+write: every pipe instance
        # after the first one is created against the SAME name and therefore
        # against this DACL, and that needs FILE_CREATE_PIPE_INSTANCE (0x4) —
        # granted here only because it sits inside FILE_ALL_ACCESS (0x1F01FF).
        # A narrower read/write mask would serve exactly one connection and
        # then fail the accept loop's next CreateNamedPipe with access denied.
        dacl.AddAccessAllowedAce(win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, user_sid)

        sd = win32security.SECURITY_DESCRIPTOR()
        sd.SetSecurityDescriptorDacl(1, dacl, 0)

        sa = win32security.SECURITY_ATTRIBUTES()
        sa.SECURITY_DESCRIPTOR = sd
        sa.bInheritHandle = False
        return sa

    def _serve_windows(self) -> None:  # pragma: no cover (Windows-only)
        # Protocol identical to the AF_UNIX path; imported lazily so
        # non-Windows never needs pywin32.
        import win32pipe
        import win32file
        import pywintypes

        name = _pipe_name(self.global_dir, self.account_id)
        try:
            sa = self._current_user_pipe_sa()
        except Exception:
            # Fail closed — no fallback to a default-DACL pipe. But the failure
            # has to be *visible*: this runs on the service thread, where an
            # escaping exception goes to threading.excepthook -> stderr, which
            # a PyInstaller --windowed build discards, and main.py's try/except
            # around start() has already returned by then. All that would be
            # left is "[ipc] listener started (ready=False)" with no reason,
            # while account_launcher reads the resulting request_activate()
            # False as "no process running" and launches a SECOND process for
            # an account that is already running.
            logging.exception("[ipc] pipe DACL build failed — listener not started")
            return
        self._ready.set()
        while not self._stop.is_set():
            # Bound to None first so the handler below can tell "CreateNamedPipe
            # itself failed" from "this iteration's handle needs closing". A
            # bare CloseHandle(handle) in the except would otherwise close the
            # *previous* iteration's handle — which a live connection thread is
            # still using — and kill a working connection.
            handle = None
            try:
                handle = win32pipe.CreateNamedPipe(
                    name,
                    win32pipe.PIPE_ACCESS_DUPLEX,
                    win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE | win32pipe.PIPE_WAIT,
                    win32pipe.PIPE_UNLIMITED_INSTANCES,
                    65536, 65536, 0, sa,
                )
                win32pipe.ConnectNamedPipe(handle, None)
            except pywintypes.error:
                # A client that connected and closed again before we reached
                # ConnectNamedPipe raises ERROR_NO_DATA (232) here — routine,
                # since request_activate()/request_quit() give up on their own
                # deadline and quit_all_accounts() is explicitly best-effort
                # about unresponsive peers. Without this close, every such
                # give-up leaked a kernel handle and a pipe instance for the
                # life of the process, and PIPE_UNLIMITED_INSTANCES means
                # nothing ever caps that.
                # (ERROR_PIPE_CONNECTED, 535 — client already waiting — is not
                # an exception in pywin32; it returns normally and the read
                # below works, so it never lands here.)
                if handle is not None:
                    try:
                        win32file.CloseHandle(handle)
                    except Exception:
                        pass
                if self._stop.is_set():
                    break
                time.sleep(0.05)
                continue
            # Handed off to its own thread so a slow reply — "quit" waits
            # here for up to _QUIT_RELEASE_POLL_SECONDS for
            # released_predicate() — can't stall this
            # loop from accepting the next connection. Before this, a "quit"
            # in flight made every other request (e.g. a concurrent
            # "activate" from another account switching in) simply time out
            # on the caller's side, since nothing was accepting it.
            threading.Thread(
                target=self._handle_pipe_connection, args=(handle,), daemon=True
            ).start()

    def _read_pipe_message(self, handle, timeout: float = 5.0) -> Optional[bytes]:
        """Read one message from a connected pipe instance, or None if the
        client sent nothing within `timeout` (mirrors the AF_UNIX side's
        conn.settimeout(5.0)).

        The pipe is PIPE_WAIT, so a bare ReadFile against a client that
        connects and never writes blocks FOREVER. Polling PeekNamedPipe is the
        cheapest way out that this transport can carry: overlapped I/O would
        mean threading FILE_FLAG_OVERLAPPED plus an OVERLAPPED/event through
        CreateNamedPipe, ConnectNamedPipe and every WriteFile in this file, and
        PIPE_NOWAIT is documented as legacy-only and just turns ReadFile into
        this same poll with worse semantics. Cancelling the read from a
        watchdog thread was the other candidate and was rejected as tearing a
        handle out from under a blocked syscall.
        """
        import win32pipe
        import win32file

        deadline = time.monotonic() + timeout
        while True:
            if win32pipe.PeekNamedPipe(handle, 0)[1] > 0:
                return win32file.ReadFile(handle, 65536)[1]
            if self._stop.is_set() or time.monotonic() >= deadline:
                return None
            time.sleep(0.01)

    def _handle_pipe_connection(self, handle) -> None:
        import win32pipe
        import win32file
        import pywintypes

        try:
            data = self._read_pipe_message(handle)
            if data is None:
                return
            for line in self._handle_message(data.decode("utf-8")):
                win32file.WriteFile(handle, (line + "\n").encode("utf-8"))
            win32file.FlushFileBuffers(handle)
        except pywintypes.error as exc:
            # The peer going away mid-exchange is a transport condition, not
            # a fault: request_quit()'s timeout is kept above
            # _QUIT_RELEASE_POLL_SECONDS, but a caller with a shorter timeout
            # (or one that gives up for an unrelated reason) can still
            # disconnect first, and WriteFile/FlushFileBuffers then raises
            # 109 (ERROR_BROKEN_PIPE). Logged at info, not error, so it
            # doesn't drown a real ERROR in log.log.
            logging.info("[ipc] pipe connection ended: winerror=%s %s",
                         exc.winerror, exc.funcname)
        except Exception:
            # Deliberately broader than pywintypes.error: a client sending
            # non-UTF-8 raises UnicodeDecodeError (a ValueError), which the
            # AF_UNIX twin catches and this one used not to — leaving an
            # unhandled exception on a connection thread AND, since the cleanup
            # lived at the end of the same try, a leaked handle with it.
            logging.exception("[ipc] pipe connection handler failed")
        finally:
            # Always: any error above used to leak the handle and its pipe
            # instance. Closing an already-closed pywin32 handle is a no-op,
            # so nothing here needs to know how far the exchange got.
            try:
                win32pipe.DisconnectNamedPipe(handle)
            except Exception:
                pass
            try:
                win32file.CloseHandle(handle)
            except Exception:
                pass

    # ── per-connection handling (shared logic) ───────────────────────────
    def _handle_conn(self, conn: socket.socket) -> None:
        # Runs on its own thread (see _serve_unix) — closes conn itself now
        # that the accept loop no longer wraps the call in `with conn:`.
        with conn:
            conn.settimeout(5.0)
            buf = b""
            try:
                while b"\n" not in buf:
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    buf += chunk
                line = buf.split(b"\n", 1)[0].decode("utf-8")
                for reply in self._handle_message(line):
                    conn.sendall((reply + "\n").encode("utf-8"))
            except (OSError, ValueError):
                pass

    def _handle_message(self, line: str) -> list[str]:
        try:
            req = json.loads(line)
        except ValueError:
            return []
        cmd = req.get("cmd")
        rid = req.get("request_id")
        replies = []
        if cmd == "activate":
            replies.append(json.dumps({"request_id": rid, "ack": True}))
            self._dispatch_activate(req.get("source", "user"))
        elif cmd == "quit":
            replies.append(json.dumps({"request_id": rid, "ack": True}))
            # Trigger shutdown, then wait until the process actually releases.
            self.on_quit()
            deadline = time.monotonic() + _QUIT_RELEASE_POLL_SECONDS
            while time.monotonic() < deadline:
                if self.released_predicate():
                    break
                time.sleep(0.05)
            replies.append(json.dumps(
                {"request_id": rid, "released": bool(self.released_predicate())}
            ))
        return replies


# ── client requests ──────────────────────────────────────────────────────────
def _send(global_dir: str, account_id: str, req: dict, timeout: float) -> Optional[list[dict]]:
    """Send one request, return the list of JSON replies, or None if no listener."""
    if _IS_WINDOWS:
        return _send_windows(global_dir, account_id, req, timeout)
    return _send_unix(global_dir, account_id, req, timeout)


def _send_unix(global_dir: str, account_id: str, req: dict, timeout: float) -> Optional[list[dict]]:
    path = _unix_path(global_dir, account_id)
    deadline = time.monotonic() + timeout
    # readiness: retry connect until listener is up or we time out
    while time.monotonic() < deadline:
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(max(0.1, deadline - time.monotonic()))
            s.connect(path)
        except OSError:
            time.sleep(0.05)
            continue
        try:
            s.sendall((json.dumps(req) + "\n").encode("utf-8"))
            return _read_replies(s, deadline)
        finally:
            s.close()
    return None


def _send_windows(global_dir: str, account_id: str, req: dict, timeout: float):  # pragma: no cover
    import win32file
    import pywintypes

    name = _pipe_name(global_dir, account_id)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            handle = win32file.CreateFile(
                name, win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0, None, win32file.OPEN_EXISTING, 0, None,
            )
        except pywintypes.error:
            time.sleep(0.05)
            continue
        try:
            win32file.WriteFile(handle, (json.dumps(req) + "\n").encode("utf-8"))
            win32file.FlushFileBuffers(handle)
            data = b""
            while time.monotonic() < deadline:
                try:
                    data += win32file.ReadFile(handle, 65536)[1]
                except pywintypes.error:
                    break
                if data.count(b"\n") >= (2 if req.get("cmd") == "quit" else 1):
                    break
            return [json.loads(x) for x in data.decode("utf-8").splitlines() if x]
        finally:
            win32file.CloseHandle(handle)
    return None


def _read_replies(s: socket.socket, deadline: float) -> list[dict]:
    buf = b""
    replies: list[dict] = []
    while time.monotonic() < deadline:
        try:
            s.settimeout(max(0.05, deadline - time.monotonic()))
            chunk = s.recv(4096)
        except socket.timeout:
            break
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if line:
                try:
                    replies.append(json.loads(line.decode("utf-8")))
                except ValueError:
                    pass
    return replies


def request_activate(global_dir: str, account_id: str, source: str = "user",
                     timeout: float = 3.0) -> bool:
    """Ask the process owning account_id to come to the foreground.

    Returns True if a listener acknowledged; False if none is running.
    """
    replies = _send(global_dir, account_id, _make_request("activate", source), timeout)
    if not replies:
        return False
    return any(r.get("ack") for r in replies)


def request_quit(global_dir: str, account_id: str, timeout: float = 85.0) -> bool:
    """Ask the process owning account_id to quit.

    Returns True only once the target confirms it has RELEASED (terminated /
    dropped its mutex+lease), not merely acknowledged the request.

    85s default: must stay above _QUIT_RELEASE_POLL_SECONDS (75s, the target
    LISTENER's own poll budget) plus slack for transport round-trip. A
    shorter timeout here does not corrupt anything — the target keeps
    closing regardless — but makes this caller read the target's honest
    "still working on it" as a flat False. Raise the two together; raising
    only one changes nothing.
    """
    replies = _send(global_dir, account_id, _make_request("quit"), timeout)
    if not replies:
        return False
    return any(r.get("released") for r in replies)
