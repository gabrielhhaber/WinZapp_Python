"""Tests for sound and TTS behavior when an archived conversation is open and focused."""

import sys
import threading
import time
import types
import unittest
from unittest.mock import MagicMock

# Conditionally stub GUI/audio dependencies only if not installed (headless environments)
try:
    import wx
    import wx.adv
except ImportError:
    for _mod in (
        "wx",
        "wx.adv",
    ):
        if _mod not in sys.modules:
            mod = types.ModuleType(_mod)
            if "." not in _mod:
                mod.__path__ = []
            sys.modules[_mod] = mod

    class _FakeWxModule(types.ModuleType):
        def __getattr__(self, name):
            if name == "__file__":
                return "<fake_wx>"
            if name == "CallAfter":
                return lambda fn, *a, **k: fn(*a, **k)
            if name.startswith("ID_") or name.startswith("wxID_") or name in ("HORIZONTAL", "VERTICAL", "EXPAND", "ALL"):
                return 1000
            if name in ("Frame", "Panel", "Dialog", "Accessible", "Timer", "App", "Window", "Control"):
                return object
            return MagicMock

    sys.modules["wx"].__class__ = _FakeWxModule
    sys.modules["wx.adv"].__class__ = _FakeWxModule

try:
    import accessible_output2
    from accessible_output2 import outputs
except ImportError:
    if "accessible_output2" not in sys.modules:
        sys.modules["accessible_output2"] = types.ModuleType("accessible_output2")
    sys.modules["accessible_output2.outputs"] = types.ModuleType("accessible_output2.outputs")
    sys.modules["accessible_output2"].outputs = sys.modules["accessible_output2.outputs"]

try:
    import sound_lib
    from sound_lib import stream, output, main, effects
except ImportError:
    for _mod in (
        "sound_lib",
        "sound_lib.output",
        "sound_lib.stream",
        "sound_lib.main",
        "sound_lib.effects",
    ):
        if _mod not in sys.modules:
            mod = types.ModuleType(_mod)
            if "." not in _mod:
                mod.__path__ = []
            sys.modules[_mod] = mod

    sys.modules["sound_lib.main"].bass_call = lambda *a, **k: None
    sys.modules["sound_lib.stream"].FileStream = object
    sys.modules["sound_lib.output"].Output = object
    sys.modules["sound_lib.effects"].Tempo = object

from main import MainWindow


class _FakeI18n:
    def t(self, key):
        strings = {
            "fg_new_msg": "Nova mensagem de {name}",
            "notif_reaction": "Reagiu com {emoji}",
            "notif_reaction_to_own": "Reagiu com {emoji} a: {text}",
        }
        return strings.get(key, f"[{key}]")


class _FakeSound:
    def __init__(self):
        self.play_count = 0

    def play(self):
        self.play_count += 1


class _FakeConversationsPanel:
    def __init__(self, remote_jid=""):
        self.conversation = {"remoteJid": remote_jid} if remote_jid else None

    def on_incoming_message(self, remote_jid, msg):
        pass

    def _matches_open_conversation(self, remote_jid):
        if not self.conversation:
            return False
        return self.conversation.get("remoteJid") == remote_jid


class _FakeNotificationManager:
    def __init__(self):
        self.sent = []

    def send(self, title, body, remote_jid, msg_key=None):
        self.sent.append((title, body, remote_jid))


class _StubMainWindow:
    """Minimal stub of MainWindow for testing on_new_message and _maybe_notify_reaction."""

    on_new_message = MainWindow.on_new_message
    # Real methods, not invented answers: a message that is not a reply
    # (and not a placeholder) returns from them before touching anything.
    _recover_quoted_placeholder = MainWindow._recover_quoted_placeholder
    _recover_placeholders_from_replies = MainWindow._recover_placeholders_from_replies
    _drop_protocol_edit = MainWindow._drop_protocol_edit
    _maybe_notify_reaction = MainWindow._maybe_notify_reaction
    _normalize_jid = staticmethod(MainWindow._normalize_jid)
    _is_self_jid = MainWindow._is_self_jid
    _phone_digits_equivalent = staticmethod(MainWindow._phone_digits_equivalent)
    _is_reply_or_mention_of_me = MainWindow._is_reply_or_mention_of_me

    def __init__(
        self, *, open_jid="", archived_jids=None, muted_jids=None,
        locked_jids=None, window_active=True,
    ):
        self.my_jid = "5511999998888@s.whatsapp.net"
        self.my_lid = ""
        self.chats = {}
        self.contacts = {}
        self.db = MagicMock()
        self._lid_to_phone = {}
        self._phone_to_lid = {}
        self._pending_lid_inserts = {}
        self._own_sent_ids = set()
        self._own_sent_ids_lock = threading.Lock()
        self._archived_chats = set(archived_jids or [])
        self._muted_chats = set(muted_jids or [])
        self._locked_chats = set(locked_jids or [])
        self._deleted_chats = set()
        self._window_active = window_active
        self._window_hidden = not window_active
        self.settings = {
            "general": {
                "notifications_enabled": True,
                "show_tray_icon": True,
                "keep_muted_chats_silent_when_open": True,
            },
            "speech_content": {
                "speak_active_conv_messages": True,
                "speak_other_conv_messages": True,
            },
        }
        self.i18n = _FakeI18n()
        self.message_current_sound = _FakeSound()
        self.message_foreground_sound = _FakeSound()
        self.spoken = []
        self.read_marked = []
        self._last_activation_time = 0
        self.conversations_panel = _FakeConversationsPanel(open_jid)
        self.notification_manager = _FakeNotificationManager()
        self._msg_bg_executor = MagicMock()
        self.ws = MagicMock()
        self.ws._connect_time = time.time()

    def _apply_group_subject_change(self, *a, **k):
        pass

    def _refresh_mention_cache_on_membership_change(self, *a, **k):
        pass

    def _is_cleared_message(self, *a, **k):
        return False

    def apply_forwarded_duration(self, *a, **k):
        pass

    def _schedule_save(self, *a, **k):
        pass

    def IsShown(self):
        return self._window_active

    def IsIconized(self):
        return not self._window_active

    def IsActive(self):
        return self._window_active

    def is_chat_locked(self, jid):
        return jid in self._locked_chats

    def is_chat_archived(self, jid):
        return jid in self._archived_chats

    def is_chat_muted(self, jid):
        return jid in self._muted_chats

    def output(self, text, interrupt=False):
        self.spoken.append(text)

    def mark_conversation_as_read(self, remote_jid, val):
        self.read_marked.append((remote_jid, val))

    def _live_events_ready(self):
        return True

    def _learn_sender_name(self, msg):
        return False

    def _extract_lid_mapping(self, msg):
        pass

    def _apply_group_settings_change(self, remote_jid, chat, msg):
        pass

    def _redirect_self_chat_artifact(self, remote_jid, key, from_me):
        # on_new_message() calls this to catch a fake self-chat sync artifact
        # (a @g.us JID built from a participant's own @lid digits). Irrelevant
        # to the archived-chat sound/TTS decision under test here, so it is a
        # pure passthrough — same as tests/test_duplicate_message_echo_matching.py.
        return remote_jid, from_me

    def _is_chat_empty(self, jid):
        return False

    def _track_last_reaction(self, jid, r):
        pass

    def _reconstruct_last_reactions_from_records(self, jid):
        pass

    def _resolve_contact_name(self, chat_or_jid, push_name=""):
        if isinstance(chat_or_jid, dict):
            jid = chat_or_jid.get("remoteJid", "")
            push_name = push_name or chat_or_jid.get("pushName", "") or chat_or_jid.get("name", "")
        else:
            jid = str(chat_or_jid)
        return push_name or (jid.split("@")[0] if "@" in jid else jid)

    def _save_message_to_local_storage(self, remote_jid, msg):
        pass

    def move_chat_row_to_top(self, remote_jid):
        return True

    def _schedule_set_chats(self):
        pass

    def sync_if_media(self, msg):
        pass

    def _reacted_message_preview(self, remote_jid, msg_id):
        return "Mensagem original"


class TestArchivedChatSoundAndTTS(unittest.TestCase):
    ARCHIVED_JID = "5511911112222@s.whatsapp.net"
    OTHER_ARCHIVED_JID = "5511933334444@s.whatsapp.net"

    def test_open_archived_chat_plays_sound_and_speaks_tts(self):
        """When an archived conversation is open and active, incoming message must play current sound and speak TTS."""
        stub = _StubMainWindow(
            open_jid=self.ARCHIVED_JID,
            archived_jids=[self.ARCHIVED_JID],
            window_active=True,
        )
        msg = {
            "key": {
                "remoteJid": self.ARCHIVED_JID,
                "id": "MSG_1",
                "fromMe": False,
            },
            "messageType": "conversation",
            "message": {"conversation": "Olá, tudo bem?"},
            "pushName": "Contato Arquivado",
            "messageTimestamp": int(time.time()),
        }

        stub.on_new_message(msg)

        self.assertEqual(stub.message_current_sound.play_count, 1)
        self.assertEqual(stub.message_foreground_sound.play_count, 0)
        self.assertEqual(len(stub.spoken), 1)
        self.assertIn("Olá, tudo bem?", stub.spoken[0])
        self.assertEqual(len(stub.notification_manager.sent), 0)

    def test_closed_archived_chat_stays_silent_when_window_active(self):
        """When an archived conversation is NOT open, incoming message must stay silent even if window is active."""
        stub = _StubMainWindow(
            open_jid="5511999990000@s.whatsapp.net",
            archived_jids=[self.ARCHIVED_JID],
            window_active=True,
        )
        msg = {
            "key": {
                "remoteJid": self.ARCHIVED_JID,
                "id": "MSG_2",
                "fromMe": False,
            },
            "messageType": "conversation",
            "message": {"conversation": "Mensagem em chat arquivado não aberto"},
            "pushName": "Contato Arquivado",
            "messageTimestamp": int(time.time()),
        }

        stub.on_new_message(msg)

        self.assertEqual(stub.message_current_sound.play_count, 0)
        self.assertEqual(stub.message_foreground_sound.play_count, 0)
        self.assertEqual(len(stub.spoken), 0)
        self.assertEqual(len(stub.notification_manager.sent), 0)

    def test_archived_chat_in_background_does_not_notify(self):
        """When window is in background, archived chat messages must not toast or speak."""
        stub = _StubMainWindow(
            open_jid=self.ARCHIVED_JID,
            archived_jids=[self.ARCHIVED_JID],
            window_active=False,
        )
        msg = {
            "key": {
                "remoteJid": self.ARCHIVED_JID,
                "id": "MSG_3",
                "fromMe": False,
            },
            "messageType": "conversation",
            "message": {"conversation": "Mensagem com janela em segundo plano"},
            "pushName": "Contato Arquivado",
            "messageTimestamp": int(time.time()),
        }

        stub.on_new_message(msg)

        self.assertEqual(stub.message_current_sound.play_count, 0)
        self.assertEqual(stub.message_foreground_sound.play_count, 0)
        self.assertEqual(len(stub.spoken), 0)
        self.assertEqual(len(stub.notification_manager.sent), 0)

    def test_reaction_in_open_archived_chat_plays_sound_and_speaks(self):
        """Reaction on a message in an open archived conversation must play sound and speak."""
        stub = _StubMainWindow(
            open_jid=self.ARCHIVED_JID,
            archived_jids=[self.ARCHIVED_JID],
            window_active=True,
        )
        msg = {
            "key": {
                "remoteJid": self.ARCHIVED_JID,
                "id": "REACT_1",
                "fromMe": False,
            },
            "message": {
                "reactionMessage": {
                    "text": "👍",
                    "key": {"id": "TARGET_MSG", "fromMe": True},
                }
            },
            "pushName": "Amigo",
            "messageTimestamp": int(time.time()),
        }

        stub._maybe_notify_reaction(self.ARCHIVED_JID, msg)

        self.assertEqual(stub.message_current_sound.play_count, 1)
        self.assertEqual(len(stub.spoken), 1)
        self.assertIn("👍", stub.spoken[0])

    def test_reaction_in_closed_locked_chat_never_leaks_sender_or_text(self):
        stub = _StubMainWindow(
            locked_jids=[self.ARCHIVED_JID],
            window_active=True,
        )
        msg = {
            "key": {
                "remoteJid": self.ARCHIVED_JID,
                "id": "REACT_PRIVATE",
                "fromMe": False,
            },
            "message": {
                "reactionMessage": {
                    "text": "👍",
                    "key": {"id": "TARGET_MSG", "fromMe": True},
                }
            },
            "pushName": "Private contact",
            "messageTimestamp": int(time.time()),
        }

        stub._maybe_notify_reaction(self.ARCHIVED_JID, msg)

        self.assertEqual(stub.message_current_sound.play_count, 0)
        self.assertEqual(stub.message_foreground_sound.play_count, 0)
        self.assertEqual(stub.spoken, [])
        self.assertEqual(stub.notification_manager.sent, [])

    def test_reaction_in_open_locked_chat_keeps_normal_active_chat_behavior(self):
        stub = _StubMainWindow(
            open_jid=self.ARCHIVED_JID,
            locked_jids=[self.ARCHIVED_JID],
            window_active=True,
        )
        msg = {
            "key": {
                "remoteJid": self.ARCHIVED_JID,
                "id": "REACT_OPEN_PRIVATE",
                "fromMe": False,
            },
            "message": {
                "reactionMessage": {
                    "text": "👍",
                    "key": {"id": "TARGET_MSG", "fromMe": True},
                }
            },
            "messageTimestamp": int(time.time()),
        }

        stub._maybe_notify_reaction(self.ARCHIVED_JID, msg)

        self.assertEqual(stub.message_current_sound.play_count, 1)
        self.assertEqual(len(stub.spoken), 1)

    def test_matches_open_conversation_robustness(self):
        """ConversationsPanel._matches_open_conversation must match across 9th-digit,
        normalization, and bidirectional LID/phone mappings."""
        from ui.conversations import ConversationsPanel

        class _MockMW:
            def __init__(self):
                self._phone_to_lid = {"5511999998888@s.whatsapp.net": "123456789@lid"}
                self._lid_to_phone = {"123456789@lid": "5511999998888@s.whatsapp.net"}

            def _normalize_jid(self, jid):
                if not jid:
                    return ""
                j = jid.strip()
                if j.endswith("@c.us"):
                    j = j[:-5] + "@s.whatsapp.net"
                return j

            def _phone_digits_equivalent(self, a, b):
                if a == b:
                    return True
                if a.startswith("55") and b.startswith("55"):
                    if len(a) == 13 and len(b) == 12 and a[4] == "9":
                        return a[:4] + a[5:] == b
                    if len(b) == 13 and len(a) == 12 and b[4] == "9":
                        return b[:4] + b[5:] == a
                return False

        class _StubPanel:
            _matches_open_conversation = ConversationsPanel._matches_open_conversation

        panel = _StubPanel()
        panel.main_window = _MockMW()
        panel.conversation = {"remoteJid": "5511999998888@s.whatsapp.net"}

        # Exact match
        self.assertTrue(panel._matches_open_conversation("5511999998888@s.whatsapp.net"))
        # @c.us normalization
        self.assertTrue(panel._matches_open_conversation("5511999998888@c.us"))
        # Brazilian 9th digit variation (12 digits vs 13 digits)
        self.assertTrue(panel._matches_open_conversation("551199998888@s.whatsapp.net"))
        # Mapped LID
        self.assertTrue(panel._matches_open_conversation("123456789@lid"))

        # When conversation was loaded as LID
        panel.conversation = {"remoteJid": "123456789@lid"}
        self.assertTrue(panel._matches_open_conversation("5511999998888@s.whatsapp.net"))
        self.assertTrue(panel._matches_open_conversation("551199998888@s.whatsapp.net"))
        self.assertTrue(panel._matches_open_conversation("123456789@lid"))
        # Unrelated JID
        self.assertFalse(panel._matches_open_conversation("5521988887777@s.whatsapp.net"))


if __name__ == "__main__":
    unittest.main()
