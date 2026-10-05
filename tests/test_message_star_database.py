"""Encrypted rows retain local stars across refreshes and concurrent edits."""
from core.message_stars import confirmed_star_state, remote_star_state
JID = "synthetic@s.whatsapp.net"


def message(mid="M", **extra):
    return {"key": {"id": mid, "fromMe": False}, "messageTimestamp": 123, "messageType": "conversation", "message": {"conversation": "synthetic"}, **extra}


async def test_single_and_batch_refresh_preserve_old_local_stars(in_memory_db):
    db = in_memory_db
    await db.insert_message(JID, message(starred=True))
    await db.insert_message(JID, message(**remote_star_state({"star": False}, 10)))
    await db.insert_messages_batch(JID, [message(**remote_star_state({"star": False}, 20))])
    saved = await db.get_message_by_id(JID, "M")
    assert saved["starred"] is True and saved["_star_local"] is True and saved["_star_remote"] is False


async def test_confirmation_changes_flags_without_overwriting_edited_body(in_memory_db):
    db = in_memory_db
    await db.insert_message(JID, message(starred=True))
    await db.insert_message(JID, message(message={"conversation": "edited"}, _edited=True))
    await db.update_message_star_state(JID, "M", confirmed_star_state(False))
    saved = await db.get_message_by_id(JID, "M")
    assert saved["message"]["conversation"] == "edited" and saved["_edited"] is True
    assert saved["starred"] is False and saved["_star_local"] is False
    await db.insert_messages_batch(JID, [message(**remote_star_state({"star": True}, 1))])
    assert (await db.get_message_by_id(JID, "M"))["starred"] is False


async def test_confirmation_never_inserts_a_deleted_or_missing_message(in_memory_db):
    await in_memory_db.update_message_star_state(JID, "absent", confirmed_star_state(True))
    assert await in_memory_db.get_message_count(JID) == 0


async def test_unloaded_legacy_row_merges_without_changing_incoming_text(in_memory_db):
    db = in_memory_db
    await db.insert_message(JID.replace("@s.whatsapp.net", "@c.us"), message(starred=True))
    incoming = message(**remote_star_state({"star": False}, 10), message={"conversation": "fetched"})
    merged = await db.merge_message_star_states(JID, [incoming])
    assert merged[0]["starred"] is True and merged[0]["message"]["conversation"] == "fetched"
    assert incoming["starred"] is False


async def test_chunked_batch_and_other_chat_isolation(in_memory_db):
    db = in_memory_db
    await db.insert_messages_batch(JID, [message(str(i), starred=True) for i in range(405)])
    await db.insert_message("other@s.whatsapp.net", message("0", starred=False))
    await db.insert_messages_batch(JID, [message(str(i), **remote_star_state({"star": False}, 10)) for i in range(405)])
    assert all(m["starred"] for m in await db.get_messages(JID, limit=500))
    assert not (await db.get_message_by_id("other@s.whatsapp.net", "0"))["starred"]


async def test_stale_duplicate_in_same_batch_does_not_undo_newer_star(in_memory_db):
    await in_memory_db.insert_messages_batch(JID, [
        message(**remote_star_state({"star": True}, 20)),
        message(**remote_star_state({"star": False}, 10)),
    ])
    assert (await in_memory_db.get_message_by_id(JID, "M"))["starred"] is True
