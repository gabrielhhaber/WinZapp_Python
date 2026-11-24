"""The request for the conversations a pairing's transfer left out
(recent_conversations.py)."""
import logging
from types import SimpleNamespace as N

from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message, PeerDataOperationRequestType
from neonize.proto.waHistorySync.WAWebProtobufsHistorySync_pb2 import Conversation, HistorySync

import history_progress
import omitted_history
import recent_conversations as rc
import server
from store import Store
from test_history_block import conversation


def capped(chats=rc.BOOTSTRAP_CAP):
    t = rc.Tracker()
    t.bootstrap(chats)
    return t


def test_the_request_is_the_whatsapp_one_for_recent_conversations():
    message = rc.request()
    assert message.protocolMessage.type == message.protocolMessage.PEER_DATA_OPERATION_REQUEST_MESSAGE
    operation = message.protocolMessage.peerDataOperationRequestMessage
    assert operation.peerDataOperationRequestType == \
        PeerDataOperationRequestType.RECENT_CONVERSATIONS_ON_DEMAND_REQUEST == rc.PEER_OPERATION
    assert operation.recentConversationsOnDemandRequest.conversationCount == rc.CONVERSATIONS
    assert operation.recentConversationsOnDemandRequest.messagesPerConversation == \
        rc.MESSAGES_PER_CONVERSATION


def test_only_a_full_bootstrap_with_no_omitted_batch_wants_the_rest():
    assert capped().wanted()
    assert not capped(rc.BOOTSTRAP_CAP - 1).wanted()
    assert not rc.Tracker().wanted()            # no bootstrap seen: a restart, not a pairing
    t = capped()
    t.omitted()
    assert not t.wanted()
    t = capped()
    t.answered()
    assert not t.wanted()


def test_a_new_pairing_starts_over():
    t = capped()
    t.answered()
    t.omitted()
    t.bootstrap(rc.BOOTSTRAP_CAP)
    assert t.wanted() and t.attempts == 0 and t.due_since is None


def test_it_asks_only_when_the_transfer_is_done_and_not_more_often_than_it_retries():
    t = capped()
    assert not t.next_request(0, transfer_done=False)
    assert t.next_request(10, transfer_done=True)
    assert not t.next_request(10 + rc.RETRY_EVERY - 1, transfer_done=True)
    assert t.next_request(10 + rc.RETRY_EVERY, transfer_done=True)
    assert t.next_request(10 + 2 * rc.RETRY_EVERY, transfer_done=True)
    assert not t.next_request(10 + 3 * rc.RETRY_EVERY, transfer_done=True)    # MAX_ATTEMPTS
    assert t.attempts == rc.MAX_ATTEMPTS


def test_an_answer_stops_the_asking():
    t = capped()
    assert t.next_request(0, transfer_done=True)
    t.answered()
    assert not t.next_request(1000, transfer_done=True)


def test_the_history_is_held_until_answered_or_given_up():
    t = capped()
    assert t.pending(0)                         # before the transfer is done
    t.next_request(100, transfer_done=True)
    assert t.pending(100 + rc.GIVE_UP - 1)
    assert not t.pending(100 + rc.GIVE_UP)      # the phone never answered
    t = capped()
    t.next_request(0, transfer_done=True)
    t.answered()
    assert not t.pending(1)
    assert not rc.Tracker().pending(0)          # nothing held for an ordinary account


def test_progress_state_obeys_the_hold():
    p = history_progress.Progress(0)
    p.started(0)
    p.finished(1, 100)
    assert p.state(2, 5)["initialSyncComplete"] is True
    state = p.state(2, 5, hold=True)
    assert state["initialSyncComplete"] is False and state["recentCompleted"] is False


def test_note_block_counts_the_chats_of_the_bootstrap_and_sees_the_omitted_batch():
    s = N()
    conversations = [Conversation(ID=f"55{i}@s.whatsapp.net") for i in range(rc.BOOTSTRAP_CAP)]
    conversations += [Conversation(ID="status@broadcast"), Conversation(ID="1@newsletter"),
                      Conversation(ID="2@g.us", isParentGroup=True)]
    rc.note_block(s, HistorySync(syncType=HistorySync.INITIAL_BOOTSTRAP,
                                 conversations=conversations))
    assert rc.tracker_of(s).bootstrap_chats == rc.BOOTSTRAP_CAP
    assert rc.tracker_of(s).wanted()
    rc.note_block(s, HistorySync(syncType=HistorySync.RECENT), omitted_batch=True)
    assert not rc.tracker_of(s).wanted()


def test_note_block_ignores_the_other_blocks():
    s = N()
    rc.note_block(s, HistorySync(syncType=HistorySync.RECENT,
                                 conversations=[Conversation(ID="1@s.whatsapp.net")]))
    assert rc.tracker_of(s).bootstrap_chats is None


def test_is_answer_reads_the_sync_type():
    assert rc.is_answer(HistorySync(syncType=9))
    assert not rc.is_answer(HistorySync(syncType=HistorySync.RECENT))
    assert not rc.is_answer(N())


def test_previews_are_the_newest_message_of_each_new_unarchived_chat():
    chats = [("a@s.whatsapp.net", {}), ("b@s.whatsapp.net", {"archived": 1}),
             ("c@s.whatsapp.net", {"archived": 0}), ("known@s.whatsapp.net", {})]
    rows = [("a@s.whatsapp.net", {"id": "1", "timestamp": 5}, None),
            ("a@s.whatsapp.net", {"id": "2", "timestamp": 9}, None),
            ("a@s.whatsapp.net", {"id": "3", "timestamp": 7}, None),
            ("b@s.whatsapp.net", {"id": "4", "timestamp": 1}, None),
            ("c@s.whatsapp.net", {"id": "5", "timestamp": 2}, None),
            ("known@s.whatsapp.net", {"id": "6", "timestamp": 3}, None)]
    previews = rc.new_chat_previews(chats, rows, known={"known@s.whatsapp.net"})
    assert sorted(w["id"] for w in previews) == ["2", "5"]


class FakeClient:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send_peer_message(self, message):
        if self.fail:
            raise RuntimeError("offline")
        self.sent.append(message)


def finished_transfer_session(client, chats=rc.BOOTSTRAP_CAP):
    s = N(client=client, db=N(counts=lambda: {"chat": chats}), status="CONNECTED")
    rc.tracker_of(s).bootstrap(chats)
    progress = history_progress.progress_of(s, 0)
    progress.started(0)
    progress.finished(1, 100)
    return s


def test_ask_once_sends_when_the_transfer_is_done(caplog):
    s = finished_transfer_session(FakeClient())
    with caplog.at_level(logging.INFO, logger="bridge"):
        assert rc.ask_once(s, 10) is True
    assert len(s.client.sent) == 1
    assert "asked the phone for the rest" in caplog.text
    assert rc.ask_once(s, 11) is False          # a retry waits


def test_ask_once_waits_for_a_transfer_still_going():
    s = finished_transfer_session(FakeClient())
    history_progress.progress_of(s, 0).finished(2, 40)
    assert rc.ask_once(s, 3) is False
    assert s.client.sent == []


def test_ask_once_does_nothing_for_an_account_that_was_not_cut():
    s = finished_transfer_session(FakeClient(), chats=rc.BOOTSTRAP_CAP - 1)
    assert rc.ask_once(s, 10) is False


def test_ask_once_survives_a_refused_send_and_a_missing_client():
    s = finished_transfer_session(FakeClient(fail=True))
    assert rc.ask_once(s, 10) is False
    s.client = None
    assert rc.ask_once(s, 10 + rc.RETRY_EVERY) is False


def test_watch_runs_while_connected_and_only_once():
    s = finished_transfer_session(FakeClient())
    clock = iter([10, 11])

    def sleep(_seconds):
        if s.client.sent:
            s.status = "DISCONNECTED"

    rc.watch(s, sleep=sleep, clock=lambda: next(clock))
    assert len(s.client.sent) == 1
    assert s._recent_conversations_watch is False
    s._recent_conversations_watch = True
    s.status = "CONNECTED"
    rc.watch(s, sleep=lambda _s: (_ for _ in ()).throw(AssertionError("second loop")))


def stored_session(tmp_path):
    db = Store(str(tmp_path / "recent.db"))
    emitted = []
    s = N(db=db, name="acct", emit=lambda *a: emitted.append(a),
          _history_progress=None, _history_resume=None)
    s._store_history_block = lambda d: server.Session._store_history_block(s, d)
    return s, db, emitted


def test_the_answer_stores_the_chats_and_tells_the_client_of_the_new_ones(tmp_path, caplog):
    s, db, emitted = stored_session(tmp_path)
    db.chats_in_batch([("old@s.whatsapp.net", {"ts": 5})])
    rc.tracker_of(s).bootstrap(rc.BOOTSTRAP_CAP)
    answer = N(syncType=9, progress=0, chunkOrder=1, pushnames=[], phoneNumberToLidMappings=[],
               conversations=[conversation("old@s.whatsapp.net", 1),
                              conversation("new@s.whatsapp.net", 3)])
    with caplog.at_level(logging.INFO, logger="bridge"):
        server.Session._store_history_block(s, answer)
    assert {c["jid"] for c in db.chats()} == {"old@s.whatsapp.net", "new@s.whatsapp.net"}
    assert len(db.messages("new@s.whatsapp.net")) == 3
    assert "recent conversations answer: 2 chats (1 new), 4 messages" in caplog.text
    assert not rc.tracker_of(s).wanted()
    assert [e[0] for e in emitted] == ["received-message"]
    assert emitted[0][1]["response"]["isMdHistoryMsg"] is True
    assert emitted[0][1]["session"] == "acct"


def test_a_block_of_the_answer_does_not_move_the_transfer_progress(tmp_path):
    s, _db, _emitted = stored_session(tmp_path)
    block = HistorySync(syncType=9, chunkOrder=1, progress=3)
    server.Session._store_history(s, block)
    assert history_progress.progress_of(s, 0).percent is None


def test_an_omitted_batch_cancels_the_request(tmp_path):
    s, _db, _emitted = stored_session(tmp_path)
    rc.tracker_of(s).bootstrap(rc.BOOTSTRAP_CAP)
    batch = omitted_history.encode_omitted_batch(
        [Conversation(ID="111@lid", name="Older", conversationTimestamp=1700000000)])
    server.Session._store_history(s, batch)
    assert not rc.tracker_of(s).wanted()
