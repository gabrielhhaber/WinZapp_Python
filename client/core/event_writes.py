"""Coalesce UI event persistence on the database loop, with close/reset fences."""
import asyncio
from copy import deepcopy
from itertools import count

from core.latest_metadata import LatestMetadataWrite
from core.event_write_storage import apply_status_batch, apply_contact_batch


class EventWritesMixin:
    def _init_event_writes(self):
        self._event_sequence = count()
        self._event_writes = LatestMetadataWrite(
            self._persist_event_batch, merge=lambda older, newer: older | newer,
            label="event-writes")

    def queue_message(self, jid, message):
        self._queue_event_write({("message", jid, message["key"]["id"]): message})

    def queue_message_status(self, jid, message):
        self._queue_event_write({("status", jid, message["key"]["id"]):
                                 message["MessageUpdate"][-1]})

    def queue_contacts(self, contacts, previous):
        # Retain ordered before/after observations, so a newer direct local
        # edit can refuse an obsolete observation under the database lock.
        self._queue_event_write({("contact", next(self._event_sequence), jid):
                                 (previous.get(jid), record)
                                 for jid, record in contacts.items()})

    def queue_metadata(self, key, value):
        self._queue_event_write({("metadata", key): value})

    def _queue_event_write(self, changes):
        # Admission and runner accounting use the same close gate as _call().
        from core.database_bridge import DatabaseBridgeClosed, DatabaseBridgeTimeout

        with self._state_lock:
            if self._closing:
                raise DatabaseBridgeClosed("DatabaseBridge is closing")
            if not self._thread.is_alive():
                raise DatabaseBridgeTimeout("db-asyncio thread is not running")
            lease = self._event_writes.offer(deepcopy(changes))
            if lease is None:
                return
            self._inflight += 1
            self._inflight_drained.clear()
        coro = self._run_event_writes(lease)
        try:
            asyncio.run_coroutine_threadsafe(coro, self._loop)
        except Exception:
            coro.close()
            self._event_writes.abort_start(lease)
            self._presence_write_finished()
            raise

    async def _run_event_writes(self, lease):
        try:
            await self._event_writes.run(lease)
        finally:
            self._presence_write_finished()

    async def _persist_event_batch(self, changes):
        messages, contacts, statuses = {}, [], []
        for key, value in changes.items():
            if key[0] == "message":
                messages.setdefault(key[1], []).append(value)
            elif key[0] == "contact":
                contacts.append((key[2], *value))
            elif key[0] == "status":
                statuses.append((key[1], key[2], value))
        for jid, records in messages.items():
            await self._db.insert_messages_batch(jid, records)
        if contacts:
            await apply_contact_batch(self._db, contacts)
        if statuses:
            await apply_status_batch(self._db, statuses)
        for key, value in changes.items():
            if key[0] == "metadata":
                await self._db.set_metadata_json(key[1], value)
