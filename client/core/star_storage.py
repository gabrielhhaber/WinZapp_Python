"""Star metadata preservation at the encrypted SQLite write boundary."""

from core.message_stars import merge_star_state


async def preserve_stars(manager, conn, jid, messages):
    # One bounded query per chunk, not one decrypting SELECT per message.
    ids = [(m.get("key") or {}).get("id") for m in messages]
    stored = {}
    variants = manager._jid_variants(jid)
    jid_marks = ",".join("?" for _ in variants)
    for start in range(0, len(ids), 400):
        chunk = [i for i in ids[start:start + 400] if i]
        if not chunk:
            continue
        marks = ",".join("?" for _ in chunk)
        cursor = await conn.execute(
            f"SELECT message_id, message_json FROM messages WHERE remote_jid IN ({jid_marks}) "
            f"AND message_id IN ({marks})", (*variants, *chunk))
        for row in await cursor.fetchall():
            stored[row["message_id"]] = manager._decrypt_json(row["message_json"]) or {}
    result = []
    for message in messages:
        mid = (message.get("key") or {}).get("id")
        merged = merge_star_state(message, stored.get(mid, {}))
        result.append(merged)
        if mid:
            stored[mid] = merged  # duplicate ids within this batch obey the same rule
    return result


async def update_star_state(manager, jid, message_id, state):
    """Update flags atomically, retaining edits; never resurrect a deleted row."""
    async with manager._write_lock:
        conn = await manager._ensure_conn()
        variants = manager._jid_variants(jid)
        marks = ",".join("?" for _ in variants)
        cursor = await conn.execute(
            f"SELECT remote_jid, message_json FROM messages WHERE message_id=? "
            f"AND remote_jid IN ({marks})", (message_id, *variants))
        for row in await cursor.fetchall():
            stored = manager._decrypt_json(row["message_json"]) or {}
            merged = merge_star_state({**stored, **state}, stored)
            await conn.execute(
                "UPDATE messages SET message_json=? WHERE remote_jid=? AND message_id=?",
                (manager._encrypt_json(merged), row["remote_jid"], message_id))
        await conn.commit()
