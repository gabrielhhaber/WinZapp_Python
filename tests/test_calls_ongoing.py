"""Live calls appear in All/Ongoing, join by native id, and never leak locked chats."""
from types import SimpleNamespace

import pytest

from calls_panel import CallsPanel
from core.call_log import CALL_TABS, TAB_ALL, TAB_MISSED, TAB_ONGOING
from core.ongoing_calls import ongoing_entries
from tests.test_calls_tab import _Panel, _CountingList, _entry, _call

GROUP = "120363000001@g.us"
PERSON = "555000001@lid"
CALL = {"id": "C1", "chatJid": GROUP, "isVideo": False}


class Panel(_Panel):
    _populate_all = CallsPanel._populate_all
    _apply_rows = CallsPanel._apply_rows
    _entry_id = staticmethod(CallsPanel._entry_id)
    _focused_id = CallsPanel._focused_id
    _row_text = CallsPanel._row_text

    def __init__(self):
        super().__init__([])
        self._lists = {tab: _CountingList() for tab in CALL_TABS}
        self._shown = {tab: [] for tab in CALL_TABS}
        self._entries = []
        self._ongoing_entries = ongoing_entries({"response": [CALL]})
        self._ongoing_watch = None
        self._ongoing_inflight = None
        self._ongoing_timer = None
        self.visible = True
        self.requests = []
        self.joins = []
        self.reply = {"response": [CALL]}
        self.main_window._post_call_control = self.post
        self.main_window._raise_for_call_response = lambda result, action: result
        self.main_window._start_call = lambda *a, **kw: self.joins.append((a, kw))

    def IsShown(self):
        return self.visible

    def post(self, endpoint, payload, **kw):
        self.requests.append((endpoint, payload, kw))
        return SimpleNamespace(json=lambda: self.reply)


@pytest.fixture
def polling(monkeypatch):
    queued, timers = [], []

    class Timer:
        def __init__(self, delay, fn, *args):
            self.fn = lambda: fn(*args)
            self.stopped = False
            timers.append(self)

        def Stop(self):
            self.stopped = True

    class Thread:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            queued.append(self.target)

    monkeypatch.setattr("ui.ongoing_calls.threading.Thread", Thread)
    monkeypatch.setattr("ui.ongoing_calls.wx.CallAfter", lambda fn, *a: fn(*a))
    monkeypatch.setattr("ui.ongoing_calls.wx.CallLater", Timer)
    return queued, timers


def test_current_calls_are_first_in_all_and_in_their_own_tab_not_missed():
    panel = Panel()
    panel._entries = [_entry(_call(call_id="old")), _entry(_call(call_id="C1"))]
    panel._populate_all()
    assert [panel._entry_id(e) for e in panel._rows[TAB_ALL]] == ["C1", "old"]
    assert len(panel._rows[TAB_ONGOING]) == 1
    assert not any(e.get("ongoing") for e in panel._rows[TAB_MISSED])
    assert "call_log_ongoing_voice" in panel._lists[TAB_ONGOING].items[0]
    assert GROUP not in panel._lists[TAB_ONGOING].items[0]


def test_enter_and_button_join_the_selected_group_or_person_call():
    for jid in (GROUP, PERSON):
        panel = Panel()
        panel._ongoing_entries = ongoing_entries({"response": [{**CALL, "chatJid": jid, "isVideo": True}]})
        panel._populate_all()
        panel._on_item_activated(None)
        assert panel.joins == [((jid, "Grupo"), {
            "is_group": True, "is_video": True, "join_call_id": "C1"})]
        assert panel._ongoing_join_btn.shown and not panel._return_call_btn.shown


def test_locked_chats_are_hidden_and_locking_after_render_refuses_join():
    panel = Panel()
    panel._populate_all()
    panel.main_window._locked_jids.add(GROUP)
    panel._join_ongoing_call()
    assert not panel.joins
    panel._populate_all()
    assert not panel._rows[TAB_ONGOING]
    assert not panel._ongoing_join_btn.shown


def test_an_ended_call_is_not_joined_from_a_stale_row():
    panel = Panel()
    panel._populate_all()
    panel._ongoing_entries = []
    panel._join_ongoing_call()
    assert not panel.joins


def test_return_to_current_call_focuses_it_without_joining_again():
    panel = Panel()
    focused = []
    panel.main_window._active_voice_call = {"call_id": "C1"}
    panel.main_window._sync_voice_call_bar = lambda: focused.append("sync")
    panel.main_window._focus_call_window = lambda: focused.append("focus")
    panel._populate_all()
    assert "calls_return_to_current" in panel._ongoing_join_btn.label
    panel._join_ongoing_call()
    assert focused == ["sync", "focus"] and not panel.joins


def test_unchanged_live_results_do_not_rewrite_or_refocus_rows():
    panel = Panel()
    panel._populate_all()
    lst = panel._lists[TAB_ALL]
    before = (len(lst.inserted), len(lst.deleted), len(lst.set_texts), lst.deletes, lst.focus_calls)
    panel._populate_all()
    assert (len(lst.inserted), len(lst.deleted), len(lst.set_texts), lst.deletes,
            lst.focus_calls) == before


def test_polling_refreshes_only_while_visible_and_one_request_at_a_time(polling):
    queued, timers = polling
    panel = Panel()
    panel._watch_ongoing_calls()
    panel._watch_ongoing_calls()
    assert len(queued) == 1
    queued.pop()()
    assert panel.requests == [("ongoing", {}, {"timeout": 20})]
    assert panel._rows[TAB_ONGOING]
    panel.reply = {"response": []}
    timers[-1].fn()
    queued.pop()()
    assert not panel._rows[TAB_ONGOING]
    assert not panel._ongoing_join_btn.shown
    panel.visible = False
    timers[-1].fn()
    assert not queued


def test_late_reply_after_hide_and_reopen_cannot_change_new_watch(polling):
    queued, timers = polling
    panel = Panel()
    panel._watch_ongoing_calls()
    event = SimpleNamespace(GetEventObject=lambda: panel, IsShown=lambda: False, Skip=lambda: None)
    panel._on_ongoing_visibility(event)
    panel._ongoing_entries = []
    panel._watch_ongoing_calls()
    queued.pop()()
    assert not panel._ongoing_entries
    timers[-1].fn()
    queued.pop()()
    assert panel._ongoing_entries


def test_unavailable_discovery_clears_joinable_state_instead_of_showing_history_as_live(polling):
    queued, _ = polling
    panel = Panel()
    panel.reply = {"response": None}
    panel._watch_ongoing_calls()
    queued.pop()()
    assert panel._ongoing_unavailable
    assert not panel._rows[TAB_ONGOING]
    assert "calls_ongoing_unavailable" in panel._lists[TAB_ONGOING].items[0]


def test_wire_parser_rejects_invalid_chats_and_duplicate_calls():
    rows = ongoing_entries({"response": [CALL, CALL, {}, {**CALL, "id": "x", "chatJid": "status@broadcast"},
                                         {**CALL, "id": "y", "isVideo": "false"}]})
    assert len(rows) == 1
    with pytest.raises(ValueError):
        ongoing_entries({"response": None})


def test_current_individual_call_appears_even_without_a_joinable_group_log():
    panel = Panel()
    panel._ongoing_entries = []
    panel.main_window._voice_call_last_announced_state = "ACTIVE"
    panel.main_window._active_voice_call = {"call_id": "individual", "peer_jid": PERSON, "is_video": False}
    panel._populate_all()
    assert len(panel._rows[TAB_ONGOING]) == 1
    assert "calls_return_to_current" in panel._ongoing_join_btn.label
    panel.main_window._active_voice_call = None
    panel._join_ongoing_call()
    assert not panel.joins


def test_an_outgoing_attempt_is_not_presented_as_an_established_current_call():
    panel = Panel()
    panel._ongoing_entries = []
    panel.main_window._voice_call_last_announced_state = "ACTIVE"
    panel.main_window._active_voice_call = {"call_id": "outgoing:"+PERSON, "peer_jid": PERSON}
    panel._populate_all()
    assert not panel._rows[TAB_ONGOING]


def test_participants_button_opens_only_the_selected_live_unlocked_call():
    panel = Panel()
    opened = []
    panel.main_window.show_call_participants = lambda *a, **kw: opened.append((a, kw))
    panel._populate_all()
    assert panel._ongoing_participants_btn.shown
    panel._show_ongoing_participants()
    assert opened == [(("C1", GROUP), {"parent": panel, "title": "Grupo", "people": ()})]
    panel.main_window._locked_jids.add(GROUP)
    panel._show_ongoing_participants()
    assert len(opened) == 1


def test_a_legacy_phone_chat_id_is_canonical_so_a_locked_chat_stays_hidden():
    """The page can report a person's chat as ``@c.us``; locks and names are
    keyed on ``@s.whatsapp.net``, so the legacy form would leak a locked chat."""
    phone = "555000002@s.whatsapp.net"
    rows = ongoing_entries({"response": [{**CALL, "chatJid": "555000002@c.us"}]})
    assert rows[0]["jid"] == rows[0]["ongoing"]["chatJid"] == phone
    panel = Panel()
    panel._ongoing_entries = rows
    panel.main_window._locked_jids.add(phone)
    panel._populate_all()
    assert not panel._rows[TAB_ONGOING]


PERSON_CHAT = "5511999990000@s.whatsapp.net"
CREATOR_LID = "123456789012345@lid"


def same_chat(a, b):
    return a == b or {a, b} == {PERSON_CHAT, CREATOR_LID}


def test_the_call_of_a_one_to_one_chat_is_found_under_the_creators_lid():
    from core.ongoing_calls import ongoing_for_chat
    entries = [{"jid": "1203@g.us", "ongoing": {"id": "G", "chatJid": "1203@g.us", "isVideo": False}},
               {"jid": CREATOR_LID, "ongoing": {"id": "C1", "chatJid": CREATOR_LID, "isVideo": True}}]
    assert ongoing_for_chat(entries, PERSON_CHAT, same_chat) == {
        "id": "C1", "groupJid": PERSON_CHAT, "chatJid": CREATOR_LID, "isVideo": True}
    assert ongoing_for_chat(entries, "5522@s.whatsapp.net", same_chat) is None


@pytest.mark.parametrize("ongoing, active, kind", [
    (None, None, ""),
    ({"id": "C1", "groupJid": PERSON_CHAT, "isVideo": False}, None, "join"),
    ({"id": "C1", "groupJid": "other@s.whatsapp.net", "isVideo": False}, None, ""),
    (None, {"call_id": "C1", "peer_jid": CREATOR_LID}, "return"),
    (None, {"call_id": "C2", "peer_jid": "5522@s.whatsapp.net"}, ""),
    ({"id": "C1", "groupJid": PERSON_CHAT, "isVideo": False},
     {"call_id": "C1", "peer_jid": "5599@lid"}, "return"),
])
def test_what_a_chat_offers_in_place_of_its_call_buttons(ongoing, active, kind):
    from core.ongoing_calls import chat_call_button
    assert chat_call_button(PERSON_CHAT, ongoing, active, same_chat) == kind


ME_LID = "555000099@lid"


def test_the_people_of_a_call_are_read_canonical_and_only_when_valid():
    rows = ongoing_entries({"response": [
        {**CALL, "id": "M1", "chatJid": ME_LID,
         "people": ["555000002@c.us", "555000002@s.whatsapp.net", "bad", 7, "555000003@lid"]},
        {**CALL, "id": "M2", "chatJid": PERSON, "people": "not a list"}]})
    assert rows[0]["ongoing"]["people"] == ["555000002@s.whatsapp.net", "555000003@lid"]
    assert "people" not in rows[1]["ongoing"]


def test_a_call_this_account_started_on_its_phone_is_offered_in_its_peoples_chats():
    """Reported 2026-11-22: the creator left the call on the phone and could
    not join it from WinZapp, where it was filed under the account itself."""
    from core.ongoing_calls import ongoing_for_chat
    is_self = lambda jid: jid == ME_LID
    entries = ongoing_entries({"response": [
        {**CALL, "id": "M1", "chatJid": ME_LID, "people": [CREATOR_LID, "555000003@lid"]}]})
    assert ongoing_for_chat(entries, PERSON_CHAT, same_chat, is_self) == {
        "id": "M1", "groupJid": PERSON_CHAT, "chatJid": ME_LID, "isVideo": False}
    assert ongoing_for_chat(entries, "555000003@lid", same_chat, is_self)["id"] == "M1"
    assert ongoing_for_chat(entries, "555000004@lid", same_chat, is_self) is None
    # someone else's call is offered only in its creator's chat
    assert ongoing_for_chat(entries, PERSON_CHAT, same_chat) is None


def test_open_conversation_of_a_call_started_on_the_phone_goes_to_a_person():
    from core.ongoing_calls import ongoing_conversation_jid
    is_self = lambda jid: jid == ME_LID
    own, = ongoing_entries({"response": [{**CALL, "chatJid": ME_LID, "people": [PERSON_CHAT]}]})
    group, = ongoing_entries({"response": [CALL]})
    assert ongoing_conversation_jid(own, is_self) == PERSON_CHAT
    assert ongoing_conversation_jid(group, is_self) == GROUP


def test_a_live_row_named_by_its_people_is_joined_by_that_name():
    panel = Panel()
    panel._ongoing_entries = ongoing_entries({"response": [
        {**CALL, "chatJid": ME_LID, "people": [PERSON_CHAT]}]})
    panel.main_window.ongoing_call_people_text = (
        lambda ongoing: "Maria" if ongoing.get("people") else "")
    panel._populate_all()
    assert panel._lists[TAB_ONGOING].items[0].startswith("Maria: ")
    panel._on_item_activated(None)
    assert panel.joins == [((ME_LID, "Maria"), {
        "is_group": True, "is_video": False, "join_call_id": "C1"})]
