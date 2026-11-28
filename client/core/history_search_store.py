"""One page of stored messages for the history search (core/history_search.py).

The rows come back still encrypted: decrypting is the scan's business, done a
row at a time off the window's thread. The page is cut by rowid, newest first,
because messages has no index on its timestamp alone and ORDER BY timestamp
over the whole table would sort it again for every page.
"""
from core.history_search import SKIPPED_TYPES


async def message_rows_page(manager, before_rowid, limit):
    """[(rowid, remote_jid, message_json)] of the *limit* newest rows older
    than *before_rowid* (None: from the newest), types with nothing to search
    left out."""
    conn = await manager._ensure_conn()
    marks = ",".join("?" for _ in SKIPPED_TYPES)
    sql = (f"SELECT rowid AS rid, remote_jid, message_json FROM messages "
           f"WHERE message_type NOT IN ({marks})")
    params = list(SKIPPED_TYPES)
    if before_rowid is not None:
        sql += " AND rowid < ?"
        params.append(int(before_rowid))
    sql += " ORDER BY rowid DESC LIMIT ?"
    params.append(int(limit))
    cursor = await conn.execute(sql, params)
    return [(row["rid"], row["remote_jid"], row["message_json"])
            for row in await cursor.fetchall()]
