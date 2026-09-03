"""Regression tests for a batch of reported bugs fixed together:

1. Archived conversations must not inflate the window-title unread count
   (MainWindow._update_title()/get_archived_unread_count()).
2. The official WhatsApp system account ("0@s.whatsapp.net") must display
   as "WhatsApp" instead of "+0" (MainWindow._resolve_contact_name()).
3. A message whose text body is actually a raw base64 image blob (observed
   live from that same official account) must never be rendered verbatim —
   neither in the chat-list preview (MainWindow._last_msg_preview()), the
   open conversation (ConversationsPanel._get_message_content()), nor a
   toast notification (notification_manager.format_notification_body()/
   _extract_msg_text()).
4. Reacting to your own message must not leave a raw reactionMessage record
   as the chat-list "last message" fallback — _track_last_reaction() must
   be reachable the same way for an own reaction as for a received one.
"""

import pytest

from main import MainWindow
from core.utils import looks_like_binary_blob
from core.notification_manager import format_notification_body, _extract_msg_text

_BLOB = (
    "/9j/4AAQSkZJRgABAgAAAQABAAD/7gAhQWRvYmUAZMAAAAABAwAQAwIDBgAABN4AAAnfAAAP1P"
    "/bAIQAFBERGhIaKRgYKTMnICczLygmJigvPzY2NjY2P0dDQ0NDQ0NHR0dHR0dHR0dHR0dHR0dH"
)


class _FakeI18n:
    _STRINGS = {
        "notif_unsupported": "[unsupported]",
        "unsupported_message": "[unsupported:{app_name}]",
        "reaction_preview_you": "voce reagiu com {emoji} a:",
        "reaction_preview_them": "{name} reagiu com {emoji} a:",
        "datetime_fmt": "%d/%m/%Y %H:%M",
    }

    def t(self, key):
        return self._STRINGS.get(key, f"[{key}]")


class _MainWindowStub:
    """Minimal stand-in carrying only what the methods under test touch."""

    def __init__(self, **kwargs):
        self.chats = {}
        self._deleted_chats = set()
        self._archived_chats = set()
        self.i18n = _FakeI18n()
        for key, value in kwargs.items():
            setattr(self, key, value)

    is_chat_archived           = MainWindow.is_chat_archived
    # is_chat_archived() now resolves the same record and the same
    # _archived_chats entry the Archived tab's own builder uses, instead of
    # trusting a single normalized key — see tests/test_archived_chat_announcements.py.
    _archived_lookup_jids      = MainWindow._archived_lookup_jids
    _chat_entry_for_archive    = MainWindow._chat_entry_for_archive
    _chat_archive_flag         = staticmethod(MainWindow._chat_archive_flag)
    get_archived_unread_count  = MainWindow.get_archived_unread_count
    _normalize_jid             = staticmethod(MainWindow._normalize_jid)
    _resolve_contact_name      = MainWindow._resolve_contact_name
    _last_msg_preview          = MainWindow._last_msg_preview
    _track_last_reaction       = MainWindow._track_last_reaction
    _reconstruct_last_reactions_from_records = MainWindow._reconstruct_last_reactions_from_records
    _counts_as_last_message    = classmethod(MainWindow._counts_as_last_message.__func__)
    _PREVIEW_MESSAGE_TYPES     = MainWindow._PREVIEW_MESSAGE_TYPES

    def _get_contact_tolerant(self, jid):
        return None


def _chat(jid, unread=0, archived=False):
    # effective_unread_count() only trusts "unreadCount" once at least one
    # local message record exists for the chat (see tests/test_unread_count.py)
    # — an empty stub chat would always compute to 0 regardless of unread.
    records = [{"key": {"id": "1"}}] if unread else []
    return {
        "remoteJid": jid,
        "unreadCount": unread,
        "archive": archived,
        "messages": {"messages": {"records": records}},
    }


class TestArchivedChatsExcludedFromMainCount:
    def test_archived_unread_chat_does_not_count_toward_get_archived_is_separate(self):
        mw = _MainWindowStub()
        mw.chats = {
            "a@s.whatsapp.net": _chat("a@s.whatsapp.net", unread=3, archived=False),
            "b@s.whatsapp.net": _chat("b@s.whatsapp.net", unread=5, archived=True),
        }
        assert mw.get_archived_unread_count() == 1

    def test_no_archived_unread_chats_is_zero(self):
        mw = _MainWindowStub()
        mw.chats = {
            "a@s.whatsapp.net": _chat("a@s.whatsapp.net", unread=3, archived=False),
        }
        assert mw.get_archived_unread_count() == 0

    def test_deleted_archived_chat_is_not_counted(self):
        mw = _MainWindowStub()
        mw.chats = {
            "a@s.whatsapp.net": _chat("a@s.whatsapp.net", unread=5, archived=True),
        }
        mw._deleted_chats = {"a@s.whatsapp.net"}
        assert mw.get_archived_unread_count() == 0

    def test_multiple_archived_unread_chats_are_all_counted(self):
        mw = _MainWindowStub()
        mw.chats = {
            "a@s.whatsapp.net": _chat("a@s.whatsapp.net", unread=1, archived=True),
            "b@s.whatsapp.net": _chat("b@s.whatsapp.net", unread=2, archived=True),
            "c@s.whatsapp.net": _chat("c@s.whatsapp.net", unread=0, archived=True),
        }
        assert mw.get_archived_unread_count() == 2


class TestOfficialWhatsAppAccountName:
    def test_local_part_zero_resolves_to_whatsapp(self):
        mw = _MainWindowStub()
        assert mw._resolve_contact_name({"remoteJid": "0@s.whatsapp.net"}) == "WhatsApp"

    def test_local_part_zero_c_us_form_also_resolves(self):
        mw = _MainWindowStub()
        assert mw._resolve_contact_name({"remoteJid": "0@c.us"}) == "WhatsApp"

    def test_a_real_phone_jid_is_unaffected(self):
        mw = _MainWindowStub()
        # No contact/db/etc. wired up — falls through every lookup and
        # returns None, same as before this fix for any ordinary contact.
        assert mw._resolve_contact_name({"remoteJid": "5511999999999@s.whatsapp.net"}) is None

    def test_phonebook_name_wins_over_stale_lid_name(self):
        lid = "83069516128367@lid"
        phone = "557791074215@s.whatsapp.net"
        contacts = {
            lid: {"name": "Joao"},
            phone: {"name": "Smart JmS"},
        }
        mw = _MainWindowStub(
            _lid_to_phone={lid: phone},
            _phone_to_lid={phone: lid},
            _presence_pushname_map={},
        )
        mw._get_contact_tolerant = contacts.get
        mw._is_bad_contact_name = lambda value: not bool(value and value.strip())

        assert mw._resolve_contact_name({"remoteJid": lid}) == "Smart JmS"


class TestBinaryBlobNeverShownAsMessageText:
    def test_looks_like_binary_blob_detects_the_reported_payload(self):
        assert looks_like_binary_blob(_BLOB) is True
        assert looks_like_binary_blob("Oi, tudo bem?") is False

    def test_chat_list_preview_falls_back_for_conversation_type(self):
        mw = _MainWindowStub()
        chat = {
            "remoteJid": "0@s.whatsapp.net",
            "messages": {"messages": {"records": [
                {
                    "key": {"id": "1", "fromMe": False},
                    "messageType": "conversation",
                    "message": {"conversation": _BLOB},
                    "messageTimestamp": 1700000000,
                },
            ]}},
        }
        preview = mw._last_msg_preview(chat)
        assert _BLOB not in preview
        assert "[unsupported]" in preview

    def test_chat_list_preview_falls_back_for_extended_text_type(self):
        mw = _MainWindowStub()
        chat = {
            "remoteJid": "0@s.whatsapp.net",
            "messages": {"messages": {"records": [
                {
                    "key": {"id": "1", "fromMe": False},
                    "messageType": "extendedTextMessage",
                    "message": {"extendedTextMessage": {"text": _BLOB}},
                    "messageTimestamp": 1700000000,
                },
            ]}},
        }
        preview = mw._last_msg_preview(chat)
        assert _BLOB not in preview

    def test_normal_text_is_unaffected(self):
        mw = _MainWindowStub()
        chat = {
            "remoteJid": "5511999999999@s.whatsapp.net",
            "messages": {"messages": {"records": [
                {
                    "key": {"id": "1", "fromMe": False},
                    "messageType": "conversation",
                    "message": {"conversation": "Oi!"},
                    "messageTimestamp": 1700000000,
                },
            ]}},
        }
        assert "Oi!" in mw._last_msg_preview(chat)

    def test_notification_body_falls_back_for_blob_conversation(self):
        msg = {"messageType": "conversation", "message": {"conversation": _BLOB}, "key": {}}
        assert format_notification_body(msg, None, _FakeI18n()) == "[unsupported]"

    def test_notification_body_falls_back_for_blob_extended_text(self):
        msg = {
            "messageType": "extendedTextMessage",
            "message": {"extendedTextMessage": {"text": _BLOB}},
            "key": {},
        }
        assert format_notification_body(msg, None, _FakeI18n()) == "[unsupported]"

    def test_extract_msg_text_ignores_a_blob_body(self):
        msg = {"message": {"conversation": _BLOB}}
        assert _extract_msg_text(msg) == ""


class TestOwnReactionUsesTheSharedPreviewChannel:
    def test_track_last_reaction_records_an_own_reaction(self):
        mw = _MainWindowStub()
        jid = "5511999999999@s.whatsapp.net"
        mw.chats = {jid: {"remoteJid": jid}}
        reaction_record = {
            "messageType": "reactionMessage",
            "message": {
                "reactionMessage": {"key": {"id": "orig1"}, "text": "👍"},
            },
            "key": {"remoteJid": jid, "fromMe": True, "id": "_rxn_orig1"},
            "messageTimestamp": 1700000000,
        }
        mw._track_last_reaction(jid, reaction_record)
        last_reaction = mw.chats[jid]["_last_reaction"]
        assert last_reaction["emoji"] == "👍"
        assert last_reaction["from_me"] is True
        assert last_reaction["target_id"] == "orig1"

    def test_last_msg_preview_prefers_the_reaction_over_the_raw_record(self):
        """Guards the actual reported bug: after _on_own_reaction_sent() both
        appends the reactionMessage record to `records` (needed so the inline
        reaction marker survives a reopen) AND calls _track_last_reaction()
        (this fix), the chat-list preview must show the "you reacted" text,
        not fall through to formatting the raw reactionMessage record — which
        has no case in _last_msg_preview() and used to render as literally
        "mensagem incompatível"."""
        mw = _MainWindowStub()
        jid = "5511999999999@s.whatsapp.net"
        original = {
            "key": {"id": "orig1", "fromMe": False},
            "messageType": "conversation",
            "message": {"conversation": "Chegando em 10 min"},
            "messageTimestamp": 1700000000,
        }
        reaction_record = {
            "messageType": "reactionMessage",
            "message": {
                "reactionMessage": {"key": {"id": "orig1"}, "text": "👍"},
            },
            "key": {"remoteJid": jid, "fromMe": True, "id": "_rxn_orig1"},
            "messageTimestamp": 1700000100,
        }
        chat = {
            "remoteJid": jid,
            "messages": {"messages": {"records": [original, reaction_record]}},
        }
        mw.chats = {jid: chat}
        mw._track_last_reaction(jid, reaction_record)

        preview = mw._last_msg_preview(chat)
        assert "[unsupported]" not in preview
        assert "👍" in preview
