"""Tests for client/ipc.py — account-scoped activate/quit IPC.

On Linux these exercise the AF_UNIX fallback transport + the shared protocol
(request_id framing, activate/quit dispatch, readiness, queue-before-window).
The Windows named-pipe transport shares the same protocol layer.
"""

import logging
import os
import sys
import threading
import time

import pytest

import ipc


def _gd(tmp_path):
    gd = str(tmp_path / "global")
    os.makedirs(gd, exist_ok=True)
    return gd


def _test_pipe_name(tag):
    """A pipe name unique to this process.

    Pipe names are machine-global: with a fixed name, two pytest runs in
    parallel — or a leftover instance from a run that was killed — create a
    SECOND instance of the same name, and the client thread below can then
    connect to the other process's instance, leaving ConnectNamedPipe here
    hanging until the join timeout.
    """
    return r"\\.\pipe\wz_test_%s_%d" % (tag, os.getpid())


def _create_test_pipe(name, sa):
    import win32pipe

    return win32pipe.CreateNamedPipe(
        name,
        win32pipe.PIPE_ACCESS_DUPLEX,
        win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE | win32pipe.PIPE_WAIT,
        win32pipe.PIPE_UNLIMITED_INSTANCES,
        65536, 65536, 0, sa,
    )


def test_no_listener_returns_false(tmp_path):
    gd = _gd(tmp_path)
    assert ipc.request_activate(gd, "a" * 32, source="user", timeout=0.3) is False
    assert ipc.request_quit(gd, "a" * 32, timeout=0.3) is False


def test_activate_delivers_with_source(tmp_path):
    gd = _gd(tmp_path)
    acc = "a" * 32
    received = []

    def on_activate(source):
        received.append(source)

    listener = ipc.IpcListener(gd, acc, on_activate=on_activate, on_quit=lambda: None)
    listener.start()
    try:
        assert listener.wait_ready(timeout=2.0)
        ok = ipc.request_activate(gd, acc, source="user", timeout=2.0)
        assert ok is True
        # give the listener thread a moment to run the callback
        for _ in range(50):
            if received:
                break
            time.sleep(0.02)
        assert received == ["user"]
    finally:
        listener.stop()


def test_quit_ack_then_release(tmp_path):
    """request_quit returns True only after the target signals release."""
    gd = _gd(tmp_path)
    acc = "b" * 32
    released = threading.Event()

    def on_quit():
        # Simulate the process shutting down and releasing its mutex/lease.
        threading.Timer(0.1, released.set).start()

    listener = ipc.IpcListener(
        gd, acc, on_activate=lambda s: None, on_quit=on_quit,
        released_predicate=lambda: released.is_set(),
    )
    listener.start()
    try:
        assert listener.wait_ready(timeout=2.0)
        ok = ipc.request_quit(gd, acc, timeout=3.0)
        assert ok is True
        assert released.is_set()
    finally:
        listener.stop()


def test_activate_scoped_by_account(tmp_path):
    """A request for account A must not reach a listener for account B."""
    gd = _gd(tmp_path)
    a, b = "a" * 32, "b" * 32
    hits = []
    la = ipc.IpcListener(gd, a, on_activate=lambda s: hits.append("a"), on_quit=lambda: None)
    lb = ipc.IpcListener(gd, b, on_activate=lambda s: hits.append("b"), on_quit=lambda: None)
    la.start(); lb.start()
    try:
        assert la.wait_ready(2.0) and lb.wait_ready(2.0)
        ipc.request_activate(gd, a, source="user", timeout=2.0)
        time.sleep(0.2)
        assert hits == ["a"]
    finally:
        la.stop(); lb.stop()


def test_queue_before_window_then_flush(tmp_path):
    """Requests arriving before the window is ready are queued and flushed."""
    gd = _gd(tmp_path)
    acc = "c" * 32
    delivered = []
    window_ready = {"v": False}

    def on_activate(source):
        if not window_ready["v"]:
            # simulate: no window yet -> the listener should have queued it,
            # so we should never see this until window_ready is True.
            delivered.append(("early", source))
        else:
            delivered.append(("ready", source))

    listener = ipc.IpcListener(
        gd, acc, on_activate=on_activate, on_quit=lambda: None,
        window_ready_predicate=lambda: window_ready["v"],
    )
    listener.start()
    try:
        assert listener.wait_ready(2.0)
        ipc.request_activate(gd, acc, source="user", timeout=2.0)
        time.sleep(0.2)
        assert delivered == []  # queued, not delivered yet
        window_ready["v"] = True
        listener.flush_queue()
        for _ in range(50):
            if delivered:
                break
            time.sleep(0.02)
        assert delivered == [("ready", "user")]
    finally:
        listener.stop()


def test_a_concurrent_activate_is_not_blocked_by_an_in_flight_quit(tmp_path):
    """Regression: handling "quit" used to happen inline in the accept loop,
    including its up-to-_QUIT_RELEASE_POLL_SECONDS wait for
    released_predicate() — so any other request sent while a quit was in
    flight had nowhere to connect to until that wait finished. Each
    connection is now handed to its own thread so the accept loop is free
    to pick up the next one immediately."""
    gd = _gd(tmp_path)
    acc = "d" * 32
    released = threading.Event()
    quit_in_flight = threading.Event()

    def on_quit():
        quit_in_flight.set()
        # Slow enough that an inline handler would clearly stall the next
        # request; short enough to keep the test fast.
        threading.Timer(1.5, released.set).start()

    listener = ipc.IpcListener(
        gd, acc, on_activate=lambda s: None, on_quit=on_quit,
        released_predicate=lambda: released.is_set(),
    )
    listener.start()
    try:
        assert listener.wait_ready(2.0)

        quit_result = {}

        def _do_quit():
            quit_result["ok"] = ipc.request_quit(gd, acc, timeout=5.0)

        t = threading.Thread(target=_do_quit)
        t.start()
        # Wait for the quit to have actually reached the handler rather than
        # sleeping a fixed amount: on a loaded CI runner request_quit's
        # retry-connect can land after any such sleep, and the activate below
        # would then be answered by an idle accept loop — passing without ever
        # exercising the regression, silently.
        assert quit_in_flight.wait(5.0)

        start = time.monotonic()
        ok = ipc.request_activate(gd, acc, source="user", timeout=2.0)
        elapsed = time.monotonic() - start

        assert ok is True
        assert elapsed < 1.0, (
            f"activate took {elapsed:.2f}s while a quit was in flight — "
            "the accept loop was blocked instead of handling it concurrently"
        )
        t.join(timeout=5)
        assert quit_result.get("ok") is True
    finally:
        listener.stop()


@pytest.mark.skipif(sys.platform != "win32", reason="named pipe DACL is Windows-only")
class TestNamedPipeDacl:
    """The module docstring has always claimed the named pipe is 'restricted
    to the current user' — this actually creates a pipe with the real
    SECURITY_ATTRIBUTES the listener builds and inspects the DACL Windows
    attached to it, rather than trusting that the pywin32 calls did what the
    comment says. A bare SECURITY_ATTRIBUTES() (the previous code) was
    confirmed, by this same technique, to grant full control to the current
    user AND BUILTIN\\Administrators AND NT AUTHORITY\\SYSTEM."""

    def test_only_the_current_user_has_an_ace(self):
        import win32file
        import win32security
        import win32api
        import ntsecuritycon

        sa = ipc.IpcListener._current_user_pipe_sa()
        handle = _create_test_pipe(_test_pipe_name("dacl"), sa)
        try:
            sd = win32security.GetSecurityInfo(
                handle, win32security.SE_KERNEL_OBJECT,
                win32security.DACL_SECURITY_INFORMATION,
            )
            dacl = sd.GetSecurityDescriptorDacl()
            assert dacl is not None
            assert dacl.GetAceCount() == 1

            token = win32security.OpenProcessToken(
                win32api.GetCurrentProcess(), win32security.TOKEN_QUERY
            )
            current_sid, _ = win32security.GetTokenInformation(token, win32security.TokenUser)
            (ace_type, _ace_flags), mask, sid = dacl.GetAce(0)
            assert sid == current_sid
            # The SID alone proves nothing about what it is granted: a single
            # ACCESS_DENIED ACE for this same user would satisfy both the count
            # and the SID check above, and lock the owner out of its own pipe.
            assert ace_type == win32security.ACCESS_ALLOWED_ACE_TYPE
            # And it must grant enough: read+write for the exchange itself,
            # plus FILE_CREATE_PIPE_INSTANCE, without which every instance of
            # this pipe after the first fails with access denied.
            required = (ntsecuritycon.FILE_GENERIC_READ
                        | ntsecuritycon.FILE_GENERIC_WRITE
                        | ntsecuritycon.FILE_CREATE_PIPE_INSTANCE)
            assert mask & required == required
        finally:
            win32file.CloseHandle(handle)

    def test_the_current_user_can_still_connect(self):
        """The security lockdown must not lock out the very process that
        creates the pipe."""
        import win32pipe
        import win32file
        import pywintypes

        sa = ipc.IpcListener._current_user_pipe_sa()
        name = _test_pipe_name("dacl_connect")
        handle = _create_test_pipe(name, sa)
        connected = {}
        server_done = threading.Event()

        def _client():
            try:
                h = win32file.CreateFile(
                    name, win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                    0, None, win32file.OPEN_EXISTING, 0, None,
                )
                connected["ok"] = True
                # Hold the client end open until the server side is through its
                # ConnectNamedPipe. Closing immediately made that call fail with
                # ERROR_NO_DATA ("the pipe is being closed") whenever the client
                # won the race — a flake, and one that reads exactly like the
                # permissions failure this test exists to rule out.
                server_done.wait(5)
                win32file.CloseHandle(h)
            except pywintypes.error as exc:
                connected["error"] = exc

        t = threading.Thread(target=_client)
        t.start()
        try:
            win32pipe.ConnectNamedPipe(handle, None)
        finally:
            server_done.set()
            t.join(timeout=5)
            win32file.CloseHandle(handle)

        assert connected.get("ok") is True, connected.get("error")


@pytest.mark.skipif(sys.platform != "win32", reason="named pipe transport is Windows-only")
class TestNamedPipeConnectionHandling:
    """The per-connection half of the Windows transport, driven against a real
    pipe. Every connection gets its own thread now, so a client that misbehaves
    no longer merely stalls the accept loop — it costs a thread and a pipe
    instance that must both be reclaimed."""

    def test_a_client_that_never_writes_does_not_block_forever(self):
        """Regression: the pipe is PIPE_WAIT, so ReadFile against a client that
        connects and then says nothing blocked indefinitely — one such client
        per connection, and thread/pipe-instance growth is unbounded. The
        AF_UNIX twin has had conn.settimeout(5.0) for this all along."""
        import win32pipe
        import win32file
        import pywintypes

        listener = ipc.IpcListener(
            "", "e" * 32, on_activate=lambda s: None, on_quit=lambda: None
        )
        name = _test_pipe_name("silent_client")
        handle = _create_test_pipe(name, ipc.IpcListener._current_user_pipe_sa())
        client = {}

        def _client():
            try:
                client["h"] = win32file.CreateFile(
                    name, win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                    0, None, win32file.OPEN_EXISTING, 0, None,
                )
            except pywintypes.error as exc:
                client["error"] = exc

        t = threading.Thread(target=_client)
        t.start()
        try:
            win32pipe.ConnectNamedPipe(handle, None)
            t.join(timeout=5)
            assert client.get("h") is not None, client.get("error")

            start = time.monotonic()
            assert listener._read_pipe_message(handle, timeout=0.3) is None
            assert time.monotonic() - start < 3.0
        finally:
            if client.get("h") is not None:
                win32file.CloseHandle(client["h"])
            win32file.CloseHandle(handle)

    def test_a_non_utf8_message_is_logged_and_the_handle_released(self, caplog):
        """Regression: data.decode("utf-8") raises UnicodeDecodeError, which the
        old `except pywintypes.error` did not catch — the connection thread died
        with an unhandled traceback (discarded entirely in a --windowed build)
        and, because the cleanup sat at the end of that same try, leaked the
        handle and its pipe instance with it."""
        import win32pipe
        import win32file
        import pywintypes

        listener = ipc.IpcListener(
            "", "f" * 32, on_activate=lambda s: None, on_quit=lambda: None
        )
        name = _test_pipe_name("bad_utf8")
        handle = _create_test_pipe(name, ipc.IpcListener._current_user_pipe_sa())
        done = threading.Event()
        client = {}

        def _client():
            try:
                h = win32file.CreateFile(
                    name, win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                    0, None, win32file.OPEN_EXISTING, 0, None,
                )
                win32file.WriteFile(h, b"\xff\xfe not utf-8\n")
                # Hold the client end open until the server side is done, so
                # the read under test can't race the buffer away.
                done.wait(5)
                win32file.CloseHandle(h)
            except pywintypes.error as exc:
                client["error"] = exc

        t = threading.Thread(target=_client)
        t.start()
        try:
            win32pipe.ConnectNamedPipe(handle, None)
            with caplog.at_level(logging.ERROR):
                listener._handle_pipe_connection(handle)  # must not raise
            assert "pipe connection handler failed" in caplog.text
            # pywin32 zeroes a PyHANDLE once it is closed — proof the finally
            # ran rather than the handle leaking on the way out.
            assert int(handle) == 0
        finally:
            done.set()
            t.join(timeout=5)
            assert client.get("error") is None
