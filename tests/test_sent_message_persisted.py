"""A delivered own message is stored when it is marked sent.

Neonize sends no echo, and the delayed ack write only merges into a stored row,
so a sent message that nothing had stored disappeared on the next reload
(reported 2026-11-26). Marking it sent queues the record under its real id.
"""
from core.sent_message_store import persist_sent_message


class _DB:
    def __init__(self, fail=False):
        self.queued, self.fail = [], fail

    def queue_message(self, jid, record):
        if self.fail:
            raise RuntimeError("closed")
        self.queued.append((jid, record))


def _record(mid="3EB0REAL"):
    return {"key": {"id": mid, "remoteJid": "chat@s.whatsapp.net", "fromMe": True},
            "message": {"conversation": "hi"}}


def test_sent_message_is_queued_under_its_real_id():
    db, record = _DB(), _record()
    assert persist_sent_message(db, record) is True
    assert db.queued == [("chat@s.whatsapp.net", record)]


def test_record_without_id_or_chat_is_not_stored():
    db = _DB()
    assert persist_sent_message(db, _record("")) is False
    assert persist_sent_message(db, {"key": {"id": "x"}}) is False
    assert persist_sent_message(None, _record()) is False
    assert db.queued == []


def test_a_failing_store_does_not_raise():
    assert persist_sent_message(_DB(fail=True), _record()) is False


async def test_queued_sent_message_survives_its_acks(in_memory_db):
    """End to end on the real queue's storage: stored row, then ack merges."""
    from core.event_write_storage import apply_status_batch
    jid = "chat@s.whatsapp.net"
    record = _record()
    record.update(messageTimestamp=1, messageType="conversation", status=2)
    await in_memory_db.insert_messages_batch(jid, [record])
    await apply_status_batch(in_memory_db, [(jid, "3EB0REAL", {"status": 3, "ts": 5})])
    saved = await in_memory_db.get_message_by_id(jid, "3EB0REAL")
    assert saved["message"]["conversation"] == "hi" and saved["MessageUpdate"][-1]["status"] == 3
