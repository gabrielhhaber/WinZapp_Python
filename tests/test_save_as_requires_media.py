"""Tests for "Save as" (Ctrl+Shift+S) refusing messages that have no file.

Reported live: pressing Ctrl+Shift+S on a plain text message opened the save
dialog offering a "<id>.bin" — _resolve_media_filename()'s fallback name for a
message with no media — and saving it errored, because no download could ever
produce that file.

The context menu already gated its "Salvar como" item on the message type, but
the accelerator and the toolbar button call _on_action_save_as() directly and
never passed near that check. Both now consult the same
_SAVEABLE_MESSAGE_TYPES set, so the menu and the shortcut cannot disagree
again.

Refusing silently would trade one bug for a worse one — a screen-reader user
pressing the shortcut and hearing nothing has no way to tell "this message has
nothing to save" from "the shortcut is broken" — so the refusal is announced.

ConversationsPanel is a wx.Panel and cannot be instantiated without a running
wx.App, so the method under test is exercised as a plain function against a
small stub — same approach as tests/test_message_bookmarks.py. The stub has no
wx.FileDialog at all: if the guard ever stops returning early, the test fails
with AttributeError instead of quietly opening a dialog.
"""

import pytest

from ui.conversations import ConversationsPanel, _SAVEABLE_MESSAGE_TYPES


class _FakeI18n:
    def t(self, key):
        return {
            "save_as_nothing_to_save": "Esta mensagem não tem arquivo para salvar",
            "default_filename_voice_message": "mensagem_de_voz",
            "default_filename_audio": "audio",
        }.get(key, key)


class _FakeMainWindow:
    def __init__(self):
        self.i18n = _FakeI18n()
        self.outputs = []
        self.settings = {"user_interface": {}}

    def output(self, text, interrupt=False):
        self.outputs.append(text)


class _FakeMessagesList:
    def __init__(self, selected):
        self._selected = selected

    def GetFirstSelected(self):
        return self._selected


class _Stub:
    """Stand-in for ConversationsPanel.

    Deliberately carries nothing the media path needs (no _resolve_media_
    filename, no wx): reaching past the guard raises rather than passing.
    """

    _is_separator = ConversationsPanel._is_separator
    _on_action_save_as = ConversationsPanel._on_action_save_as
    # The by-message half _on_action_save_as() delegates to (the Media tab
    # in the group data dialog calls it directly, having no row index).
    save_media_message  = ConversationsPanel.save_media_message
    _bulk_shortcuts_enabled = ConversationsPanel._bulk_shortcuts_enabled

    def __init__(self, sorted_messages, selected=0):
        self._sorted_messages = sorted_messages
        self.messages_list = _FakeMessagesList(selected)
        self.main_window = _FakeMainWindow()
        self.selected_messages = set()


def _msg(msg_type, text="oi"):
    return {
        "key": {"id": "ABC123", "fromMe": False},
        "message": {msg_type: text if msg_type == "conversation" else {}},
        "messageType": msg_type,
    }


NOTHING_TO_SAVE = "Esta mensagem não tem arquivo para salvar"


class TestMessagesWithNoFile:
    @pytest.mark.parametrize("msg_type", [
        "conversation",          # the reported case: plain text
        "extendedTextMessage",   # text with a link/quote
        "stickerMessage",
        "locationMessage",
        "reactionMessage",
        "protocolMessage",       # system events
        "groupNotification",
        "",                      # messageType missing entirely
    ])
    def test_refuses_and_says_why(self, msg_type):
        panel = _Stub([_msg(msg_type)])

        panel._on_action_save_as(None)

        assert panel.main_window.outputs == [NOTHING_TO_SAVE]

    def test_a_separator_row_is_ignored_without_speaking(self):
        # Not a message at all — there is nothing to explain to the user.
        panel = _Stub([{"_type": "unread_separator", "count": 3}])

        panel._on_action_save_as(None)

        assert panel.main_window.outputs == []

    def test_nothing_selected_does_nothing(self):
        panel = _Stub([_msg("conversation")], selected=-1)

        panel._on_action_save_as(None)

        assert panel.main_window.outputs == []


class TestContactMessageRoutesToSaveContactInstead:
    """contactMessage has a file-less "save" of its own — adding the contact
    locally (see tests/test_contact_message_actions.py) — so it must never
    fall into the generic "nothing to save" refusal above."""

    def test_gets_routed_to_save_contact_message(self):
        calls = []
        panel = _Stub([_msg("contactMessage")])
        panel._on_save_contact_message = lambda event: calls.append(event)

        panel._on_action_save_as(None)

        assert calls == [None]
        assert panel.main_window.outputs == []


class TestMessagesThatDoHaveAFile:
    @pytest.mark.parametrize("msg_type", sorted(_SAVEABLE_MESSAGE_TYPES))
    def test_gets_past_the_guard(self, msg_type):
        """The guard must not swallow the real cases. The stub has no
        _resolve_media_filename, so getting past it raises AttributeError —
        which is exactly the proof that the guard let it through."""
        panel = _Stub([_msg(msg_type)])

        with pytest.raises(AttributeError):
            panel._on_action_save_as(None)

        assert panel.main_window.outputs == []


def test_the_saveable_set_is_what_the_context_menu_offers():
    """Audio is the one type the menu handles in its own branch (separate
    cache, its own label); the rest come straight from the shared set."""
    assert _SAVEABLE_MESSAGE_TYPES == {
        "documentMessage", "imageMessage", "videoMessage", "audioMessage",
    }


class _FilenameStub:
    def __init__(self):
        self.main_window = _FakeMainWindow()

    _resolve_media_filename = ConversationsPanel._resolve_media_filename


@pytest.mark.parametrize(
    ("mimetype", "expected_ext"),
    [
        ("audio/ogg; codecs=opus", ".ogg"),
        ("audio/mp4", ".m4a"),
        ("audio/m4a", ".m4a"),
        ("audio/x-m4a", ".m4a"),
        ("audio/aac", ".aac"),
        ("audio/wav", ".wav"),
        ("audio/flac", ".flac"),
        ("audio/opus", ".opus"),
    ],
)
def test_audio_save_as_uses_real_mimetype_extension(mimetype, expected_ext):
    msg = {
        "key": {"id": "ABC123", "fromMe": False},
        "message": {"audioMessage": {"mimetype": mimetype}},
        "messageType": "audioMessage",
        "messageTimestamp": 1700000000,
    }

    filename = _FilenameStub()._resolve_media_filename(msg)

    assert filename.endswith(expected_ext)
    assert not filename.endswith(".mp3") or expected_ext == ".mp3"


def test_audio_save_as_preserves_original_filename_extension():
    msg = {
        "key": {"id": "ABC123", "fromMe": False},
        "message": {
            "audioMessage": {
                "mimetype": "audio/mp4",
                "fileName": "minha-musica.m4a",
            }
        },
        "messageType": "audioMessage",
        "messageTimestamp": 1700000000,
    }

    assert _FilenameStub()._resolve_media_filename(msg) == "minha-musica.m4a"


def test_audio_save_as_mimetype_overrides_wrong_filename_extension():
    msg = {
        "key": {"id": "ABC123", "fromMe": False},
        "message": {
            "audioMessage": {
                "mimetype": "audio/ogg; codecs=opus",
                "fileName": "minha-musica.mp3",
            }
        },
        "messageType": "audioMessage",
        "messageTimestamp": 1700000000,
    }

    assert _FilenameStub()._resolve_media_filename(msg) == "minha-musica.ogg"


def test_ptt_uses_reported_mimetype_extension_when_available():
    msg = {
        "key": {"id": "ABC123", "fromMe": False},
        "message": {
            "audioMessage": {
                "ptt": True,
                "mimetype": "audio/webm",
            }
        },
        "messageType": "audioMessage",
        "messageTimestamp": 1700000000,
    }

    assert _FilenameStub()._resolve_media_filename(msg).endswith(".webm")


def test_unknown_regular_audio_no_longer_forces_mp3():
    msg = {
        "key": {"id": "ABC123", "fromMe": False},
        "message": {"audioMessage": {}},
        "messageType": "audioMessage",
        "messageTimestamp": 1700000000,
    }

    filename = _FilenameStub()._resolve_media_filename(msg)

    assert not filename.lower().endswith(".mp3")
    assert "." not in filename

