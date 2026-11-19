"""The "Starred messages in this chat" button of an open conversation.

It opens the starred messages window with the open chat's JID, and carries an
Alt+letter in every locale. That letter must not repeat one the chat screen
already answers to: a menu-bar title, a label that moves the focus, a button
of the chat screen, or an Alt accelerator of the main window, the chat list or
the open conversation (read from the real tables, tests/mnemonics.py). A
shared letter only cycles the focus, and no screen reader says so. Which letter
each locale picks is its own decision. No window is created.
"""

import types

import pytest

import ui.dialogs.starred_messages as starred
from tests.locales import registered_locale_codes
from tests.mnemonics import alt_letters, load_strings, mnemonic
from ui.conversation_panel.conversation_starred import ConversationStarredMixin

KEY = "conversation_starred_button"
#: What the main window's menu bar opens with Alt.
MENU_KEYS = ("menu_file", "menu_sync", "menu_help")
#: Labels of the chat screen whose mnemonic moves the focus.
LABEL_KEYS = ("main_nav", "type_message", "type_message_group", "messages")
#: Buttons on the chat screen and in the open conversation.
SCREEN_KEYS = ("wa_lists_reload", "wa_lists_manage", "pinned_messages_title", "search_in_conv",
               "conversation_data", "video_call_button", "voice_call_button",
               "group_call_join_button", "group_call_join_video_button",
               "group_call_unavailable_label", "read_more_button", "send_message",
               "record_voice_message", "emoji_button", "add_attachment", "reactions_label")


@pytest.fixture(params=registered_locale_codes())
def strings(request):
    return load_strings(request.param)


def test_the_button_has_a_mnemonic_in_every_locale(strings):
    assert mnemonic(strings[KEY]) is not None


def test_the_letter_is_not_taken_on_the_chat_screen(strings, monkeypatch):
    letter = mnemonic(strings[KEY])
    taken = {mnemonic(strings[k]) for k in MENU_KEYS + LABEL_KEYS + SCREEN_KEYS if k in strings}
    assert letter not in taken
    assert letter not in alt_letters(strings, monkeypatch)


class _Panel(ConversationStarredMixin):
    def __init__(self, conversation):
        self.conversation = conversation
        self.main_window = types.SimpleNamespace(i18n=types.SimpleNamespace(t=lambda key: key))


def test_the_button_opens_the_window_of_the_open_chat(monkeypatch):
    calls = []
    monkeypatch.setattr(starred, "show_starred_messages",
                        lambda mw, parent, jid=None: calls.append((mw, parent, jid)))
    panel = _Panel({"remoteJid": "120363000000000001@g.us"})
    assert panel._on_show_conversation_starred() is True
    assert calls == [(panel.main_window, panel.main_window, "120363000000000001@g.us")]


def test_with_no_chat_open_nothing_opens(monkeypatch):
    calls = []
    monkeypatch.setattr(starred, "show_starred_messages", lambda *a, **k: calls.append(a))
    assert _Panel(None)._on_show_conversation_starred() is False
    assert _Panel({})._on_show_conversation_starred() is False
    assert calls == []


def test_a_language_change_relabels_the_button():
    panel = _Panel(None)
    labels = []
    panel._conversation_starred_btn = types.SimpleNamespace(SetLabel=labels.append)
    panel._refresh_conversation_starred_label()
    assert labels == [KEY]
    del panel._conversation_starred_btn
    panel._refresh_conversation_starred_label()      # no button yet: nothing to do
