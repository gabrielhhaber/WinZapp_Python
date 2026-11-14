"""Live roster validation, keyboard-stable updates and stale/closed-dialog rejection. No windows."""
from types import SimpleNamespace

import pytest

from core.call_participants import (active_participants_available, held_here, parse_participants,
                                    participant_rows)
from ui.dialogs.call_participants import ParticipantListMixin
from main_window.call_participants import CallParticipantsMixin
from tests.test_calls_tab import _CountingList

PERSON = {"jid": "555000001@lid", "name": "Synthetic Alice", "isSelf": False}


def body(people=None, **kwargs):
    return {"response": {"id": "C1", "available": True,
                         "participants": [PERSON] if people is None else people, **kwargs}}


class Control:
    def __init__(self):
        self.label = ""
        self.enabled = True
        self.enable_calls = []

    def GetLabel(self):
        return self.label

    def SetLabel(self, value):
        self.label = value

    def Enable(self, value):
        self.enabled = value
        self.enable_calls.append(value)


class View(ParticipantListMixin):
    def __init__(self):
        self._alive, self._inflight, self._timer, self._shown = True, None, None, []
        self._call_id = "C1"
        self.allowed = True
        self.held = False           # the call this device is in, live
        self._allowed = lambda: self.allowed
        self._list = _CountingList()
        self._status, self._refresh_btn = Control(), Control()
        self.requests, self.spoken = [], []
        self.reply = body()
        self._mw = SimpleNamespace(
            i18n=SimpleNamespace(t=lambda key: "Connected: {count}" if key == "call_participants_count" else key),
            chat_display_name=lambda chat: chat["name"] or "Unknown contact",
            _raise_for_call_response=lambda response, action: response,
            _post_call_control=self.post,
            output=lambda text: self.spoken.append(text),
            _call_held_here=lambda call_id: self.held and call_id == self._call_id,
        )

    def post(self, endpoint, payload, **kwargs):
        self.requests.append((endpoint, payload, kwargs))
        return SimpleNamespace(json=lambda: self.reply)


@pytest.fixture
def polling(monkeypatch):
    queued, timers = [], []

    class Timer:
        def __init__(self, delay, fn):
            self.fn, self.stopped = fn, False
            timers.append(self)

        def Stop(self):
            self.stopped = True

    class Thread:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            queued.append(self.target)

    monkeypatch.setattr("ui.dialogs.call_participants.threading.Thread", Thread)
    monkeypatch.setattr("ui.dialogs.call_participants.wx.CallAfter", lambda fn, *a: fn(*a))
    monkeypatch.setattr("ui.dialogs.call_participants.wx.CallLater", Timer)
    return queued, timers


def test_names_and_you_are_shown_without_raw_whatsapp_ids():
    data = parse_participants(body([PERSON, {"jid": "555000099@c.us", "name": "", "isSelf": True}]), "C1")
    rows = participant_rows(data, View()._mw.i18n, lambda jid, hint: hint)
    assert rows == [(PERSON["jid"], "Synthetic Alice"), ("self", "call_participants_you")]
    rows = participant_rows(data[:1], View()._mw.i18n, lambda jid, hint: jid)
    assert rows[0][1] == "unknown_contact"


def test_phone_aliases_and_duplicate_self_rows_are_deduplicated():
    data = parse_participants(body([
        {**PERSON, "jid": "555000001@c.us"}, {**PERSON, "jid": "555000001@s.whatsapp.net"},
        {**PERSON, "isSelf": True}, {**PERSON, "jid": "555000099@lid", "isSelf": True},
    ]), "C1")
    assert len(data) == 2


def test_a_contact_lookup_failure_uses_the_native_name():
    def unavailable(*_args):
        raise RuntimeError("lookup unavailable")
    data = parse_participants(body(), "C1")
    assert participant_rows(data, View()._mw.i18n, unavailable)[0][1] == "Synthetic Alice"


@pytest.mark.parametrize("reply", [{}, body(id="old"), body(available="true"),
                                  body([{**PERSON, "jid": "1@g.us"}]),
                                  body([{**PERSON, "isSelf": 1}]), body(available=False)])
def test_wrong_calls_and_malformed_responses_never_become_rosters(reply):
    with pytest.raises(ValueError):
        parse_participants(reply, "C1")


def test_ended_call_has_a_distinct_result():
    assert parse_participants(body([], available=False), "C1") is None


def test_explicit_roster_read_does_not_start_a_call_or_open_devices(polling):
    queued, timers = polling
    view = View()
    view._refresh_participants()
    view._refresh_participants()
    assert len(queued) == 1
    queued.pop()()
    assert view.requests == [("participants", {"callId": "C1"}, {"timeout": 20})]
    assert view._list.items == ["Synthetic Alice"]
    assert view._status.label == "Connected: 1"
    assert not view.spoken
    old = view._list.deletes
    timers[-1].fn()
    queued.pop()()
    assert view._list.deletes == old


def test_refresh_button_stays_enabled_so_focus_never_jumps_to_close(polling):
    """Disabling the focused Refresh button on the timed refresh moved focus
    to Close on Windows, so the user's next Space closed the dialog. The
    in-flight guard alone refuses the second request."""
    queued, timers = polling
    view = View()
    view._refresh_participants()
    timers_before = len(timers)
    view._refresh_participants(SimpleNamespace())  # Refresh pressed while in flight
    assert len(queued) == 1 and len(timers) == timers_before
    queued.pop()()
    timers[-1].fn()  # the automatic 10-second refresh
    assert len(queued) == 1
    queued.pop()()
    assert len(view.requests) == 2
    assert view._refresh_btn.enable_calls == [] and view._refresh_btn.enabled


def test_refresh_pressed_during_the_timed_refresh_still_speaks_its_result(polling):
    queued, _timers = polling
    view = View()
    view.reply = body([])
    view._refresh_participants()  # the automatic refresh, silent by itself
    view._refresh_participants(SimpleNamespace())  # Refresh pressed meanwhile
    queued.pop()()
    assert view.spoken == ["call_participants_empty"]
    assert len(view.requests) == 1


def test_f5_says_how_many_are_connected(polling):
    """F5 is the only refresh (no Refresh button): it said nothing when the
    call had people in it."""
    queued, _timers = polling
    view = View()
    view._refresh_participants(announce=True)      # opening: rows are read with the focus
    queued.pop()()
    assert view.spoken == []
    view._refresh_participants(SimpleNamespace())  # F5
    queued.pop()()
    assert view.spoken == ["Connected: 1"]


def test_membership_change_keeps_focus_on_the_same_person(polling):
    queued, timers = polling
    view = View()
    view._refresh_participants()
    queued.pop()()
    view.reply = body([{**PERSON, "jid": "555000002@lid", "name": "Synthetic Bob"}, PERSON])
    timers[-1].fn()
    queued.pop()()
    assert view._list.GetFocusedItem() == 1
    assert view._list.items[1] == "Synthetic Alice"


def test_closing_before_reply_and_disposing_after_reply_stop_updates(polling):
    queued, timers = polling
    view = View()
    view._refresh_participants()
    view._dispose_participants()
    queued.pop()()
    assert not view._list.items and not timers
    view = View()
    view._refresh_participants()
    queued.pop()()
    view._dispose_participants()
    assert timers[-1].stopped
    timers[-1].fn()
    assert not queued


def test_failed_refresh_clears_stale_names_and_periodic_failures_are_silent(polling):
    queued, timers = polling
    view = View()
    view._refresh_participants()
    queued.pop()()
    view.reply = {}
    timers[-1].fn()
    queued.pop()()
    assert not view._list.items and view._status.label == "call_participants_failed"
    assert not view.spoken


def test_ending_or_locking_the_call_clears_the_dialog(polling):
    queued, timers = polling
    view = View()
    view._refresh_participants()
    queued.pop()()
    view.reply = body([], available=False)
    timers[-1].fn()
    queued.pop()()
    assert not view._list.items and view._status.label == "call_participants_ended"
    assert view._timer is None
    view.allowed = False
    before = len(view.requests)
    view._refresh_participants(announce=True)
    assert not queued and len(view.requests) == before
    assert view.spoken == ["call_participants_failed"]


def test_the_call_held_here_is_refreshed_by_its_events_not_every_10_s(polling):
    """The participant events refresh the window at once (on_call_participants);
    a timed request on top of them only made the list late."""
    queued, timers = polling
    view = View()
    view.held = True
    view._refresh_participants()
    queued.pop()()
    assert view._follows_events and not timers and view._timer is None


def test_a_call_this_device_is_not_in_is_still_asked_every_10_s(polling):
    queued, timers = polling
    view = View()                                   # the Calls tab's ongoing call
    view._refresh_participants()
    queued.pop()()
    assert not view._follows_events and len(timers) == 1


def test_the_end_of_the_held_call_is_shown_although_no_event_comes(polling):
    queued, timers = polling
    view = View()
    view.held = True
    view._refresh_participants()
    queued.pop()()
    mw = SimpleNamespace(_call_participants_dialog=view, _call_held_here=lambda call_id: view.held)
    follow = lambda: CallParticipantsMixin._participants_dialog_follow_call(mw)
    follow()                                        # still held: nothing asked
    assert not queued
    view.held = False
    view.reply = body([], available=False)
    follow()
    follow()                                        # asked once, not on every call change
    assert len(queued) == 1
    queued.pop()()
    assert view._status.label == "call_participants_ended" and view._timer is None


def test_held_here_is_the_live_call_with_that_id():
    active = {"call_id": "C1"}
    assert held_here(active, "ACTIVE", "C1") and held_here(active, "REJOINING", "C1")
    assert not held_here(active, "RINGING", "C1")
    assert not held_here(active, "ACTIVE", "C2")
    assert not held_here(None, "ACTIVE", "C1")


def test_active_call_button_dispatches_the_current_id_without_a_window():
    requests = []
    stub = SimpleNamespace(_active_voice_call={"call_id": "C1", "peer_jid": PERSON["jid"]},
        voice_call_window=object(), _voice_call_last_announced_state="ACTIVE",
        show_call_participants=lambda *a, **kw: requests.append((a, kw)))
    CallParticipantsMixin._show_active_call_participants(stub)
    assert requests == [(("C1", PERSON["jid"]), {"parent": stub.voice_call_window})]


def test_participants_control_waits_for_a_connected_real_call():
    assert not active_participants_available({"call_id": "outgoing:peer"}, "ACTIVE")
    assert not active_participants_available({"call_id": "C1"}, "")
    assert not active_participants_available(None, "ACTIVE")
    assert active_participants_available({"call_id": "C1"}, "REJOINING")


def test_a_locked_chats_participants_are_refused_out_loud_without_a_dialog(monkeypatch):
    """Refusing silently left the user pressing a button that did nothing."""
    spoken = []

    def no_dialog(*_a, **_kw):
        raise AssertionError("a locked chat's roster must not open")
    monkeypatch.setattr("ui.dialogs.call_participants.CallParticipantsDialog", no_dialog)
    stub = SimpleNamespace(is_chat_locked=lambda jid: jid == PERSON["jid"],
        i18n=SimpleNamespace(t=lambda key: key),
        output=lambda text, interrupt=False: spoken.append((text, interrupt)))
    CallParticipantsMixin.show_call_participants(stub, "C1", PERSON["jid"])
    assert spoken == [("call_participants_ended", True)]


# ---------------------------------------------- everyone, not only the connected
class _Words:
    def t(self, key):
        return {"call_participants_you": "Você", "unknown_contact": "Desconhecido",
                "call_participant_left_row": "{name}, saiu da chamada",
                "call_participant_ringing_row": "{name}, chamando",
                "call_participant_invited_row": "{name}, fora da chamada",
                "call_participant_declined_row": "{name}, recusou",
                "call_participant_missed_row": "{name}, não atendeu"}.get(key, key)


def _roster(*people):
    return {"response": {"id": "C1", "available": True, "participants": list(people)}}


def test_states_are_read_and_an_old_server_means_connected():
    from core.call_participants import parse_participants
    rows = parse_participants(_roster(
        {"jid": "1@lid", "name": "Ana", "isSelf": False},
        {"jid": "2@lid", "name": "Bia", "isSelf": False, "state": "left"}), "C1")
    assert [r["state"] for r in rows] == ["connected", "left"]
    import pytest
    with pytest.raises(ValueError):
        parse_participants(_roster({"jid": "3@lid", "name": "", "isSelf": False, "state": "dancing"}), "C1")


def test_the_list_is_ordered_like_the_phones_and_marks_each_state():
    from core.call_participants import parse_participants, participant_rows
    data = parse_participants(_roster(
        {"jid": "2@lid", "name": "Bia", "isSelf": False, "state": "left"},
        {"jid": "3@lid", "name": "Caio", "isSelf": False, "state": "ringing"},
        {"jid": "1@lid", "name": "Ana", "isSelf": False},
        {"jid": "9@lid", "name": "", "isSelf": True}), "C1")
    rows = participant_rows(data, _Words(), lambda jid, hint: hint,
                            left=[("2@s.whatsapp.net", "Bia"), ("5@lid", "Duda")])
    assert [text for _key, text in rows] == [
        "Ana", "Você", "Caio, chamando", "Bia, saiu da chamada", "Duda, saiu da chamada"]
    # Bia, reported left by the server, is not listed twice from what this device saw
    assert len({key for key, _ in rows}) == 5


def test_someone_out_of_the_call_is_not_shown_as_ringing():
    """A member who left a group call stays on its list as invited (WhatsApp
    offers to ring them; nobody is ringing them): seen live listed as
    "ringing". Out of the call, after who is being called."""
    from core.call_participants import parse_participants, participant_rows
    data = parse_participants(_roster(
        {"jid": "4@lid", "name": "Dom", "isSelf": False, "state": "invited"},
        {"jid": "3@lid", "name": "Caio", "isSelf": False, "state": "ringing"},
        {"jid": "1@lid", "name": "Ana", "isSelf": False}), "C1")
    rows = participant_rows(data, _Words(), lambda jid, hint: hint)
    assert [text for _key, text in rows] == ["Ana", "Caio, chamando", "Dom, fora da chamada"]
