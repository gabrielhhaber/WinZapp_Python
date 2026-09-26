"""Tests for when "this chat has no older history" is allowed to become durable.

fetch_older_messages() writes a chat off when WhatsApp Web answers with an
empty page. WhatsApp only pushes a bounded window of history to a linked
device, so an empty page usually means "this device has no more", not "this
conversation has no more" — hence request_older_messages(), which asks the
phone. The phone replies with a history-sync chunk *minutes later*, never in
that response.

The deep backfill revisits a chat roughly 30 seconds after the ask, so the
second empty page — the one that writes the chat off — arrives an order of
magnitude before the answer it is judging. That was survivable while
_exhausted_chats died with the process. Once it was persisted, the same
premature write-off became permanent: fetch_older_messages() early-returns on
an exhausted chat, and that early return is shared with the user scrolling up,
so the conversation stopped loading older messages in every future session,
with no removal path anywhere in the code and F5 preserving the metadata.

So: keep the chat re-queryable for the whole reply window. A temporary API
refusal while RECENT history is still landing returns None and must not start
the grace clock at all. Only an empty page observed after the grace window is
evidence strong enough to mark or persist exhaustion.

MainWindow is a wx.Frame and cannot be instantiated without a running wx.App,
so the method is bound to a plain stub, as elsewhere in this suite.
"""

import inspect
import types

import pytest

import main
from main import MainWindow


GRACE = MainWindow._OLDER_REQUEST_GRACE
JID = "5511999999999@s.whatsapp.net"
ANCHOR = {"key": {"id": "m1", "remoteJid": JID}, "messageTimestamp": 1_700_000_000}


class _DB:
    def __init__(self):
        self.metadata = {}

    def set_metadata_json(self, key, value):
        self.metadata[key] = value


class _Response:
    """An empty 200 — the shape that triggers the write-off branch."""

    status_code = 200

    @staticmethod
    def json():
        return {"response": []}


class _Stub:
    def __init__(self, ask_succeeds=True):
        self.db = _DB()
        self._exhausted_chats = set()
        self._older_requested_chats = {}
        self._phone_to_lid = {}
        self._lid_to_phone = {}
        self.settings = {"user_interface": {"messages_page_size": 200}}
        self.wpp_server = "http://127.0.0.1"
        self.wpp_port = 6300
        self.token = "tok"
        self.ws = None
        self._ask_succeeds = ask_succeeds
        self.asks = []

    def request_older_messages(self, jid, timeout=60):
        self.asks.append(jid)
        return self._ask_succeeds

    def _resolve_jid_for_msg_key(self, jid):
        return jid

    def _serialize_msg_id(self, jid, key):
        return f"false_{jid}_{key.get('id', '')}"


def _make(ask_succeeds=True):
    stub = _Stub(ask_succeeds)
    for name in ("fetch_older_messages", "_persist_exhausted_chats",
                 "_persist_older_requested", "_forget_history_exhaustion",
                 "_normalize_jid"):
        raw = inspect.getattr_static(MainWindow, name)
        if isinstance(raw, (staticmethod, classmethod)):
            setattr(stub, name, getattr(MainWindow, name))
        else:
            setattr(stub, name, types.MethodType(raw, stub))
    stub._OLDER_REQUEST_GRACE = MainWindow._OLDER_REQUEST_GRACE
    return stub


@pytest.fixture(autouse=True)
def _empty_page(monkeypatch):
    monkeypatch.setattr(main.requests, "get", lambda *a, **kw: _Response())


class TestTheFirstEmptyPage:
    def test_it_asks_the_phone_and_writes_nothing_off(self):
        """Unchanged behaviour: the ask goes out and the chat stays
        re-queryable so the reply can be picked up."""
        stub = _make()
        stub.fetch_older_messages(JID, ANCHOR)
        assert stub.asks == [JID]
        assert JID not in stub._exhausted_chats
        assert "exhausted_chats" not in stub.db.metadata

    def test_the_ask_is_recorded_and_persisted(self):
        """The timestamp is the whole mechanism: an empty page can only be
        judged against how long ago the phone was asked, and that has to
        outlive the session."""
        stub = _make()
        stub.fetch_older_messages(JID, ANCHOR)
        assert JID in stub._older_requested_chats
        assert JID in stub.db.metadata["older_history_requested"]


class TestTheSecondEmptyPageInsideTheReplyWindow:
    """The captured failure: a backfill pass revisits the chat ~30 s after the
    ask, long before the phone's history-sync chunk lands."""

    def test_it_stays_requeryable(self):
        stub = _make()
        stub.fetch_older_messages(JID, ANCHOR)          # asks
        stub.fetch_older_messages(JID, ANCHOR)          # 30 s later, still empty
        assert JID not in stub._exhausted_chats

    def test_it_does_NOT_persist(self):
        stub = _make()
        stub.fetch_older_messages(JID, ANCHOR)
        stub.fetch_older_messages(JID, ANCHOR)
        assert "exhausted_chats" not in stub.db.metadata, (
            "a write-off made inside the reply window must not outlive the "
            "session — it is what silently killed scroll-up for good")

    def test_the_phone_is_not_asked_twice(self):
        stub = _make()
        stub.fetch_older_messages(JID, ANCHOR)
        stub.fetch_older_messages(JID, ANCHOR)
        assert stub.asks == [JID]


class TestOnceTheReplyWindowHasPassed:
    """The next launch: _older_requested_chats came back from metadata, so the
    ask is provably older than anything the phone still owes."""

    def test_it_persists(self, monkeypatch):
        stub = _make()
        stub._older_requested_chats[JID] = main.time.time() - (GRACE + 1)
        stub.fetch_older_messages(JID, ANCHOR)
        assert JID in stub._exhausted_chats
        assert stub.db.metadata["exhausted_chats"] == [JID]

    def test_a_legacy_entry_counts_as_asked_long_ago(self):
        """prepare_sync maps the old list form to timestamp 0.0 — those asks
        were made by an earlier run, which is exactly the evidence wanted."""
        stub = _make()
        stub._older_requested_chats[JID] = 0.001
        stub.fetch_older_messages(JID, ANCHOR)
        assert stub.db.metadata["exhausted_chats"] == [JID]

    def test_user_scroll_reopens_an_exhausted_chat(self):
        """A durable conclusion must not permanently disable explicit scroll."""
        stub = _make()
        stub._exhausted_chats.add(JID)
        assert stub.fetch_older_messages(JID, ANCHOR) is None
        assert stub.asks == [JID]
        assert JID not in stub._exhausted_chats

    def test_background_walk_still_respects_exhaustion(self):
        stub = _make()
        stub._exhausted_chats.add(JID)
        assert stub.fetch_older_messages(JID, ANCHOR, store_only=True) == []
        assert stub.asks == []


class TestAFailedAskIsNotDurableEvidenceEither:
    def test_a_request_that_never_went_out_stays_requeryable(self):
        """request_older_messages() returning False covers a 60 s timeout and a
        dropped connection. Its own docstring calls this out as writing off
        exactly the chats that still have history coming."""
        stub = _make(ask_succeeds=False)
        assert stub.fetch_older_messages(JID, ANCHOR) is None
        assert JID not in stub._exhausted_chats
        assert "exhausted_chats" not in stub.db.metadata

    def test_recent_sync_refusal_does_not_start_the_grace_clock(self):
        stub = _make(ask_succeeds=None)
        assert stub.fetch_older_messages(JID, ANCHOR) is None
        assert JID not in stub._older_requested_chats
        assert stub.db.metadata["older_history_requested"] == {}
        assert JID not in stub._exhausted_chats


class TestTheUserCanUndoIt:
    """F5 keeps system_metadata so local cleared/archived/muted state survives.
    These two entries must not ride along on that — they are a cached
    conclusion about WhatsApp Web, and F5 is the only escape from one that was
    reached wrongly."""

    def test_the_resync_forgets_the_exhausted_set(self):
        stub = _make()
        stub._exhausted_chats.add(JID)
        stub._older_requested_chats[JID] = 1.0
        stub._forget_history_exhaustion()
        assert stub._exhausted_chats == set()
        assert stub._older_requested_chats == {}

    def test_it_clears_the_persisted_copies_too(self):
        """Only clearing memory would let prepare_sync() bring them straight
        back on the next launch."""
        stub = _make()
        stub._exhausted_chats.add(JID)
        stub._older_requested_chats[JID] = 1.0
        stub._forget_history_exhaustion()
        assert stub.db.metadata["exhausted_chats"] == []
        assert stub.db.metadata["older_history_requested"] == {}

    def test_the_chat_is_queryable_again_afterwards(self):
        stub = _make()
        stub._exhausted_chats.add(JID)
        stub._forget_history_exhaustion()
        stub.fetch_older_messages(JID, ANCHOR)
        assert stub.asks == [JID], "the early return must be gone"

    def test_the_resync_worker_actually_calls_it(self):
        """The helper working is not the same as it being wired up, and
        _resync_all_worker() is all wx teardown — it cannot be bound to a stub
        the way the rest of this file is. Two commits on this branch already
        shipped a defence that no test reached; read the call out of the source
        instead of leaving the wiring uncovered.
        """
        import ast
        import inspect
        import textwrap

        # getsource() on a method keeps its class indentation, which ast
        # rejects on its own.
        tree = ast.parse(textwrap.dedent(
            inspect.getsource(MainWindow._resync_all_worker)))
        called = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert "_forget_history_exhaustion" in called, (
            "F5 must forget the exhausted set, or a wrongly walked-out chat has "
            "no escape at all")
        assert "clear_local_data" in called, "premise check: this is still the F5 worker"


class TestTheGraceIsNotSoLongItStopsPersistingAtAll:
    def test_a_genuinely_finished_chat_still_becomes_durable(self):
        """The point of persisting at all: an account already walked to its
        beginning must not be re-walked on every relaunch."""
        stub = _make()
        stub.fetch_older_messages(JID, ANCHOR)
        # …relaunch: the map came back from metadata, the clock moved on.
        reborn = _make()
        reborn._older_requested_chats = {
            j: t - (GRACE + 60) for j, t in stub._older_requested_chats.items()}
        reborn.fetch_older_messages(JID, ANCHOR)
        assert reborn.db.metadata["exhausted_chats"] == [JID]
