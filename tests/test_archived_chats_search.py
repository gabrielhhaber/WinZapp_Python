"""Tests for the search field added to the archived-chats panel.

Mirrors ConversationsPanel's own search field (same Ctrl+F shortcut, same
Down-arrow-to-first-result behavior, same "route through the rebuilder rather
than touch the list directly" wiring), but scoped to the archived list only:
this field must never reach chats outside ArchivedConversationsPanel, unlike
the main panel's field, which merges in archived results on a non-empty query
(see MainWindow._conversation_search_candidates(), tests/test_conversation_search_candidates.py).

MainWindow and ArchivedConversationsPanel are a wx.Frame/wx.Panel and cannot be
instantiated without a running wx.App, so both the filtering logic and the
event handlers are exercised as plain functions against stubs — same approach
as tests/test_search_normalization.py and tests/test_archived_chats.py.
"""

import wx

from main import MainWindow
from ui.conversations import ArchivedConversationsPanel

GROUP = "120363151058129530@g.us"
ANA = "5511999999999@s.whatsapp.net"
BEL = "5511888888888@s.whatsapp.net"


def _chat(jid, unread=0):
    chat = {"remoteJid": jid, "unreadCount": unread}
    if unread:
        # effective_unread_count() suppresses a reported count to 0 unless at
        # least one local message record backs it up (core/utils.py) — give
        # every "unread" fixture one, or the unread-filter tests below would
        # be exercising the suppression instead of the filter.
        chat["messages"] = {"messages": {"records": [{"key": {"id": "m1"}}]}}
    return chat


class TestFilterArchivedChats:
    """MainWindow._filter_archived_chats() — the staticmethod _refresh_archived_chats_in_ui()
    delegates to, combining the filter tabs with this panel's own search field."""

    def test_no_filter_no_search_keeps_everything(self):
        chats = [_chat(ANA), _chat(GROUP)]
        names = ["Ana", "Grupo"]
        out_chats, out_names = MainWindow._filter_archived_chats(
            chats, names, "all", "", "off"
        )
        assert out_names == ["Ana", "Grupo"]
        assert out_chats == chats

    def test_search_matches_by_name_case_insensitively(self):
        chats = [_chat(ANA), _chat(BEL)]
        names = ["Ana", "Beatriz"]
        out_chats, out_names = MainWindow._filter_archived_chats(
            chats, names, "all", "ana", "off"
        )
        assert out_names == ["Ana"]
        assert out_chats == [chats[0]]

    def test_search_with_no_match_returns_nothing(self):
        chats = [_chat(ANA)]
        names = ["Ana"]
        out_chats, out_names = MainWindow._filter_archived_chats(
            chats, names, "all", "carlos", "off"
        )
        assert out_chats == [] and out_names == []

    def test_filter_and_search_combine(self):
        """A group named "Ana" must not survive the 'individual' filter even
        though the search matches it — both conditions apply together."""
        chats = [_chat(ANA), _chat(GROUP)]
        names = ["Ana", "Ana e amigos"]
        out_chats, _ = MainWindow._filter_archived_chats(
            chats, names, "individual", "ana", "off"
        )
        assert out_chats == [chats[0]]

    def test_unread_filter_still_applies_with_search_active(self):
        chats = [_chat(ANA, unread=0), _chat(BEL, unread=2)]
        names = ["Ana", "Ana Beatriz"]
        out_chats, _ = MainWindow._filter_archived_chats(
            chats, names, "unread", "ana", "off"
        )
        assert out_chats == [chats[1]]

    def test_search_folding_mode_is_honoured(self):
        """The archived search reuses the same Unicode-normalization setting
        as every other search in the app (Settings > Geral)."""
        chats = [_chat(ANA)]
        names = ["Reunião"]
        assert MainWindow._filter_archived_chats(chats, names, "all", "reuniao", "off")[0] == []
        assert MainWindow._filter_archived_chats(chats, names, "all", "reuniao", "nfd")[0] == chats

    def test_empty_search_does_not_filter_by_name_at_all(self):
        """An empty query is not "match the empty string" — it means the
        search box is inert and only the filter tabs apply."""
        chats = [_chat(ANA)]
        names = [""]
        out_chats, _ = MainWindow._filter_archived_chats(chats, names, "all", "", "off")
        assert out_chats == chats


class _FakeAccessible:
    def __init__(self, shortcut=None):
        self.shortcut = shortcut


class _FakeSearchField:
    def __init__(self, value=""):
        self._value = value
        self.focused = False
        self._accessible = None

    def GetValue(self):
        return self._value

    def SetFocus(self):
        self.focused = True

    def SetAccessible(self, acc):
        self._accessible = acc

    def Bind(self, *a, **kw):
        pass


class _FakeConversationsList:
    def __init__(self, count=0):
        self._count = count
        self.focused = False
        self.focused_row = None
        self.selected_row = None

    def GetItemCount(self):
        return self._count

    def SetFocus(self):
        self.focused = True

    def Focus(self, row):
        self.focused_row = row

    def Select(self, row):
        self.selected_row = row


class _FakeKeyEvent:
    def __init__(self, key_code):
        self._key_code = key_code
        self.skipped = False

    def GetKeyCode(self):
        return self._key_code

    def Skip(self):
        self.skipped = True


class _FakeMainWindow:
    def __init__(self):
        self.add_chats_to_ui_calls = 0

    def add_chats_to_ui(self):
        self.add_chats_to_ui_calls += 1


class _ArchivedStub:
    """Binds the unbound ArchivedConversationsPanel methods under test onto a
    plain stub — ArchivedConversationsPanel is a wx.Panel and needs a live
    wx.App to construct."""

    on_search_query_changed = ArchivedConversationsPanel.on_search_query_changed
    on_ctrl_f = ArchivedConversationsPanel.on_ctrl_f
    _on_search_field_key_down = ArchivedConversationsPanel._on_search_field_key_down

    def __init__(self, list_count=1):
        self.main_window = _FakeMainWindow()
        self.search_field = _FakeSearchField()
        self.conversations_list = _FakeConversationsList(list_count)


class TestSearchFieldWiring:
    def test_typing_routes_through_add_chats_to_ui(self):
        """Never a direct call to _refresh_archived_chats_in_ui(): routing
        through add_chats_to_ui() is what keeps the active filter tab and
        this query applied together, exactly like the main panel's own field."""
        panel = _ArchivedStub()

        panel.on_search_query_changed(None)

        assert panel.main_window.add_chats_to_ui_calls == 1

    def test_ctrl_f_focuses_the_search_field(self):
        panel = _ArchivedStub()

        panel.on_ctrl_f(None)

        assert panel.search_field.focused is True

    def test_down_arrow_moves_focus_to_the_first_result(self):
        panel = _ArchivedStub(list_count=3)
        event = _FakeKeyEvent(wx.WXK_DOWN)

        panel._on_search_field_key_down(event)

        assert panel.conversations_list.focused is True
        assert panel.conversations_list.focused_row == 0
        assert panel.conversations_list.selected_row == 0
        assert event.skipped is False

    def test_down_arrow_does_nothing_when_there_are_no_results(self):
        panel = _ArchivedStub(list_count=0)
        event = _FakeKeyEvent(wx.WXK_DOWN)

        panel._on_search_field_key_down(event)

        assert panel.conversations_list.focused is False
        # Still consumed (matches ConversationsPanel._on_search_field_key_down,
        # which also returns without calling event.Skip() on WXK_DOWN).
        assert event.skipped is False

    def test_other_keys_are_not_consumed(self):
        panel = _ArchivedStub(list_count=3)
        event = _FakeKeyEvent(ord("A"))

        panel._on_search_field_key_down(event)

        assert event.skipped is True
        assert panel.conversations_list.focused is False


class TestSearchFieldSourceWiring:
    """Source-level checks for what a stub can't exercise: the widget's
    placement, its accessible shortcut, the accelerator table entry, and the
    label being kept in sync on a language change — mirrors the style of
    TestArchivedPanelAccessibility in tests/test_archived_chats.py."""

    def test_search_field_is_created_after_the_filter_radio_and_before_the_list(self):
        import inspect

        src = inspect.getsource(ArchivedConversationsPanel._init_ui)
        filter_pos = src.index("self._filter_radio")
        search_pos = src.index("self.search_field")
        list_pos = src.index("self.conversations_list = wx.ListCtrl")
        assert filter_pos < search_pos < list_pos

    def test_search_field_reports_ctrl_f_as_its_shortcut(self):
        import inspect

        src = inspect.getsource(ArchivedConversationsPanel._init_ui)
        assert 'AccessibleSearchConversations("Ctrl+F")' in src
        assert "search_field.SetAccessible" in src

    def test_search_label_uses_the_archived_specific_key(self):
        import inspect

        src = inspect.getsource(ArchivedConversationsPanel._init_ui)
        assert '"search_archived_conversations"' in src
        # And not the plain conversations key, which would misdescribe scope
        # (this field only searches archived chats) to a screen-reader user.
        assert '"search_conversations"' not in src

    def test_ctrl_f_is_wired_in_the_accelerator_table(self):
        import inspect

        src = inspect.getsource(ArchivedConversationsPanel.create_accelerator_table)
        assert 'ord("F")' in src
        assert "self.on_ctrl_f" in src

    def test_relabelling_updates_the_search_label_too(self):
        import inspect

        src = inspect.getsource(ArchivedConversationsPanel.refresh_labels)
        assert "search_archived_conversations" in src
