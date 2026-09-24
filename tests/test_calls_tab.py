"""The Calls tab (Alt+6): every call of every conversation in one list.

The pure half -- which call goes on which tab, how records from the database
and from memory become one list, what a row reads -- is core/call_log.py.
CallsPanel and MainWindow need a wx.App, so their methods run against plain
stubs; no window is ever created (CLAUDE.md rule 1).
"""

from core.call_log import (
    CALL_TABS,
    TAB_ALL,
    TAB_ANSWERED,
    TAB_DECLINED,
    TAB_MISSED,
    call_log_in_tab,
    call_row_text,
    collect_call_logs,
)
from calls_panel import CallsPanel
from main import MainWindow

PHONE = "5511999999999@s.whatsapp.net"
LID = "68904344899801@lid"


def _call(outcome="Missed", from_me=False, call_id="C1", ts=100, jid=PHONE, duration=0):
    return {
        "key": {"remoteJid": jid, "fromMe": from_me, "id": call_id},
        "messageType": "callLogMessage",
        "message": {"callLogMessage": {"outcome": outcome, "isVideo": False,
                                       "durationSeconds": duration, "isGroupCall": False}},
        "messageTimestamp": ts,
    }


def _legacy(call_id="L1", ts=50):
    return {"key": {"remoteJid": PHONE, "fromMe": False, "id": call_id},
            "messageType": "call_log", "message": {}, "messageTimestamp": ts}


def _chat(jid, *records):
    return {"remoteJid": jid, "messages": {"messages": {"records": list(records)}}}


class TestTabs:
    def test_the_order_the_user_asked_for(self):
        assert CALL_TABS == (TAB_ALL, TAB_MISSED, TAB_DECLINED, TAB_ANSWERED)

    def test_missed_is_what_the_user_missed(self):
        assert call_log_in_tab(_call("Missed"), TAB_MISSED)
        assert call_log_in_tab(_call("Canceled"), TAB_MISSED)
        assert not call_log_in_tab(_call("Missed", from_me=True), TAB_MISSED)

    def test_declined_is_either_side(self):
        assert call_log_in_tab(_call("Rejected"), TAB_DECLINED)
        assert call_log_in_tab(_call("Rejected", from_me=True), TAB_DECLINED)

    def test_answered_is_every_call_that_connected(self):
        for rec in (_call("Completed"), _call("Completed", from_me=True),
                    _call("AcceptedElsewhere")):
            assert call_log_in_tab(rec, TAB_ANSWERED)
        assert not call_log_in_tab(_call("Missed"), TAB_ANSWERED)

    def test_all_holds_everything_even_a_legacy_record(self):
        for rec in (_call("Missed", from_me=True), _call("Failed"), _legacy()):
            assert call_log_in_tab(rec, TAB_ALL)
        assert not call_log_in_tab(_legacy(), TAB_MISSED)

    def test_only_call_records(self):
        text = {"key": {"id": "T"}, "messageType": "conversation", "message": {}}
        assert not call_log_in_tab(text, TAB_ALL)


class TestCollect:
    def test_every_conversation_newest_first(self):
        stored = [(PHONE, _call(call_id="A", ts=10)), ("1@g.us", _call(call_id="B", ts=30))]
        chats = {"99@s.whatsapp.net": _chat("99@s.whatsapp.net", _call(call_id="C", ts=20))}
        entries = collect_call_logs(stored, chats)
        assert [e["msg"]["key"]["id"] for e in entries] == ["B", "C", "A"]
        assert [e["jid"] for e in entries] == ["1@g.us", "99@s.whatsapp.net", PHONE]

    def test_memory_wins_for_the_same_call(self):
        """Memory may hold a newer state the database has not got yet."""
        stored = [(LID, _call("Ongoing", call_id="X"))]
        chats = {PHONE: _chat(PHONE, _call("Completed", call_id="X", duration=5))}
        (entry,) = collect_call_logs(stored, chats)
        assert entry["jid"] == PHONE
        assert entry["msg"]["message"]["callLogMessage"]["outcome"] == "Completed"

    def test_but_not_with_a_record_that_knows_less(self):
        stored = [(PHONE, _call("Missed", call_id="X"))]
        chats = {PHONE: _chat(PHONE, dict(_legacy(call_id="X")))}
        (entry,) = collect_call_logs(stored, chats)
        assert entry["msg"]["messageType"] == "callLogMessage"

    def test_other_messages_are_ignored(self):
        text = {"key": {"id": "T"}, "messageType": "conversation", "message": {},
                "messageTimestamp": 1}
        assert collect_call_logs([(PHONE, text)], {PHONE: _chat(PHONE, text)}) == []

    def test_nothing_stored_nothing_in_memory(self):
        assert collect_call_logs([], {}) == []
        assert collect_call_logs(None, None) == []


class TestRowText:
    def test_the_other_party_then_what_then_when(self):
        assert call_row_text({}, "Maria", "Ligação de voz perdida", "01:03") == (
            "Maria: Ligação de voz perdida, 01:03")

    def test_no_name_no_prefix(self):
        assert call_row_text({}, "", "Ligação", "") == "Ligação"


# ── CallsPanel methods on a stub ─────────────────────────────────────────────

class _I18n:
    def t(self, key):
        return f"[{key}]"


class _List:
    def __init__(self, focused=0, count=0):
        self.focused = focused
        self.items = [None] * count
        self.focused_calls = []

    def GetFocusedItem(self):
        return self.focused

    def GetItemCount(self):
        return len(self.items)

    def SetFocus(self):
        pass

    def Focus(self, i):
        self.focused = i

    def Select(self, i):
        pass


class _Notebook:
    def __init__(self, page=0):
        self.page = page

    def GetSelection(self):
        return self.page


class _Button:
    shown = False

    def Show(self, show=True):
        self.shown = bool(show)


class _MW:
    def __init__(self):
        self.i18n = _I18n()
        self.calls = []
        self.opened = []
        self.spoken = []

    def chat_display_name(self, jid):
        return {"5511999999999@s.whatsapp.net": "Maria"}.get(jid, "Grupo")

    def start_voice_call(self, jid, name=""):
        self.calls.append(("voice", jid, name))

    def start_video_call(self, jid, name=""):
        self.calls.append(("video", jid, name))

    def navigate_to_conversation_jid(self, jid, name=""):
        self.opened.append(jid)

    def output(self, text, interrupt=False):
        self.spoken.append(text)


class _Panel:
    current_tab = CallsPanel.current_tab
    current_list = CallsPanel.current_list
    focused_entry = CallsPanel.focused_entry
    _update_return_call_button = CallsPanel._update_return_call_button
    _on_item_activated = CallsPanel._on_item_activated
    _on_return_call = CallsPanel._on_return_call

    def __init__(self, entries, page=0):
        self.main_window = _MW()
        self.notebook = _Notebook(page)
        self._lists = {tab: _List(count=len(entries)) for tab in CALL_TABS}
        self._rows = {tab: list(entries) for tab in CALL_TABS}
        self._return_call_btn = _Button()

    def Layout(self):
        pass


def _entry(msg, jid=PHONE):
    return {"jid": jid, "msg": msg}


class TestPanel:
    def test_todas_is_the_default_tab(self):
        assert _Panel([], page=0).current_tab() == TAB_ALL

    def test_the_button_shows_only_on_a_missed_one_to_one_call(self):
        panel = _Panel([_entry(_call("Missed")), _entry(_call("Completed"))])
        panel._update_return_call_button()
        assert panel._return_call_btn.shown is True
        panel._lists[TAB_ALL].focused = 1
        panel._update_return_call_button()
        assert panel._return_call_btn.shown is False

    def test_not_for_a_group_call(self):
        panel = _Panel([_entry(_call("Missed"), jid="1@g.us")])
        panel._update_return_call_button()
        assert panel._return_call_btn.shown is False

    def test_ctrl_shift_r_calls_back_by_name(self):
        panel = _Panel([_entry(_call("Missed"))])
        panel._on_return_call()
        assert panel.main_window.calls == [("voice", PHONE, "Maria")]

    def test_ctrl_shift_r_elsewhere_says_why(self):
        panel = _Panel([_entry(_call("Completed"))])
        panel._on_return_call()
        assert panel.main_window.calls == []
        assert panel.main_window.spoken == ["[return_call_unavailable]"]

    def test_enter_opens_the_conversation(self):
        panel = _Panel([_entry(_call("Missed"), jid="1@g.us")])
        panel._on_item_activated(None)
        assert panel.main_window.opened == ["1@g.us"]

    def test_the_placeholder_row_opens_nothing_and_says_why(self):
        panel = _Panel([])
        panel._lists[TAB_ALL].items = [None]  # "Nenhuma ligação."
        panel._on_item_activated(None)
        panel._on_return_call()
        assert panel.main_window.opened == [] and panel.main_window.calls == []
        assert panel.main_window.spoken == ["[return_call_unavailable]"]


class TestRowTextFromThePanel:
    """_row_text() puts the conversation's name in front of the same label
    the conversation row reads."""

    class _Stub:
        _row_text = CallsPanel._row_text

        def __init__(self):
            conv = type("CP", (), {
                "_format_duration": staticmethod(lambda s: f"{s}s"),
                "_extract_timestamp": staticmethod(lambda m: m.get("messageTimestamp")),
                "_format_date": staticmethod(lambda ts: "01:03"),
            })()
            self.main_window = _MW()
            self.main_window.conversations_panel = conv

    def test_an_outgoing_answered_call(self):
        text = self._Stub()._row_text(_entry(_call("Completed", from_me=True, duration=9)))
        assert text == "Maria: [call_log_made_voice], [duration]: 9s, 01:03"


class TestChatDisplayName:
    class _Stub:
        chat_display_name = MainWindow.chat_display_name

        def __init__(self, chats):
            self.chats = chats
            self.i18n = _I18n()

        def get_chat(self, jid):
            return self.chats.get(jid)

        def _resolve_contact_name(self, chat):
            return None

        def find_name_through_messages(self, chat):
            return ""

        def find_jid_through_messages(self, chat):
            return ""

        def _format_jid_for_display(self, jid):
            return ""

    def test_the_chat_name(self):
        stub = self._Stub({"1@g.us": {"remoteJid": "1@g.us", "name": "Família"}})
        assert stub.chat_display_name("1@g.us") == "Família"

    def test_an_unknown_chat_is_never_a_raw_jid(self):
        assert self._Stub({}).chat_display_name(LID) == "[unknown_contact]"
        assert self._Stub({}).chat_display_name("2@g.us") == "[unknown_group]"


# ── Navigation: Ligações sits under Status, Settings moved down one ─────────

class _Shown:
    def __init__(self):
        self.shown = False
        self.on_show_calls = 0

    def Show(self):
        self.shown = True

    def Hide(self):
        self.shown = False

    def on_show(self):
        self.on_show_calls += 1


class _Layout:
    def Layout(self):
        pass


class _NavWindow:
    on_alt_6 = MainWindow.on_alt_6

    def __init__(self):
        self.conversations_panel = _Shown()
        self.conversations_panel.conversation = None
        self.archived_conversations_panel = _Shown()
        self.status_panel = _Shown()
        self.calls_panel = _Shown()
        self.content_panel = _Layout()
        self.settings_opened = 0

    def open_settings(self):
        self.settings_opened += 1


class _Event:
    def __init__(self, index):
        self.index = index

    def GetIndex(self):
        return self.index


def _nav(mw):
    from ui.navigation import NavigationPanel

    class _Nav:
        on_nav_item_selected = NavigationPanel.on_nav_item_selected

        def __init__(self):
            self.main_window = mw

    return _Nav()


class TestNavigation:
    def test_item_3_shows_the_calls_tab_alone(self):
        mw = _NavWindow()
        mw.status_panel.shown = True
        _nav(mw).on_nav_item_selected(_Event(3))
        assert mw.calls_panel.shown and mw.calls_panel.on_show_calls == 1
        assert not mw.status_panel.shown and not mw.conversations_panel.shown

    def test_item_4_is_settings_now(self):
        mw = _NavWindow()
        _nav(mw).on_nav_item_selected(_Event(4))
        assert mw.settings_opened == 1

    def test_status_hides_the_calls_tab(self):
        mw = _NavWindow()
        mw.calls_panel.shown = True
        mw.status_panel._add_status_btn = type("B", (), {"SetFocus": lambda self: None})()
        _nav(mw).on_nav_item_selected(_Event(2))
        assert mw.status_panel.shown and not mw.calls_panel.shown

    def test_alt_6(self):
        mw = _NavWindow()
        mw.status_panel.shown = True
        mw.on_alt_6(None)
        assert mw.calls_panel.shown and mw.calls_panel.on_show_calls == 1
        assert not mw.status_panel.shown


# ── The database read the tab starts from ──────────────────────────────────

class TestStoredCallLogs:
    async def test_every_chat_newest_first_and_only_calls(self, in_memory_db):
        await in_memory_db.insert_message(PHONE, _call(call_id="A", ts=10))
        await in_memory_db.insert_message("1@g.us", _call(call_id="B", ts=30, jid="1@g.us"))
        await in_memory_db.insert_message(PHONE, _legacy(call_id="L", ts=20))
        await in_memory_db.insert_message(PHONE, {
            "key": {"remoteJid": PHONE, "id": "T"}, "messageTimestamp": 40,
            "message": {"conversation": "oi"}, "messageType": "conversation"})

        rows = await in_memory_db.get_call_logs()

        assert [(jid, m["key"]["id"]) for jid, m in rows] == [
            ("1@g.us", "B"), (PHONE, "L"), (PHONE, "A")]

    async def test_the_limit_keeps_the_newest(self, in_memory_db):
        for i in range(5):
            await in_memory_db.insert_message(PHONE, _call(call_id=f"C{i}", ts=i))
        rows = await in_memory_db.get_call_logs(limit=2)
        assert [m["key"]["id"] for _, m in rows] == ["C4", "C3"]



# ── A reload writes only what changed (screen-reader-speech.md) ─────────────

from core.call_log import list_update_plan


class TestListUpdatePlan:
    def test_nothing_changed_writes_nothing(self):
        rows = [("A", "Maria: perdida"), ("B", "João: atendida")]
        assert list_update_plan(rows, list(rows)) == ("none", [])

    def test_a_settled_call_rewrites_its_own_row(self):
        old = [("A", "Maria: em andamento"), ("B", "João: atendida")]
        new = [("A", "Maria: efetuada, duração: 9s"), ("B", "João: atendida")]
        assert list_update_plan(old, new) == ("set", [(0, "Maria: efetuada, duração: 9s")])

    def test_a_new_call_rebuilds(self):
        assert list_update_plan([("A", "x")], [("N", "y"), ("A", "x")]) == ("rebuild", [])

    def test_the_placeholder_to_rows_rebuilds(self):
        assert list_update_plan([("", "Carregando...")], [("A", "x")]) == ("rebuild", [])


class _CountingList(_List):
    def __init__(self):
        super().__init__(focused=-1)
        self.deletes = 0
        self.appends = []
        self.set_texts = []

    def Freeze(self):
        pass

    def Thaw(self):
        pass

    def DeleteAllItems(self):
        self.deletes += 1
        self.items = []

    def Append(self, row):
        self.appends.append(row[0])
        self.items.append(row[0])

    def SetItemText(self, index, text):
        self.set_texts.append((index, text))
        self.items[index] = text


class _ApplyPanel:
    _apply_rows = CallsPanel._apply_rows

    def __init__(self):
        self._lists = {TAB_ALL: _CountingList()}
        self._shown = {TAB_ALL: []}


class TestApplyRows:
    def test_loading_the_same_calls_twice_writes_nothing_the_second_time(self):
        panel = _ApplyPanel()
        rows = [("A", "Maria: perdida"), ("B", "João: atendida")]
        panel._apply_rows(TAB_ALL, rows)
        lst = panel._lists[TAB_ALL]
        assert lst.deletes == 1 and lst.appends == ["Maria: perdida", "João: atendida"]

        panel._apply_rows(TAB_ALL, list(rows))

        assert lst.deletes == 1 and len(lst.appends) == 2 and lst.set_texts == []

    def test_one_settled_call_rewrites_only_its_row(self):
        panel = _ApplyPanel()
        panel._apply_rows(TAB_ALL, [("A", "Maria: em andamento"), ("B", "João: atendida")])
        lst = panel._lists[TAB_ALL]

        panel._apply_rows(TAB_ALL, [("A", "Maria: efetuada"), ("B", "João: atendida")])

        assert lst.deletes == 1
        assert lst.set_texts == [(0, "Maria: efetuada")]

    def test_a_rebuild_keeps_focus_on_the_same_call(self):
        panel = _ApplyPanel()
        panel._apply_rows(TAB_ALL, [("A", "a"), ("B", "b")])
        panel._apply_rows(TAB_ALL, [("N", "n"), ("A", "a"), ("B", "b")], keep_id="B")
        assert panel._lists[TAB_ALL].focused == 2
