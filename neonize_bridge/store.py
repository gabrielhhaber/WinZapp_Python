"""The bridge's storage (SQLite): neonize has no chat list nor on-demand history
reading, so the bridge stores what arrives (history packages and live messages)
and answers list-chats / get-messages from here."""
import json
import sqlite3
import threading


def _is_channel(jid):
    """A channel (newsletter) is not a conversation: the store keeps nothing of it,
    whichever path (live message, history, phone state notice) brings it."""
    return str(jid or "").endswith("@newsletter")

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats(
    jid TEXT PRIMARY KEY, name TEXT DEFAULT '', ts INTEGER DEFAULT 0,
    unread INTEGER DEFAULT 0, archived INTEGER DEFAULT 0,
    pinned INTEGER DEFAULT 0, muted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS contacts(
    jid TEXT PRIMARY KEY, name TEXT DEFAULT '', pushname TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS msgs(
    chat TEXT NOT NULL, id TEXT NOT NULL, ts INTEGER NOT NULL, data TEXT NOT NULL,
    raw BLOB, PRIMARY KEY(chat, id));
CREATE INDEX IF NOT EXISTS msgs_by_date ON msgs(chat, ts);
-- receipts, reactions, edits and message-ack look a message up by id alone;
-- without this each lookup scanned every message (300 ms on 228k), under the
-- store's lock, and the routes waiting on it stalled the bridge's loop
CREATE INDEX IF NOT EXISTS msgs_by_id ON msgs(id);
CREATE TABLE IF NOT EXISTS pinned(
    msg TEXT PRIMARY KEY, chat TEXT NOT NULL, ts INTEGER DEFAULT 0, duration INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS votes(
    msg TEXT NOT NULL, who TEXT NOT NULL, ids TEXT NOT NULL, ts INTEGER DEFAULT 0,
    PRIMARY KEY(msg, who));
CREATE TABLE IF NOT EXISTS reactions(
    msg TEXT NOT NULL, who TEXT NOT NULL, emoji TEXT NOT NULL, ts INTEGER DEFAULT 0,
    PRIMARY KEY(msg, who));
CREATE TABLE IF NOT EXISTS receipts(
    msg TEXT NOT NULL, who TEXT NOT NULL, stage TEXT NOT NULL, ts INTEGER DEFAULT 0,
    PRIMARY KEY(msg, who, stage));
CREATE TABLE IF NOT EXISTS comments(
    chat TEXT NOT NULL, id TEXT NOT NULL, parent_id TEXT NOT NULL,
    author TEXT DEFAULT '', ts INTEGER DEFAULT 0, type TEXT NOT NULL,
    body TEXT DEFAULT '', from_me INTEGER DEFAULT 0, parent_serial TEXT DEFAULT '',
    PRIMARY KEY(chat, id));
CREATE INDEX IF NOT EXISTS comments_by_parent ON comments(chat, parent_id, ts);
"""

# Databases written before the bridge's names were in English use these table and
# column names; they are renamed in place, so no stored message is lost.
LEGACY_TABLES = {"fixadas": "pinned", "votos": "votes", "reacoes": "reactions",
                 "recibos": "receipts", "comentarios": "comments"}
LEGACY_COLUMNS = {"pinned": {"duracao": "duration"}, "votes": {"quem": "who"},
                  "reactions": {"quem": "who"}, "receipts": {"quem": "who", "etapa": "stage"},
                  "comments": {"serial_pai": "parent_serial"}}
LEGACY_INDEXES = ("msgs_por_data", "comentarios_por_pai")


def migrate_legacy_schema(conn):
    """Renames the tables, columns and indexes of a database created with the
    old names. Safe to run on a database that has the new names already."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for old, new in LEGACY_TABLES.items():
        if old in tables and new not in tables:
            conn.execute(f"ALTER TABLE {old} RENAME TO {new}")
    for table, columns in LEGACY_COLUMNS.items():
        present = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for old, new in columns.items():
            if old in present and new not in present:
                conn.execute(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in ("votes", "reactions"):    # the own account used to be stored as "eu"
        if table in tables:
            conn.execute(f"UPDATE OR IGNORE {table} SET who='me' WHERE who='eu'")
    for index in LEGACY_INDEXES:
        conn.execute(f"DROP INDEX IF EXISTS {index}")
    conn.commit()

# Messages the bridge makes up from events (system_messages.py):
# nobody's to read nor to mark as read.
SYSTEM_TYPES = ("gp2", "call_log")

CHAT_FIELDS = {"name", "ts", "unread", "archived", "pinned", "muted", "locked",
               "ephemeral", "ephemeral_ts"}


#: receipt stage -> WhatsApp Web ack
STAGE_ACK = {"delivered": 2, "read": 3, "played": 4}


def receipts_ack(receipts):
    """The highest ack among the receipts [(who, stage, ts)]; 1 (sent) if none
    arrived."""
    return max([1] + [STAGE_ACK.get(stage, 1) for _, stage, _ in receipts])


def lower_dates_raised_by_notices(conn):
    """A chat whose date came from a group notice (`gp2`: someone joined, left,
    was promoted...) goes back to the date of its newest real message, which
    is where the client sorts it; with no real message stored, to 0, and the
    client sorts it by the messages it holds itself (it never lowers a chat
    below those, chats_store). Only when the notice is the newest thing
    stored: a date from elsewhere (a history sync) is left alone. Returns how
    many chats moved."""
    cursor = conn.execute("""
        UPDATE chats SET ts = IFNULL((
            SELECT MAX(m.ts) FROM msgs m
            WHERE m.chat = chats.jid AND IFNULL(json_extract(m.data, '$.type'), '') <> 'gp2'), 0)
        WHERE EXISTS (SELECT 1 FROM msgs g WHERE g.chat = chats.jid AND g.ts = chats.ts
                      AND json_extract(g.data, '$.type') = 'gp2')
          AND NOT EXISTS (SELECT 1 FROM msgs r WHERE r.chat = chats.jid AND r.ts >= chats.ts
                          AND IFNULL(json_extract(r.data, '$.type'), '') <> 'gp2')""")
    conn.commit()
    return cursor.rowcount


class Store:
    def __init__(self, path):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            migrate_legacy_schema(self._conn)
            self._conn.executescript(SCHEMA)
            try:    # databases created before the locked column
                self._conn.execute("ALTER TABLE chats ADD COLUMN locked INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            try:    # databases created before the raw column
                self._conn.execute("ALTER TABLE msgs ADD COLUMN raw BLOB")
            except sqlite3.OperationalError:
                pass
            try:    # databases created before this column
                self._conn.execute("ALTER TABLE chats ADD COLUMN read_ts INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            # the chat's temporary-messages timer, in seconds (chat_timers.py)
            for column in ("ephemeral", "ephemeral_ts"):
                try:
                    self._conn.execute(f"ALTER TABLE chats ADD COLUMN {column} INTEGER DEFAULT 0")
                except sqlite3.OperationalError:
                    pass
            lower_dates_raised_by_notices(self._conn)

    def close(self):
        """Releases the file (on Windows a folder with an open database cannot be deleted)."""
        with self._lock:
            self._conn.close()

    # ---------------------------------------------------------- chats
    def chat(self, jid, **fields):
        """Creates the chat if it does not exist and writes only the given fields."""
        if _is_channel(jid):
            return
        fields = {k: v for k, v in fields.items() if k in CHAT_FIELDS}
        with self._lock:
            self._conn.execute("INSERT OR IGNORE INTO chats(jid) VALUES (?)", (jid,))
            if fields:
                sets = ", ".join(f"{k}=?" for k in fields)
                self._conn.execute(f"UPDATE chats SET {sets} WHERE jid=?",
                                   (*fields.values(), jid))
            self._conn.commit()

    def chat_timer(self, jid):
        """(seconds, setting timestamp) of the chat's temporary messages; None
        when the chat is not stored. (0, ...) is off."""
        with self._lock:
            r = self._conn.execute(
                "SELECT ephemeral, ephemeral_ts FROM chats WHERE jid=?", (jid,)).fetchone()
        return None if r is None else (int(r["ephemeral"] or 0), int(r["ephemeral_ts"] or 0))

    def chats_in_batch(self, rows):
        """Rows (jid, fields) as `chat` writes them, in one transaction: a
        history block brings up to two thousand chats, and a commit for each
        held the lock long enough to stall every route behind it."""
        with self._lock:
            for jid, fields in rows:
                if _is_channel(jid):
                    continue
                fields = {k: v for k, v in fields.items() if k in CHAT_FIELDS}
                self._conn.execute("INSERT OR IGNORE INTO chats(jid) VALUES (?)", (jid,))
                if fields:
                    sets = ", ".join(f"{k}=?" for k in fields)
                    self._conn.execute(f"UPDATE chats SET {sets} WHERE jid=?",
                                       (*fields.values(), jid))
            self._conn.commit()

    def copy_state(self, source, destination):
        """Puts in `destination` the archived/pinned/muted of `source`: the same
        chat stored under two addresses (number and LID) changes together."""
        with self._lock:
            r = self._conn.execute(
                "SELECT archived, pinned, muted FROM chats WHERE jid=?", (source,)).fetchone()
            if r is not None:
                self._conn.execute(
                    "UPDATE chats SET archived=?, pinned=?, muted=? WHERE jid=?",
                    (r["archived"], r["pinned"], r["muted"], destination))
                self._conn.commit()

    def on_arrival(self, jid, ts, from_me):
        """New message: raises the chat's date and counts an unread."""
        if _is_channel(jid):
            return
        with self._lock:
            self._conn.execute("INSERT OR IGNORE INTO chats(jid) VALUES (?)", (jid,))
            self._conn.execute(
                "UPDATE chats SET ts=MAX(ts, ?), unread=unread+? WHERE jid=?",
                (ts, 0 if from_me else 1, jid))
            self._conn.commit()

    def chats(self):
        """Every chat, newest first. A chat the history gave no date (0) takes the
        date of its newest real message, and keeps it: history leaves 0 on most
        conversations (132 of 145 private chats of a real account) and the client
        takes a chat with no date, no last message and no unread count for a
        "phantom" and does not keep it. A group notice (`gp2`) is not a real
        message here, as in `lower_dates_raised_by_notices`; a date the history
        did give is left alone."""
        with self._lock:
            rows = [dict(r) for r in self._conn.execute("SELECT * FROM chats")]
            # The newest real message of every dateless chat, in ONE query.
            # It was one query per chat, from Python: a real account had 6,760
            # dateless chats (almost all without a message) and list-chats
            # spent 1.1 s here, holding the store's lock, which stalled the
            # bridge's loop and everything behind it, sends included. Calls
            # and revocations still count as activity; notices do not.
            newest = {chat: int(ts) for chat, ts in self._conn.execute(
                "SELECT m.chat, MAX(m.ts) FROM msgs m "
                "WHERE m.chat IN (SELECT jid FROM chats WHERE IFNULL(ts, 0) <= 0) "
                "AND IFNULL(json_extract(m.data, '$.type'), '') <> 'gp2' "
                "GROUP BY m.chat") if ts}
            dated = []
            for row in rows:
                if int(row["ts"] or 0) > 0 or row["jid"] not in newest:
                    continue
                row["ts"] = newest[row["jid"]]
                dated.append((row["ts"], row["jid"]))
            if dated:
                self._conn.executemany("UPDATE chats SET ts=? WHERE jid=?", dated)
                self._conn.commit()
        rows.sort(key=lambda row: int(row["ts"] or 0), reverse=True)
        return rows

    # ----------------------------------------------------------- contacts
    def contact(self, jid, name=None, pushname=None):
        with self._lock:
            self._conn.execute("INSERT OR IGNORE INTO contacts(jid) VALUES (?)", (jid,))
            if name:
                self._conn.execute("UPDATE contacts SET name=? WHERE jid=?", (name, jid))
            if pushname:
                self._conn.execute("UPDATE contacts SET pushname=? WHERE jid=?",
                                   (pushname, jid))
            self._conn.commit()

    def set_contact_name(self, jid, name):
        """The address-book name as the phone has it now: unlike `contact`, an
        empty name clears it (the contact was removed from the address book)."""
        with self._lock:
            self._conn.execute("INSERT OR IGNORE INTO contacts(jid) VALUES (?)", (jid,))
            self._conn.execute("UPDATE contacts SET name=? WHERE jid=?", (name or "", jid))
            self._conn.commit()

    def contacts_in_batch(self, rows):
        """Rows (jid, name, pushname), in one transaction. Like `contact`, an empty
        field keeps what is stored; only what differs is written. Returns how many
        saved names (`name`) are new or changed."""
        changed = 0
        with self._lock:
            for jid, name, pushname in rows:
                r = self._conn.execute(
                    "SELECT name, pushname FROM contacts WHERE jid=?", (jid,)).fetchone()
                if r is None:
                    self._conn.execute(
                        "INSERT INTO contacts(jid, name, pushname) VALUES (?,?,?)",
                        (jid, name or "", pushname or ""))
                    changed += bool(name)
                    continue
                if name and name != r["name"]:
                    self._conn.execute("UPDATE contacts SET name=? WHERE jid=?", (name, jid))
                    changed += 1
                if pushname and pushname != r["pushname"]:
                    self._conn.execute("UPDATE contacts SET pushname=? WHERE jid=?",
                                       (pushname, jid))
            self._conn.commit()
        return changed

    def contact_of(self, *jids):
        """Name and nickname of a contact (merges the given forms of the JID)."""
        found = {}
        with self._lock:
            for jid in [j for j in jids if j]:
                r = self._conn.execute("SELECT name, pushname FROM contacts WHERE jid=?", (jid,)).fetchone()
                if r:
                    found.update({k: r[k] for k in ("name", "pushname") if r[k]})
        return found

    def contacts(self):
        with self._lock:
            return [dict(r) for r in self._conn.execute("SELECT * FROM contacts")]

    # ---------------------------------------------------------- messages
    def message(self, chat, wpp, raw=None):
        """Stores (or replaces) a message in the WhatsApp Web format; `raw` is the
        media record (serialized proto) to download it later."""
        self.messages_in_batch([(chat, wpp, raw)])

    def messages_in_batch(self, rows):
        """Rows (chat, wpp) or (chat, wpp, raw)."""
        rows = [r for r in rows if not _is_channel(r[0])]
        if not rows:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO msgs(chat, id, ts, data, raw) VALUES (?,?,?,?,?)",
                [(l[0], l[1]["id"]["id"], l[1]["t"], json.dumps(l[1]),
                  l[2] if len(l) > 2 else None) for l in rows])
            self._conn.commit()

    def change_message(self, msg_id, **fields):
        """Changes fields of a stored message's JSON (star, type...)."""
        with self._lock:
            r = self._conn.execute("SELECT chat, data FROM msgs WHERE id=?", (msg_id,)).fetchone()
            if r is None:
                return False
            data = json.loads(r["data"])
            data.update(fields)
            self._conn.execute("UPDATE msgs SET data=? WHERE id=?", (json.dumps(data), msg_id))
            self._conn.commit()
        return True

    def apply_revocations(self):
        """Marks as deleted, in place, the original message of each stored 'delete
        for everyone' notice (until now only the notice was stored, and the
        message came back when the app reloaded the chat)."""
        with self._lock:
            targets = [json.loads(r["data"]).get("protocolMessageKey", {}).get("id")
                       for r in self._conn.execute(
                           "SELECT data FROM msgs WHERE json_extract(data,'$.type')='revoked'")]
        done = 0
        for target in [a for a in targets if a]:
            if self.change_message(target, type="revoked", body=""):
                done += 1
        return done

    def react(self, msg_id, who, emoji, ts=0):
        """Stores `who`'s reaction to a message; an empty emoji removes the reaction."""
        with self._lock:
            if emoji:
                self._conn.execute(
                    "INSERT OR REPLACE INTO reactions(msg, who, emoji, ts) VALUES (?,?,?,?)",
                    (msg_id, who, emoji, ts))
            else:
                self._conn.execute("DELETE FROM reactions WHERE msg=? AND who=?", (msg_id, who))
            self._conn.commit()

    def pin_message(self, msg_id, chat, pin, ts=0, duration=0):
        """Pins (or unpins) a message in the chat. `duration` in seconds; 0 =
        no known end."""
        with self._lock:
            if pin:
                self._conn.execute(
                    "INSERT OR REPLACE INTO pinned(msg, chat, ts, duration) VALUES (?,?,?,?)",
                    (msg_id, chat, ts, duration))
            else:
                self._conn.execute("DELETE FROM pinned WHERE msg=?", (msg_id,))
            self._conn.commit()

    def pinned_of(self, chats, now):
        """Messages (WhatsApp Web format) pinned in the given chats, without the
        ones past their deadline."""
        found = []
        with self._lock:
            rows = [r for chat in chats for r in self._conn.execute(
                "SELECT msg, ts, duration FROM pinned WHERE chat=? ORDER BY ts", (chat,))]
        for r in rows:
            if r["duration"] and r["ts"] and r["ts"] + r["duration"] <= now:
                continue
            w = self.wpp_message(r["msg"])
            if w is not None:
                found.append(w)
        return found

    def vote(self, msg_id, who, indexes, ts=0):
        """Stores `who`'s vote in a poll (positions of the options); an empty list
        removes the vote."""
        with self._lock:
            if indexes:
                self._conn.execute(
                    "INSERT OR REPLACE INTO votes(msg, who, ids, ts) VALUES (?,?,?,?)",
                    (msg_id, who, json.dumps(list(indexes)), ts))
            else:
                self._conn.execute("DELETE FROM votes WHERE msg=? AND who=?", (msg_id, who))
            self._conn.commit()

    def votes_of(self, msg_id):
        """[(who, [positions])] of a poll's votes."""
        with self._lock:
            return [(r["who"], json.loads(r["ids"])) for r in self._conn.execute(
                "SELECT who, ids FROM votes WHERE msg=? ORDER BY ts, rowid", (msg_id,))]

    def reactions_of(self, msg_id):
        """[(who, emoji)] of the reactions to a message, from the oldest to the newest."""
        with self._lock:
            return [(r["who"], r["emoji"]) for r in self._conn.execute(
                "SELECT who, emoji FROM reactions WHERE msg=? ORDER BY ts, rowid", (msg_id,))]

    def delete_message(self, msg_id):
        with self._lock:
            self._conn.execute("DELETE FROM msgs WHERE id=?", (msg_id,))
            self._conn.commit()

    def wpp_message(self, msg_id):
        """The message (WhatsApp Web format) of an id, in any chat, or None."""
        with self._lock:
            r = self._conn.execute("SELECT data FROM msgs WHERE id=?", (msg_id,)).fetchone()
        return json.loads(r["data"]) if r else None

    def media_record(self, msg_id):
        """The media record of a message (any chat) or None."""
        with self._lock:
            r = self._conn.execute("SELECT raw FROM msgs WHERE id=? AND raw IS NOT NULL",
                                   (msg_id,)).fetchone()
        return bytes(r["raw"]) if r else None

    def messages(self, chat, quantity=20, before=None):
        """The `quantity` most recent (or the ones before `before`, a message
        id), in chronological order. `chat` is one key or several (the two
        addresses, number and LID, of the same chat). An unknown anchor returns
        [] and never the newest: the client would read as a previous page what
        is actually the end of the chat."""
        chats = (chat,) if isinstance(chat, str) else tuple(chat)
        marks = ",".join("?" * len(chats))
        with self._lock:
            limit = None
            if before:
                r = self._conn.execute(
                    f"SELECT ts FROM msgs WHERE chat IN ({marks}) AND id=?",
                    (*chats, before)).fetchone()
                if r is None:       # the anchor may be under another address of the chat
                    r = self._conn.execute("SELECT ts FROM msgs WHERE id=? LIMIT 1",
                                           (before,)).fetchone()
                if r is None:
                    return []
                limit = r["ts"]
            if limit is None:
                rows = self._conn.execute(
                    f"SELECT data FROM msgs WHERE chat IN ({marks}) "
                    "ORDER BY ts DESC, rowid DESC LIMIT ?", (*chats, quantity)).fetchall()
            else:
                rows = self._conn.execute(
                    f"SELECT data FROM msgs WHERE chat IN ({marks}) "
                    "AND (ts<? OR (ts=? AND id<?)) ORDER BY ts DESC, rowid DESC LIMIT ?",
                    (*chats, limit, limit, before, quantity)).fetchall()
        return [json.loads(r["data"]) for r in reversed(rows)]

    def for_response(self, messages, ack_of=None):
        """Copies of the messages in the format that get-messages and message-by-id
        return: `star` always boolean (without it the client never reconciles
        the local stars) and, on mine, `ack` (1 sent, 2 delivered, 3 read,
        4 played) coming from the stored receipts, at least 1: the client builds
        the ticks only from `ack` and the sync replaces the local record with the
        one read. `ack_of(message)` (optional) decides the ack of group messages
        (all members, group_receipts.py); None = the receipts' one stands."""
        out = []
        replies = self.comment_counts()
        for w in messages:
            w = {**w, "star": bool(w.get("star"))}
            if w["id"]["id"] in replies:
                w["replyCount"] = replies[w["id"]["id"]]
            if w.get("fromMe"):
                ack = ack_of(w) if ack_of else None
                w["ack"] = ack if ack is not None else receipts_ack(
                    self.receipts_of(w["id"]["id"]))
            out.append(w)
        return out

    # -------------------------------------------- announcement comments
    _COUNTED = "type IN ('comment','ciphertext')"     # the deleted one does not count

    def store_comment(self, msg_id, parent_id, chat, author, ts, kind, body, from_me,
                      parent_serial=""):
        """Stores an announcement comment. A repeated one (the server redelivers)
        changes nothing, unless the stored one was unreadable and this one opens."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO comments(chat, id, parent_id, author, ts, type, body, from_me,"
                " parent_serial) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(chat, id) DO UPDATE SET"
                " type=excluded.type, body=excluded.body, author=excluded.author"
                " WHERE comments.type='ciphertext' AND excluded.type='comment'",
                (chat, msg_id, parent_id, author, int(ts), kind, body, int(bool(from_me)),
                 parent_serial))
            self._conn.commit()

    def comment_author(self, chat, msg_id):
        """(author, from_me) of a comment not yet deleted, or None."""
        with self._lock:
            r = self._conn.execute(
                "SELECT author, from_me FROM comments WHERE chat=? AND id=?"
                " AND type!='revoked'", (chat, msg_id)).fetchone()
        return None if r is None else (r["author"], bool(r["from_me"]))

    def revoke_comment(self, chat, msg_id):
        """Marks the comment as deleted. (id of the commented message, its serial)
        if there was a not yet deleted comment with that id; otherwise None."""
        with self._lock:
            r = self._conn.execute(
                "SELECT parent_id, parent_serial FROM comments WHERE chat=? AND id=?"
                " AND type!='revoked'", (chat, msg_id)).fetchone()
            if r is None:
                return None
            self._conn.execute("UPDATE comments SET type='revoked', body='' WHERE chat=? AND id=?",
                               (chat, msg_id))
            self._conn.commit()
        return r["parent_id"], r["parent_serial"]

    def comments_of(self, chat, parent_id):
        """A message's comments, from the oldest to the newest."""
        with self._lock:
            return [dict(r) for r in self._conn.execute(
                "SELECT * FROM comments WHERE chat=? AND parent_id=? ORDER BY ts, rowid",
                (chat, parent_id))]

    def count_comments(self, chat, parent_id):
        with self._lock:
            return self._conn.execute(
                f"SELECT COUNT(*) FROM comments WHERE chat=? AND parent_id=? AND {self._COUNTED}",
                (chat, parent_id)).fetchone()[0]

    def comment_counts(self):
        """{id of the commented message: how many comments} of everything there is."""
        with self._lock:
            return {r[0]: r[1] for r in self._conn.execute(
                f"SELECT parent_id, COUNT(*) FROM comments WHERE {self._COUNTED}"
                " GROUP BY parent_id")}

    def last_comment(self, chat):
        """The newest comment of the chat that was not deleted, or None."""
        with self._lock:
            r = self._conn.execute(
                f"SELECT * FROM comments WHERE chat=? AND {self._COUNTED}"
                " ORDER BY ts DESC, rowid DESC LIMIT 1", (chat,)).fetchone()
        return dict(r) if r else None

    def received_to_read(self, chat, limit=3000):
        """The last received messages that count as unread: as many as the chat's
        counter says (the history ones and the live ones).
        Returns [(id, sender_wpp, ts)] and zeroes the chat."""
        with self._lock:
            r = self._conn.execute("SELECT unread FROM chats WHERE jid=?", (chat,)).fetchone()
            n = min(int(r["unread"] or 0), limit) if r else 0
            found = []
            if n > 0:
                rows = self._conn.execute(
                    "SELECT data FROM msgs WHERE chat=? ORDER BY ts DESC, rowid DESC LIMIT ?",
                    (chat, n * 3 + 50)).fetchall()
                for row in rows:
                    w = json.loads(row["data"])
                    if not w.get("fromMe") and w.get("type") not in SYSTEM_TYPES:
                        found.append((w["id"]["id"], w.get("author") or w.get("from") or chat, w["t"]))
                        if len(found) == n:
                            break
                found.reverse()
            self._conn.execute("UPDATE chats SET unread=0 WHERE jid=?", (chat,))
            self._conn.commit()
        return found

    def counts(self):
        with self._lock:
            c = self._conn.execute("SELECT COUNT(*) FROM chats").fetchone()[0]
            m = self._conn.execute("SELECT COUNT(*) FROM msgs").fetchone()[0]
        return {"chat": c, "message": m}

    def state_counts(self):
        """(archived, pinned): only to see whether a copy of the state changed anything."""
        with self._lock:
            a = self._conn.execute("SELECT COUNT(*) FROM chats WHERE archived=1").fetchone()[0]
            f = self._conn.execute("SELECT COUNT(*) FROM chats WHERE pinned>0").fetchone()[0]
        return a, f

    def chat_exists(self, jid):
        with self._lock:
            return self._conn.execute("SELECT 1 FROM chats WHERE jid=?", (jid,)).fetchone() is not None

    def last_received(self, chat):
        """The id of the chat's most recent received message, or ''."""
        for w in reversed(self.messages(chat, 60)):
            if not w.get("fromMe") and w.get("type") not in SYSTEM_TYPES:
                return w["id"]["id"]
        return ""

    def unread(self, chat):
        with self._lock:
            r = self._conn.execute("SELECT unread FROM chats WHERE jid=?", (chat,)).fetchone()
        return int(r["unread"] or 0) if r else 0

    def raw_last(self, chat):
        """The most recent stored message (any author), or None."""
        items = self.messages(chat, 1)
        return items[0] if items else None

    def last(self, chat):
        items = self.messages(chat, 1)
        return items[0] if items else None

    # ------------------------------------------- clear / delete chat
    def clear_chat(self, jid, keep_starred=True):
        """Deletes the chat's stored messages (only the unstarred ones, if
        `keep_starred`) and zeroes the unread counter. Returns how many messages
        went away."""
        with self._lock:
            filter_ = " AND COALESCE(json_extract(data,'$.star'),0)=0" if keep_starred else ""
            deleted = self._conn.execute(
                "DELETE FROM msgs WHERE chat=?" + filter_, (jid,)).rowcount
            self._conn.execute(
                "DELETE FROM pinned WHERE chat=? AND msg NOT IN (SELECT id FROM msgs WHERE chat=?)",
                (jid, jid))
            self._conn.execute("UPDATE chats SET unread=0 WHERE jid=?", (jid,))
            self._conn.commit()
        return deleted

    def delete_chat(self, jid):
        """Removes the chat from the list, with all its messages (starred too)."""
        with self._lock:
            self.clear_chat(jid, keep_starred=False)
            self._conn.execute("DELETE FROM chats WHERE jid=?", (jid,))
            self._conn.commit()

    # ----------------------------------------------------------- receipts
    def receipt(self, msg_id, who, stage, ts=0):
        """Stores that `who` reached `stage` ("delivered", "read", "played") of a
        message of ours. The first time the stage was seen is kept."""
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO receipts(msg, who, stage, ts) VALUES (?,?,?,?)",
                (msg_id, who, stage, ts))
            self._conn.commit()

    def receipts(self, msg_ids, who, stage, ts=0):
        """`receipt` for every message of one receipt event, in one transaction:
        an event names many messages, and a commit for each (a disk sync,
        ~6 ms) held the lock long enough, in a busy group, to stall the
        bridge's loop. All or none is written, as each one was before."""
        with self._lock:
            self._conn.executemany(
                "INSERT OR IGNORE INTO receipts(msg, who, stage, ts) VALUES (?,?,?,?)",
                [(msg_id, who, stage, ts) for msg_id in msg_ids])
            self._conn.commit()

    def receipts_of(self, msg_id):
        """[(who, stage, ts)] of a message's stored receipts."""
        with self._lock:
            return [(r["who"], r["stage"], r["ts"]) for r in self._conn.execute(
                "SELECT who, stage, ts FROM receipts WHERE msg=? ORDER BY rowid", (msg_id,))]
