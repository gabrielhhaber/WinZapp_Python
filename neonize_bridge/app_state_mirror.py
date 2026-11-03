"""The bridge's copy of the app state entries whatsmeow has no event for: labels
and custom lists (`label_edit`, `label_jid`), who sees my Status
(`status_privacy`) and "Keep chats archived" (`setting_unarchiveChats`, read
by chat_archive.py).

WinZapp's neonize build emits `AppStateMutationEv` for every mutation, set or
remove, with its collection and version, also during a full sync
(neonize_patches/whatsmeow/0003). The entries of INDEXES are kept in
`app_state.db` in the session folder: the index (JSON) and the serialized
SyncActionValue. A full sync re-sends every entry of a collection, so when it
completes (`AppStateSyncCompleteEv`, handled in contact_events.py, which calls
`full_sync_completed`) the entries it did not re-send are gone and are dropped.

`seeded(collection)` says the copy is complete: true after the first full sync
of that collection seen by this bridge (`ensure_seeded` asks for one). Until
then a route must not decide from it (a list not seen yet is not "no list").

Every mutation's kind is logged once per (collection, index name, version,
operation), never its JIDs: that is how a new index is found on a live account.

The pure functions are at the top (tested in test_app_state_mirror.py).
"""
import json
import logging
import os
import sqlite3
import threading
import time

log = logging.getLogger("bridge")

FILE = "app_state.db"
INDEXES = ("label_edit", "label_jid", "status_privacy", "setting_unarchiveChats")
SEED_TIMEOUT = 12.0     # the client reads lists with a 15 s budget

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries(
  idx TEXT PRIMARY KEY, name TEXT NOT NULL, collection TEXT NOT NULL,
  version INTEGER NOT NULL, value BLOB NOT NULL, at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS seeded(collection TEXT PRIMARY KEY, at INTEGER NOT NULL);
"""


def wanted(index) -> bool:
    """Is this mutation index one the bridge keeps?"""
    return bool(index) and index[0] in INDEXES


def index_key(index) -> str:
    """The stored key of an index: its JSON, as WhatsApp hashes it."""
    return json.dumps(list(index), ensure_ascii=False, separators=(",", ":"))


def describe(collection, index, version, remove) -> tuple:
    """What is logged about a mutation: no JID, only its kind, and of each
    argument that is an address only its server ('contact[c.us]'), which is
    what tells the index forms apart."""
    name = str(index[0]) if index else ""
    servers = [str(arg).rpartition("@")[2] for arg in index[1:] if "@" in str(arg)]
    if servers:
        name += "[" + ",".join(servers) + "]"
    return (str(collection), name, int(version), bool(remove))


class Mirror:
    """The stored entries of one session. Thread-safe: neonize's thread writes,
    the routes read from executor threads."""

    def __init__(self, path):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.RLock()
        self._full_seen = {}         # collection -> keys re-sent by the running full sync
        self._seeded = {}            # collection -> threading.Event
        self._logged = set()
        with self._lock:
            self._conn.executescript(SCHEMA)
            for (collection,) in self._conn.execute("SELECT collection FROM seeded"):
                self._event(collection).set()

    def close(self):
        with self._lock:
            self._conn.close()

    def _event(self, collection):
        with self._lock:
            return self._seeded.setdefault(collection, threading.Event())

    # ------------------------------------------------------------- writes
    def apply(self, collection, index, version, remove, value_bytes, full_sync=False):
        """One mutation (value_bytes: the serialized SyncActionValue)."""
        kind = describe(collection, index, version, remove)
        if kind not in self._logged:
            self._logged.add(kind)
            log.info("app state: %s %s v%d %s", kind[0], kind[1] or "?", kind[2],
                     "remove" if kind[3] else "set")
        if not wanted(index):
            return
        key = index_key(index)
        with self._lock:
            if full_sync:
                self._full_seen.setdefault(collection, set()).add(key)
            if remove:
                self._conn.execute("DELETE FROM entries WHERE idx=?", (key,))
            else:
                self._conn.execute(
                    "INSERT OR REPLACE INTO entries(idx, name, collection, version, value, at) "
                    "VALUES(?,?,?,?,?,?)",
                    (key, index[0], collection, int(version), bytes(value_bytes),
                     int(time.time())))
            self._conn.commit()

    def full_sync_completed(self, collection):
        """A full sync of `collection` finished: drop what it did not re-send."""
        with self._lock:
            seen = self._full_seen.pop(collection, set())
            stale = [key for (key,) in self._conn.execute(
                "SELECT idx FROM entries WHERE collection=?", (collection,)) if key not in seen]
            self._conn.executemany("DELETE FROM entries WHERE idx=?", [(k,) for k in stale])
            self._conn.execute("INSERT OR REPLACE INTO seeded(collection, at) VALUES(?,?)",
                               (collection, int(time.time())))
            self._conn.commit()
        self._event(collection).set()
        if stale:
            log.info("app state: %d %s entr(ies) gone after a full sync", len(stale), collection)

    # -------------------------------------------------------------- reads
    def seeded(self, collection) -> bool:
        return self._event(collection).is_set()

    def wait_seeded(self, collection, timeout) -> bool:
        return self._event(collection).wait(timeout)

    def entries(self, name):
        """[(index list, version, value bytes)] of the index `name`."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT idx, version, value FROM entries WHERE name=? ORDER BY idx",
                (name,)).fetchall()
        return [(json.loads(key), version, value) for key, version, value in rows]


def mirror_of(s) -> Mirror:
    """The session's mirror, created on first use."""
    mirror = getattr(s, "app_state", None)
    if mirror is None:
        os.makedirs(s.folder, exist_ok=True)
        mirror = s.app_state = Mirror(os.path.join(s.folder, FILE))
    return mirror


def supported() -> bool:
    """Does the installed neonize emit AppStateMutationEv (WinZapp's build)?"""
    try:
        import neonize.events as nev
    except ImportError:
        return False
    return hasattr(nev, "AppStateMutationEv")


def ensure_seeded(s, collection, timeout=SEED_TIMEOUT) -> bool:
    """Makes the copy of `collection` complete: on the first call a full sync of
    it (the same download repair_collection does). Runs on a thread."""
    mirror = mirror_of(s)
    if mirror.seeded(collection):
        return True
    if s.client is None or s.status != "CONNECTED":
        return False
    lock = getattr(s, "_app_state_seed_lock", None)
    if lock is None:
        lock = s._app_state_seed_lock = threading.Lock()
    with lock:
        if mirror.seeded(collection):
            return True
        log.info("app state: downloading %s once to learn its lists and labels", collection)
        if not s.repair_collection(collection, force=True):
            return False
    # the events of the download reach the mirror on neonize's thread
    return mirror.wait_seeded(collection, timeout)


def send(s, collection, mutations) -> None:
    """Sends one patch of `mutations` [(index, version, SyncActionValue,
    remove)] to `collection` and, once WhatsApp accepted it, keeps them in the
    mirror (whatsmeow's echo of the patch would arrive later, on neonize's
    thread). A collection out of sync (LTHash, 409) is downloaded again and
    the patch sent once more. Raises when it is refused. Runs on a thread."""
    from neonize.proto.Neonize_pb2 import MutationInfo, PatchInfo
    kind = PatchInfo.WAPatchName.Value(collection.upper())
    patch = PatchInfo(Timestamp=time.time_ns(), Type=kind)
    for index, version, value, remove in mutations:
        mutation = MutationInfo(Index=list(index), Version=version, Value=value)
        if remove:          # a field only WinZapp's build has (neonize_patches/)
            mutation.Remove = True
        patch.Mutations.append(mutation)
    try:
        s.client.send_app_state(patch)
    except Exception as error:
        if "LTHash" not in str(error) and "409" not in str(error):
            raise
        if not s.repair_collection(collection):
            raise
        s.client.send_app_state(patch)
    mirror = mirror_of(s)
    for index, version, value, remove in mutations:
        mirror.apply(collection, list(index), version, remove, value.SerializeToString())


def register_events(cli, s):
    """Wires AppStateMutationEv of the neonize client `cli` to the session's
    mirror and sets `s.app_state_synced`, which contact_events calls when a
    full sync completes. An older neonize build has no such event: nothing is
    registered and the routes that need the mirror answer 501."""
    import neonize.events as nev

    if not hasattr(nev, "AppStateMutationEv"):
        return
    mirror = mirror_of(s)
    s.app_state_synced = mirror.full_sync_completed

    @cli.event(nev.AppStateMutationEv)
    def _mutation(c, e):
        try:
            value = e.Action.SerializeToString() if e.HasField("Action") else b""
            mirror.apply(e.Name, list(e.Index), e.Version, e.Remove, value, e.FromFullSync)
        except Exception:
            log.exception("app state mutation")
