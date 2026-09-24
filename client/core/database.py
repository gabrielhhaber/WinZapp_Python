"""
WinZapp Database Manager  (async version)
------------------------------------------
ACID-compliant SQLite storage for chats, messages, contacts, LID mappings,
and status updates.  Replaces the monolithic encrypted JSON (messages.dat)
with incremental, transactional writes.

Design decisions:
  - Fully async via ``aiosqlite`` — single connection, no threading hacks.
  - Structured concurrency via ``anyio`` — works on both asyncio and Trio.
  - WAL mode for concurrent reads without writer blocking.
  - Indexed columns (jid, timestamp) stored in plain text.
    Payload columns (message_json, last_message_json) encrypted via Fernet.
  - All public methods accept/return plain dicts matching the shapes that
    ``main.py`` expects, making the switch transparent.
  - ``import_from_dict`` / ``export_as_dict`` support the full messages.dat
    shape for migration.
  - Fernet is CPU-bound but fast (~1 µs per record); kept in the async
    context.  If profiling shows it blocks, move to
    ``anyio.to_thread.run_sync``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import aiosqlite
from cryptography.fernet import Fernet

from core.utils import MEASURED_SECONDS_KEY

log = logging.getLogger(__name__)

# ── Schema ────────────────────────────────────────────────────────────────────

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS chats (
    jid             TEXT PRIMARY KEY,
    remote_jid      TEXT NOT NULL,
    unread_count    INTEGER DEFAULT 0,
    push_name       TEXT DEFAULT '',
    name            TEXT DEFAULT '',
    archived        INTEGER DEFAULT 0,
    chat_type       TEXT DEFAULT 'chat',
    last_message_json TEXT DEFAULT '',
    t               INTEGER DEFAULT 0,
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS messages (
    message_id      TEXT NOT NULL,
    remote_jid      TEXT NOT NULL,
    from_me         INTEGER DEFAULT 0,
    participant     TEXT DEFAULT '',
    message_type    TEXT DEFAULT '',
    message_json    TEXT NOT NULL,
    timestamp       INTEGER NOT NULL,
    status          INTEGER DEFAULT 0,
    PRIMARY KEY (message_id, remote_jid)
);
-- The Calls tab reads every call record of every chat (get_call_logs()).
CREATE INDEX IF NOT EXISTS idx_msgs_type_ts
    ON messages(message_type, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_msgs_jid_ts
    ON messages(remote_jid, timestamp DESC);

CREATE TABLE IF NOT EXISTS contacts (
    jid             TEXT PRIMARY KEY,
    remote_jid      TEXT NOT NULL,
    name            TEXT DEFAULT '',
    push_name       TEXT DEFAULT '',
    profile_pic_url TEXT DEFAULT '',
    is_saved        INTEGER DEFAULT 0,
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS lid_mappings (
    lid_jid     TEXT PRIMARY KEY,
    phone_jid   TEXT NOT NULL
);

-- A retry damper, never a verdict: `recorded_at` (unix seconds) is what lets
-- MainWindow purge entries once they are old enough to be worth asking about
-- again.  Without it a LID recorded here once was blacklisted for good, across
-- every future launch, and its contact kept reading as "Contato sem nome" even
-- after WhatsApp had learned the real name.
-- The key is (jid, type) and not jid alone because both add_unresolvable_lid()
-- and add_unresolvable_name() can fire for the same JID in a single resolution
-- pass: under a jid-only key the first INSERT won and the second was silently
-- dropped, so memory held both flags for the session and the database held
-- only one of them after a restart.
CREATE TABLE IF NOT EXISTS unresolvable_lids (
    jid         TEXT NOT NULL,
    type        TEXT NOT NULL DEFAULT 'lid',
    recorded_at INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (jid, type)
);

CREATE TABLE IF NOT EXISTS status_updates (
    participant_jid TEXT NOT NULL,
    message_id      TEXT NOT NULL,
    message_json    TEXT NOT NULL,
    timestamp       INTEGER NOT NULL,
    PRIMARY KEY (participant_jid, message_id)
);

CREATE TABLE IF NOT EXISTS system_metadata (
    key             TEXT PRIMARY KEY,
    value           TEXT
);
"""

# How many messages to include inside each chat's ``messages.messages.records``
# when ``get_chats()`` builds the backward-compatible wrapper.  The caller can
# always use ``get_messages()`` to paginate the full set.
_CHAT_PAGE_SIZE = 200


# ── Helpers ───────────────────────────────────────────────────────────────────


def _now_ts() -> str:
    """ISO-8601 timestamp for SQLite TEXT columns."""
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _jid_from_key(key: dict) -> str:
    """Extract the effective remote JID from a message key dict."""
    return key.get("remoteJidAlt") or key.get("remoteJid", "")


def _msg_id(key: dict) -> str:
    """Extract message id from key dict."""
    return key.get("id", "")


def _timestamp(msg: dict) -> int:
    """Extract numeric timestamp from a message dict (0 if missing)."""
    ts = msg.get("messageTimestamp", 0)
    try:
        ts = int(ts)
        if ts > 1_000_000_000_000:
            ts //= 1000
        return ts
    except (TypeError, ValueError):
        return 0


def _delivery_status(msg: dict) -> int:
    """Latest delivery status of a message, for the indexed ``status`` column.

    Scale: -1 failed, 0 pending/unknown, 2 sent, 3 delivered, 4 read, 5 played
    (see core/websocket_client.py). The authoritative value is the last entry of
    ``MessageUpdate``, which is where acks land; a top-level ``status`` is the
    fallback used by history-synced messages. Both are already on the app's
    scale — ``msg["ack"]`` deliberately is NOT consulted here, because that one
    is on WhatsApp's own scale (1=sent, 2=received, …) and storing it unmapped
    would record every sent message as delivered.

    This column used to be hardcoded to 0 on every insert, so the delivery state
    only ever existed in memory: after a restart every message you had sent read
    back as "no status", and a failed send was indistinguishable from a
    delivered one.
    """
    updates = msg.get("MessageUpdate")
    if isinstance(updates, list):
        for update in reversed(updates):
            if not isinstance(update, dict):
                continue
            try:
                return int(update.get("status"))
            except (TypeError, ValueError):
                continue
    raw = msg.get("status")
    if raw is not None and not isinstance(raw, bool):
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    return 0


def _message_type(msg: dict) -> str:
    """Determine the message-type label from a normalized message."""
    mt = msg.get("messageType", "")
    if mt:
        return mt
    m = msg.get("message", {})
    if isinstance(m, dict):
        for known in (
            "conversation",
            "extendedTextMessage",
            "imageMessage",
            "audioMessage",
            "videoMessage",
            "documentMessage",
            "stickerMessage",
            "contactMessage",
            "pollCreationMessage",
            "buttonsMessage",
            "listMessage",
            "templateMessage",
            "protocolMessage",
        ):
            if known in m:
                return known
    return "unknown"


# ── DatabaseManager ───────────────────────────────────────────────────────────


class DatabaseManager:
    """Async SQLite manager for WinZapp data.

    Parameters
    ----------
    db_path : str
        Path to the SQLite file, or ``\":memory:\"`` for testing.
    key : bytes
        Fernet symmetric key used to encrypt/decrypt payload columns.
    """

    def __init__(self, db_path: str, key: bytes):
        self.db_path = db_path
        self._fernet = Fernet(key)
        self._conn: aiosqlite.Connection | None = None
        # Serialise every DB write.  aiosqlite uses a single sqlite3 connection;
        # without this lock, two coroutines interleave at await points and one
        # may try BEGIN while the other already started an auto-transaction.
        self._write_lock = asyncio.Lock()

    # ── Async context manager ─────────────────────────────────────────────

    async def __aenter__(self) -> DatabaseManager:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        await self.close()

    async def connect(self) -> None:
        """Open the SQLite connection and initialise the schema."""
        if self._conn is not None:
            return
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        # Without this, any external lock on the file (antivirus scanning
        # messages.db/-wal, OneDrive sync, etc.) makes SQLite raise "database
        # is locked" immediately instead of waiting briefly and retrying.
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.executescript(_SCHEMA_SQL)
        try:
            await self._conn.execute("ALTER TABLE chats ADD COLUMN t INTEGER DEFAULT 0")
        except Exception as exc:
            # Only "duplicate column" (the column already exists, from a prior
            # run) is expected here. Anything else (disk I/O error, corrupted
            # schema, permission issue) used to be swallowed silently, which
            # meant the app would carry on as if the `t` column existed, then
            # fail much later — in whichever unrelated method next tried to
            # read/write `chats.t` — with an error that gave no hint the real
            # cause was this ALTER TABLE never having succeeded.
            if "duplicate column" not in str(exc).lower():
                log.error("[connect] ALTER TABLE chats ADD COLUMN t failed: %s", exc)
                raise
        await self._upgrade_unresolvable_lids()
        await self._conn.commit()

    async def _upgrade_unresolvable_lids(self) -> None:
        assert self._conn is not None
        cursor = await self._conn.execute("PRAGMA table_info(unresolvable_lids)")
        columns = {row["name"] for row in await cursor.fetchall()}
        if "recorded_at" in columns:
            return
        log.info("[connect] Upgrading unresolvable_lids to the expiring schema.")
        await self._conn.execute("DROP TABLE IF EXISTS unresolvable_lids")
        await self._conn.executescript(_SCHEMA_SQL)

    async def close(self) -> None:
        """Close the connection if open."""
        if self._conn is not None:
            try:
                await self._conn.close()
            except Exception:
                pass
            self._conn = None

    # ── Internal helpers ───────────────────────────────────────────────────

    def _encrypt(self, plain: str) -> str:
        """Encrypt a string with Fernet, returns base64 token."""
        if not plain:
            return ""
        return self._fernet.encrypt(plain.encode()).decode()

    def _decrypt(self, token: str) -> str:
        """Decrypt a Fernet token back to string."""
        if not token:
            return ""
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except Exception as exc:
            log.warning("Failed to decrypt field: %s", exc)
            return ""

    def _encrypt_json(self, obj: Any) -> str:
        """JSON-serialize then encrypt. Returns empty string if obj is falsy."""
        if not obj:
            return ""
        return self._encrypt(json.dumps(obj, ensure_ascii=False))

    def _decrypt_json(self, token: str) -> Any:
        """Decrypt then JSON-deserialize."""
        plain = self._decrypt(token)
        if not plain:
            return {} if token else None
        try:
            return json.loads(plain)
        except json.JSONDecodeError:
            log.warning("Failed to JSON-decode decrypted field")
            return {}

    async def _ensure_conn(self) -> aiosqlite.Connection:
        """Auto-connect if not already connected."""
        if self._conn is None:
            await self.connect()
        assert self._conn is not None
        return self._conn

    async def get_metadata(self, key: str, default: str | None = None) -> str | None:
        """Retrieve a string value from system_metadata table."""
        conn = await self._ensure_conn()
        async with self._write_lock:
            cursor = await conn.execute(
                "SELECT value FROM system_metadata WHERE key = ?", (key,)
            )
            row = await cursor.fetchone()
            if row:
                return row["value"]
            return default

    async def set_metadata(self, key: str, value: str) -> None:
        """Insert or replace a string value in system_metadata table."""
        conn = await self._ensure_conn()
        async with self._write_lock:
            await conn.execute(
                "INSERT OR REPLACE INTO system_metadata (key, value) VALUES (?, ?)",
                (key, value),
            )
            await conn.commit()

    async def get_metadata_json(self, key: str, default: Any = None) -> Any:
        """Retrieve a JSON-decoded value from system_metadata table."""
        val = await self.get_metadata(key)
        if val is None:
            return default
        try:
            return json.loads(val)
        except Exception:
            return default

    async def set_metadata_json(self, key: str, value: Any) -> None:
        """Insert or replace a JSON-serializable value in system_metadata table."""
        await self.set_metadata(key, json.dumps(value))

    # ── Chats ───────────────────────────────────────────────────────────────

    async def get_chats(self, limit: int = _CHAT_PAGE_SIZE) -> dict[str, dict]:
        """Return all chats as ``{jid: chat_dict}``, compatible with main.py.

        Each chat dict includes a ``messages`` wrapper with the first
        ``_CHAT_PAGE_SIZE`` records so callers that iterate ``records``
        continue to work.  The full message set can be fetched via
        ``get_messages()``.

        ``limit`` used to silently default to 5 here — every unread-count
        badge, tray tooltip and title-bar counter derives from
        ``len(records)`` (see ``effective_unread_count()``), so any chat with
        more than 5 unread messages showed "5" until a full sync happened to
        overwrite it with the real (larger) record set. Defaulting to the
        same page size used everywhere else in the app removes that whole
        class of wrong-at-a-glance counts.
        """
        conn = await self._ensure_conn()
        cursor = await conn.execute(
            "SELECT * FROM chats ORDER BY remote_jid"
        )
        rows = await cursor.fetchall()
        result: dict[str, dict] = {}
        for row in rows:
            jid = row["jid"]
            last_msg = self._decrypt_json(row["last_message_json"])
            msgs = await self._build_message_wrapper(jid, limit=limit)
            t = 0
            try:
                t = int(row["t"] or 0)
            except (IndexError, KeyError, TypeError, ValueError):
                pass
            if not t and isinstance(last_msg, dict):
                try:
                    t = int(last_msg.get("timestamp") or last_msg.get("messageTimestamp") or last_msg.get("t") or 0)
                    if t > 1_000_000_000_000:
                        t //= 1000
                except (TypeError, ValueError):
                    t = 0
            result[jid] = {
                "remoteJid": row["remote_jid"],
                "unreadCount": row["unread_count"],
                "pushName": row["push_name"] or "",
                "name": row["name"] or "",
                "messages": msgs,
                "lastMessage": last_msg,
                "t": t,
                "archived": bool(row["archived"]),
                "archive": bool(row["archived"]),
                "type": row["chat_type"] or "chat",
            }
        return result

    async def _build_message_wrapper(self, jid: str, limit: int = 200) -> dict:
        """Build the ``{messages: {{records: [...], total: N, ...}}}`` wrapper."""
        total = await self.get_message_count(jid)
        records = await self.get_messages(jid, limit=limit, offset=0)
        return {
            "messages": {
                "records": records,
                "total": total,
                "pages": max(1, (total + limit - 1) // limit),
                "currentPage": 1,
            }
        }

    def _build_chat_values(self, jid: str, data: dict, updated_at: int) -> tuple:
        """Compute the 10-tuple bound to the chats upsert, shared by every
        chat-import path (upsert_chat/upsert_chats_batch/import_from_dict)."""
        remote_jid = data.get("remoteJid", jid)
        unread = int(data.get("unreadCount", 0) or 0)
        push_name = data.get("pushName", "") or ""
        name = data.get("name", "") or ""
        archived = 1 if (data.get("archived") or data.get("archive")) else 0
        chat_type = data.get("type", "chat") or "chat"
        last_msg = data.get("lastMessage")
        last_msg_enc = self._encrypt_json(last_msg) if last_msg else ""

        t = 0
        if "t" in data:
            try:
                t = int(data.get("t") or 0)
            except (TypeError, ValueError):
                t = 0
        if not t and isinstance(last_msg, dict):
            try:
                t = int(last_msg.get("timestamp") or last_msg.get("messageTimestamp") or last_msg.get("t") or 0)
            except (TypeError, ValueError):
                t = 0
        if t > 1_000_000_000_000:
            t //= 1000

        return (jid, remote_jid, unread, push_name, name,
                archived, chat_type, last_msg_enc, t, updated_at)

    async def upsert_chat(self, jid: str, data: dict) -> None:
        """Insert or replace a chat record from a chat dict."""
        async with self._write_lock:
            conn = await self._ensure_conn()
            await conn.execute(
                """INSERT OR REPLACE INTO chats
                   (jid, remote_jid, unread_count, push_name, name,
                    archived, chat_type, last_message_json, t, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                self._build_chat_values(jid, data, _now_ts()),
            )
            await conn.commit()

    async def upsert_chats_batch(self, chats: dict[str, dict]) -> None:
        """Insert/replace multiple chats in one transaction."""
        async with self._write_lock:
            conn = await self._ensure_conn()
            try:
                await conn.execute("BEGIN")
                for jid, data in chats.items():
                    await conn.execute(
                        """INSERT OR REPLACE INTO chats
                           (jid, remote_jid, unread_count, push_name, name,
                            archived, chat_type, last_message_json, t, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        self._build_chat_values(jid, data, _now_ts()),
                    )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

    async def get_chat_jids(self) -> list[str]:
        """Return a sorted list of all chat JIDs."""
        conn = await self._ensure_conn()
        cursor = await conn.execute(
            "SELECT jid FROM chats ORDER BY remote_jid"
        )
        rows = await cursor.fetchall()
        return [r["jid"] for r in rows]

    async def delete_chat(self, jid: str) -> None:
        """Remove a chat and all its messages."""
        async with self._write_lock:
            conn = await self._ensure_conn()
            await conn.execute("DELETE FROM messages WHERE remote_jid=?", (jid,))
            await conn.execute("DELETE FROM chats WHERE jid=?", (jid,))
            await conn.commit()

    async def delete_contact(self, jid: str) -> None:
        """Remove a contact record — used to undo a locally-added contact
        (NewContactDialog). A periodic WPPConnect contact sync repopulates
        this row from scratch if *jid* also happens to be a real WhatsApp
        contact independently of the local add."""
        async with self._write_lock:
            conn = await self._ensure_conn()
            await conn.execute("DELETE FROM contacts WHERE jid=?", (jid,))
            await conn.commit()

    async def merge_or_rename_chat(self, old_jid: str, new_jid: str) -> None:
        """Merge or rename a chat and its messages in the database.

        Used to dedupe a chat that exists under two JID forms (typically
        ``@lid`` vs the resolved phone JID) into one.  A message under
        ``old_jid`` whose ``message_id`` already exists under ``new_jid`` is,
        in every real call site, the *same* WhatsApp message filed under both
        keys before the merge — WhatsApp's own message IDs are effectively
        globally unique, so an ID collision here isn't two different messages
        clashing.  Even so, this moves every message it safely can before
        deleting anything, and only ever deletes an ``old_jid`` row once it
        has confirmed an equivalent row survives under ``new_jid`` — a blind
        "UPDATE OR IGNORE then DELETE everything left" could not tell "this
        was a duplicate" apart from "this update silently failed for some
        other reason", and would drop the row either way.
        """
        async with self._write_lock:
            conn = await self._ensure_conn()
            # 1. Move every old_jid message whose ID does NOT already exist
            #    under new_jid — these are the ones a blind UPDATE could lose.
            cursor = await conn.execute(
                """SELECT message_id FROM messages
                   WHERE remote_jid=? AND message_id NOT IN (
                       SELECT message_id FROM messages WHERE remote_jid=?
                   )""",
                (old_jid, new_jid),
            )
            movable_ids = [r["message_id"] for r in await cursor.fetchall()]
            for mid in movable_ids:
                await conn.execute(
                    "UPDATE messages SET remote_jid=? WHERE remote_jid=? AND message_id=?",
                    (new_jid, old_jid, mid),
                )

            # 2. Anything still under old_jid at this point has a confirmed
            #    surviving twin under new_jid — safe to drop.
            cursor = await conn.execute(
                "SELECT COUNT(*) AS cnt FROM messages WHERE remote_jid=?", (old_jid,)
            )
            row = await cursor.fetchone()
            remaining = row["cnt"] if row else 0
            if remaining:
                log.info(
                    "[merge_or_rename_chat] %s -> %s: moved %d message(s), "
                    "dropped %d duplicate(s) already present under %s",
                    old_jid, new_jid, len(movable_ids), remaining, new_jid,
                )
            await conn.execute("DELETE FROM messages WHERE remote_jid=?", (old_jid,))

            # 3. Merge/rename chats table
            cursor = await conn.execute("SELECT 1 FROM chats WHERE jid=? LIMIT 1", (new_jid,))
            exists = await cursor.fetchone()

            if exists:
                # If new_jid exists, delete the old_jid row to prevent constraint failures
                await conn.execute("DELETE FROM chats WHERE jid=?", (old_jid,))
            else:
                # If new_jid does not exist, rename it
                await conn.execute(
                    "UPDATE chats SET jid=?, remote_jid=? WHERE jid=?",
                    (new_jid, new_jid, old_jid)
                )
            await conn.commit()


    async def has_message(self, remote_jid: str, message_id: str) -> bool:
        """Return True if the message exists in the database."""
        conn = await self._ensure_conn()
        cursor = await conn.execute(
            "SELECT 1 FROM messages WHERE remote_jid=? AND message_id=? LIMIT 1",
            (remote_jid, message_id)
        )
        row = await cursor.fetchone()
        return row is not None

    # ── Messages ────────────────────────────────────────────────────────────


    @staticmethod
    def _jid_variants(remote_jid: str) -> list[str]:
        """*remote_jid* plus its @c.us/@s.whatsapp.net counterpart.

        Everything is SUPPOSED to be normalized to @s.whatsapp.net before it
        ever reaches the database (see main.py's extensive JID-normalization
        docs — "the single most common bug source in this codebase"), so
        under correct operation this is a one-element list and a no-op. It
        exists as a defensive second line: if a message ever got inserted
        under the legacy @c.us form by some path that skipped
        normalization, a plain single-JID lookup would silently never find
        it again (not an error — just messages that quietly never load).
        Querying both forms costs nothing extra when only one is ever
        actually populated (SQLite's `IN` short-circuits false-y branches
        the same as `=`), and closes that failure mode either way.
        """
        if remote_jid.endswith("@s.whatsapp.net"):
            return [remote_jid, remote_jid.replace("@s.whatsapp.net", "@c.us")]
        if remote_jid.endswith("@c.us"):
            return [remote_jid, remote_jid.replace("@c.us", "@s.whatsapp.net")]
        return [remote_jid]

    async def get_messages(
        self, remote_jid: str, limit: int = 200, offset: int = 0
    ) -> list[dict]:
        """Return message dicts for a chat, newest-first.

        Parameters
        ----------
        remote_jid : str
            Chat JID.
        limit : int
            Maximum records to return (default 200).
        offset : int
            Skip this many records (for pagination).

        Returns
        -------
        list[dict]
            Normalized message dicts (same shape as current messages.dat).
        """
        conn = await self._ensure_conn()
        jids = self._jid_variants(remote_jid)
        placeholders = ",".join("?" for _ in jids)
        cursor = await conn.execute(
            f"""SELECT message_json FROM messages
               WHERE remote_jid IN ({placeholders})
               ORDER BY timestamp DESC, message_id
               LIMIT ? OFFSET ?""",
            (*jids, limit, offset),
        )
        rows = await cursor.fetchall()
        result = []
        for row in rows:
            msg = self._decrypt_json(row["message_json"])
            if msg:
                result.append(msg)
        return result

    async def get_messages_asc(
        self, remote_jid: str, limit: int = 200, offset: int = 0
    ) -> list[dict]:
        """Return message dicts oldest-first (for initial chat load)."""
        conn = await self._ensure_conn()
        jids = self._jid_variants(remote_jid)
        placeholders = ",".join("?" for _ in jids)
        cursor = await conn.execute(
            f"""SELECT message_json FROM messages
               WHERE remote_jid IN ({placeholders})
               ORDER BY timestamp ASC, message_id
               LIMIT ? OFFSET ?""",
            (*jids, limit, offset),
        )
        rows = await cursor.fetchall()
        result = []
        for row in rows:
            msg = self._decrypt_json(row["message_json"])
            if msg:
                result.append(msg)
        return result

    async def get_message_by_id(self, remote_jid: str, message_id: str) -> dict | None:
        """Return one stored message of a chat by its id, or None."""
        if not message_id:
            return None
        conn = await self._ensure_conn()
        jids = self._jid_variants(remote_jid)
        placeholders = ",".join("?" for _ in jids)
        cursor = await conn.execute(
            f"""SELECT message_json FROM messages
               WHERE message_id = ? AND remote_jid IN ({placeholders})
               LIMIT 1""",
            (message_id, *jids),
        )
        row = await cursor.fetchone()
        return (self._decrypt_json(row["message_json"]) or None) if row else None

    async def get_call_logs(self, limit: int = 1000) -> list[tuple[str, dict]]:
        """Every stored call record of every chat, newest first, as
        ``(remote_jid, msg)`` -- what the Calls tab lists (core/call_log.py).

        ``message_type`` is stored in clear, so only the matching rows are
        decrypted. Both spellings: builds before the call-log branch stored the
        raw WhatsApp type.
        """
        conn = await self._ensure_conn()
        cursor = await conn.execute(
            """SELECT remote_jid, message_json FROM messages
               WHERE message_type IN ('callLogMessage', 'call_log')
               ORDER BY timestamp DESC
               LIMIT ?""",
            (int(limit),),
        )
        rows = await cursor.fetchall()
        result = []
        for row in rows:
            msg = self._decrypt_json(row["message_json"])
            if msg:
                result.append((row["remote_jid"], msg))
        return result

    async def get_message_count(self, remote_jid: str) -> int:
        """Return total message count for a chat."""
        conn = await self._ensure_conn()
        jids = self._jid_variants(remote_jid)
        placeholders = ",".join("?" for _ in jids)
        cursor = await conn.execute(
            f"SELECT COUNT(*) AS cnt FROM messages WHERE remote_jid IN ({placeholders})",
            tuple(jids),
        )
        row = await cursor.fetchone()
        return row["cnt"] if row else 0

    def _build_message_values(self, remote_jid: str, msg: dict) -> tuple | None:
        """Compute the 8-tuple bound to the messages upsert, shared by every
        message-import path (insert_message/insert_messages_batch/
        import_from_dict). Returns None for an id-less message — an empty
        message_id is not "no ID yet", it IS a value, and the
        (message_id, remote_jid) primary key means every id-less message in
        the same chat would silently overwrite the previous one via
        INSERT OR REPLACE. That has already happened in the wild with
        malformed/edge-case payloads; callers must skip instead of quietly
        losing messages against each other."""
        key = msg.get("key", {})
        mid = _msg_id(key)
        if not mid:
            return None
        from_me = 1 if key.get("fromMe") else 0
        participant = key.get("participant", "") or ""
        mtype = _message_type(msg)
        ts = _timestamp(msg)
        msg_enc = self._encrypt_json(msg)
        return (mid, remote_jid, from_me, participant, mtype, msg_enc, ts,
                _delivery_status(msg))

    async def _with_known_video_duration(self, conn, remote_jid: str, msg: dict) -> dict:
        """*msg*, with a video duration WinZapp measured earlier restored when
        the incoming copy carries none.

        Some videos are handed over by WhatsApp Web with duration 0 forever —
        the sending client left the field out of the message, and no field on
        the payload carries the real length. WinZapp measures those from the
        file itself once it has been downloaded or played, and that
        measurement lives only in the stored record: the server never learns
        it. So every resync, which overwrites the row with the server's copy,
        used to erase it — the length showed up after playing the video and
        was gone again on the next restart.

        The rule lives here, at the single point every write passes through,
        rather than in each of the several sync paths that batch-insert
        messages, so no future one can forget it. A message's video is
        immutable (WhatsApp has no way to change the media of a sent
        message), so a duration measured once stays valid for that id. A copy
        that DOES state a duration always wins — that is the sender's own
        answer finally arriving — and anything that is not a video without a
        duration returns untouched without hitting the database at all.
        """
        video = (msg.get("message") or {}).get("videoMessage")
        if not isinstance(video, dict) or MEASURED_SECONDS_KEY in video:
            return msg
        message_id = (msg.get("key") or {}).get("id") or ""
        if not message_id:
            return msg
        cursor = await conn.execute(
            "SELECT message_json FROM messages WHERE remote_jid=? AND message_id=?",
            (remote_jid, message_id),
        )
        row = await cursor.fetchone()
        if not row:
            return msg
        stored = self._decrypt_json(row["message_json"]) or {}
        stored_video = (stored.get("message") or {}).get("videoMessage")
        if not isinstance(stored_video, dict) or MEASURED_SECONDS_KEY not in stored_video:
            return msg
        known = stored_video[MEASURED_SECONDS_KEY]
        log.info(
            "[messages] %s: keeping the measured %ss duration; the incoming copy carries none",
            message_id, known,
        )
        merged = dict(msg)
        merged["message"] = dict(msg.get("message") or {})
        merged["message"]["videoMessage"] = {**video, MEASURED_SECONDS_KEY: known}
        return merged

    async def insert_message(self, remote_jid: str, msg: dict) -> None:
        """Insert a single message record.

        The whole read-then-write sits inside one lock hold: the read is
        _with_known_video_duration()'s lookup of a duration this row may
        already carry, and releasing the lock between the two would let
        another write land in the gap — the very race that helper exists to
        close.
        """
        async with self._write_lock:
            conn = await self._ensure_conn()
            msg = await self._with_known_video_duration(conn, remote_jid, msg)
            values = self._build_message_values(remote_jid, msg)
            if values is None:
                log.warning(
                    "[insert_message] dropping message with empty key.id for %s "
                    "(would collide with any other id-less message in this chat)",
                    remote_jid,
                )
                return
            await conn.execute(
                """INSERT OR REPLACE INTO messages
                   (message_id, remote_jid, from_me, participant,
                    message_type, message_json, timestamp, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                values,
            )
            await conn.commit()

    async def insert_messages_batch(
        self, remote_jid: str, msgs: list[dict]
    ) -> None:
        """Insert many messages in a single transaction."""
        if not msgs:
            return
        skipped = 0
        async with self._write_lock:
            conn = await self._ensure_conn()
            try:
                await conn.execute("BEGIN")
                for msg in msgs:
                    msg = await self._with_known_video_duration(conn, remote_jid, msg)
                    values = self._build_message_values(remote_jid, msg)
                    if values is None:
                        # See _build_message_values() — an empty id-less
                        # message would silently overwrite another id-less
                        # message in the same batch/chat instead of being
                        # rejected.
                        skipped += 1
                        continue
                    await conn.execute(
                        """INSERT OR REPLACE INTO messages
                           (message_id, remote_jid, from_me, participant,
                            message_type, message_json, timestamp, status)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        values,
                    )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        if skipped:
            log.warning(
                "[insert_messages_batch] dropped %d message(s) with empty "
                "key.id for %s", skipped, remote_jid,
            )

    async def update_message_id(
        self, remote_jid: str, old_id: str, new_id: str
    ) -> None:
        """Update a message's ID from old_id to new_id in the database."""
        async with self._write_lock:
            conn = await self._ensure_conn()
            # First, check if the new_id already exists (to prevent duplicates)
            cursor = await conn.execute(
                "SELECT 1 FROM messages WHERE remote_jid=? AND message_id=?",
                (remote_jid, new_id),
            )
            exists = await cursor.fetchone()
            if exists:
                # If the new ID already exists, delete the old UUID message
                await conn.execute(
                    "DELETE FROM messages WHERE remote_jid=? AND message_id=?",
                    (remote_jid, old_id),
                )
            else:
                # Otherwise, update the message ID
                await conn.execute(
                    "UPDATE messages SET message_id=? WHERE remote_jid=? AND message_id=?",
                    (new_id, remote_jid, old_id),
                )
            await conn.commit()

    async def update_message_status(
        self, remote_jid: str, message_id: str, status: int
    ) -> None:
        """Update delivery/read status for a message."""
        async with self._write_lock:
            conn = await self._ensure_conn()
            await conn.execute(
                "UPDATE messages SET status=? WHERE message_id=? AND remote_jid=?",
                (status, message_id, remote_jid),
            )
            await conn.commit()

    async def delete_message(self, remote_jid: str, message_id: str) -> None:
        """Delete a single message by remote_jid + message_id."""
        async with self._write_lock:
            conn = await self._ensure_conn()
            await conn.execute(
                "DELETE FROM messages WHERE remote_jid=? AND message_id=?",
                (remote_jid, message_id),
            )
            await conn.commit()

    async def delete_chat_messages(self, remote_jid: str) -> None:
        """Remove all messages for a chat."""
        async with self._write_lock:
            conn = await self._ensure_conn()
            await conn.execute(
                "DELETE FROM messages WHERE remote_jid=?", (remote_jid,)
            )
            await conn.commit()

    async def delete_chat_messages_except(self, remote_jid: str, keep_message_ids) -> None:
        """Remove all messages for a chat except *keep_message_ids* — used
        by "clear chat" so starred messages survive it, same as WhatsApp's
        own behavior (starring a message is meant to make it durable)."""
        keep = [m for m in (keep_message_ids or []) if m]
        async with self._write_lock:
            conn = await self._ensure_conn()
            if not keep:
                await conn.execute(
                    "DELETE FROM messages WHERE remote_jid=?", (remote_jid,)
                )
            else:
                placeholders = ",".join("?" * len(keep))
                await conn.execute(
                    f"DELETE FROM messages WHERE remote_jid=? AND message_id NOT IN ({placeholders})",
                    (remote_jid, *keep),
                )
            await conn.commit()

    # ── Contacts ────────────────────────────────────────────────────────────

    async def get_contacts(self) -> dict[str, dict]:
        """Return all contacts as ``{jid: contact_dict}``."""
        conn = await self._ensure_conn()
        cursor = await conn.execute(
            "SELECT * FROM contacts ORDER BY remote_jid"
        )
        rows = await cursor.fetchall()
        result: dict[str, dict] = {}
        for row in rows:
            result[row["jid"]] = {
                "id": row["jid"],
                "remoteJid": row["remote_jid"],
                "name": row["name"] or "",
                "pushName": row["push_name"] or "",
                "profilePicUrl": row["profile_pic_url"] or "",
                "type": "contact",
                "isSaved": bool(row["is_saved"]),
            }
        return result

    async def upsert_contact(self, jid: str, data: dict) -> None:
        """Insert or replace a contact record."""
        async with self._write_lock:
            conn = await self._ensure_conn()
            remote_jid = data.get("remoteJid", jid)
            name = data.get("name", "")
            push_name = data.get("pushName", "")
            pic = data.get("profilePicUrl", "")
            saved = 1 if data.get("isSaved") else 0

            await conn.execute(
                """INSERT OR REPLACE INTO contacts
                   (jid, remote_jid, name, push_name, profile_pic_url,
                    is_saved, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (jid, remote_jid, name, push_name, pic, saved, _now_ts()),
            )
            await conn.commit()

    async def upsert_contacts_batch(self, contacts: dict[str, dict]) -> None:
        """Insert/replace multiple contacts in one transaction."""
        async with self._write_lock:
            conn = await self._ensure_conn()
            try:
                await conn.execute("BEGIN")
                for jid, data in contacts.items():
                    remote_jid = data.get("remoteJid", jid)
                    name = data.get("name", "")
                    push_name = data.get("pushName", "")
                    pic = data.get("profilePicUrl", "")
                    saved = 1 if data.get("isSaved") else 0
                    await conn.execute(
                        """INSERT OR REPLACE INTO contacts
                           (jid, remote_jid, name, push_name, profile_pic_url,
                            is_saved, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (jid, remote_jid, name, push_name, pic, saved, _now_ts()),
                    )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

    # ── LID Mappings ────────────────────────────────────────────────────────

    async def get_lid_mappings(self) -> dict[str, str]:
        """Return ``{lid_jid: phone_jid}``."""
        conn = await self._ensure_conn()
        cursor = await conn.execute(
            "SELECT lid_jid, phone_jid FROM lid_mappings"
        )
        rows = await cursor.fetchall()
        return {r["lid_jid"]: r["phone_jid"] for r in rows}

    async def set_lid_mapping(self, lid_jid: str, phone_jid: str) -> None:
        """Insert or update a single LID → phone mapping."""
        async with self._write_lock:
            conn = await self._ensure_conn()
            await conn.execute(
                "INSERT OR REPLACE INTO lid_mappings (lid_jid, phone_jid) VALUES (?, ?)",
                (lid_jid, phone_jid),
            )
            await conn.commit()

    async def delete_lid_mapping(self, lid_jid: str) -> None:
        """Delete a single JID mapping."""
        async with self._write_lock:
            conn = await self._ensure_conn()
            await conn.execute(
                "DELETE FROM lid_mappings WHERE lid_jid = ?",
                (lid_jid,),
            )
            await conn.commit()

    async def get_unresolvable_lids(self) -> tuple[set[str], set[str]]:
        """Return ``(set_of_lids, set_of_names)``."""
        conn = await self._ensure_conn()
        cursor = await conn.execute(
            "SELECT jid, type FROM unresolvable_lids"
        )
        rows = await cursor.fetchall()
        lids: set[str] = set()
        names: set[str] = set()
        for r in rows:
            if r["type"] == "name":
                names.add(r["jid"])
            else:
                lids.add(r["jid"])
        return lids, names

    async def add_unresolvable_lid(self, jid: str) -> None:
        async with self._write_lock:
            conn = await self._ensure_conn()
            await conn.execute(
                "INSERT OR REPLACE INTO unresolvable_lids (jid, type, recorded_at) "
                "VALUES (?, 'lid', ?)",
                (jid, int(time.time())),
            )
            await conn.commit()

    async def add_unresolvable_name(self, jid: str) -> None:
        async with self._write_lock:
            conn = await self._ensure_conn()
            await conn.execute(
                "INSERT OR REPLACE INTO unresolvable_lids (jid, type, recorded_at) "
                "VALUES (?, 'name', ?)",
                (jid, int(time.time())),
            )
            await conn.commit()

    async def delete_unresolvable_lid(self, jid: str) -> None:
        async with self._write_lock:
            conn = await self._ensure_conn()
            await conn.execute(
                "DELETE FROM unresolvable_lids WHERE jid = ? AND type = 'lid'",
                (jid,),
            )
            await conn.commit()

    async def delete_unresolvable_name(self, jid: str) -> None:
        async with self._write_lock:
            conn = await self._ensure_conn()
            await conn.execute(
                "DELETE FROM unresolvable_lids WHERE jid = ? AND type = 'name'",
                (jid,),
            )
            await conn.commit()

    async def delete_expired_unresolvable(self, cutoff_ts: int) -> int:
        async with self._write_lock:
            conn = await self._ensure_conn()
            cursor = await conn.execute(
                "DELETE FROM unresolvable_lids WHERE recorded_at < ?",
                (cutoff_ts,),
            )
            await conn.commit()
            return cursor.rowcount if cursor.rowcount is not None else 0

    # ── Status Updates (Stories) ─────────────────────────────────────────────

    async def get_status_updates(self) -> dict[str, list[dict]]:
        """Return ``{participant_jid: [msg_dict, ...]}``."""
        conn = await self._ensure_conn()
        cursor = await conn.execute(
            "SELECT * FROM status_updates ORDER BY participant_jid, timestamp"
        )
        rows = await cursor.fetchall()
        result: dict[str, list[dict]] = {}
        for row in rows:
            p = row["participant_jid"]
            msg = self._decrypt_json(row["message_json"])
            if msg:
                result.setdefault(p, []).append(msg)
        return result

    async def delete_expired_status_updates(self, cutoff_ts: int) -> int:
        """Delete status/story updates older than *cutoff_ts* (unix seconds).

        WhatsApp stories expire after 24h; nothing ever pruned this table on
        the WinZapp side, so it grew forever — every status ever received or
        viewed stayed in the database (payload included) indefinitely. This
        is one of the main contributors to "the database got huge" over
        months of use. Returns the number of rows deleted.
        """
        async with self._write_lock:
            conn = await self._ensure_conn()
            cursor = await conn.execute(
                "DELETE FROM status_updates WHERE timestamp < ?", (cutoff_ts,)
            )
            await conn.commit()
            return cursor.rowcount if cursor.rowcount is not None else 0

    async def delete_status_update(self, message_id: str) -> int:
        """Delete one locally cached status by its WhatsApp message id."""
        if not message_id:
            return 0
        async with self._write_lock:
            conn = await self._ensure_conn()
            cursor = await conn.execute(
                "DELETE FROM status_updates WHERE message_id = ?", (message_id,)
            )
            await conn.commit()
            return cursor.rowcount if cursor.rowcount is not None else 0

    async def upsert_status_update(self, participant: str, msg: dict) -> None:
        """Insert or replace a status update message."""
        async with self._write_lock:
            conn = await self._ensure_conn()
            key = msg.get("key", {})
            mid = _msg_id(key)
            ts = _timestamp(msg)
            msg_enc = self._encrypt_json(msg)

            await conn.execute(
                """INSERT OR REPLACE INTO status_updates
                   (participant_jid, message_id, message_json, timestamp)
                   VALUES (?, ?, ?, ?)""",
                (participant, mid, msg_enc, ts),
            )
            await conn.commit()

    # ── Bulk Import / Export (for migration) ─────────────────────────────────

    async def import_from_dict(self, data: dict, clear_first: bool = False,
                                clear_metadata: bool = True) -> int:
        """Populate the database from a messages.dat-shaped dict.

        Parameters
        ----------
        data : dict
            The messages.dat-shaped dict (keys: ``chats``, ``contacts``,
            ``lid_to_phone``, ``unresolvable_lids``, ``unresolvable_names``,
            ``status_updates``).
        clear_first : bool
            If ``True``, delete all existing records before importing.
        clear_metadata : bool
            If ``True`` (default) and ``clear_first`` is also ``True``, also
            wipes ``system_metadata`` — the key/value table backing
            cleared_chats/deleted_chats/archived_chats/pinned_chats/
            muted_chats/blocked_contacts and more. Pass ``False`` for a
            resync-in-place (MainWindow.clear_local_data(), F5): the point
            there is only to refetch chats/messages from WhatsApp, not to
            discard the user's own local actions on top of them — an
            account switch/logout (the only other clear_first=True caller)
            still wants the full wipe, since leaving another account's
            blocked-contacts list or archived state behind would leak
            between accounts.

        Returns
        -------
        int
            Total number of records imported.

        Notes
        -----
        All SQL is inlined here (not delegated to helper methods) because
        this method holds _write_lock for the entire operation; calling any
        other write method from here would deadlock on that same lock.
        One explicit BEGIN … COMMIT wraps everything so the import is atomic.
        """
        async with self._write_lock:
            conn = await self._ensure_conn()
            total = 0
            try:
                await conn.execute("BEGIN")

                if clear_first:
                    tables = ["chats", "messages", "contacts",
                              "lid_mappings", "unresolvable_lids", "status_updates"]
                    if clear_metadata:
                        tables.append("system_metadata")
                    for tbl in tables:
                        await conn.execute(f"DELETE FROM {tbl}")

                # ── Chats + messages ─────────────────────────────────────
                now = _now_ts()
                for jid, chat in data.get("chats", {}).items():
                    remote_jid = chat.get("remoteJid", jid)
                    await conn.execute(
                        """INSERT OR REPLACE INTO chats
                           (jid, remote_jid, unread_count, push_name, name,
                            archived, chat_type, last_message_json, t, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        self._build_chat_values(jid, chat, now),
                    )
                    total += 1

                    records = (
                        chat.get("messages", {})
                            .get("messages", {})
                            .get("records", [])
                    )
                    for msg in records:
                        values = self._build_message_values(remote_jid, msg)
                        if values is None:
                            # See _build_message_values(): an id-less message
                            # would silently overwrite any other id-less
                            # message in the same chat under INSERT OR REPLACE.
                            continue
                        await conn.execute(
                            """INSERT OR REPLACE INTO messages
                               (message_id, remote_jid, from_me, participant,
                                message_type, message_json, timestamp, status)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                            values,
                        )
                        total += 1

                # ── Contacts ─────────────────────────────────────────────
                for jid, contact in data.get("contacts", {}).items():
                    remote_jid = contact.get("remoteJid", jid)
                    name       = contact.get("name", "") or ""
                    push_name  = contact.get("pushName", "") or ""
                    pic        = contact.get("profilePicUrl", "") or ""
                    saved      = 1 if contact.get("isSaved") else 0
                    await conn.execute(
                        """INSERT OR REPLACE INTO contacts
                           (jid, remote_jid, name, push_name, profile_pic_url,
                            is_saved, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (jid, remote_jid, name, push_name, pic, saved, now),
                    )
                    total += 1

                # ── LID mappings ──────────────────────────────────────────
                for lid_jid, phone_jid in data.get("lid_to_phone", {}).items():
                    await conn.execute(
                        "INSERT OR REPLACE INTO lid_mappings (lid_jid, phone_jid) VALUES (?, ?)",
                        (lid_jid, phone_jid),
                    )
                    total += 1

                # ── Unresolvable LIDs / names ─────────────────────────────
                for lid in data.get("unresolvable_lids", []):
                    await conn.execute(
                        "INSERT OR IGNORE INTO unresolvable_lids (jid, type) VALUES (?, 'lid')",
                        (lid,),
                    )
                    total += 1
                for nm in data.get("unresolvable_names", []):
                    await conn.execute(
                        "INSERT OR IGNORE INTO unresolvable_lids (jid, type) VALUES (?, 'name')",
                        (nm,),
                    )
                    total += 1

                # ── Status updates ────────────────────────────────────────
                for participant, statuses in data.get("status_updates", {}).items():
                    for smsg in statuses:
                        key  = smsg.get("key", {})
                        mid  = _msg_id(key)
                        ts   = _timestamp(smsg)
                        menc = self._encrypt_json(smsg)
                        await conn.execute(
                            """INSERT OR REPLACE INTO status_updates
                               (participant_jid, message_id, message_json, timestamp)
                               VALUES (?, ?, ?, ?)""",
                            (participant, mid, menc, ts),
                        )
                        total += 1

                await conn.commit()
                return total

            except Exception:
                await conn.rollback()
                raise

    async def export_as_dict(self) -> dict[str, Any]:
        """Export the full database as a messages.dat-shaped dict.

        Used for validation after migration.
        """
        chats = await self.get_chats()
        contacts = await self.get_contacts()
        lid_to_phone = await self.get_lid_mappings()
        lids, names = await self.get_unresolvable_lids()
        status_updates = await self.get_status_updates()

        return {
            "chats": chats,
            "contacts": contacts,
            "lid_to_phone": lid_to_phone,
            "unresolvable_lids": sorted(lids),
            "unresolvable_names": sorted(names),
            "status_updates": status_updates,
        }

    async def clear_all(self) -> None:
        """Delete all records from every table (for full-state replacement)."""
        async with self._write_lock:
            conn = await self._ensure_conn()
            try:
                await conn.execute("BEGIN")
                for table in (
                    "chats", "messages", "contacts",
                    "lid_mappings", "unresolvable_lids", "status_updates",
                    "system_metadata",
                ):
                    await conn.execute(f"DELETE FROM {table}")
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

    async def vacuum(self) -> None:
        """Recover disk space.  Call during idle periods.

        Holds ``_write_lock`` for the duration: SQLite raises
        ``cannot VACUUM from within a transaction`` if this runs while another
        coroutine has a write in flight, which — before this lock was added
        here — was only avoided by nothing ever calling this method at all.
        """
        async with self._write_lock:
            conn = await self._ensure_conn()
            await conn.execute("VACUUM")
