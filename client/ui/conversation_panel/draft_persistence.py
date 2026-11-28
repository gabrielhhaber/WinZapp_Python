"""Unsent drafts survive a restart (core/message_drafts.py has the format).

The drafts of the conversations left behind live in the MessageDrafts store;
the one in the open conversation lives in the message field. Both go into one
Fernet token in the account's database, so closing WinZapp, an automatic
update or a crash costs at most the last second of typing. The write is
queued on the database loop (DatabaseBridge.queue_message_drafts): typing
never waits for SQLite, and a failure is only logged, by exception type, never
with a word of the text.

A locked conversation's draft is saved like any other, inside the same opaque
token, so nothing on disk tells that it exists. It comes back through take(),
which only runs once the vault has let that conversation open.

The text of the open conversation is cached when the field changes
(`_note_draft_changed`), so the shutdown flush, which may not run on the wx
thread, never reads a control.
"""

import logging

import wx

from core import message_drafts
from core.message_drafts import Draft

_SAVE_DELAY_MS = 1000


class DraftPersistenceMixin:
    """Keeps the drafts on disk, encrypted. Part of ConversationsPanel."""

    def _load_persisted_drafts(self, store) -> None:
        """Called once, when the store is created: put the drafts of the last
        run in it. With the option off, what an earlier run left is erased.
        Never raises: unreadable data is no drafts."""
        self._persisted_payload = ""
        self._draft_live = None
        self._draft_timer = None
        mw = self.main_window
        db, key = getattr(mw, "db", None), getattr(mw, "key", None)
        if db is None or not key:
            return
        try:
            token = db.get_metadata(message_drafts.METADATA_KEY)
        except Exception as exc:
            logging.warning("[drafts] could not read: %s", type(exc).__name__)
            return
        if not message_drafts.persist_enabled(getattr(mw, "settings", None)):
            # None: whatever is stored differs from "nothing", so the next
            # persist writes the erasure.
            self._persisted_payload = None if token else ""
            return
        loaded = message_drafts.decode(token, key)
        store.load(loaded)
        self._persisted_payload = message_drafts.to_payload(loaded)

    def _draft_snapshot(self) -> dict:
        """The kept drafts plus the open conversation's, {jid: Draft}."""
        drafts = self._message_drafts().snapshot()
        live = getattr(self, "_draft_live", None)
        if live and getattr(self, "_editing_message_id", None) is None:
            jid, text = live
            if jid and text.strip():
                drafts[jid] = Draft(
                    text, list(getattr(self, "_pending_mentions", ()) or ()),
                    dict(getattr(self, "_pending_mention_display_names", None) or {}))
        return drafts

    def _persist_message_drafts(self) -> None:
        """Queue the current drafts for the database, if they changed."""
        try:
            mw = self.main_window
            db, key = getattr(mw, "db", None), getattr(mw, "key", None)
            if db is None or not key or getattr(self, "_message_drafts_store", None) is None:
                return
            enabled = message_drafts.persist_enabled(getattr(mw, "settings", None))
            payload = message_drafts.to_payload(self._draft_snapshot()) if enabled else ""
            if payload == getattr(self, "_persisted_payload", None):
                return
            db.queue_message_drafts(message_drafts.seal(payload, key))
            self._persisted_payload = payload
        except Exception as exc:
            logging.warning("[drafts] could not save: %s", type(exc).__name__)

    def _note_draft_changed(self, text: str) -> None:
        """The message field changed (on_change_message_field): remember what
        it holds and save shortly after typing pauses."""
        if getattr(self, "_message_drafts_store", None) is None:
            return
        key = self._draft_key(self.conversation)
        self._draft_live = (key, text) if key else None
        if not message_drafts.persist_enabled(getattr(self.main_window, "settings", None)):
            return
        timer = getattr(self, "_draft_timer", None)
        try:
            if timer is not None:
                timer.Restart(_SAVE_DELAY_MS)
            else:
                self._draft_timer = wx.CallLater(_SAVE_DELAY_MS, self._persist_message_drafts)
        except Exception:
            self._draft_timer = None

    def _set_live_draft(self, key: str, text: str) -> None:
        """A draft was put in the field by restore: it is the live one."""
        self._draft_live = (key, text) if key else None

    def forget_message_draft(self, jid: str) -> None:
        """The conversation was deleted: its draft goes, on disk too."""
        key = self._draft_key({"remoteJid": jid})
        if not key or getattr(self, "_message_drafts_store", None) is None:
            return
        self._message_drafts().take(key, self._same_chat)
        live = getattr(self, "_draft_live", None)
        if live and (live[0] == key or self._same_chat(live[0], key)):
            self._draft_live = None
        self._persist_message_drafts()

    def discard_message_drafts(self) -> None:
        """Logout or account switch: no draft of the old account survives.
        The table is wiped by the caller; this empties the memory side."""
        store = getattr(self, "_message_drafts_store", None)
        if store is not None:
            store.clear()
        self._draft_live = None
        self._persisted_payload = ""

    def flush_message_drafts(self) -> None:
        """Shutdown: save now what a pending timer would have saved."""
        timer = getattr(self, "_draft_timer", None)
        if timer is not None:
            try:
                timer.Stop()
            except Exception:
                pass
            self._draft_timer = None
        self._persist_message_drafts()
