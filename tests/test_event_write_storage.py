"""A delayed receipt or remote contact observation cannot undo newer user work."""
from core.event_write_storage import apply_status_batch, apply_contact_batch
import pytest

JID = "synthetic@s.whatsapp.net"


def message(kind="conversation", body="original", status=2):
    return {"key": {"id": "m", "remoteJid": JID, "fromMe": True},
            "messageType": kind, "message": {"conversation": body},
            "messageTimestamp": 1, "status": status}


@pytest.mark.parametrize("kind,body", [("protocolMessage", "revoked"),
                                       ("conversation", "new edit")])
async def test_late_ack_updates_only_status_on_the_current_stored_copy(in_memory_db, kind, body):
    db = in_memory_db
    await db.insert_message(JID, message())
    update = {"status": 3, "ts": 10}
    await db.insert_message(JID, message(kind, body))
    await apply_status_batch(db, [(JID, "m", update)])
    saved = await db.get_message_by_id(JID, "m")
    assert saved["messageType"] == kind
    assert saved["message"]["conversation"] == body
    assert saved["MessageUpdate"][-1] == update


async def test_stale_positive_ack_cannot_regress_a_newer_stored_status(in_memory_db):
    db = in_memory_db
    await db.insert_message(JID, message(status=4))
    await apply_status_batch(db, [(JID, "m", {"status": 3, "ts": 10})])
    assert (await db.get_message_by_id(JID, "m"))["status"] == 4


async def test_ack_never_recreates_a_deleted_message(in_memory_db):
    await apply_status_batch(in_memory_db, [(JID, "m", {"status": 3, "ts": 10})])
    assert await in_memory_db.get_message_by_id(JID, "m") is None


@pytest.mark.parametrize("status,expected", [("READ", 4), ("DELIVERY_ACK", 3),
                                            ("SERVER_ACK", 2), ("-2", -2)])
async def test_symbolic_and_failure_statuses_do_not_poison_other_items(in_memory_db, status, expected):
    db = in_memory_db
    await db.insert_message(JID, message())
    other = message()
    other["key"]["id"] = "other"
    await db.insert_message(JID, other)
    await apply_status_batch(db, [(JID, "m", {"status": status, "ts": 10}),
                                 (JID, "bad", {"status": "malformed"}),
                                 (JID, "other", {"status": 5, "ts": 11})])
    conn = await db._ensure_conn()
    rows = await (await conn.execute("SELECT message_id, status FROM messages")).fetchall()
    assert {row["message_id"]: row["status"] for row in rows} == {"m": expected, "other": 5}


async def test_existing_symbolic_read_status_cannot_be_downgraded(in_memory_db):
    db = in_memory_db
    original = message()
    original["MessageUpdate"] = [{"status": "READ", "ts": 8}]
    await db.insert_message(JID, original)
    await apply_status_batch(db, [(JID, "m", {"status": "DELIVERY_ACK", "ts": 10})])
    assert (await db.get_message_by_id(JID, "m"))["MessageUpdate"] == original["MessageUpdate"]


async def test_remote_contact_cannot_overwrite_a_newer_local_save(in_memory_db):
    db = in_memory_db
    before = {"remoteJid": JID, "name": "old", "pushName": "profile"}
    after = {**before, "name": "remote rename"}
    await db.upsert_contact(JID, before)
    await db.upsert_contact(JID, {**before, "name": "local name", "isSaved": True})
    await apply_contact_batch(db, [(JID, before, after)])
    saved = (await db.get_contacts())[JID]
    assert saved["name"] == "local name" and saved["isSaved"] is True


async def test_ordered_remote_observations_apply_in_one_batch(in_memory_db):
    first = {"remoteJid": JID, "name": "first"}
    second = {**first, "name": "second"}
    await apply_contact_batch(in_memory_db, [(JID, None, first), (JID, first, second)])
    assert (await in_memory_db.get_contacts())[JID]["name"] == "second"


async def test_contact_deleted_after_observation_is_not_recreated(in_memory_db):
    before = {"remoteJid": JID, "name": "old"}
    await apply_contact_batch(in_memory_db, [(JID, before, {**before, "name": "remote"})])
    assert await in_memory_db.get_contacts() == {}
