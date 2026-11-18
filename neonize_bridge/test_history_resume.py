"""Where the phone's history transfer stands and the request that picks it up
(history_resume.py)."""
import logging
from types import SimpleNamespace as N

from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message, ProtocolMessage
from neonize.proto.waHistorySync.WAWebProtobufsHistorySync_pb2 import HistorySync

import history_resume as hr

FULL = HistorySync.FULL
RECENT = HistorySync.RECENT


def stalled_tracker(percent=40, chunk=23, at=100):
    t = hr.Tracker()
    t.block(FULL, chunk, percent, at)
    return t


def test_no_numbered_block_yet_reports_nothing():
    t = hr.Tracker()
    assert t.status(0) is None
    assert t.next_request(10_000) is None


def test_other_history_types_leave_the_transfer_as_it_was():
    t = hr.Tracker()
    t.block(HistorySync.PUSH_NAME, 0, 0, 5)
    t.block(HistorySync.INITIAL_BOOTSTRAP, 0, 100, 6)
    assert t.status(7) is None
    t.block(FULL, 3, 12, 8)
    t.block(HistorySync.NON_BLOCKING_DATA, 0, 0, 9)
    assert t.status(10)["progress"] == 12


def test_the_reported_stall_is_seen_after_a_minute_without_blocks():
    """22:20:30 the 40% block, then nothing: stalled one minute later."""
    t = stalled_tracker(at=100)
    assert t.status(100 + hr.STALL - 1) == {
        "type": "FULL", "progress": 40, "stalled": False,
        "resumeAttempts": 0, "resumeRefused": False}
    assert t.status(100 + hr.STALL)["stalled"] is True


def test_a_finished_transfer_never_stalls():
    t = hr.Tracker()
    t.block(RECENT, 9, 100, 0)
    assert t.status(10_000) == {"type": "RECENT", "progress": 100, "stalled": False,
                                "resumeAttempts": 0, "resumeRefused": False}
    assert t.next_request(10_000) is None


def test_stalled_asks_for_the_block_after_the_last_and_spaces_the_requests():
    t = stalled_tracker(chunk=23, at=0)
    assert t.next_request(hr.STALL - 1) is None
    assert t.next_request(hr.STALL) == (FULL, 24)
    assert t.next_request(hr.STALL + hr.RETRY_EVERY - 1) is None
    assert t.next_request(hr.STALL + hr.RETRY_EVERY) == (FULL, 24)
    assert t.status(hr.STALL + hr.RETRY_EVERY)["resumeAttempts"] == 2


def test_requests_stop_after_max_attempts():
    t = stalled_tracker(at=0)
    now = hr.STALL
    for _ in range(hr.MAX_ATTEMPTS):
        assert t.next_request(now) is not None
        now += hr.RETRY_EVERY
    assert t.next_request(now) is None


def test_a_new_block_resets_the_attempts():
    t = stalled_tracker(chunk=23, at=0)
    assert t.next_request(hr.STALL) == (FULL, 24)
    t.block(FULL, 24, 42, hr.STALL + 5)
    assert t.status(hr.STALL + 6) == {"type": "FULL", "progress": 42, "stalled": False,
                                      "resumeAttempts": 0, "resumeRefused": False}


def test_a_final_answer_stops_the_asking_until_the_next_block():
    t = stalled_tracker(at=0)
    assert t.next_request(hr.STALL) is not None
    t.answered(5, None)                       # CHUNK_EXHAUSTED
    assert t.next_request(hr.STALL + hr.RETRY_EVERY) is None
    assert t.status(hr.STALL)["resumeRefused"] is True
    t.block(FULL, 24, 45, 1000)
    assert t.status(1001)["resumeRefused"] is False


def test_can_recover_false_stops_and_a_timeout_does_not():
    t = stalled_tracker(at=0)
    t.answered(3, None)                       # TIMEOUT: worth asking again
    assert t.next_request(hr.STALL) is not None
    t.answered(None, False)
    assert t.next_request(hr.STALL + hr.RETRY_EVERY) is None


def test_request_is_a_chunk_retry_peer_message():
    message = hr.request(FULL, 24)
    protocol = message.protocolMessage
    assert protocol.type == ProtocolMessage.PEER_DATA_OPERATION_REQUEST_MESSAGE
    operation = protocol.peerDataOperationRequestMessage
    assert operation.peerDataOperationRequestType == hr.CHUNK_RETRY
    retry = operation.historySyncChunkRetryRequest
    assert (retry.syncType, retry.chunkOrder, retry.regenerateChunk) == (FULL, 24, True)


def answer(code=None, can_recover=None, chunk=None, kind=hr.CHUNK_RETRY):
    message = Message()
    response = message.protocolMessage.peerDataOperationRequestResponseMessage
    response.peerDataOperationRequestType = kind
    retry = response.peerDataOperationResult.add().historySyncChunkRetryResponse
    if code is not None:
        retry.responseCode = code
    if can_recover is not None:
        retry.canRecover = can_recover
    if chunk is not None:
        retry.chunkOrder = chunk
    return message


def test_answer_of_reads_the_phone_answer():
    assert hr.answer_of(answer(4, False, 24)) == (4, False, 24)
    assert hr.answer_of(answer()) == (None, None, None)


def test_answer_of_ignores_everything_else():
    assert hr.answer_of(None) is None
    assert hr.answer_of(Message(conversation="hi")) is None
    assert hr.answer_of(answer(kind=8)) is None     # an app state copy


def test_received_logs_and_swallows_only_the_own_answer(caplog):
    s = N()
    hr.tracker_of(s).block(FULL, 23, 40, 0)
    assert hr.received(s, answer(5, None, 24), from_me=False) is False
    with caplog.at_level(logging.INFO, logger="bridge"):
        assert hr.received(s, answer(5, None, 24), from_me=True) is True
    assert "CHUNK_EXHAUSTED" in caplog.text
    assert hr.tracker_of(s).status(1)["resumeRefused"] is True
    assert hr.received(s, Message(conversation="hi"), from_me=True) is False


def test_note_block_and_status_of_read_the_history_block():
    s = N()
    assert hr.status_of(s, 0) == {"historyTransfer": None}
    hr.note_block(s, HistorySync(syncType=FULL, chunkOrder=7, progress=33), now=10)
    assert hr.status_of(s, 11)["historyTransfer"]["progress"] == 33


class FakeClient:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def send_peer_message(self, message):
        if self.fail:
            raise RuntimeError("offline")
        self.sent.append(message)


def test_ask_once_sends_when_stalled():
    s = N(client=FakeClient())
    hr.tracker_of(s).block(FULL, 23, 40, 0)
    assert hr.ask_once(s, hr.STALL - 1) is False
    assert hr.ask_once(s, hr.STALL) is True
    assert s.client.sent[0].protocolMessage.peerDataOperationRequestMessage \
        .historySyncChunkRetryRequest.chunkOrder == 24


def test_ask_once_survives_a_refused_send_and_a_missing_client():
    s = N(client=FakeClient(fail=True))
    hr.tracker_of(s).block(FULL, 23, 40, 0)
    assert hr.ask_once(s, hr.STALL) is False
    s.client = None
    assert hr.ask_once(s, hr.STALL + hr.RETRY_EVERY) is False


def test_watch_runs_while_connected_and_only_once():
    s = N(status="CONNECTED", client=FakeClient())
    hr.tracker_of(s).block(FULL, 23, 40, 0)
    clock = iter([hr.STALL, hr.STALL + 1])

    def sleep(_seconds):
        if s.client.sent:
            s.status = "DISCONNECTED"

    hr.watch(s, sleep=sleep, clock=lambda: next(clock))
    assert len(s.client.sent) == 1
    assert s._history_resume_watch is False
    s._history_resume_watch = True             # a loop already running
    s.status = "CONNECTED"
    hr.watch(s, sleep=lambda _s: (_ for _ in ()).throw(AssertionError("second loop")))


def test_type_name_names_every_block_and_survives_a_new_one():
    assert hr.type_name(HistorySync.INITIAL_BOOTSTRAP) == "INITIAL_BOOTSTRAP"
    assert hr.type_name(FULL) == "FULL"
    assert hr.type_name(HistorySync.NON_BLOCKING_DATA) == "NON_BLOCKING_DATA"
    assert hr.type_name(42) == "42"
