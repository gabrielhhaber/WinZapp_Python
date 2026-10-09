"""The Groups in common tab's behaviour, on a stand-in for the dialog.

The mixin only needs a handful of attributes from the dialog, so these bind it
to a plain class with a recording list control: no wx.App and no network. How
the tab sits in the real notebook is pinned in test_private_chat_data_dialog.py.
"""

import wx

from ui.dialogs import common_groups_tab
from ui.dialogs.common_groups_tab import CommonGroupsTabMixin

G1 = "120363000000000001@g.us"
G2 = "120363000000000002@g.us"
NAMES = {G1: "Zeta team", G2: "alpha club"}


class _I18n:
    TEXT = {
        "loading": "Loading",
        "common_groups_none": "None in common",
        "common_groups_unavailable": "Unavailable",
        "unknown_group": "Unknown group",
    }

    def t(self, key):
        return self.TEXT[key]


class _List:
    """Records what the tab does to the list control."""

    def __init__(self):
        self.rows = []
        self.frozen = 0
        self.focused = -1

    def Freeze(self):
        self.frozen += 1

    def Thaw(self):
        self.frozen -= 1

    def DeleteAllItems(self):
        self.rows = []

    def InsertItem(self, index, text):
        self.rows.insert(index, text)

    def GetFocusedItem(self):
        return self.focused


class _Main:
    def __init__(self):
        self.opened = []

    def chat_display_name(self, jid):
        return NAMES.get(jid, "")

    def navigate_to_conversation_jid(self, jid, name):
        self.opened.append((jid, name))


class _Dialog(CommonGroupsTabMixin):
    def __init__(self):
        self._i18n = _I18n()
        self._mw = _Main()
        self._jid = "5511999990000@c.us"
        self._common_groups_list = _List()
        self._common_group_jids = []
        self._common_group_names = []
        self.ended = []

    def EndModal(self, code):
        self.ended.append(code)


class _Key:
    def __init__(self, code):
        self._code, self.skipped = code, False

    def GetKeyCode(self):
        return self._code

    def Skip(self):
        self.skipped = True


class _Activated:
    def __init__(self, index):
        self._index = index

    def GetIndex(self):
        return self._index


def _run_now(monkeypatch):
    """wx.CallAfter runs the callable on the spot, and is recorded."""
    posted = []

    def call_after(func, *args):
        posted.append((func.__name__, args))
        func(*args)

    monkeypatch.setattr(wx, "CallAfter", call_after)
    return posted


class TestWhatTheListShows:
    def test_groups_are_listed_by_name_ignoring_case(self):
        d = _Dialog()
        d._populate_common_groups_unsafe([G1, G2])
        assert d._common_groups_list.rows == ["alpha club", "Zeta team"]
        assert d._common_group_jids == [G2, G1]
        assert d._common_group_names == ["alpha club", "Zeta team"]

    def test_the_rebuild_is_one_frozen_batch(self):
        d = _Dialog()
        d._populate_common_groups_unsafe([G1, G2])
        assert d._common_groups_list.frozen == 0  # thawed again

    def test_a_group_with_no_known_name_reads_as_unknown_never_as_its_jid(self):
        d = _Dialog()
        d._populate_common_groups_unsafe(["120363000000000009@g.us"])
        assert d._common_groups_list.rows == ["Unknown group"]

    def test_none_in_common(self):
        d = _Dialog()
        d._populate_common_groups_unsafe([])
        assert d._common_groups_list.rows == ["None in common"]

    def test_server_could_not_say(self):
        d = _Dialog()
        d._populate_common_groups_unsafe(None)
        assert d._common_groups_list.rows == ["Unavailable"]

    def test_a_new_answer_replaces_the_loading_row(self):
        d = _Dialog()
        d._show_common_groups_message("Loading")
        d._populate_common_groups_unsafe([G1])
        assert d._common_groups_list.rows == ["Zeta team"]


class TestOpeningAGroup:
    def test_enter_opens_that_group_and_closes_the_dialog(self, monkeypatch):
        posted = _run_now(monkeypatch)
        d = _Dialog()
        d._populate_common_groups_unsafe([G1, G2])
        d._on_common_group_activated(_Activated(1))
        assert d._mw.opened == [(G1, "Zeta team")]
        assert d.ended == [wx.ID_CANCEL]
        assert posted == [("navigate_to_conversation_jid", (G1, "Zeta team"))]

    def test_the_status_rows_do_nothing(self, monkeypatch):
        _run_now(monkeypatch)
        for show in (lambda d: d._show_common_groups_message("Loading"),
                     lambda d: d._populate_common_groups_unsafe([]),
                     lambda d: d._populate_common_groups_unsafe(None)):
            d = _Dialog()
            show(d)
            d._on_common_group_activated(_Activated(0))
            assert d._mw.opened == [] and d.ended == []

    def test_an_index_outside_the_list_is_ignored(self, monkeypatch):
        _run_now(monkeypatch)
        d = _Dialog()
        d._populate_common_groups_unsafe([G1])
        for bad in (-1, 1, 99):
            d._on_common_group_activated(_Activated(bad))
        assert d._mw.opened == [] and d.ended == []

    def test_space_opens_the_focused_group(self, monkeypatch):
        _run_now(monkeypatch)
        d = _Dialog()
        d._populate_common_groups_unsafe([G1, G2])
        d._common_groups_list.focused = 0
        event = _Key(wx.WXK_SPACE)
        d._on_common_groups_key_down(event)
        assert d._mw.opened == [(G2, "alpha club")]
        assert not event.skipped

    def test_other_keys_are_left_to_the_control(self, monkeypatch):
        _run_now(monkeypatch)
        d = _Dialog()
        d._populate_common_groups_unsafe([G1])
        event = _Key(wx.WXK_DOWN)
        d._on_common_groups_key_down(event)
        assert event.skipped and d._mw.opened == []


class TestLoading:
    def test_the_answer_is_posted_to_the_ui_thread(self, monkeypatch):
        posted = _run_now(monkeypatch)
        monkeypatch.setattr(common_groups_tab, "fetch_common_groups",
                            lambda owner, jid: [G1])
        d = _Dialog()
        d._load_common_groups()
        assert posted == [("_populate_common_groups", ([G1],))]
        assert d._common_groups_list.rows == ["Zeta team"]

    def test_a_fetch_that_raises_becomes_unavailable(self, monkeypatch):
        _run_now(monkeypatch)

        def boom(owner, jid):
            raise RuntimeError("down")

        monkeypatch.setattr(common_groups_tab, "fetch_common_groups", boom)
        d = _Dialog()
        d._load_common_groups()
        assert d._common_groups_list.rows == ["Unavailable"]

    def test_a_dialog_closed_meanwhile_is_not_an_error(self):
        d = _Dialog()

        def gone(jids):
            raise RuntimeError("wrapped C/C++ object has been deleted")

        d._populate_common_groups_unsafe = gone
        d._populate_common_groups([G1])  # must not raise

    def test_an_unexpected_failure_while_filling_shows_unavailable(self, monkeypatch):
        def broken_rows(jids, name_for, fallback):
            raise ValueError("bad")

        monkeypatch.setattr(common_groups_tab, "common_group_rows", broken_rows)
        d = _Dialog()
        d._populate_common_groups([G1])
        assert d._common_groups_list.rows == ["Unavailable"]
