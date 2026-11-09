"""Star metadata preservation at the encrypted SQLite write boundary, and
the starred messages of every chat (the starred messages window, issue #708).

message_json is encrypted, so whether a message is starred is also kept in
clear in messages.starred, written with the row (Database._build_message_values)
and by update_star_state(). A database from before the column fills it once,
the first time the window asks (fill_starred_column)."""
import logging

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
                "UPDATE messages SET message_json=?, starred=? WHERE remote_jid=? AND message_id=?",
                (manager._encrypt_json(merged), 1 if merged.get("starred") else 0,
                 row["remote_jid"], message_id))
        await conn.commit()

log = logging.getLogger(__name__)

#: system_metadata key set once messages.starred holds every row's star
FILLED_KEY = "messages_starred_column_filled"
#: rows decrypted per step of that one-time fill
FILL_CHUNK = 500


async def upgrade_starred_column(conn):
    """Add messages.starred (and its index of starred rows only) to a
    database created before it; existing rows start at 0 until filled."""
    cursor = await conn.execute("PRAGMA table_info(messages)")
    columns = {row["name"] for row in await cursor.fetchall()}
    if "starred" not in columns:
        log.info("[connect] Adding messages.starred.")
        await conn.execute("ALTER TABLE messages ADD COLUMN starred INTEGER DEFAULT 0")
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_msgs_starred ON messages(timestamp DESC) WHERE starred=1")


async def fill_starred_column(manager):
    """Once per database: decrypt every row, a chunk at a time, and set
    messages.starred from the star inside it."""
    if await manager.get_metadata(FILLED_KEY) == "1":
        return
    last, marked = 0, 0
    while True:
        async with manager._write_lock:
            conn = await manager._ensure_conn()
            cursor = await conn.execute(
                "SELECT rowid, message_json FROM messages WHERE rowid > ? ORDER BY rowid LIMIT ?",
                (last, FILL_CHUNK))
            rows = await cursor.fetchall()
            if not rows:
                break
            starred = [row["rowid"] for row in rows
                       if (manager._decrypt_json(row["message_json"]) or {}).get("starred")]
            if starred:
                marks = ",".join("?" for _ in starred)
                await conn.execute(f"UPDATE messages SET starred=1 WHERE rowid IN ({marks})", starred)
                await conn.commit()
            marked += len(starred)
            last = rows[-1]["rowid"]
    await manager.set_metadata(FILLED_KEY, "1")
    log.info("[stars] messages.starred filled: %d starred message(s)", marked)


async def starred_messages(manager, limit=2000):
    """[(remote_jid, msg)] of every starred message, newest first. A row
    whose decrypted star says otherwise is left out."""
    await fill_starred_column(manager)
    conn = await manager._ensure_conn()
    cursor = await conn.execute(
        "SELECT remote_jid, message_json FROM messages WHERE starred=1 "
        "ORDER BY timestamp DESC LIMIT ?", (int(limit),))
    result = []
    for row in await cursor.fetchall():
        msg = manager._decrypt_json(row["message_json"])
        if msg and msg.get("starred"):
            result.append((row["remote_jid"], msg))
    return result
