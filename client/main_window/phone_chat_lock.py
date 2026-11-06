"""PhoneChatLockMixin — part of MainWindow (see main_window/__init__.py).

WhatsApp's own Chat Lock, set on the phone and reported by WPPConnect in each
chat record's ``isLocked`` field (core/phone_chat_lock.py has the rules). WinZapp
treats such a chat exactly like one in its own locked-chats vault: it leaves the
main list, the archived list, notifications and the tray, and appears only in the
Locked chats panel after the vault is opened. ChatLockMixin.is_chat_locked() asks
this mixin, so every place that already hides a locked chat hides these too.

The one difference: WinZapp cannot undo it. The phone's code is the only thing
that unlocks it, so Unlock is not offered for these chats (see
ChatLockMixin.unlock_chat()).

The membership set is persisted for the same reason archived_chats is: a chat
record never carries ``isLocked`` on disk, so after a restart nothing says which
chats the phone locked until the first list-chats answers. Without the persisted
set those chats would sit in the main list for that first moment. It holds keyed
JID fingerprints (core.chat_lock_vault.jid_fingerprint), never the JIDs: which
contacts someone hid behind Chat Lock must not be readable from messages.db,
the same rule as the vault's own index (ChatLockMixin._load_chat_lock_vault()).

Methods run with ``self`` bound to the MainWindow instance.
"""

import logging

import wx

from core import phone_chat_lock
from core.chat_lock_vault import jid_fingerprint
from main_window.storage_privacy import invalidate as invalidate_storage_visibility


class PhoneChatLockMixin:
    """State and lookups for chats locked with WhatsApp Chat Lock on the phone."""

    _PHONE_LOCK_KEY = "phone_lock_index_v1"

    def _phone_lock_fp(self, jid: str) -> str:
        """The keyed fingerprint the persisted set holds for *jid*, or ""."""
        return jid_fingerprint(self.key, jid) if jid else ""

    def _load_phone_locked_chats(self):
        """Read the persisted membership at startup (see sync.py)."""
        raw = self.db.get_metadata_json(self._PHONE_LOCK_KEY, [])
        self._phone_locked_chats = {
            value for value in raw
            if isinstance(value, str) and len(value) == 64
        } if isinstance(raw, list) else set()

    def _persist_phone_locked_chats(self):
        db = getattr(self, "db", None)
        if db is None:
            return
        try:
            db.set_metadata_json(self._PHONE_LOCK_KEY, sorted(self._phone_locked_chats))
        except Exception as exc:
            # The type only: nothing here should put a JID in the log.
            logging.warning("[phone_chat_lock] could not persist: %s", type(exc).__name__)

    def _phone_lock_counterpart(self, jid: str) -> str:
        """The same conversation's other JID (LID <-> phone), normalized, or ""."""
        if jid.endswith("@lid"):
            alt = getattr(self, "_lid_to_phone", {}).get(jid, "")
        else:
            alt = getattr(self, "_phone_to_lid", {}).get(jid, "")
        return self._normalize_jid(alt) if alt else ""

    def _phone_locked_in_answer(self, response_data) -> set:
        """Every name (normalized JID and its LID/phone counterpart) of a chat
        some entry of this list answer states as locked."""
        locked = set()
        for chat in response_data or ():
            if phone_chat_lock.stated_flag(chat) is not True:
                continue
            jid = self._normalize_jid(chat.get("remoteJid", ""))
            if jid:
                locked.update(n for n in (jid, self._phone_lock_counterpart(jid)) if n)
        return locked

    def _sync_phone_chat_lock(self, chat, jid: str, chats: dict,
                              absent_means_unlocked: bool = False,
                              locked_in_answer=frozenset()) -> bool:
        """Mirror a chat record's stated ``isLocked`` onto the records and the
        persisted set. True when the set changed (the caller then persists).

        Whenever the record states the flag it wins, in both directions: a chat
        the phone unlocked leaves the set instead of staying hidden for ever.
        A record that states nothing changes nothing, except in the server's
        own list answer (*absent_means_unlocked*): an unlocked chat may simply
        omit the field there, and keeping it in the set would hide it for good,
        with nothing in WinZapp able to bring it back.

        One answer can carry the same conversation twice (its @lid entry and
        its phone entry). When another entry of it is locked
        (*locked_in_answer*, from _phone_locked_in_answer()), the lock wins:
        the twin that does not say true must not unlock both names.
        """
        flag = phone_chat_lock.stated_flag(chat)
        if jid in locked_in_answer and flag is not True:
            return False
        if flag is None and absent_means_unlocked:
            flag = False
        if flag is None:
            return False
        chat["isLocked"] = flag
        counterpart = self._phone_lock_counterpart(jid)
        for name in (jid, counterpart):
            record = chats.get(name) if name else None
            if isinstance(record, dict):
                record["isLocked"] = flag
        changed = phone_chat_lock.apply_flag(
            self._phone_locked_chats,
            (self._phone_lock_fp(jid), self._phone_lock_fp(counterpart)),
            flag,
        )
        if flag:
            invalidate_storage_visibility(self, only_if_hidden=True)
        return changed

    def is_chat_phone_locked(self, jid: str) -> bool:
        """Whether the PHONE has this chat under WhatsApp Chat Lock.

        A chat can be filed under its LID or its phone JID, so every name of it
        is tried. A record that states the flag settles it; the persisted set
        decides only when the record says nothing.

        An empty set answers at once: _sync_phone_chat_lock() is the only code
        that writes ``isLocked=True`` onto a record, and it fills the set in the
        same call. is_chat_locked() runs for every chat on every list rebuild,
        so this must stay a few dict lookups, never a scan of all chats.
        """
        members = self._phone_locked_chats
        if not jid or not members:
            return False
        for candidate in self._archived_lookup_jids(jid):
            flag = phone_chat_lock.stated_flag(self.chats.get(candidate))
            if flag is not None:
                return flag
            if self._phone_lock_fp(candidate) in members:
                return True
        return False

    def has_phone_locked_chats(self) -> bool:
        """Whether any known chat is locked on the phone right now (a stale
        member that no chat backs does not count)."""
        if not self._phone_locked_chats:
            return False
        return any(self.is_chat_phone_locked(jid) for jid in list(self.chats))

    def can_unlock_chat_in_app(self, jid: str) -> bool:
        """Whether WinZapp can unlock this chat: anything its own vault holds,
        but not a chat only the phone's Chat Lock hides."""
        return self._chat_lock_vault_holds(jid) or not self.is_chat_phone_locked(jid)

    def _require_pin_for_phone_locked_chats(self) -> bool:
        """Chats the phone locked must never be shown without a secret, so the
        Locked chats panel needs a PIN even when the person never set up the
        vault. Asks whether to create one (explaining why); True once the vault
        is configured and unlocked, False if the person declines or cancels.
        Unlocked too, so the panel does not ask for the PIN just created.
        """
        answer = wx.MessageBox(
            self.i18n.t("chat_lock_phone_needs_pin"),
            self.app_name,
            wx.YES_NO | wx.YES_DEFAULT | wx.ICON_QUESTION,
            self,
        )
        if answer != wx.YES:
            return False
        return bool(self.unlock_chat_lock_settings())
