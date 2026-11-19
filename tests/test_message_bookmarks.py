"""Tests for message-list bookmarks (Ctrl+0..9 / Ctrl+Shift+0..9).

Ctrl+<digit> on the focused message row either creates a bookmark (no
bookmark yet for that digit) or jumps focus/selection to the already-
bookmarked message. Ctrl+Shift+<digit> removes a bookmark if one exists.

Bookmarks are keyed by (conversation JID, message key.id), not by raw list
index or a cached conversation position, so they keep pointing at the right
message even if either list is rebuilt/reordered (new message arriving,
pagination) between setting a bookmark and using it. Bookmarks now span
conversations: jumping to one set in a conversation other than the one
currently open navigates there first, then focuses the message — closing or
switching conversations no longer clears them.

ConversationsPanel is a wx.Panel and cannot be instantiated without a running
wx.App, so the methods under test are exercised as plain functions against a
small stub carrying just the attributes they touch — same approach as
tests/test_sender_names.py and tests/test_notifications.py.
"""

import pytest

from ui.conversations import ConversationsPanel


class _FakeI18n:
    """Returns the real pt-BR templates for the keys these methods use."""

    _STRINGS = {
        "bookmark_set": "Posicionado marcador {digit} no elemento {position} (conversa {conv_position}): {text}",
        "bookmark_set_no_position": "Posicionado marcador {digit} no elemento {position}: {text}",
        "bookmark_jumped": "Movido para a posição {position} da lista (marcador {digit})",
        "bookmark_already_there": "Você já está na posição {position} da lista (marcador {digit})",
        "bookmark_jumped_other_conversation": "Movido para a conversa {conv_position}, {conv_name}, posição {position} da lista (marcador {digit})",
        "bookmark_jumped_other_conversation_no_position": "Movido para a conversa {conv_name}, posição {position} da lista (marcador {digit})",
        "bookmark_removed": "Removido marcador {digit} na posição {position} (conversa {conv_position})",
        "bookmark_removed_no_position": "Removido marcador {digit} na posição {position}",
        "bookmark_removed_other_conversation": "Removido marcador {digit} (apontava para outra conversa)",
        "bookmark_removed_stale": "Removido marcador {digit} (a mensagem original não está mais na lista)",
        "bookmark_not_found": "Nenhum marcador encontrado para o número {digit}",
    }

    def t(self, key):
        return self._STRINGS[key]


class _FakeMessagesList:
    """Records Focus()/Select()/EnsureVisible()/SetFocus() calls."""

    def __init__(self, focused_item=-1, has_focus=False):
        self._focused_item = focused_item
        self._has_focus = has_focus
        self.focused_calls = []
        self.selected_calls = []
        self.ensure_visible_calls = []
        self.set_focus_called = False
        self._texts = {}

    def HasFocus(self):
        return self._has_focus

    def GetFocusedItem(self):
        return self._focused_item

    def Focus(self, idx):
        self._focused_item = idx
        self.focused_calls.append(idx)

    def Select(self, idx, on=True):
        self.selected_calls.append((idx, on))

    def EnsureVisible(self, idx):
        self.ensure_visible_calls.append(idx)

    def SetFocus(self):
        self.set_focus_called = True

    def GetItemText(self, idx):
        return self._texts.get(idx, f"item-{idx}")


class _FakeMainWindow:
    def __init__(self, chats=None):
        self.i18n = _FakeI18n()
        self.outputs = []
        self.settings = {}
        self.chats = chats or {}

    def output(self, text, interrupt=False):
        self.outputs.append(text)


def _chat(jid, name):
    return {"remoteJid": jid, "name": name}


CONV_A = "a@s.whatsapp.net"
CONV_B = "b@s.whatsapp.net"


class _Stub:
    """Minimal stand-in for ConversationsPanel."""

    _is_separator = ConversationsPanel._is_separator
    _find_index_by_msg_id = ConversationsPanel._find_index_by_msg_id
    _conversation_position = ConversationsPanel._conversation_position
    _already_on_message_row = ConversationsPanel._already_on_message_row
    _focus_message_row = ConversationsPanel._focus_message_row
    _select_bookmarked_message = ConversationsPanel._select_bookmarked_message
    _on_bookmark_set_or_jump = ConversationsPanel._on_bookmark_set_or_jump
    _on_bookmark_remove = ConversationsPanel._on_bookmark_remove
    _drop_bookmark_after_use = ConversationsPanel._drop_bookmark_after_use

    def __init__(self, sorted_messages, focused_item=-1, conversation_jid=CONV_A,
                 chats=None, chats_list=None, list_has_focus=False):
        self._msg_bookmarks = {}
        self._sorted_messages = sorted_messages
        self.messages_list = _FakeMessagesList(focused_item, list_has_focus)
        self.main_window = _FakeMainWindow(chats=chats)
        self.conversation = _chat(conversation_jid, conversation_jid) if conversation_jid else None
        self.conversation_name = conversation_jid
        self.chats_list = chats_list if chats_list is not None else (
            [self.conversation] if self.conversation else []
        )
        self.navigate_calls = []

    def navigate_to_conversation(self, chat):
        self.navigate_calls.append(chat.get("remoteJid"))
        self.conversation = chat
        self.conversation_name = chat.get("remoteJid")
        # Real navigate_to_conversation() would rebuild _sorted_messages for
        # the new conversation — tests set self._sorted_messages beforehand
        # to simulate "the target conversation's messages are now loaded".


def _msg(msg_id, text="oi"):
    return {
        "key": {"id": msg_id, "fromMe": False},
        "message": {"conversation": text},
        "messageType": "conversation",
    }


def _separator():
    return {"_type": "unread_separator", "count": 3}


@pytest.fixture(autouse=True)
def _no_wx(monkeypatch):
    """wx.CallAfter would need a running app; run the callback inline."""
    monkeypatch.setattr("ui.conversations.wx.CallAfter", lambda fn, *a, **kw: fn(*a, **kw))


class TestBookmarkSet:
    def test_creates_bookmark_on_focused_message(self):
        panel = _Stub([_msg("A"), _msg("B"), _msg("C")], focused_item=1)

        panel._on_bookmark_set_or_jump(5)

        assert panel._msg_bookmarks[5] == (CONV_A, "B")
        assert panel.main_window.outputs == [
            "Posicionado marcador 5 no elemento 2 (conversa 1): item-1"
        ]

    def test_no_focused_item_creates_no_bookmark(self):
        panel = _Stub([_msg("A")], focused_item=-1)

        panel._on_bookmark_set_or_jump(0)

        assert panel._msg_bookmarks == {}
        assert panel.main_window.outputs == []

    def test_focused_separator_creates_no_bookmark(self):
        panel = _Stub([_msg("A"), _separator(), _msg("B")], focused_item=1)

        panel._on_bookmark_set_or_jump(3)

        assert panel._msg_bookmarks == {}
        assert panel.main_window.outputs == []

    def test_set_without_a_visible_conversation_position_omits_it(self):
        """The conversation isn't in chats_list (filtered out by search, or
        archived) — the announcement degrades gracefully instead of saying
        "(conversa 0)"."""
        panel = _Stub([_msg("A")], focused_item=0, chats_list=[])

        panel._on_bookmark_set_or_jump(2)

        assert panel.main_window.outputs == ["Posicionado marcador 2 no elemento 1: item-0"]


class TestBookmarkJumpSameConversation:
    def test_second_press_jumps_to_bookmarked_message(self):
        panel = _Stub([_msg("A"), _msg("B"), _msg("C")], focused_item=0)
        panel._on_bookmark_set_or_jump(7)  # bookmark "A" at position 1
        panel.main_window.outputs.clear()

        # User moved focus elsewhere, then presses Ctrl+7 again.
        panel.messages_list._focused_item = 2
        panel._on_bookmark_set_or_jump(7)

        assert panel.messages_list.focused_calls[-1] == 0
        assert panel.messages_list.selected_calls[-1] == (0, True)
        assert panel.messages_list.ensure_visible_calls[-1] == 0
        assert panel.messages_list.set_focus_called is True
        assert panel.main_window.outputs == [
            "Movido para a posição 1 da lista (marcador 7)"
        ]
        # Jumping must not touch/recreate the bookmark itself.
        assert panel._msg_bookmarks[7] == (CONV_A, "A")
        # Same conversation — no navigation should have happened.
        assert panel.navigate_calls == []

    def test_pressing_it_while_already_on_the_message_says_so(self):
        # Announcing "moved to position 1" without moving anything is a lie a
        # screen-reader user cannot check against the screen.
        panel = _Stub([_msg("A"), _msg("B")], focused_item=0, list_has_focus=True)
        panel._on_bookmark_set_or_jump(7)
        panel.main_window.outputs.clear()

        panel._on_bookmark_set_or_jump(7)

        assert panel.main_window.outputs == [
            "Você já está na posição 1 da lista (marcador 7)"
        ]
        assert panel.messages_list.focused_calls == []
        assert panel.messages_list.set_focus_called is False
        assert panel._msg_bookmarks[7] == (CONV_A, "A")   # the bookmark survives

    def test_the_row_cursor_alone_is_not_being_there(self):
        # Focus moved to the message field: the list still reports the row as
        # its focused item, but the jump does have work to do — bring focus
        # back into the list — so it must not short-circuit.
        panel = _Stub([_msg("A"), _msg("B")], focused_item=0, list_has_focus=True)
        panel._on_bookmark_set_or_jump(7)
        panel.messages_list._has_focus = False
        panel.main_window.outputs.clear()

        panel._on_bookmark_set_or_jump(7)

        assert panel.messages_list.set_focus_called is True
        assert panel.main_window.outputs == ["Movido para a posição 1 da lista (marcador 7)"]

    def test_jump_follows_message_after_list_is_rebuilt(self):
        """A bookmark keeps pointing at the same message even if the list
        was rebuilt (e.g. a new message arrived) and the position shifted."""
        panel = _Stub([_msg("A"), _msg("B")], focused_item=1)
        panel._on_bookmark_set_or_jump(1)  # bookmark "B" at position 2
        panel.main_window.outputs.clear()

        # Simulate populate_messages() rebuilding the list with a new
        # message inserted before the bookmarked one.
        panel._sorted_messages = [_msg("A"), _msg("NEW"), _msg("B")]

        panel._on_bookmark_set_or_jump(1)

        assert panel.messages_list.focused_calls[-1] == 2  # "B" is now at index 2
        assert panel.main_window.outputs == [
            "Movido para a posição 3 da lista (marcador 1)"
        ]

    def test_jump_to_deleted_message_drops_stale_bookmark(self):
        panel = _Stub([_msg("A"), _msg("B")], focused_item=1)
        panel._on_bookmark_set_or_jump(2)  # bookmark "B"
        panel.main_window.outputs.clear()

        # "B" is no longer in the list (deleted, paged out, ...).
        panel._sorted_messages = [_msg("A")]

        panel._on_bookmark_set_or_jump(2)

        assert 2 not in panel._msg_bookmarks
        assert panel.main_window.outputs == [
            "Nenhum marcador encontrado para o número 2"
        ]
        # Must not have moved focus anywhere.
        assert panel.messages_list.focused_calls == []


class TestBookmarkJumpOtherConversation:
    def test_jump_navigates_to_the_bookmarks_own_conversation_first(self):
        chat_b = _chat(CONV_B, "Bob")
        panel = _Stub(
            [_msg("X")], focused_item=0, conversation_jid=CONV_A,
            chats=None, chats_list=[_chat(CONV_A, "Alice"), chat_b],
        )
        panel.main_window.chats = {CONV_B: chat_b}
        panel._msg_bookmarks[3] = (CONV_B, "X")

        panel._on_bookmark_set_or_jump(3)

        assert panel.navigate_calls == [CONV_B]
        assert panel.messages_list.focused_calls[-1] == 0
        assert panel.main_window.outputs == [
            "Movido para a conversa 2, b@s.whatsapp.net, posição 1 da lista (marcador 3)"
        ]

    def test_a_coinciding_row_index_is_not_reported_as_already_being_there(self):
        """The user really did move — the conversation changed — even though
        the row number happens to match the one focused before. Only the
        same-conversation path may short-circuit."""
        chat_b = _chat(CONV_B, "Bob")
        panel = _Stub(
            [_msg("X")], focused_item=0, conversation_jid=CONV_A,
            chats_list=[_chat(CONV_A, "Alice"), chat_b], list_has_focus=True,
        )
        panel.main_window.chats = {CONV_B: chat_b}
        panel._msg_bookmarks[3] = (CONV_B, "X")   # also lands on row index 0

        panel._on_bookmark_set_or_jump(3)

        assert panel.navigate_calls == [CONV_B]
        assert panel.messages_list.set_focus_called is True
        assert panel.main_window.outputs == [
            "Movido para a conversa 2, b@s.whatsapp.net, posição 1 da lista (marcador 3)"
        ]

    def test_jump_to_a_conversation_not_in_the_list_omits_position(self):
        """The target conversation exists (chats dict) but isn't currently
        shown in chats_list (archived, filtered) — position is omitted."""
        chat_b = _chat(CONV_B, "Bob")
        panel = _Stub(
            [_msg("X")], focused_item=0, conversation_jid=CONV_A,
            chats_list=[_chat(CONV_A, "Alice")],
        )
        panel.main_window.chats = {CONV_B: chat_b}
        panel._msg_bookmarks[3] = (CONV_B, "X")

        panel._on_bookmark_set_or_jump(3)

        assert panel.main_window.outputs == [
            "Movido para a conversa b@s.whatsapp.net, posição 1 da lista (marcador 3)"
        ]

    def test_jump_to_a_deleted_conversation_drops_the_bookmark(self):
        panel = _Stub([_msg("X")], focused_item=0, conversation_jid=CONV_A)
        panel.main_window.chats = {}  # CONV_B no longer exists
        panel._msg_bookmarks[3] = (CONV_B, "X")

        panel._on_bookmark_set_or_jump(3)

        assert 3 not in panel._msg_bookmarks
        assert panel.navigate_calls == []
        assert panel.main_window.outputs == [
            "Nenhum marcador encontrado para o número 3"
        ]

    def test_jump_to_a_message_missing_from_the_other_conversation_drops_it(self):
        """Navigated to the right conversation, but the bookmarked message
        itself isn't in what's loaded there (deleted, paged out)."""
        chat_b = _chat(CONV_B, "Bob")
        panel = _Stub([_msg("other")], focused_item=0, conversation_jid=CONV_A)
        panel.main_window.chats = {CONV_B: chat_b}
        panel._msg_bookmarks[3] = (CONV_B, "X")

        panel._on_bookmark_set_or_jump(3)

        assert panel.navigate_calls == [CONV_B]
        assert 3 not in panel._msg_bookmarks
        assert panel.main_window.outputs == [
            "Nenhum marcador encontrado para o número 3"
        ]


class TestBookmarksSurviveConversationSwitches:
    def test_bookmark_dict_is_not_a_conversation_scoped_attribute(self):
        """navigate_to_conversation()/close_conversation() must not reset
        _msg_bookmarks — this test just documents the stub mirrors real
        behavior; the actual non-clearing is exercised by the fact that
        cross-conversation jump tests above rely on a bookmark set while
        "in" one conversation still being there after switching to another."""
        panel = _Stub([_msg("A")], focused_item=0, conversation_jid=CONV_A)
        panel._on_bookmark_set_or_jump(1)
        assert 1 in panel._msg_bookmarks

        panel.navigate_to_conversation(_chat(CONV_B, "Bob"))
        assert 1 in panel._msg_bookmarks


class TestBookmarkRemove:
    def test_removes_existing_bookmark(self):
        panel = _Stub([_msg("A"), _msg("B")], focused_item=1)
        panel._on_bookmark_set_or_jump(9)  # bookmark "B" at position 2
        panel.main_window.outputs.clear()

        panel._on_bookmark_remove(9)

        assert 9 not in panel._msg_bookmarks
        assert panel.main_window.outputs == [
            "Removido marcador 9 na posição 2 (conversa 1)"
        ]

    def test_removing_bookmark_without_a_visible_conversation_position_omits_it(self):
        """Same graceful-degradation as TestBookmarkSet: the conversation
        isn't in chats_list (filtered out by search, or archived)."""
        panel = _Stub([_msg("A"), _msg("B")], focused_item=1, chats_list=[])
        panel._on_bookmark_set_or_jump(9)  # bookmark "B" at position 2
        panel.main_window.outputs.clear()

        panel._on_bookmark_remove(9)

        assert panel.main_window.outputs == ["Removido marcador 9 na posição 2"]

    def test_removing_unset_bookmark_announces_not_found(self):
        panel = _Stub([_msg("A")], focused_item=0)

        panel._on_bookmark_remove(3)

        assert panel._msg_bookmarks == {}
        assert panel.main_window.outputs == [
            "Nenhum marcador encontrado para o número 3"
        ]

    def test_removing_bookmark_for_deleted_message(self):
        panel = _Stub([_msg("A"), _msg("B")], focused_item=1)
        panel._on_bookmark_set_or_jump(4)  # bookmark "B"
        panel.main_window.outputs.clear()
        panel._sorted_messages = [_msg("A")]  # "B" gone

        panel._on_bookmark_remove(4)

        assert 4 not in panel._msg_bookmarks
        assert panel.main_window.outputs == [
            "Removido marcador 4 (a mensagem original não está mais na lista)"
        ]

    def test_removing_a_bookmark_that_points_to_another_conversation(self):
        panel = _Stub([_msg("A")], focused_item=0, conversation_jid=CONV_A)
        panel._msg_bookmarks[6] = (CONV_B, "Z")

        panel._on_bookmark_remove(6)

        assert 6 not in panel._msg_bookmarks
        assert panel.main_window.outputs == [
            "Removido marcador 6 (apontava para outra conversa)"
        ]


class TestBookmarksAreIndependentPerDigit:
    def test_ten_digits_can_hold_ten_independent_bookmarks(self):
        messages = [_msg(str(i)) for i in range(10)]
        panel = _Stub(messages, focused_item=0)

        for digit in range(10):
            panel.messages_list._focused_item = digit
            panel._on_bookmark_set_or_jump(digit)

        assert panel._msg_bookmarks == {d: (CONV_A, str(d)) for d in range(10)}


class TestRemoveAfterFirstUse:
    """Settings > Messages, "Remover marcadores permanentes automaticamente
    após o primeiro uso": the jump that uses a bookmark removes it, so the same
    Ctrl+<digit> sets a new one next time instead of jumping to the old one."""

    def _on(self, panel):
        panel.main_window.settings = {"user_interface": {"remove_bookmarks_after_use": True}}

    def test_off_by_default_the_bookmark_stays(self):
        panel = _Stub([_msg("A"), _msg("B")], focused_item=0)
        panel._on_bookmark_set_or_jump(1)
        panel.messages_list._focused_item = 1
        panel._on_bookmark_set_or_jump(1)

        assert panel._msg_bookmarks == {1: (CONV_A, "A")}

    def test_the_jump_still_happens_and_removes_it(self):
        panel = _Stub([_msg("A"), _msg("B")], focused_item=0)
        self._on(panel)
        panel._on_bookmark_set_or_jump(1)
        panel.messages_list._focused_item = 1
        panel.main_window.outputs.clear()

        panel._on_bookmark_set_or_jump(1)

        assert panel.messages_list.focused_calls[-1] == 0
        assert panel.main_window.outputs == ["Movido para a posição 1 da lista (marcador 1)"]
        assert 1 not in panel._msg_bookmarks

    def test_the_same_key_then_sets_a_new_bookmark_where_the_user_is(self):
        panel = _Stub([_msg("A"), _msg("B")], focused_item=0)
        self._on(panel)
        panel._on_bookmark_set_or_jump(1)
        panel.messages_list._focused_item = 1
        panel._on_bookmark_set_or_jump(1)   # jump to A, bookmark gone
        panel.messages_list._focused_item = 1

        panel._on_bookmark_set_or_jump(1)

        assert panel._msg_bookmarks == {1: (CONV_A, "B")}

    def test_already_on_it_counts_as_the_use(self):
        panel = _Stub([_msg("A"), _msg("B")], focused_item=0, list_has_focus=True)
        self._on(panel)
        panel._msg_bookmarks[2] = (CONV_A, "A")

        panel._on_bookmark_set_or_jump(2)

        assert 2 not in panel._msg_bookmarks

    def test_a_jump_into_another_conversation_removes_it_too(self):
        chat_b = _chat(CONV_B, "Bob")
        panel = _Stub([_msg("X")], focused_item=0, conversation_jid=CONV_A,
                      chats_list=[_chat(CONV_A, "Alice"), chat_b])
        self._on(panel)
        panel.main_window.chats = {CONV_B: chat_b}
        panel._msg_bookmarks[3] = (CONV_B, "X")

        panel._on_bookmark_set_or_jump(3)

        assert panel.navigate_calls == [CONV_B]
        assert 3 not in panel._msg_bookmarks

    def test_the_temporary_setting_does_not_touch_permanent_bookmarks(self):
        panel = _Stub([_msg("A"), _msg("B")], focused_item=1)
        panel.main_window.settings = {"user_interface": {"remove_temp_bookmarks_after_use": True}}
        panel._msg_bookmarks[4] = (CONV_A, "A")

        panel._on_bookmark_set_or_jump(4)

        assert panel._msg_bookmarks == {4: (CONV_A, "A")}

    def test_a_bookmark_set_again_meanwhile_is_kept(self):
        # The jump into another conversation lands through wx.CallAfter: if the
        # digit was given a new bookmark by then, that one is not the one used.
        panel = _Stub([_msg("A")], focused_item=0)
        self._on(panel)
        panel._msg_bookmarks[5] = (CONV_A, "NEW")

        panel._drop_bookmark_after_use(5, (CONV_A, "OLD"), temporary=False)

        assert panel._msg_bookmarks == {5: (CONV_A, "NEW")}
