"""core/call_invites.py and its wiring: adding people to the call in progress."""
import pytest

from core.call_invites import (
    candidates,
    parse_results,
    room_left,
    saved_contact_jids,
    summary,
)
from main_window.call_participants import CallParticipantsMixin


class I18n:
    def t(self, key):
        return {"call_invite_sent": "sent {count}", "call_invite_failed_people": "not {names}",
                "call_invite_failed": "failed", "unknown_contact": "unknown"}[key]


def same(a, b):
    """'1@lid' and '11@s.whatsapp.net' are one person here."""
    pair = {"1@lid", "11@s.whatsapp.net"}
    return a == b or {a, b} == pair


def test_candidates_leave_out_who_is_in_the_call_self_and_non_people():
    rows = candidates(
        ["11@s.whatsapp.net", "22@c.us", "99@s.whatsapp.net", "1203@g.us", "33@lid", "22@s.whatsapp.net"],
        in_call=["1@lid"], is_self=lambda jid: jid == "99@s.whatsapp.net", same_person=same,
        name_of={"22@s.whatsapp.net": "  Zé  Silva ", "33@lid": "ana"}.get)
    assert rows == [("33@lid", "ana"), ("22@s.whatsapp.net", "Zé Silva")]


def test_saved_contacts_are_the_address_book_ones():
    contacts = {"1@s.whatsapp.net": {"isMyContact": True}, "2@lid": {"name": "x"},
                "3@s.whatsapp.net": {"syncToAddressbook": True}, "4@lid": "bad"}
    assert saved_contact_jids(contacts) == ["1@s.whatsapp.net", "3@s.whatsapp.net"]


@pytest.mark.parametrize("connected, left", [(0, 32), (2, 30), (32, 0), (40, 0), (None, 32)])
def test_room_left_follows_the_group_call_cap(connected, left):
    assert room_left(connected) == left


def test_results_are_read_for_the_people_asked():
    body = {"response": {"id": "C1", "results": [
        {"jid": "11@s.whatsapp.net", "status": "invited"}, {"jid": "2@lid", "status": "rang"}]}}
    assert parse_results(body, "C1", ["11@c.us", "2@lid"]) == {"11@c.us": "invited", "2@lid": "rang"}


@pytest.mark.parametrize("body", [
    {"response": {"id": "OTHER", "results": []}},
    {"response": {"id": "C1", "results": [{"jid": "5@lid", "status": "invited"}]}},
    {"response": {"id": "C1", "results": [{"jid": "2@lid", "status": "maybe"}]}},
    {"status": "error"},
])
def test_a_wrong_answer_is_refused(body):
    with pytest.raises(ValueError):
        parse_results(body, "C1", ["2@lid"])


def test_the_summary_counts_and_names_who_failed():
    names = {"a": "Ana", "b": "Bia", "c": ""}
    assert summary({"a": "invited", "b": "rang", "c": "failed"}, names, I18n()) == "sent 2 not unknown"
    assert summary({"a": "failed"}, {"a": "Ana", "b": "Bia"}, I18n()) == "not Ana, Bia"
    assert summary({}, {}, I18n()) == "failed"


class Window:
    def __init__(self, active, state="ACTIVE"):
        self._active_voice_call = active
        self._voice_call_last_announced_state = state


@pytest.mark.parametrize("active, state, asked, chat", [
    ({"call_id": "C1", "peer_jid": "1203@g.us"}, "ACTIVE", "C1", "1203@g.us"),
    ({"call_id": "C1", "peer_jid": "55@lid"}, "ACTIVE", "C1", "55@lid"),      # a private call too
    ({"call_id": "C1", "peer_jid": "1203@g.us"}, "ACTIVE", "C2", ""),        # another call
    ({"call_id": "C1", "peer_jid": "1203@g.us"}, "CALLING", "C1", ""),       # not in it yet
    ({}, "ACTIVE", "C1", ""),
])
def test_people_are_offered_only_for_the_call_this_device_is_in(active, state, asked, chat):
    window = Window(active, state)
    assert CallParticipantsMixin._call_invite_chat(window, asked, "") == chat


def test_a_contact_kept_under_both_addresses_is_offered_once():
    from core.call_invites import one_per_person
    jids = ["55@s.whatsapp.net", "1@lid", "77@s.whatsapp.net", "55@c.us"]
    assert one_per_person(jids, {"1@lid": "55@s.whatsapp.net"}) == ["1@lid", "77@s.whatsapp.net"]
