"""Tests for the archived-chats row context menu and its keyboard shortcuts.

Reported live: the archived list's context menu offered only Unarchive/Clear/
Delete — missing conversation data, mark as read/unread, mute, block, copy
number, pin, leave group and add member, all of which the normal conversations
list's row menu already had. The archived list also had NO keyboard
accelerators of its own at all (Delete, Ctrl+Shift+L for clear, etc. did
nothing while it had focus), even though the normal list has had them for a
long time.

ArchivedConversationsPanel.on_context_menu() now mirrors
ConversationsPanel.on_conversations_context_menu() item-for-item, with two
deliberate differences: Archive/Unarchive always shows "Desarquivar" (every
row here is archived by definition, nothing to toggle), and "Close
conversation" is left out (there is no split conversation view to close from
this list). create_accelerator_table() gives it the same key combos as the
normal list's, minus Ctrl+N (nowhere to create a conversation from this list)
and Ctrl+W (not applicable), with Ctrl+Q hardwired to unarchive instead of
toggling. Ctrl+F is bound too, now that this panel has its own search field
(scoped to archived chats only — see tests/test_archived_chats_search.py).

Both panels are wx.Panel subclasses and cannot be instantiated without a
running wx.App, so the menu-building methods (which construct real wx.Menu
objects) are checked structurally via source inspection — same approach
TestArchivedPanelAccessibility in test_archived_chats.py already uses — while
the plain delegate handlers (which touch no wx widgets) are exercised directly
against a stub, mirroring tests/test_message_bookmarks.py.
"""

import inspect

import pytest

from ui.conversations import ArchivedConversationsPanel, ConversationsPanel


# ── Structural parity between the two context menus ─────────────────────────


class TestMenuParity:
    @staticmethod
    def _source(fn):
        return inspect.getsource(fn)

    @staticmethod
    def _has_key(src: str, key: str) -> bool:
        """True if the bare i18n key string literal appears in *src*, quoted
        either way — block/unblock go through a
        `label = "unblock_contact" if ... else "block_contact"` indirection in
        both menus, so the key never appears literally inside an i18n.t(...)
        call in the source text; checking for the bare quoted literal covers
        both that and the direct i18n.t('key') calls."""
        return f"'{key}'" in src or f'"{key}"' in src

    def test_every_i18n_key_in_the_normal_menu_is_also_in_the_archived_one(self):
        normal_src = self._source(ConversationsPanel.on_conversations_context_menu)
        archived_src = self._source(ArchivedConversationsPanel.on_context_menu)
        expected_keys = [
            "group_data", "conversation_data",
            "mark_as_read", "mark_as_unread",
            "unmute_chat", "mute_chat",
            "unblock_contact", "block_contact",
            "copy_number",
            "unarchive_chat",
            "unpin_chat", "pin_chat",
            "clear_chat",
            "delete_chat",
            "leave_group", "add_member",
        ]
        for key in expected_keys:
            assert self._has_key(normal_src, key), f"test setup: {key!r} not even in the normal menu, fix the expected list"
            assert self._has_key(archived_src, key), f"archived context menu is missing the {key!r} item"

    def test_close_conversation_is_deliberately_not_offered(self):
        """The one item that must NOT carry over — there is no split
        conversation view to close from the archived list."""
        archived_src = self._source(ArchivedConversationsPanel.on_context_menu)
        assert not self._has_key(archived_src, "close_conversation")

    def test_close_conversation_on_the_normal_menu_is_gated_on_the_open_chat(self):
        """Reported live: "Fechar conversa" showed up for every row in the
        normal list, not just the one currently open — right-clicking a
        DIFFERENT chat than the open one and picking it silently closed
        whatever conversation actually was open (on_context_menu_close() has
        no idea which jid the menu was opened on). The item must only be
        built when the row's own jid matches self.conversation."""
        normal_src = self._source(ConversationsPanel.on_conversations_context_menu)
        close_call = normal_src.index("i18n.t('close_conversation')")
        preceding = normal_src[:close_call]
        # The nearest un-closed "if" before the close_conversation Append()
        # call must be the jid-matches-open-conversation guard.
        guard_idx = preceding.rindex("if (self.conversation")
        guard_to_call = normal_src[guard_idx:close_call]
        assert 'self.conversation.get("remoteJid") == jid' in guard_to_call
        assert "self.conversation_panel.IsShown()" in guard_to_call

    def test_archive_is_never_offered_only_unarchive(self):
        """Every row in this list is already archived — there is nothing to
        toggle, unlike the normal list which shows either Archive or
        Unarchive depending on state."""
        archived_src = self._source(ArchivedConversationsPanel.on_context_menu)
        assert not self._has_key(archived_src, "archive_chat"), (
            "the archived menu must never offer to (re-)archive a row"
        )
        assert self._has_key(archived_src, "unarchive_chat")

    def test_same_keyboard_shortcut_hints_shown_for_shared_items(self):
        """The \\t hint shown in each menu item must match what the
        accelerator table actually does — a hint that lies is worse than none."""
        normal_src = self._source(ConversationsPanel.on_conversations_context_menu)
        archived_src = self._source(ArchivedConversationsPanel.on_context_menu)
        shared_hints = [
            "Ctrl+Shift+D", "Ctrl+Shift+M", "Alt+Shift+S",
            "Ctrl+Shift+B", "Alt+Shift+C", "Ctrl+Shift+Q", "Ctrl+P",
            "Ctrl+Shift+L", "Delete",
        ]
        for hint in shared_hints:
            assert hint in normal_src, f"test setup: {hint!r} not in the normal menu"
            assert hint in archived_src, f"archived menu is missing the {hint!r} shortcut hint"


class TestAcceleratorTableParity:
    def test_shared_shortcuts_bind_to_the_same_keys(self):
        src = inspect.getsource(ArchivedConversationsPanel.create_accelerator_table)
        for combo in (
            "wx.WXK_DELETE", 'ord("C")', 'ord("D")', 'ord("M")',
            'ord("S")', 'ord("B")', 'ord("L")', 'ord("Q")', 'ord("P")',
        ):
            assert combo in src, f"{combo} missing from the archived list's accelerator table"

    def test_new_conversation_and_close_are_not_bound(self):
        """Nowhere to create a conversation from, and no split conversation
        view on this panel — binding these would either do nothing or crash."""
        src = inspect.getsource(ArchivedConversationsPanel.create_accelerator_table)
        assert 'ord("N")' not in src
        assert 'ord("W")' not in src

    def test_ctrl_f_focuses_this_panels_own_search_field(self):
        """This panel gained its own search field (scoped to archived chats
        only) — Ctrl+F must focus it, distinct from ConversationsPanel's,
        which additionally merges in archived results on a non-empty query."""
        src = inspect.getsource(ArchivedConversationsPanel.create_accelerator_table)
        assert 'ord("F")' in src
        assert "self.on_ctrl_f" in src

    def test_ctrl_q_always_unarchives_never_toggles(self):
        src = inspect.getsource(ArchivedConversationsPanel.create_accelerator_table)
        assert "_on_accel_unarchive" in src
        assert "_on_accel_archive" not in src


# ── Behavior of the plain delegate handlers (no wx widgets involved) ───────


class _FakeMainWindow:
    def __init__(self):
        self.calls = []
        self._muted = set()
        self._pinned = set()

    def mark_conversation_as_read(self, jid):
        self.calls.append(("mark_read", jid))

    def mark_conversation_as_unread(self, jid):
        self.calls.append(("mark_unread", jid))

    def mute_chat(self, jid, secs):
        self.calls.append(("mute", jid, secs))

    def unmute_chat(self, jid):
        self.calls.append(("unmute", jid))

    def is_chat_muted(self, jid):
        return jid in self._muted

    def pin_chat(self, jid):
        self.calls.append(("pin", jid))

    def unpin_chat(self, jid):
        self.calls.append(("unpin", jid))

    def is_chat_pinned(self, jid):
        return jid in self._pinned

    def unarchive_chat(self, jid):
        self.calls.append(("unarchive", jid))

    def _is_self_jid(self, jid):
        return False

    def is_contact_blocked(self, jid):
        return False


class _FakeList:
    def __init__(self, focused=-1):
        self._focused = focused

    def GetFirstSelected(self):
        return self._focused

    def GetFocusedItem(self):
        return self._focused


class _Stub:
    _on_mark_read = ArchivedConversationsPanel._on_mark_read
    _on_mark_unread = ArchivedConversationsPanel._on_mark_unread
    _on_mute = ArchivedConversationsPanel._on_mute
    _on_unmute = ArchivedConversationsPanel._on_unmute
    _on_pin = ArchivedConversationsPanel._on_pin
    _on_unpin = ArchivedConversationsPanel._on_unpin
    _on_unarchive = ArchivedConversationsPanel._on_unarchive
    _on_copy_number = ArchivedConversationsPanel._on_copy_number
    _selected_chat_from_list = ArchivedConversationsPanel._selected_chat_from_list

    def __init__(self, chats_list=None, focused=0):
        self.main_window = _FakeMainWindow()
        self.chats_list = chats_list if chats_list is not None else []

        self.conversations_list = _FakeList(focused)


def test_mark_read_delegates_to_main_window():
    s = _Stub()
    s._on_mark_read("j1@s.whatsapp.net")
    import time
    time.sleep(0.05)  # runs on a background thread, same as the original
    assert ("mark_read", "j1@s.whatsapp.net") in s.main_window.calls


def test_mark_unread_delegates_to_main_window():
    s = _Stub()
    s._on_mark_unread("j1@s.whatsapp.net")
    assert s.main_window.calls == [("mark_unread", "j1@s.whatsapp.net")]


def test_mute_and_unmute_delegate_with_the_right_duration():
    s = _Stub()
    s._on_mute("j1@s.whatsapp.net", 3600)
    s._on_unmute("j1@s.whatsapp.net")
    assert s.main_window.calls == [
        ("mute", "j1@s.whatsapp.net", 3600),
        ("unmute", "j1@s.whatsapp.net"),
    ]


def test_pin_and_unpin_delegate():
    s = _Stub()
    s._on_pin("j1@s.whatsapp.net")
    s._on_unpin("j1@s.whatsapp.net")
    assert s.main_window.calls == [
        ("pin", "j1@s.whatsapp.net"),
        ("unpin", "j1@s.whatsapp.net"),
    ]


def test_unarchive_delegates():
    s = _Stub()
    s._on_unarchive("j1@s.whatsapp.net")
    assert s.main_window.calls == [("unarchive", "j1@s.whatsapp.net")]


def test_copy_number_never_touches_main_window(monkeypatch):
    """Pure clipboard action — no main_window call expected."""
    copied = []
    monkeypatch.setattr("ui.conversations.pyperclip.copy", lambda v: copied.append(v))
    s = _Stub()
    s._on_copy_number("5511999999999@s.whatsapp.net")
    assert len(copied) == 1 and "99999" in copied[0]
    assert s.main_window.calls == []


class TestSelectedChatFromList:
    def test_returns_the_selected_row(self):
        chat = {"remoteJid": "a@s.whatsapp.net"}
        s = _Stub(chats_list=[chat], focused=0)
        assert s._selected_chat_from_list() is chat

    def test_falls_back_to_focused_item_when_nothing_is_selected(self):
        chat = {"remoteJid": "a@s.whatsapp.net"}

        class _FakeList:
            def GetFirstSelected(self):
                return -1

            def GetFocusedItem(self):
                return 0

        s = _Stub(chats_list=[chat])
        s.conversations_list = _FakeList()
        assert s._selected_chat_from_list() is chat

    def test_out_of_range_returns_none(self):
        s = _Stub(chats_list=[], focused=0)
        assert s._selected_chat_from_list() is None
