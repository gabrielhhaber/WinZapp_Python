"""TranscriptionStoreMixin — part of MainWindow (see main_window/__init__.py).

This code was written for issue #112, part 7. The mechanical merge of the
split into mixins dropped it into main_window/media.py, and it was moved from
there to here verbatim. Methods run with ``self`` bound to the MainWindow
instance, so every attribute set in MainWindow.__init__ is available here.
"""

import logging
import time
import wx
from core.transcription import errors as transcription_errors
from core.transcription import stored as stored_transcription


class TranscriptionStoreMixin:
    """Saved transcriptions of voice messages: keeping one on every in-memory
    copy of the message and in the database, and deleting it.
    """

    # ── Saved transcriptions (issue #112) ───────────────────────────────────
    # The rule that keeps a transcription across resyncs, and why deleting
    # writes a tombstone, is in core/transcription/stored.py. What lives here
    # is only which dicts hold the message and how the write is scheduled.

    def _transcription_copies(self, jid: str, msg_id: str) -> list:
        """Every in-memory dict that may be message *msg_id* of chat *jid*.

        More than one, routinely: the chat's records and its lastMessage, and
        the conversation panel's own lists — which keep the dicts of the chat
        they were built from after a resync has swapped self.chats[jid] for a
        new one. Each of them can be written back to the database by some
        later path (a star toggled, a status update, save_data()), and the
        menu reads the panel's copy, so all of them have to agree.
        """
        copies = []
        chat = self.get_chat(jid)
        if isinstance(chat, dict):
            copies.extend(((chat.get("messages") or {}).get("messages") or {}).get("records") or [])
            copies.append(chat.get("lastMessage"))
        cp = getattr(self, "conversations_panel", None)
        conversation = getattr(cp, "conversation", None)
        if (isinstance(conversation, dict)
                and self._normalize_jid(conversation.get("remoteJid", "")) == self._normalize_jid(jid)):
            copies.extend(((conversation.get("messages") or {}).get("messages") or {}).get("records") or [])
            copies.extend(getattr(cp, "_all_sorted_messages", None) or [])
            copies.extend(getattr(cp, "_sorted_messages", None) or [])
        return copies

    def store_message_transcription(self, jid: str, msg_id: str, value: dict,
                                    existing: bool = False) -> str:
        """Keep *value* as the transcription of message *msg_id* (UI thread).

        Returns one of stored_transcription's SAVE_* answers, which the flow
        turns into a note in the result window when it is not SAVE_STORED —
        or, for SAVE_WITHDRAWN, into one spoken sentence and no window
        (ui.transcription_flow).

        The message is looked up again here, by id, rather than trusted from
        the dict the flow held when the run started: a transcription takes
        minutes, and a sync may have replaced that dict since, or the send of
        an own message may have given it its real id. Memory first, on every
        copy, so the menu offers `transcription_view` at once; the database gets
        the dedicated key-only write (never insert_message() of this record,
        whose other fields may be minutes stale) on the transcription write
        queue, in one call covering every JID the conversation's rows may be
        filed under (_transcription_storage_jids()). When none of them has
        the row yet — a message that arrived live and has not been persisted
        — the record is written whole through insert_message(), whose rule
        keeps whichever decision is later. Only an actual failure of the
        database is said to the user, in one sentence: the window must not go
        on implying the text was kept when nothing was written.

        A message its sender deleted for everyone while it was being
        transcribed is refused outright (SAVE_WITHDRAWN): _apply_remote_revoke()
        turns the record into a protocolMessage in place, so find_record()
        still finds it, and the text is exactly what that sender withdrew.
        The database refuses it too (_rewrite_transcription()), for a revoke
        that reaches the disk before it reaches this dict.
        """
        copies = self._transcription_copies(jid, msg_id)
        current = stored_transcription.find_record(copies, msg_id)
        if current is None:
            return stored_transcription.SAVE_MISSING
        if stored_transcription.is_withdrawn(current):
            return stored_transcription.SAVE_WITHDRAWN
        if stored_transcription.is_unsent(current):
            return stored_transcription.SAVE_UNSENT
        # After the three answers above, which outrank it: a withdrawn message
        # must never have its text shown whatever the cache says. `existing`:
        # a text already kept and only marked again (into the line) — the
        # cache switches decide about keeping new ones.
        if not existing and not stored_transcription.may_cache(self.settings, value):
            return stored_transcription.SAVE_DISABLED
        real_id = (current.get("key") or {}).get("id", "")
        if real_id != msg_id:
            # Sent while it was being transcribed: the copies now carry the
            # real id, and that is the one the row is stored under.
            copies = self._transcription_copies(jid, real_id)
        # Dated after anything these copies hold: the flow dated it with the
        # wall clock minutes ago, and a clock set back since would make this
        # newer decision lose to the one it replaces.
        at = stored_transcription.decision_time(value)
        later = stored_transcription.next_decision_time(
            at if at is not None else time.time(),
            *(c.get(stored_transcription.TRANSCRIPTION_KEY) for c in copies
              if isinstance(c, dict) and (c.get("key") or {}).get("id") == real_id),
        )
        if later != at:
            value = dict(value, at=later)
        stored_transcription.set_on_copies(copies, real_id, value)
        db = getattr(self, "db", None)
        if db is not None:
            # Taken here, on the UI thread, already carrying the value: the
            # fallback below writes this snapshot, never a dict still in use.
            record = dict(current)
            storage_jids = self._transcription_storage_jids(jid)

            def _bg_persist():
                # Nothing about the message in these lines — not its id either.
                try:
                    if not db.set_message_transcription(storage_jids, real_id, value):
                        db.insert_message(jid, record)
                        # The missing row is the database's own line; this
                        # one says only what was done about it.
                        logging.info("[transcription] fell back to writing the message whole")
                except Exception as exc:
                    logging.warning("[transcription] storing a transcription failed: %s",
                                    type(exc).__name__)
                    wx.CallAfter(self._say_transcription_not_stored)
            self._transcription_write_queue.submit(_bg_persist)
        self._schedule_save(dirty_jid=jid)
        return stored_transcription.SAVE_STORED

    def _say_transcription_not_stored(self):
        """The one sentence for a transcription the database refused (UI thread).

        Said over whatever is on screen — usually the result window, which
        opened before the background write answered — without interrupting
        it: the text is still there to read, and the copies in memory still
        carry it, but nothing promises it will be there next time.
        """
        if hasattr(self, "error_sound"):
            # Guarded, as every sound on an error path must be
            # (docs/traps/audio-devices.md): raising here would swallow the
            # sentence, and it is the only news that the text was not kept.
            try:
                self.error_sound.play()
            except Exception as exc:
                logging.warning("[transcription] could not play the error sound: %s",
                                transcription_errors.exception_report(exc))
        self.output(self.i18n.t("transcription_store_failed"))

    def _transcription_storage_jids(self, jid: str) -> list:
        """The chat JIDs the rows of conversation *jid* may be stored under.

        The conversation's own, and the @lid its older history can still be
        filed under — the same lookup ConversationsPanel._history_storage_jid()
        makes to read that history back. A message loaded from there lives in
        the database under the @lid, so a write keyed on the phone JID alone
        finds no row, and the transcription would reach the disk only if some
        later save happened to carry it.
        """
        jids = [jid]
        lid = (getattr(self, "_phone_to_lid", None) or {}).get(jid, "")
        if lid and lid != jid:
            jids.append(lid)
        return jids

    def delete_message_transcription(self, jid: str, msg_id: str, on_done) -> None:
        """Delete message *msg_id*'s transcription; call *on_done(ok)* on the
        UI thread once the database has answered.

        The database first and memory after, the reverse of storing: saying
        "deleted" while the disk still holds the text would be false, and it
        is the disk that outlives the session. *on_done* runs only once every
        copy on disk has the tombstone: the message's row under each JID the
        conversation may be stored under — one call, one lock hold — and the
        chat's preview, which never holds the text
        (DatabaseManager._build_chat_values()) and is scrubbed by the same
        write if an older one did. A row that does not exist is not a failure
        — nothing is left holding the text, which is what the user asked for.
        Queued behind any save still waiting for the database, never beside
        it: see _transcription_write_queue in __init__. When a newer
        transcription took the deletion's place while it waited, *on_done*
        is not called at all — there is nothing true to say about a deletion
        the user has already superseded.
        """
        copies = self._transcription_copies(jid, msg_id)
        # After whatever is stored, whatever the clock says — see
        # store_message_transcription(); a delete dated before the text it
        # deletes is one the next sync would undo.
        deleted_at = stored_transcription.next_decision_time(
            time.time(),
            *(c.get(stored_transcription.TRANSCRIPTION_KEY) for c in copies
              if isinstance(c, dict) and (c.get("key") or {}).get("id") == msg_id),
        )
        value = stored_transcription.tombstone(deleted_at)
        # A video can hold a description and a transcription: deleting one
        # keeps the other (None: nothing known in memory, everything goes).
        # The value is then the newest copy without that category — dated
        # `deleted_at` like the tombstone, so everything below (a later
        # decision wins, the copies are brought to the newest) applies as is;
        # a tombstone when nothing else is left.
        newest_held = None
        for copy in copies:
            if isinstance(copy, dict) and (copy.get("key") or {}).get("id") == msg_id:
                newest_held = stored_transcription.newer_decision(
                    copy.get(stored_transcription.TRANSCRIPTION_KEY), newest_held)
        description = stored_transcription.delete_target(newest_held)
        if description is not None:
            value, _ = stored_transcription.drop_kind(newest_held, description, deleted_at)
        db = getattr(self, "db", None)
        storage_jids = self._transcription_storage_jids(jid)

        def _finish(ok):
            if ok:
                # Only where the tombstone is still the latest decision: this
                # delete may have waited in the queue behind a
                # `transcription_transcribe_again` whose text is already on the copies (and on disk,
                # written after the tombstone). Overwriting it would put the
                # older decision back in memory under the new result window.
                current_copies = self._transcription_copies(jid, msg_id)
                accepted, found = stored_transcription.set_where_newer(
                    current_copies, msg_id, value
                )
                if accepted:
                    self._schedule_save(dirty_jid=jid)
                if found > accepted:
                    # Saying "deleted" now would contradict the transcription
                    # the user is looking at; the later decision is the answer.
                    # Any copy refusing it is enough, not only all of them: a
                    # copy reloaded from the database can carry the newer text
                    # while another still holds the old one, and "apagada"
                    # would then be said over text that is still there.
                    #
                    # Nor can the copies that took the tombstone keep it: the
                    # disk holds the newer decision, so memory would disagree
                    # with it — some copies deleted, one with text — until the
                    # next reload. Not because a tombstone can lose on disk
                    # (_rewrite_transcription() lets it win over whatever the
                    # row holds), but because of when it was written: it is
                    # dated after every copy memory held when the delete was
                    # decided, so any decision newer than it was made and
                    # queued after it, and the write queue has one thread —
                    # that decision reached the disk after the tombstone and
                    # replaced it there.
                    # The newest decision any copy holds goes on all of them.
                    newest = value
                    for copy in current_copies:
                        if isinstance(copy, dict) and (copy.get("key") or {}).get("id") == msg_id:
                            newest = stored_transcription.newer_decision(
                                copy.get(stored_transcription.TRANSCRIPTION_KEY), newest
                            )
                    stored_transcription.set_on_copies(current_copies, msg_id, newest)
                    logging.info("[transcription] a later transcription replaced the deleted "
                                 "one while the delete was queued: %d of %d copy(ies) kept it",
                                 found - accepted, found)
                    return
            on_done(ok)

        def _bg_delete():
            ok = db is not None
            if ok:
                try:
                    if description is None:
                        db.delete_message_transcription(storage_jids, msg_id, deleted_at)
                    else:
                        db.delete_message_transcription(
                            storage_jids, msg_id, deleted_at, description)
                except Exception as exc:
                    ok = False
                    logging.warning("[transcription] deleting a transcription failed: %s",
                                    type(exc).__name__)
            wx.CallAfter(_finish, ok)

        self._transcription_write_queue.submit(_bg_delete)

    def _all_transcription_copies(self) -> list:
        """Every in-memory dict that may hold a stored transcription, in any chat."""
        copies = []
        for chat in list((getattr(self, "chats", None) or {}).values()):
            if not isinstance(chat, dict):
                continue
            copies.extend(((chat.get("messages") or {}).get("messages") or {}).get("records") or [])
            copies.append(chat.get("lastMessage"))
        cp = getattr(self, "conversations_panel", None)
        conversation = getattr(cp, "conversation", None)
        if isinstance(conversation, dict):
            copies.extend(((conversation.get("messages") or {}).get("messages") or {}).get("records") or [])
            copies.extend(getattr(cp, "_all_sorted_messages", None) or [])
            copies.extend(getattr(cp, "_sorted_messages", None) or [])
        return copies

    def clear_model_transcriptions(self, model_id: str, backend_id, on_done) -> None:
        """Delete every saved transcription made by one model, in every chat;
        call *on_done(count)* on the UI thread once the database has answered
        (count is None when it failed).

        The database first and memory after, like delete_message_transcription():
        "cleared" must be true of the disk. Queued behind any save still waiting
        for the database, on the same single-thread queue. Memory is then
        updated on every copy of every chat, and the open conversation repaints
        the rows that changed.
        """
        self._clear_transcriptions(
            lambda db, decided_at: db.clear_model_transcriptions(model_id, backend_id, decided_at),
            lambda copies, decided_at, present: stored_transcription.drop_model_on_copies(
                copies, model_id, backend_id, decided_at, present),
            on_done,
        )

    def clear_all_transcriptions(self, on_done) -> None:
        """clear_model_transcriptions() for every model at once."""
        self._clear_transcriptions(
            lambda db, decided_at: db.clear_all_transcriptions(decided_at),
            stored_transcription.drop_all_on_copies,
            on_done,
        )

    def _clear_transcriptions(self, clear_on_disk, clear_in_memory, on_done) -> None:
        db = getattr(self, "db", None)
        decided_at = time.time()
        # What memory holds now: a result stored while the clear waits in the
        # queue is saved after it, and must not be cleared in memory either.
        present = stored_transcription.values_on(self._all_transcription_copies())

        def _finish(count):
            if count is None:
                on_done(None)
                return
            changed = clear_in_memory(self._all_transcription_copies(), decided_at, present)
            if changed:
                # No save: the database already holds the cleared state.
                cp = getattr(self, "conversations_panel", None)
                repaint = getattr(cp, "_repaint_or_repopulate", None)
                if callable(repaint) and getattr(cp, "conversation", None):
                    try:
                        repaint(changed)
                    except Exception as exc:
                        logging.warning("[transcription] repainting after clearing failed: %s",
                                        type(exc).__name__)
            on_done(count)

        def _bg_clear():
            count = None
            if db is not None:
                try:
                    count = clear_on_disk(db, decided_at)
                except Exception as exc:
                    logging.warning("[transcription] clearing saved transcriptions failed: %s",
                                    type(exc).__name__)
            wx.CallAfter(_finish, count)

        self._transcription_write_queue.submit(_bg_clear)
