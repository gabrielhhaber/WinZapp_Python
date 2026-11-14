"""System messages: group notices (gp2) and call records (call_log) built
from fake events, with an in-memory Store (no DLL and no account)."""
import sys
import types
from types import SimpleNamespace as N

import calls
import group_events as ge
import system_messages as sm
from store import Store

NOW = 1700000100


def jid(user, server="s.whatsapp.net"):
    return N(User=user, Server=server)


def person(j):
    return ge.msgmap.jid_wpp(ge.jid_text(j))


def group(**fields):
    base = dict(JID=jid("120", "g.us"), Sender=jid("1"), Timestamp=NOW - 5,
                Join=[], Leave=[], Promote=[], Demote=[])
    base.update(fields)
    return N(**base)


def notices(**fields):
    return sm.group_notices(group(**fields), person, NOW)


class Session:
    name = "s1"
    me = "9@s.whatsapp.net"
    lid = "99@lid"

    def __init__(self):
        self.db = Store(":memory:")
        self.sent = []

    def emit(self, event, data):
        self.sent.append((event, data))


# ------------------------------------------------------------- group notices
def test_new_name_becomes_gp2_subject_with_the_name_in_the_body():
    (w,) = notices(Name=N(Name="  Family "))
    assert (w["type"], w["subtype"], w["body"]) == ("gp2", "subject", "Family")
    assert w["author"] == "1@c.us" and w["recipients"] == []
    assert w["to"] == w["from"] == "120@g.us" and w["isStatus"] is False
    assert w["t"] == w["timestamp"] == NOW - 5 and w["fromMe"] is False
    assert w["id"]["remote"] == "120@g.us"
    assert w["id"]["_serialized"] == f"false_120@g.us_{w['id']['id']}_1@c.us"


def test_new_and_deleted_description():
    (new,) = notices(Topic=N(Topic="rules", TopicDeleted=False))
    assert (new["subtype"], new["body"]) == ("description", "rules")
    (deleted,) = notices(Topic=N(Topic="old", TopicDeleted=True))
    assert deleted["body"] == ""


def test_settings_carry_on_off_in_the_body_and_a_boolean_in_the_value():
    locked, announcement, ephemeral = notices(
        Locked=N(isLocked=True), Announce=N(IsAnnounce=False),
        Ephemeral=N(IsEphemeral=True, DisappearingTimer=86400))
    assert (locked["subtype"], locked["body"], locked["value"]) == ("restrict", "on", True)
    assert (announcement["subtype"], announcement["body"], announcement["value"]) == ("announce", "off", False)
    assert (ephemeral["subtype"], ephemeral["value"]) == ("ephemeral", True)


def test_members_become_one_notice_per_action():
    ws = notices(Join=[jid("2"), jid("3")], Promote=[jid("4")], Demote=[jid("5")])
    assert [(w["subtype"], w["recipients"]) for w in ws] == [
        ("add", ["2@c.us", "3@c.us"]), ("promote", ["4@c.us"]), ("demote", ["5@c.us"])]
    assert all(w["author"] == "1@c.us" for w in ws)


def test_whoever_leaves_alone_is_leave_and_whoever_is_removed_by_another_is_remove():
    (left,) = notices(Sender=jid("2"), Leave=[jid("2")])
    assert left["subtype"] == "leave" and left["recipients"] == ["2@c.us"]
    assert "author" not in left
    (removed,) = notices(Sender=jid("1"), Leave=[jid("2")])
    assert (removed["subtype"], removed["author"]) == ("remove", "1@c.us")
    (no_author,) = notices(Sender=None, Leave=[jid("2")])
    assert no_author["subtype"] == "leave"


def test_joining_by_link_has_no_author_and_lid_becomes_a_number():
    pairs = {"99@lid": "5511@c.us"}

    def with_pair(j):
        return pairs.get(person(j), person(j))

    e = group(Sender=jid("99", "lid"), Join=[jid("99", "lid")], JoinReason="invite")
    (w,) = sm.group_notices(e, with_pair, NOW)
    # WhatsApp Web's own subtype for a join by link: the client says "joined
    # using the invite link", as it does on WPPConnect
    assert w["subtype"] == "invite" and w["recipients"] == ["5511@c.us"]
    assert "author" not in w


def test_joining_for_another_reason_stays_a_join_without_author():
    e = group(Sender=jid("99"), Join=[jid("99")], JoinReason="linked_group_join")
    (w,) = sm.group_notices(e, person, NOW)
    assert w["subtype"] == "add" and "author" not in w
    (added,) = notices(Join=[jid("2")])
    assert added["subtype"] == "add" and added["author"] == "1@c.us"


def _joined(reason="invite", version="v1"):
    return N(Reason=reason, Type="", GroupInfo=N(JID=jid("120", "g.us"),
                                                ParticipantVersionID=version))


def test_the_account_joining_by_link_gets_its_own_notice():
    w = sm.own_join_notice(_joined(), "9@c.us", NOW)
    assert (w["type"], w["subtype"], w["recipients"]) == ("gp2", "invite", ["9@c.us"])
    assert w["id"]["remote"] == "120@g.us" and w["t"] == NOW and "author" not in w
    # the same join delivered twice is one notice; a later join is another
    assert sm.own_join_notice(_joined(), "9@c.us", NOW + 30)["id"]["id"] == w["id"]["id"]
    assert sm.own_join_notice(_joined(version="v2"), "9@c.us", NOW)["id"]["id"] != w["id"]["id"]


def test_creating_or_being_added_is_not_a_join_by_link():
    assert sm.own_join_notice(_joined(reason=""), "9@c.us", NOW) is None
    assert sm.own_join_notice(_joined(), "", NOW) is None


def test_several_changes_of_the_same_event_have_different_ids_and_repeating_gives_the_same():
    fields = dict(Name=N(Name="X"), Join=[jid("2")], Leave=[jid("3")])
    ids = [w["id"]["id"] for w in notices(**fields)]
    assert len(set(ids)) == 3
    assert ids == [w["id"]["id"] for w in notices(**fields)]
    assert all("_" not in i for i in ids)
    another = [w["id"]["id"] for w in notices(Name=N(Name="Y"))]
    assert another[0] not in ids


def test_event_outside_a_group_or_without_a_change_generates_no_notice():
    assert notices(JID=jid("5511")) == []
    assert notices() == []
    assert sm.group_notices(N(), person, NOW) == []
    assert notices(Name=N(Name="  ")) == []


def test_event_without_a_date_uses_the_current_time():
    (w,) = notices(Timestamp=0, Name=N(Name="X"))
    assert w["t"] == NOW


# ------------------------------------------------------- store and emit
def test_store_and_emit_stores_emits_and_adds_no_unread():
    s = Session()
    (w,) = notices(Name=N(Name="X"))
    assert sm.store_and_emit(s, "120@g.us", w, NOW)
    assert s.db.messages("120@g.us") == [w]
    assert s.db.unread("120@g.us") == 0
    # a group notice does not lift the chat (the client never lets one move it)
    assert s.db.chats()[0]["ts"] == 0
    event, data = s.sent[0]
    assert event == "received-message" and data["session"] == "s1"
    assert data["response"] == w            # recent: without the history mark


def test_the_same_event_twice_does_not_duplicate_nor_emit_again():
    s = Session()
    (w,) = notices(Name=N(Name="X"))
    assert sm.store_and_emit(s, "120@g.us", w, NOW)
    assert not sm.store_and_emit(s, "120@g.us", w, NOW)
    assert len(s.sent) == 1 and len(s.db.messages("120@g.us")) == 1


def test_an_old_notice_goes_out_marked_as_history_but_is_stored_without_the_mark():
    s = Session()
    (w,) = sm.group_notices(group(Timestamp=NOW - 3600, Name=N(Name="X")), person, NOW)
    sm.store_and_emit(s, "120@g.us", w, NOW)
    assert s.sent[0][1]["response"]["isMdHistoryMsg"] is True
    assert "isMdHistoryMsg" not in s.db.messages("120@g.us")[0]


def test_system_counts_neither_as_last_received_nor_as_to_read():
    s = Session()
    real = {"id": {"fromMe": False, "remote": "120@g.us", "id": "REAL", "_serialized": "x"},
            "fromMe": False, "t": NOW - 20, "type": "chat", "body": "oi", "from": "1@c.us"}
    s.db.message("120@g.us", real)
    s.db.on_arrival("120@g.us", real["t"], False)
    (w,) = notices(Name=N(Name="X"))
    sm.store_and_emit(s, "120@g.us", w, NOW)
    assert s.db.last_received("120@g.us") == "REAL"
    assert [x[0] for x in s.db.received_to_read("120@g.us")] == ["REAL"]


# -------------------------------------------------------------- calls
def node(tag="", attrs=None, children=()):
    return N(Tag=tag, Attrs=[N(name=k, text=v) for k, v in (attrs or {}).items()],
             Nodes=list(children))


def end(call_id="C1", reason="timeout", creator=None, alternative=None, data=None):
    meta = N(callID=call_id, timestamp=NOW - 3, callCreator=creator or jid("5511"),
             callCreatorAlt=alternative or jid(""))
    return N(basicCallMeta=meta, reason=reason, data=data or node("terminate"))


def finish(s, e, was_ringing=True):
    return calls.record_end(s, e, calls.end_event(e, NOW), was_ringing, NOW)


def test_outcomes():
    assert sm.call_outcome("NOT_ANSWERED", False) == "Missed"
    assert sm.call_outcome("REJECTED", False) == "Rejected"
    assert sm.call_outcome("HANDLED_REMOTELY", False) == "AcceptedElsewhere"
    assert sm.call_outcome("ENDED", True) == "Canceled"
    assert sm.call_outcome("ENDED", False) is None


def test_missed_call_becomes_call_log_with_the_call_id():
    s = Session()
    assert finish(s, end())
    (w,) = s.db.messages("5511@s.whatsapp.net")
    assert (w["type"], w["callOutcome"], w["finalCallOutcome"]) == ("call_log", "Missed", "Missed")
    assert w["id"]["id"] == "C1" and w["id"]["_serialized"] == "false_5511@c.us_C1"
    assert w["isVideoCall"] is False and w["fromMe"] is False
    assert w["callCreator"] == "5511@c.us" and w["t"] == NOW - 3
    assert s.db.unread("5511@s.whatsapp.net") == 0
    assert s.sent[0][0] == "received-message"
    assert s.db.wpp_message("C1") == w          # the client rereads it through message-by-id
    # a call is activity: it lifts the chat, as a call does in WhatsApp
    assert s.db.chats()[0]["ts"] == NOW - 3


def test_refused_video_call_and_the_same_call_is_only_recorded_once():
    s = Session()
    e = end(reason="reject", data=node("terminate", children=[node("video")]))
    assert finish(s, e, was_ringing=False)
    assert not finish(s, e, was_ringing=False)
    (w,) = s.db.messages("5511@s.whatsapp.net")
    assert w["callOutcome"] == "Rejected" and w["isVideoCall"] is True
    assert len(s.sent) == 1


def test_end_without_a_reason_of_a_call_that_was_not_ringing_here_is_not_recorded():
    s = Session()
    assert not finish(s, end(reason=""), was_ringing=False)
    assert s.sent == [] and s.db.chats() == []


def test_group_call_goes_to_the_group_with_who_called():
    s = Session()
    e = end(data=node("terminate", {"group-jid": "123-4@g.us"}))
    assert finish(s, e)
    (w,) = s.db.messages("123-4@g.us")
    assert w["to"] == "123-4@g.us" and w["callCreator"] == "5511@c.us"
    assert w["id"]["_serialized"] == "false_123-4@g.us_C1_5511@c.us"
    assert "author" not in w     # the client names who called through callCreator


def test_my_own_call_from_another_device_is_not_recorded():
    s = Session()
    assert not finish(s, end(creator=jid("9")))
    assert not finish(s, end(creator=jid("99", "lid")))
    assert s.sent == []


def test_call_chat_follows_the_form_the_chat_already_has():
    s = Session()
    meta = end(creator=jid("77", "lid"), alternative=jid("5511")).basicCallMeta
    assert calls.call_chat(s.db, meta, "") == "5511@s.whatsapp.net"
    s.db.chat("77@lid")
    assert calls.call_chat(s.db, meta, "") == "77@lid"
    empty = N(callCreator=jid(""), callCreatorAlt=jid(""))
    assert calls.call_chat(s.db, empty, "") == ""


# ------------------------------------------- wiring to the handlers (without neonize)
class _Cli:
    def __init__(self):
        self.handlers = {}

    def event(self, kind):
        def bind(f):
            self.handlers[kind] = f
            return f
        return bind

    def get_pn_from_lid(self, j):
        raise KeyError(j.User)


def test_group_changed_emits_groups_update_and_the_gp2_notice():
    s = Session()
    ge.group_changed(_Cli(), s, group(Name=N(Name="New")), now=NOW)
    assert [e for e, _ in s.sent] == ["groups.update", "received-message"]
    assert s.sent[1][1]["response"]["subtype"] == "subject"
    ge.group_changed(_Cli(), s, group(Name=N(Name="New")), now=NOW)   # repeated
    assert [e for e, _ in s.sent].count("received-message") == 1


def test_group_changed_survives_a_session_without_a_store():
    s = N(name="s1", emit=lambda *a: None)
    ge.group_changed(_Cli(), s, group(Name=N(Name="New")), now=NOW)


def _fake_events(monkeypatch):
    events = types.ModuleType("neonize.events")
    events.CallOfferEv = type("CallOfferEv", (), {})
    events.CallAcceptEv = type("CallAcceptEv", (), {})
    events.CallTerminateEv = type("CallTerminateEv", (), {})
    package = types.ModuleType("neonize")
    package.events = events
    monkeypatch.setitem(sys.modules, "neonize", package)
    monkeypatch.setitem(sys.modules, "neonize.events", events)
    return events


def test_the_end_of_a_call_is_recorded_through_the_handler(monkeypatch):
    events = _fake_events(monkeypatch)
    cli, s = _Cli(), Session()
    calls.register_events(cli, s)
    s.ringing_calls.remember("C1", jid("5511"), 1)
    cli.handlers[events.CallTerminateEv](cli, end(reason=""))
    (w,) = s.db.messages("5511@s.whatsapp.net")
    assert w["callOutcome"] == "Canceled"
    assert [e for e, _ in s.sent] == ["incomingcall", "callstate", "received-message"]
    cli.handlers[events.CallTerminateEv](cli, N())      # broken: does not raise


def test_accept_followed_by_the_end_records_answered_on_another_device_through_the_handler(monkeypatch):
    events = _fake_events(monkeypatch)
    cli, s = _Cli(), Session()
    calls.register_events(cli, s)
    s.ringing_calls.remember("C1", jid("5511"), 1)
    accept = N(basicCallMeta=N(callID="C1", timestamp=NOW - 33, callCreator=jid("5511"),
                               callCreatorAlt=jid("")))
    cli.handlers[events.CallAcceptEv](cli, accept)
    cli.handlers[events.CallAcceptEv](cli, N())         # broken: does not raise
    cli.handlers[events.CallTerminateEv](cli, end(reason=""))
    (w,) = s.db.messages("5511@s.whatsapp.net")
    assert w["callOutcome"] == "AcceptedElsewhere" and w["callDuration"] == 30
    callstate = [d for e, d in s.sent if e == "callstate"][0]
    assert callstate["data"]["state"] == "HANDLED_REMOTELY"


def test_a_call_answered_on_the_phone_does_not_become_missed_and_carries_the_duration():
    """An end without a reason after a CallAccept was stored as "Canceled", which
    the client counts as missed, although the user had answered on the phone."""
    s = Session()
    e = end(reason="")
    data = calls.refine_end(calls.end_event(e, NOW), (NOW - 33, NOW - 33))
    assert calls.record_end(s, e, data, True, NOW)
    (w,) = s.db.messages("5511@s.whatsapp.net")
    assert (w["callOutcome"], w["finalCallOutcome"]) == ("AcceptedElsewhere", "AcceptedElsewhere")
    assert w["callDuration"] == 30                  # (NOW - 3) - (NOW - 33)


def test_end_without_a_reason_and_without_an_accept_is_still_canceled():
    s = Session()
    assert finish(s, end(reason=""), was_ringing=True)
    (w,) = s.db.messages("5511@s.whatsapp.net")
    assert (w["callOutcome"], w["callDuration"]) == ("Canceled", 0)
