"""Regression tests: the @lid<->phone mapping caches were mutated from two
threads with nothing coordinating them.

_extract_lid_mapping() runs unprotected on the Socket.IO callback thread —
WebSocketClient.on_messages_upsert() (core/websocket_client.py) calls it
directly, not via wx.CallAfter like on_new_message()/on_historical_message()
— specifically so it can start bridging @lid identifiers before a sync has
even begun (see the method's own docstring). The wx main thread reaches the
very same _lid_to_phone/_phone_to_lid/_message_pushname_cache/
_chats_without_alt_jid dictionaries through on_new_message()'s own call into
this method; a background sync thread writes them through
register_jid_mapping() (_backfill_names -> resolve_lid_jids_via_api) and
rebuilds them wholesale via _build_lid_to_phone_cache(); and
resolve_self_lid() runs a cleanup pass over them on a thread of its own.

None of that was serialized, and the two failure modes are different:

  * a *lost update* — _build_lid_to_phone_cache() scanning every message of
    every chat takes seconds on a large account, so a pair learned from the
    Socket.IO thread meanwhile was thrown away by the wholesale replace at
    the end. The pair stayed in SQLite, so nothing put it back until the
    next restart and that chat kept showing a raw @lid.
  * an outright *RuntimeError* — resolve_self_lid() iterates the live dicts
    in two list comprehensions, and an insert from the Socket.IO thread
    mid-comprehension raises "dictionary changed size during iteration".
    Its blanket `except Exception` swallowed that, so the
    register_jid_mapping() call on its last line never ran: the user's own
    LID<->phone pair went unregistered for the whole session and their own
    messages in groups showed up with no name.

The fix is self._lid_mapping_lock (a threading.RLock, set up alongside the
already-existing _own_sent_ids_lock in MainWindow.__init__) around every
in-memory mutation of and iteration over those dicts — and, just as
deliberately, around none of the blocking work (self.db goes through
DatabaseBridge and waits on the DB thread; the HTTP calls; wx.CallAfter),
which stays outside so the Socket.IO thread never queues behind SQLite.

MainWindow is a wx.Frame and cannot be instantiated without a running
wx.App, so the methods are exercised bound to small stubs — same approach as
tests/test_lid_merge_keeps_messages.py, whose own docstring explains why.
"""

import inspect
import sys
import threading
import types

import pytest

import main
from main import MainWindow

_extract_lid_mapping = MainWindow._extract_lid_mapping


class _TrackingLock:
    """A real RLock that counts how many times it was entered.

    It cannot prove mutual exclusion — being a real lock, it enforces it,
    so any "were two threads inside at once?" counter it kept would be
    tautologically zero. What it does prove is that the code under test
    goes through the lock at all on the paths the tests drive; the races
    themselves are exercised for real in TestTheRacesThatWereObserved
    below.
    """

    def __init__(self):
        self._real = threading.RLock()
        self._guard = threading.Lock()
        self.enters = 0

    def __enter__(self):
        self._real.acquire()
        with self._guard:
            self.enters += 1
        return self

    def __exit__(self, exc_type, exc, tb):
        self._real.release()
        return False


class _FakeDB:
    def __init__(self):
        self.mapping_calls = []

    def set_lid_mapping(self, lid, phone):
        self.mapping_calls.append((lid, phone))

    def upsert_contacts_batch(self, contacts):
        pass

    def delete_lid_mapping(self, lid):
        pass

    def set_metadata(self, key, value):
        pass


class _Stub:
    _extract_lid_mapping = MainWindow._extract_lid_mapping

    def __init__(self):
        self._ui_ready_event = threading.Event()
        self._ui_ready_event.set()
        self._chats_without_alt_jid = set()
        self._message_pushname_cache = {}
        self._lid_to_phone = {}
        self._phone_to_lid = {}
        self.contacts = {}
        self.db = _FakeDB()
        self._lid_mapping_lock = _TrackingLock()

    def _is_self_jid(self, jid):
        return False

    def _schedule_set_chats(self):
        pass

    def _schedule_refresh_active_messages(self, jids=None):
        pass


def _msg(i):
    lid = f"{1000 + i}@lid"
    phone = f"{2000 + i}@s.whatsapp.net"
    return {
        "key": {
            "remoteJid": lid,
            "remoteJidAlt": phone,
            "fromMe": True,  # skips the sender/mention resolution tail —
                              # irrelevant to the mapping caches under test
        },
        "messageType": "conversation",
        "pushName": f"Contact {i}",
    }


@pytest.fixture(autouse=True)
def _no_real_wx_callafter(monkeypatch):
    monkeypatch.setattr(main.wx, "CallAfter", lambda fn, *a, **kw: None)


class TestConcurrentCallsAreSerialized:
    def test_every_call_goes_through_the_lock(self):
        stub = _Stub()
        threads = [threading.Thread(target=stub._extract_lid_mapping, args=(_msg(i),))
                   for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert stub._lid_mapping_lock.enters == 20

    def test_no_pair_is_lost_under_concurrency(self):
        stub = _Stub()
        threads = [threading.Thread(target=stub._extract_lid_mapping, args=(_msg(i),))
                   for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(stub._lid_to_phone) == 20
        assert len(stub._phone_to_lid) == 20
        for i in range(20):
            assert stub._lid_to_phone[f"{1000 + i}@lid"] == f"{2000 + i}@s.whatsapp.net"

    def test_no_exception_escapes_any_thread(self):
        stub = _Stub()
        errors = []

        def _run(i):
            try:
                stub._extract_lid_mapping(_msg(i))
            except Exception as exc:  # pragma: no cover - failure path only
                errors.append(exc)

        threads = [threading.Thread(target=_run, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert errors == []


class TestSingleCallStillWorksNormally:
    def test_a_lone_call_records_the_mapping(self):
        stub = _Stub()

        stub._extract_lid_mapping(_msg(0))

        assert stub._lid_to_phone == {"1000@lid": "2000@s.whatsapp.net"}
        assert stub._phone_to_lid == {"2000@s.whatsapp.net": "1000@lid"}
        assert stub.db.mapping_calls == [("1000@lid", "2000@s.whatsapp.net")]

    def test_an_unchanged_repeat_call_does_not_write_the_db_again(self):
        stub = _Stub()
        stub._extract_lid_mapping(_msg(0))

        stub._extract_lid_mapping(_msg(0))

        assert stub.db.mapping_calls == [("1000@lid", "2000@s.whatsapp.net")]


# ── The two races, reproduced ────────────────────────────────────────────

MY_LID = "999888777666555@lid"
MY_PHONE = "5511900000000@s.whatsapp.net"

LATE_LID = "444555666777888@lid"
LATE_PHONE = "5511911111111@s.whatsapp.net"


class _SelfLidStub(_Stub):
    """Enough of MainWindow for resolve_self_lid()'s worker thread to run."""

    def __init__(self):
        super().__init__()
        self.chats = {}
        self.my_jid = MY_PHONE
        self.my_lid = ""
        self.wpp_server = "http://127.0.0.1"
        self.wpp_port = 6300
        self.token = "t"
        self._unresolvable_lids = set()
        # Set on the way out of register_jid_mapping() so the test can wait
        # for the worker thread instead of sleeping. Under the bug it is
        # never set at all: the RuntimeError fires first and the blanket
        # `except Exception` in _resolve() swallows it.
        self.self_mapping_done = threading.Event()

    _normalize_jid = staticmethod(MainWindow._normalize_jid)
    resolve_self_lid = MainWindow.resolve_self_lid

    def register_jid_mapping(self, lid_jid, phone_jid, save=True, defer_ui=False):
        try:
            MainWindow.register_jid_mapping(self, lid_jid, phone_jid,
                                            save=save, defer_ui=defer_ui)
        finally:
            self.self_mapping_done.set()


class _FakeResponse:
    status_code = 200

    @staticmethod
    def json():
        return {"lid": {"_serialized": MY_LID}, "phone": {"_serialized": MY_PHONE}}


@pytest.fixture
def _tight_thread_switching():
    """Python only reschedules threads every 5 ms by default, which is long
    enough for a whole list comprehension to run start to finish without
    ever yielding — i.e. long enough to hide the very race under test.
    Shrink the interval so the interleaving actually happens."""
    previous = sys.getswitchinterval()
    sys.setswitchinterval(0.000001)
    try:
        yield
    finally:
        sys.setswitchinterval(previous)


class TestTheRacesThatWereObserved:
    """These two fail against the unlocked code, which is the whole point:
    a test that passes either way documents nothing."""

    ROUNDS = 6
    PREFILLED_PAIRS = 4000

    @pytest.mark.usefixtures("_tight_thread_switching")
    def test_self_lid_resolution_survives_messages_arriving_mid_cleanup(
            self, monkeypatch, caplog):
        """resolve_self_lid()'s cleanup comprehensions iterate the live
        dicts. A message landing on the Socket.IO thread while one is
        running used to raise "dictionary changed size during iteration",
        which the method's own `except Exception` swallowed — so the user's
        own mapping was never registered."""
        monkeypatch.setattr(main, "api_get", lambda *a, **kw: _FakeResponse())
        caplog.set_level("ERROR")

        for round_no in range(self.ROUNDS):
            stub = _SelfLidStub()
            # A real account's cache is large, and the comprehension has to
            # take long enough for the writer below to land inside it.
            for i in range(self.PREFILLED_PAIRS):
                lid, phone = f"{500000 + i}@lid", f"{600000 + i}@s.whatsapp.net"
                stub._lid_to_phone[lid] = phone
                stub._phone_to_lid[phone] = lid

            started = threading.Barrier(2)
            stop = threading.Event()

            def _socket_io_thread():
                started.wait(timeout=5)
                i = 0
                while not stop.is_set():
                    stub._extract_lid_mapping(_msg(i))
                    i += 1

            writer = threading.Thread(target=_socket_io_thread, daemon=True)
            writer.start()
            started.wait(timeout=5)
            try:
                stub.resolve_self_lid()  # spawns its own worker thread
                registered = stub.self_mapping_done.wait(timeout=10)
            finally:
                stop.set()
                writer.join(timeout=5)

            assert registered, (
                f"round {round_no}: resolve_self_lid() never reached "
                f"register_jid_mapping() — it died inside its own cleanup and "
                f"the error was swallowed. Captured: {caplog.text!r}"
            )
            assert stub._lid_to_phone[MY_LID] == MY_PHONE
            assert stub._phone_to_lid[MY_PHONE] == MY_LID

    def test_a_pair_learned_during_the_scan_survives_the_rebuild(self):
        """_build_lid_to_phone_cache() scans every message of every chat
        outside the lock — seconds of work on a large account. Whatever the
        Socket.IO thread learns in that window is only in the live dict, so
        the rebuild has to merge into it, not assign over it."""

        class _ChatThatIsInterruptedMidScan(dict):
            """Stands in for the Socket.IO thread learning a pair while the
            scan is walking the chats: the real window is measured in
            seconds, which a test cannot sit and wait for."""

            def __init__(self, stub):
                super().__init__()
                self._stub = stub

            def get(self, *args, **kwargs):
                self._stub._lid_to_phone[LATE_LID] = LATE_PHONE
                self._stub._phone_to_lid[LATE_PHONE] = LATE_LID
                return super().get(*args, **kwargs)

        stub = _Stub()
        stub._build_lid_to_phone_cache = types.MethodType(
            MainWindow.__dict__["_build_lid_to_phone_cache"], stub)
        stub.chats = {"a@s.whatsapp.net": _ChatThatIsInterruptedMidScan(stub)}

        stub._build_lid_to_phone_cache()

        assert stub._lid_to_phone.get(LATE_LID) == LATE_PHONE, (
            "the rebuild replaced the live cache instead of merging into it, "
            "dropping a pair learned while it was scanning"
        )
        assert stub._phone_to_lid.get(LATE_PHONE) == LATE_LID


#: Every MainWindow method that mutates _lid_to_phone/_phone_to_lid. The list
#: is the audit itself: a writer missing from it is a writer nobody re-checked.
_MAPPING_WRITERS = [
    "_extract_lid_mapping",
    "_build_lid_to_phone_cache",
    "register_jid_mapping",
    "resolve_self_lid",
    "get_contact_profile",
    "get_remote_chats",
    "_load_local_lid_cache",
    "clear_local_data",
]


class TestTheLockIsWiredUpStructurally:
    """inspect.getsource pins that every writer actually uses the lock —
    same style as test_lid_merge_keeps_messages.py's own structural test for
    a method too large to exercise every branch of directly."""

    @pytest.mark.parametrize("method_name", _MAPPING_WRITERS)
    def test_every_mapping_writer_uses_the_lock(self, method_name):
        src = inspect.getsource(getattr(MainWindow, method_name))
        assert "with self._lid_mapping_lock:" in src

    @pytest.mark.parametrize("method_name", _MAPPING_WRITERS)
    def test_no_blocking_call_happens_under_the_lock(self, method_name):
        """The rule that keeps this lock from becoming a freeze: the
        Socket.IO thread must never queue behind something that blocks its
        caller — a DatabaseBridge call waits on .result(timeout=), and the
        rest reach the network or the wx loop. Every writer collects such
        work and does it after releasing.

        Parametrized over all writers, not just the ones that persist today.
        _load_local_lid_cache and _build_lid_to_phone_cache are the point:
        moving db.get_lid_mappings() / the message scan out of the section is
        the whole design, and tidying either back into one `with` block would
        otherwise reintroduce a blocking call under the lock with a green suite.
        """
        blocking = ("self.db.", "wx.CallAfter", "api_get", "api_post",
                    "save_data(", "time.sleep", ".result(")
        src = inspect.getsource(getattr(MainWindow, method_name))
        inside = _lines_inside_the_lock(src)
        offenders = [line for line in inside
                     if any(token in line for token in blocking)]
        assert not offenders, (
            f"{method_name}() blocks while holding _lid_mapping_lock: {offenders}"
        )

    def test_the_lock_is_reentrant(self):
        """RLock, not Lock — _extract_lid_mapping calling other self methods
        while already inside the critical section must not be a deadlock
        trap for whoever touches this next."""
        src = inspect.getsource(MainWindow.__init__)
        assert "_lid_mapping_lock = threading.RLock()" in src


def _lines_inside_the_lock(src):
    """Every source line more deeply indented than a `with
    self._lid_mapping_lock:` statement, i.e. the critical sections."""
    lines = src.splitlines()
    inside, guard_indent = [], None
    for line in lines:
        if not line.strip():
            continue
        # Comments are prose, not calls — a critical section explaining *why*
        # its db write was deferred would otherwise fail this test.
        if line.strip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if guard_indent is not None:
            if indent > guard_indent:
                inside.append(line.strip())
                continue
            guard_indent = None
        if line.strip() == "with self._lid_mapping_lock:":
            guard_indent = indent
    return inside
