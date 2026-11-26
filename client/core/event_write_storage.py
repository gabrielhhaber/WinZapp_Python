"""Apply delayed event fields without overwriting newer direct user edits."""
from core.database import _synced_to_phone, _now_ts
from core.ack_status import normalize_status, reached_status_rank


async def apply_status_batch(db, updates):
    async with db._write_lock:
        conn = await db._ensure_conn()
        try:
            for jid, mid, update in updates:
                status = normalize_status(update.get("status"))
                if status is None:
                    continue
                stored = await db._stored_message(conn, jid, mid)
                if not stored:
                    continue  # A delayed ack cannot recreate a deleted row.
                if status >= 0 and reached_status_rank(stored) > status:
                    continue
                history = stored.setdefault("MessageUpdate", [])
                if not history or history[-1] != update:
                    history.append(update)
                await conn.execute(
                    "UPDATE messages SET message_json=?, status=? "
                    "WHERE remote_jid=? AND message_id=?",
                    (db._encrypt_json(stored), status, jid, mid))
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise


def _contact_values(jid, data):
    return (data.get("remoteJid", jid), data.get("name", ""),
            data.get("pushName", ""), data.get("profilePicUrl", ""),
            int(bool(data.get("isSaved"))), _synced_to_phone(data))


async def apply_contact_batch(db, observations):
    async with db._write_lock:
        conn = await db._ensure_conn()
        try:
            for jid, before, after in observations:
                cursor = await conn.execute(
                    "SELECT remote_jid, name, push_name, profile_pic_url, is_saved, "
                    "synced_to_phone FROM contacts WHERE jid=?", (jid,))
                row = await cursor.fetchone()
                if row is not None:
                    # Local save/remove/rename overtook the remote event. Its
                    # stored values are authoritative, not the queued copy.
                    if before is None or tuple(row) != _contact_values(jid, before):
                        continue
                    await conn.execute(
                        "UPDATE contacts SET remote_jid=?, name=?, push_name=?, "
                        "profile_pic_url=?, is_saved=?, synced_to_phone=?, updated_at=? WHERE jid=?",
                        (*_contact_values(jid, after), _now_ts(), jid))
                elif before is None:
                    await conn.execute(
                        "INSERT INTO contacts (remote_jid, name, push_name, profile_pic_url, "
                        "is_saved, synced_to_phone, updated_at, jid) VALUES (?,?,?,?,?,?,?,?)",
                        (*_contact_values(jid, after), _now_ts(), jid))
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
