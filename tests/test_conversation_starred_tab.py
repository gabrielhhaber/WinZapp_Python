"""The "Starred messages" tab of the conversation and group data dialogs.

It replaced the "Starred messages in this chat" button of the open
conversation. Nothing here creates a window: the mixin's methods run on a
stub whose controls record what is done to them, and the order of the tabs is
read from the builders' source.
"""

import ast
import inspect
import textwrap
import threading
import types

import pytest

import ui.dialogs.conversation_starred_tab as tab
from core.starred_list import StarredEntry
from ui.conversations import ConversationsPanel
from ui.dialogs.conversation_data_dialog import ConversationDataDialog
from ui.dialogs.conversation_starred_tab import ConversationStarredTabMixin

JID = "5511999990000@s.whatsapp.net"
TEXTS = {"loading": "loading", "starred_none": "none", "starred_unavailable": "unavailable",
         "starred_no_match": "no match", "starred_count": "{count} starred"}


class _List:
    def __init__(self):
        self.items, self.selection, self.frozen = [], -1, 0

    def Freeze(self):
        self.frozen += 1

    def Thaw(self):
        self.frozen -= 1

    def Set(self, items):
        assert self.frozen == 1, "list mutated outside Freeze/Thaw"
        self.items, self.selection = list(items), -1

    def SetSelection(self, index):
        self.selection = index

    def GetSelection(self):
        return self.selection


class _Control:
    def __init__(self, value=""):
        self.value, self.enabled = value, True

    def GetValue(self):
        return self.value

    def SetLabel(self, text):
        self.value = text

    def Enable(self, flag):
        self.enabled = flag


class _Panel:
    def __init__(self, accepts=True):
        self.accepts, self.synced = accepts, []

    def _sync_message_stars(self, jid, messages, starred):
        self.synced.append((jid, messages, starred))
        return self.accepts


def _msg(msg_id):
    return {"key": {"id": msg_id, "fromMe": True}}


def _entry(msg_id, text="hello"):
    return StarredEntry(JID, _msg(msg_id), text)


class _Dialog(ConversationStarredTabMixin):
    def __init__(self, panel=None, rows=()):
        self.spoken, self.ended, self.closed_event = [], [], threading.Event()
        self._closed = self.closed_event
        self._jid = JID
        self._i18n = types.SimpleNamespace(t=lambda key: TEXTS.get(key, key))
        self._notebook = types.SimpleNamespace(GetCurrentPage=lambda: self._starred_page)
        self._starred_page = object()
        self._mw = types.SimpleNamespace(
            conversations_panel=panel,
            speak_output=types.SimpleNamespace(output=self.spoken.append),
            db=types.SimpleNamespace(get_starred_messages=lambda limit, remote_jids=None: list(rows)),
            chat_display_name=lambda jid: "Ana",
        )
        self._starred_entries, self._starred_shown = [], []
        self._starred_loaded = False
        self.starred_choice = None
        self._starred_search, self._starred_status = _Control(), _Control()
        self._starred_list = _List()
        self._starred_open_btn, self._starred_unstar_btn = _Control(), _Control()

    def EndModal(self, code):
        self.ended.append(code)


@pytest.fixture
def after(monkeypatch):
    """wx.CallAfter calls recorded, to run when the test says."""
    queued = []
    monkeypatch.setattr(tab.wx, "CallAfter", lambda fn, *a: queued.append((fn, a)))
    return queued


def _calls(name):
    """The names of the self._build_*_tab() calls of *name*, in order."""
    func = ast.parse(textwrap.dedent(inspect.getsource(getattr(ConversationDataDialog, name)))).body[0]
    return [n.func.attr for n in ast.walk(func)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr.startswith("_build_") and n.func.attr.endswith("_tab")]


class TestTabOrder:
    def test_a_private_chat_has_starred_after_media_and_groups_in_common_last(self):
        calls = _calls("_build_personal_ui")
        assert calls == ["_build_media_tab", "_build_starred_tab", "_build_common_groups_tab"]

    def test_a_group_has_starred_after_media(self):
        calls = _calls("_build_group_ui")
        assert calls == ["_build_media_tab", "_build_starred_tab"]

    def test_the_conversation_panel_no_longer_has_the_button(self):
        assert not hasattr(ConversationsPanel, "_init_conversation_starred_button")
        assert not hasattr(ConversationsPanel, "_on_show_conversation_starred")


class TestLoading:
    def test_the_read_happens_off_the_main_thread_and_is_applied_through_callafter(
            self, monkeypatch, after):
        seen = {}
        dialog = _Dialog(_Panel())
        main = threading.get_ident()

        def read(limit, remote_jids=None):
            seen["thread"], seen["forms"] = threading.get_ident(), remote_jids
            return []
        dialog._mw.db.get_starred_messages = read
        threads = []
        real = threading.Thread

        def make(**kw):
            threads.append(real(**kw))
            threads[-1].start = lambda: None       # started by hand below
            return threads[-1]
        monkeypatch.setattr(tab.threading, "Thread", make)
        dialog._load_starred()
        real.start(threads[0])
        threads[0].join(5)
        assert seen["thread"] != main
        assert JID in seen["forms"]
        assert after and after[0][0] == dialog._finish_starred_load

    def test_an_answer_for_a_closed_dialog_is_dropped(self, monkeypatch):
        dialog = _Dialog(_Panel())
        dialog.closed_event.set()
        monkeypatch.setattr(tab, "entries_for", lambda *a, **k: pytest.fail("applied"))
        dialog._finish_starred_load([(JID, _msg("1"))])
        assert dialog._starred_list.items == [] and not dialog.spoken

    def test_the_rows_are_listed_and_counted(self, monkeypatch):
        dialog = _Dialog(_Panel())
        monkeypatch.setattr(tab, "entries_for",
                            lambda mw, rows, in_chat=False: [_entry("1", "a"), _entry("2", "b")])
        dialog._finish_starred_load([object(), object()])
        assert dialog._starred_list.items == ["a", "b"]
        assert dialog._starred_status.value == "2 starred"
        assert dialog.spoken == ["2 starred"]
        assert dialog._starred_open_btn.enabled and dialog._starred_unstar_btn.enabled

    def test_no_message_says_so_and_offers_no_action(self):
        dialog = _Dialog(_Panel())
        dialog._finish_starred_load([])
        assert dialog._starred_list.items == ["none"]
        assert dialog.spoken == ["none"]
        assert not dialog._starred_open_btn.enabled

    def test_an_unreadable_database_says_unavailable(self):
        dialog = _Dialog(_Panel())
        dialog._finish_starred_load(None)
        assert dialog._starred_list.items == ["unavailable"]
        assert dialog.spoken == ["unavailable"]
        assert not dialog._starred_open_btn.enabled

    def test_the_answer_is_not_spoken_while_another_tab_is_on_screen(self):
        dialog = _Dialog(_Panel())
        dialog._notebook = types.SimpleNamespace(GetCurrentPage=lambda: object())
        dialog._finish_starred_load([])
        assert dialog._starred_status.value == "none" and dialog.spoken == []

    def test_a_chat_the_vault_hides_is_left_out_and_no_jid_is_shown(self):
        panel = types.SimpleNamespace(
            _get_message_content=lambda msg: "secret", _format_date=lambda ts: "",
            _get_participant_name=lambda *a, **k: "")
        mw = types.SimpleNamespace(
            conversations_panel=panel, self_reference_label=lambda: "You",
            chat_display_name=lambda jid: "Ana",
            is_chat_hidden_by_vault=lambda jid: jid == "hidden@s.whatsapp.net",
            i18n=types.SimpleNamespace(t=lambda key: "{sender}: {text}" if key.endswith("sender") else key))
        entries = tab.entries_for(mw, [("hidden@s.whatsapp.net", _msg("1")), (JID, _msg("2"))],
                                  in_chat=True)
        assert [e.jid for e in entries] == [JID]
        assert "@" not in entries[0].text


class TestSearch:
    def test_the_search_narrows_the_list(self):
        dialog = _Dialog(_Panel())
        dialog._starred_loaded = True
        dialog._starred_entries = [_entry("1", "pizza tonight"), _entry("2", "meeting")]
        dialog._starred_search.value = "PIZZA"
        dialog._on_starred_search()
        assert dialog._starred_list.items == ["pizza tonight"]
        assert dialog._starred_status.value == "1 starred"

    def test_no_match_shows_the_placeholder(self):
        dialog = _Dialog(_Panel())
        dialog._starred_loaded = True
        dialog._starred_entries = [_entry("1", "pizza")]
        dialog._starred_search.value = "zzz"
        dialog._on_starred_search()
        assert dialog._starred_list.items == ["no match"]


class TestOpen:
    def _selected(self, dialog, entry):
        dialog._starred_shown = [entry]
        dialog._starred_list.selection = 0

    def test_open_keeps_the_choice_and_ends_the_modal_without_jumping(self, after):
        dialog = _Dialog(_Panel())
        entry = _entry("1")
        self._selected(dialog, entry)
        dialog._on_starred_open()
        assert dialog.starred_choice is entry and dialog.ended == [tab.wx.ID_OK]
        assert after == []

    def test_nothing_selected_opens_nothing(self):
        dialog = _Dialog(_Panel())
        dialog._on_starred_open()
        assert dialog.starred_choice is None and dialog.ended == []

    def test_the_jump_is_queued_for_after_the_dialog_is_gone(self, after):
        dialog = _Dialog(_Panel())
        dialog.starred_choice = _entry("1")
        dialog._hand_over_starred_choice()
        assert [fn for fn, _ in after] == [tab.jump_to_starred]
        dialog._hand_over_starred_choice()          # once only
        assert len(after) == 1

    def test_no_choice_queues_nothing(self, after):
        _Dialog(_Panel())._hand_over_starred_choice()
        assert after == []

    def test_the_conversation_on_screen_is_jumped_to_in_place(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tab, "jump_in_view", lambda mw, e: calls.append("view") or True)
        monkeypatch.setattr(tab, "open_starred_message", lambda mw, e: calls.append("open"))
        tab.jump_to_starred(object(), _entry("1"))
        assert calls == ["view"]

    def test_another_conversation_is_opened(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tab, "jump_in_view", lambda mw, e: calls.append("view") or False)
        monkeypatch.setattr(tab, "open_starred_message", lambda mw, e: calls.append("open"))
        tab.jump_to_starred(object(), _entry("1"))
        assert calls == ["view", "open"]


class TestRemoveStar:
    def _dialog(self, panel):
        dialog = _Dialog(panel)
        dialog._starred_loaded = True
        dialog._starred_entries = [_entry("1", "a"), _entry("2", "b")]
        dialog._show_starred()
        return dialog

    def test_the_star_goes_through_the_message_menu_path_and_the_row_disappears(self):
        panel = _Panel()
        dialog = self._dialog(panel)
        first = dialog._starred_entries[0]
        dialog._starred_list.selection = 0
        dialog._on_starred_unstar()
        assert panel.synced == [(JID, [first.message], False)]
        assert dialog._starred_list.items == ["b"]

    def test_a_refused_removal_keeps_the_row(self):
        dialog = self._dialog(_Panel(accepts=False))
        dialog._starred_list.selection = 0
        dialog._on_starred_unstar()
        assert dialog._starred_list.items == ["a", "b"]

    def test_without_a_conversation_panel_the_button_is_disabled(self):
        dialog = self._dialog(None)
        assert dialog._starred_open_btn.enabled and not dialog._starred_unstar_btn.enabled
        dialog._on_starred_unstar()                   # no crash, nothing removed
        assert dialog._starred_list.items == ["a", "b"]
