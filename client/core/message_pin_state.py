"""Pin observations modify flags, never a stale copy of message content."""
import time


def apply_pin_state(record, pinned, timestamp, duration):
    if record.get("_pin_changed_at", 0) > timestamp:
        return False
    record["pinInChat"] = bool(pinned and (not duration or timestamp / 1000 + duration > time.time()))
    record["_pin_changed_at"] = timestamp
    record["_pin_duration"] = duration
    return True


async def update_pin_state(manager, jid, mid, pinned, timestamp, duration):
    async with manager._write_lock:
        conn = await manager._ensure_conn()
        for alias in manager._jid_variants(jid):
            row = await manager._stored_message(conn, alias, mid)
            if row is None or not apply_pin_state(row, pinned, timestamp, duration):
                continue
            await conn.execute("UPDATE messages SET message_json=? WHERE remote_jid=? AND message_id=?",
                               (manager._encrypt_json(row), alias, mid))
        await conn.commit()
