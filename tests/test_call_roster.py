"""Who joined and left a group call is said, from the call-participants event
(both engines send the whole connected list); the participants dialog follows
it instead of a Refresh button."""

import types

from core.call_roster import KEPT, remember, roster_changes
from main import MainWindow


def test_the_first_report_says_nothing():
    assert roster_changes(None, ["1@lid", "2@lid"]) == ([], [])


def test_joined_and_left_are_the_differences():
    assert roster_changes(["1@lid", "2@lid"], ["2@lid", "3@lid"]) == (["3@lid"], ["1@lid"])


def test_the_account_itself_never_joins_or_leaves():
    assert roster_changes(["1@lid"], ["1@lid", "555@lid"], {"555"}) == ([], [])


def test_only_the_last_calls_are_kept():
    rosters = {}
    for n in range(KEPT + 3):
        remember(rosters, f"C{n}", [])
    assert len(rosters) == KEPT and "C0" not in rosters


def _window(state="ACTIVE", settings=None):
    said, refreshed = [], []
    win = types.SimpleNamespace(
        my_jid="555@s.whatsapp.net", my_lid="", output=said.append,
        settings=settings or {}, _active_voice_call={"call_id": "G1"},
        _voice_call_last_announced_state=state,
        i18n=types.SimpleNamespace(t=lambda key: {"call_participant_joined": "{name} joined",
                                                  "call_participant_left": "{name} left"}[key]),
        chat_display_name=lambda chat: {"1@lid": "Ana", "2@lid": "Bia"}.get(chat["remoteJid"], "?"),
        _call_participants_dialog=types.SimpleNamespace(
            _call_id="G1", _refresh_participants=lambda: refreshed.append(True)))
    win.on_call_participants = types.MethodType(MainWindow.on_call_participants, win)
    win._call_held_here = types.MethodType(MainWindow._call_held_here, win)
    return win, said, refreshed


def test_the_call_held_here_says_who_joined_and_left():
    win, said, refreshed = _window()
    win.on_call_participants("G1", ["1@lid"])
    win.on_call_participants("G1", ["2@lid", "555@s.whatsapp.net"])
    assert said == ["Bia joined", "Ana left"]
    assert refreshed == [True, True]


def test_each_announcement_can_be_turned_off():
    win, said, _refreshed = _window(settings={"calls": {"announce_participant_left": False}})
    win.on_call_participants("G1", ["1@lid"])
    win.on_call_participants("G1", ["2@lid"])
    assert said == ["Bia joined"]


def test_another_call_or_one_not_live_says_nothing():
    win, said, refreshed = _window(state="RINGING")
    win.on_call_participants("G1", ["1@lid"])
    win.on_call_participants("G1", ["2@lid"])
    win._active_voice_call = {"call_id": "OTHER"}
    win.on_call_participants("G2", ["1@lid"])
    win.on_call_participants("G2", ["2@lid"])
    assert said == [] and refreshed == [True, True]


def test_the_socket_hands_a_roster_to_the_window(monkeypatch):
    from core import websocket_client as wsc
    got = []
    monkeypatch.setattr(wsc.wx, "CallAfter", lambda fn, *a: fn(*a))
    client = types.SimpleNamespace(
        main_window=types.SimpleNamespace(on_call_participants=lambda c, l: got.append((c, l))),
        _belongs_to_this_session=lambda data: data.get("session") == "me")
    client.on_call_participants = types.MethodType(wsc.WebSocketClient.on_call_participants, client)
    client.on_call_participants({"session": "me", "callId": "G1", "connected": ["1@lid"]})
    client.on_call_participants({"session": "me", "callId": "G1", "connected": "1@lid"})
    client.on_call_participants({"session": "other", "callId": "G1", "connected": []})
    assert got == [("G1", ["1@lid"])]


def test_who_was_seen_and_is_gone_stays_listed():
    from core.call_roster import left_of, note_seen
    seen = {}
    note_seen(seen, "C1", [("1@lid", "Ana"), ("2@lid", ""), ("9@lid", "eu")], mine={"9"})
    note_seen(seen, "C1", [("2@lid", "Bia")])
    assert left_of(seen, "C1", ["1:4@lid"]) == [("2@lid", "Bia")]
    assert left_of(seen, "OTHER", []) == []


# -- the same person by number and by @lid (user report, 2026-11-14) ----------
_PERSON = {"7@lid": "5511@s.whatsapp.net"}.get


def _person(jid):
    return _PERSON(jid) or jid


def test_someone_reported_under_their_other_address_neither_left_nor_joined():
    assert roster_changes(["7@lid", "2@lid"], ["5511@s.whatsapp.net", "2@lid"],
                          person=_person) == ([], [])
    # without the mapping the two addresses are two people, as before
    assert roster_changes(["7@lid"], ["5511@s.whatsapp.net"]) == (
        ["5511@s.whatsapp.net"], ["7@lid"])


def test_a_person_seen_by_lid_and_still_listed_by_number_has_not_left():
    from core.call_roster import left_of, note_seen
    seen = {}
    note_seen(seen, "C1", [("7@lid", "Ana"), ("2@lid", "Bia")])
    assert left_of(seen, "C1", ["5511@s.whatsapp.net"], _person) == [("2@lid", "Bia")]
    assert left_of(seen, "C1", ["5511@s.whatsapp.net"]) == [("7@lid", "Ana"), ("2@lid", "Bia")]


def test_a_person_seen_under_both_addresses_leaves_once():
    from core.call_roster import left_of, note_seen
    seen = {}
    note_seen(seen, "C1", [("7@lid", "Ana"), ("5511@s.whatsapp.net", "Ana")])
    assert left_of(seen, "C1", [], _person) == [("7@lid", "Ana")]


def test_the_window_compares_people_through_the_lid_mapping():
    win = types.SimpleNamespace(_lid_to_phone={"7@lid": "5511@s.whatsapp.net"})
    assert MainWindow._call_person(win, "7@lid") == "5511@s.whatsapp.net"
    # a companion device of the person is the same person
    assert MainWindow._call_person(win, "7:4@lid") == "5511@s.whatsapp.net"
    assert MainWindow._call_person(win, "9@lid") == "9@lid"
    assert MainWindow._call_person(types.SimpleNamespace(), "9@lid") == "9@lid"
