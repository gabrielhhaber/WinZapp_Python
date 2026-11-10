"""The starred messages of every chat come from one indexed query (#708).

message_json is encrypted: messages.starred keeps the star in clear, written
with every row and on every star change, and filled once for an older
database."""
from core import star_storage
from core.message_stars import confirmed_star_state

A, B = "a@s.whatsapp.net", "b@g.us"


def message(mid, ts, **extra):
    return {"key": {"id": mid, "fromMe": False}, "messageTimestamp": ts,
            "messageType": "conversation", "message": {"conversation": mid}, **extra}


async def test_starred_messages_of_every_chat_newest_first(in_memory_db):
    db = in_memory_db
    await db.insert_message(A, message("A1", 100, starred=True))
    await db.insert_message(A, message("A2", 300))
    await db.insert_messages_batch(B, [message("B1", 200, starred=True), message("B2", 400)])
    got = await db.get_starred_messages()
    assert [(jid, m["key"]["id"]) for jid, m in got] == [(B, "B1"), (A, "A1")]


async def test_a_star_change_moves_the_message_in_or_out(in_memory_db):
    db = in_memory_db
    await db.insert_message(A, message("A1", 100))
    await db.update_message_star_state(A, "A1", confirmed_star_state(True))
    assert [m["key"]["id"] for _jid, m in await db.get_starred_messages()] == ["A1"]
    await db.update_message_star_state(A, "A1", confirmed_star_state(False))
    assert await db.get_starred_messages() == []


async def test_an_older_database_is_filled_once(in_memory_db):
    db = in_memory_db
    await db.insert_message(A, message("A1", 100, starred=True))
    await db.insert_message(A, message("A2", 200))
    conn = await db._ensure_conn()
    # as a database written before the column: every row 0, never filled
    await conn.execute("UPDATE messages SET starred=0")
    await conn.execute("DELETE FROM system_metadata WHERE key=?", (star_storage.FILLED_KEY,))
    await conn.commit()
    assert [m["key"]["id"] for _jid, m in await db.get_starred_messages()] == ["A1"]
    assert int(await db.get_metadata(star_storage.FILLED_KEY)) >= 1


async def test_rows_an_older_version_rewrote_are_filled_again(in_memory_db):
    """Reinstalled an older WinZapp, then upgraded again: the rows it rewrote
    have starred=0 and a new rowid. The fill reads them on the next opening
    (the first version filled once, and they never came back)."""
    db = in_memory_db
    await db.insert_message(A, message("A1", 100, starred=True))
    await db.insert_message(A, message("A2", 200))
    assert len(await db.get_starred_messages()) == 1
    conn = await db._ensure_conn()
    # the older version: INSERT OR REPLACE with no starred column
    cursor = await conn.execute("SELECT * FROM messages WHERE message_id='A1'")
    row = dict(await cursor.fetchone())
    await conn.execute("DELETE FROM messages WHERE message_id='A1'")
    row["starred"] = 0
    row.pop("rowid", None)
    columns = ",".join(row)
    await conn.execute(f"INSERT INTO messages ({columns}) VALUES ({','.join('?' for _ in row)})",
                       tuple(row.values()))
    await conn.commit()
    assert [m["key"]["id"] for _jid, m in await db.get_starred_messages()] == ["A1"]


def test_the_mark_is_a_rowid():
    assert star_storage.filled_up_to("4321") == 4321
    assert star_storage.filled_up_to(None) == 0 and star_storage.filled_up_to("x") == 0


async def test_the_fill_walks_the_table_in_chunks(in_memory_db, monkeypatch):
    db = in_memory_db
    monkeypatch.setattr(star_storage, "FILL_CHUNK", 2)
    await db.insert_messages_batch(A, [message(f"M{i}", i, starred=i % 3 == 0) for i in range(1, 8)])
    conn = await db._ensure_conn()
    await conn.execute("UPDATE messages SET starred=0")
    await conn.commit()
    got = await db.get_starred_messages()
    assert sorted(m["key"]["id"] for _jid, m in got) == ["M3", "M6"]


async def test_a_stale_column_never_lists_an_unstarred_message(in_memory_db):
    db = in_memory_db
    await db.insert_message(A, message("A1", 100))
    conn = await db._ensure_conn()
    await conn.execute("UPDATE messages SET starred=1")
    await conn.commit()
    assert await db.get_starred_messages() == []
