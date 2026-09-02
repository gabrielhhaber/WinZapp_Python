"""Test for ConversationsPanel._on_accel_jump_last() (Alt+2).

Two bugs, fixed in two passes:

1. (Session fix #1) Sending a message and pressing Alt+2 no longer focused
   the user's own just-sent message — the unread separator row could end
   up placed at the very bottom of the list even when the true last
   message was the user's own, and _on_accel_jump_last() blindly trusted
   `messages_list.GetItemCount() - 1` as "the last message" without
   checking whether that row was actually a sentinel (separator/
   placeholder). Fixed to walk backwards over any trailing sentinel rows.

2. (Session fix #2, found testing a compiled build after fix #1 shipped)
   Alt+2 "either stays where it is, or just deselects the current message
   without really moving focus" MOST of the time. Root cause, pre-existing
   and NOT introduced by fix #1: this handler only ever called Select(),
   never Focus() — every other "jump to a row" handler in this file calls
   BOTH together (_on_accel_jump_unread() right below it, and
   populate_messages()'s own default-tail-selection block) because
   Select() alone does not reliably move the keyboard-focus/screen-reader
   cursor; Focus() is what actually does that. The original _FakeMessagesList
   test double below didn't even have a Focus() method, so this test suite
   could not have caught fix #1 leaving that gap in place — it's been
   added now specifically so a future regression here fails loudly instead
   of silently passing again.

ConversationsPanel is a wx.Panel and cannot be instantiated without a
running wx.App — exercised against a small stub, same approach as
tests/test_unread_separator_reuse.py.
"""

from ui.conversations import ConversationsPanel


class _FakeMessagesList:
    def __init__(self, focused=-1):
        self._focused = focused
        self.selected = None
        self.focused_item = None
        self.ensured_visible = None
        self.focus_set = False

    def HasFocus(self):
        return self.focus_set

    def SetFocus(self):
        self.focus_set = True

    def Focus(self, idx):
        self.focused_item = idx

    def Select(self, idx, on=True):
        self.selected = idx

    def EnsureVisible(self, idx):
        self.ensured_visible = idx


def _msg(mid):
    return {"key": {"id": mid, "fromMe": True}, "messageType": "conversation"}


def _sep():
    return {"_type": "unread_separator", "count": 1}


_CONV = {"remoteJid": "5551999990000@s.whatsapp.net"}


class _I18n:
    def t(self, key):
        return key


class _MainWindow:
    def __init__(self):
        self.i18n = _I18n()
        self.spoken = []

    def output(self, text, interrupt=False):
        self.spoken.append(text)


class _Stub:
    _on_accel_jump_last = ConversationsPanel._on_accel_jump_last
    _is_separator = ConversationsPanel._is_separator
    # Alt+2 now speaks instead of doing nothing in the two cases this file
    # already covered — no conversation open, and a conversation holding no
    # real message (issues #86 and #87).
    _no_conversation_open_announced = ConversationsPanel._no_conversation_open_announced

    def __init__(self, sorted_messages, conversation=_CONV):
        self._sorted_messages = sorted_messages
        self.messages_list = _FakeMessagesList()
        self.conversation = conversation
        self.main_window = _MainWindow()


class TestJumpToLastMessage:
    def test_last_row_is_a_real_message(self):
        stub = _Stub([_msg("a"), _msg("b"), _msg("c")])
        stub._on_accel_jump_last(None)
        assert stub.messages_list.selected == 2
        assert stub.messages_list.focused_item == 2

    def test_skips_a_trailing_unread_separator(self):
        """The exact reported bug: a separator ends up sitting at the very
        bottom of the list (e.g. right after being (re)placed for a message
        that just arrived) — Alt+2 must land on the real message before it,
        not the separator row itself."""
        stub = _Stub([_msg("a"), _msg("b"), _sep()])
        stub._on_accel_jump_last(None)
        assert stub.messages_list.selected == 1
        assert stub.messages_list.focused_item == 1

    def test_skips_multiple_trailing_sentinel_rows(self):
        stub = _Stub([_msg("a"), _sep(), {"_type": "empty_placeholder"}])
        stub._on_accel_jump_last(None)
        assert stub.messages_list.selected == 0
        assert stub.messages_list.focused_item == 0

    def test_empty_list_selects_nothing_and_says_the_chat_is_empty(self):
        stub = _Stub([])
        stub._on_accel_jump_last(None)
        assert stub.messages_list.selected is None
        assert stub.messages_list.focused_item is None
        assert stub.main_window.spoken == ["chat_is_empty"]

    def test_a_list_of_only_sentinels_selects_nothing_and_says_so(self):
        stub = _Stub([_sep()])
        stub._on_accel_jump_last(None)
        assert stub.messages_list.selected is None
        assert stub.messages_list.focused_item is None
        assert stub.main_window.spoken == ["chat_is_empty"]

    def test_with_no_conversation_open_it_says_that_instead(self):
        stub = _Stub([], conversation=None)
        stub._on_accel_jump_last(None)
        assert stub.messages_list.selected is None
        assert stub.main_window.spoken == ["no_chat_open"]

    def test_ensures_the_selected_row_is_visible_and_focuses_the_list(self):
        stub = _Stub([_msg("a"), _msg("b")])
        stub._on_accel_jump_last(None)
        assert stub.messages_list.ensured_visible == 1
        assert stub.messages_list.focus_set is True

    def test_calls_focus_before_select_matching_the_rest_of_the_codebase(self):
        """Regression test for bug #2 above: Focus() must actually be
        called (not just Select()) — every working "jump to row" handler
        in this file does both."""
        calls = []
        stub = _Stub([_msg("a"), _msg("b")])
        stub.messages_list.Focus = lambda idx: calls.append(("Focus", idx))
        stub.messages_list.Select = lambda idx, on=True: calls.append(("Select", idx))

        stub._on_accel_jump_last(None)

        assert ("Focus", 1) in calls
        assert ("Select", 1) in calls
