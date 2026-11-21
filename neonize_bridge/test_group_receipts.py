"""Group receipts: the ack of a message of ours in a group only advances when ALL
the recipients have got it. Pure functions, the real Session (ReceiptEv handler,
message-ack and get-messages) against a fake neonize client.

Nothing here talks to a real account; the blue-tick rule in a real group
was not verified (COVERAGE.md)."""
import sys
import time
import types
from types import SimpleNamespace as N

import pytest

import group_receipts as gr
import server
from group_receipts import Member, evaluate, keys_of, recipients
from test_chats_server import ME, Fake, _open, _msg, sessions  # noqa: F401

GROUP = "120363000000000001@g.us"
A = "5511911110001@s.whatsapp.net"
B = "5511911110002@s.whatsapp.net"
C = "5511911110003@s.whatsapp.net"
LID_A, LID_B, LID_C = "1001@lid", "1002@lid", "1003@lid"
MY_LID = "999@lid"


def member(phone, lid="", since=0):
    return gr.member_from_address(phone, lid, since)


def trio():
    return [member(A, LID_A), member(B, LID_B), member(C, LID_C)]


def r(who, stage, ts=1000):
    return (who, stage, ts)


NOW = 1000 + 60       # a minute after the receipts of ts=1000: within the grace


# ------------------------------------------------------------------ addresses
def test_keys_join_the_forms_of_the_same_address():
    assert keys_of("5511911110001@c.us") == keys_of("5511911110001:7@s.whatsapp.net")
    assert keys_of("5511911110001@c.us") != keys_of(LID_A)


def test_keys_of_the_brazilian_mobile_with_and_without_the_ninth_digit():
    assert keys_of("5511911110001@c.us") & keys_of("551111110001@c.us")


def test_keys_ignore_an_empty_or_broken_address():
    assert keys_of("", None, "noat", "@lid") == frozenset()


def test_lid_is_not_treated_as_a_phone():
    # the digits of a LID are not a number: 5511... on @lid does not cross with @c.us
    assert not (keys_of("5511911110001@lid") & keys_of("5511911110001@c.us"))


# ------------------------------------------------------------------ evaluation
def test_one_read_receipt_in_three_does_not_mark_the_message_as_read():
    # the bug: the first reader became ack 3 for the whole group
    r_ = evaluate(trio(), [r(A, "delivered"), r(A, "read")], NOW)
    assert r_.ack == 1 and (r_.delivered, r_.reads, r_.expected) == (1, 1, 3)


def test_delivered_only_when_everyone_received():
    assert evaluate(trio(), [r(A, "delivered"), r(B, "delivered")], NOW).ack == 1
    assert evaluate(trio(), [r(A, "delivered"), r(B, "delivered"),
                             r(C, "delivered")], NOW).ack == 2


def test_read_only_when_everyone_read():
    delivered = [r(p, "delivered") for p in (A, B, C)]
    assert evaluate(trio(), delivered + [r(A, "read"), r(B, "read")], NOW).ack == 2
    assert evaluate(trio(), delivered + [r(p, "read") for p in (A, B, C)], NOW).ack == 3


def test_read_receipt_without_the_delivery_one_counts_as_delivered():
    assert evaluate(trio(), [r(p, "read") for p in (A, B, C)], NOW).ack == 3


def test_played_only_when_everyone_played():
    base = [r(p, "read") for p in (A, B, C)]
    assert evaluate(trio(), base + [r(A, "played"), r(B, "played")], NOW).ack == 3
    assert evaluate(trio(), base + [r(p, "played") for p in (A, B, C)], NOW).ack == 4


def test_voice_played_by_everyone_without_a_read_receipt_is_played():
    # 'played' implies 'read' and 'delivered'
    assert evaluate(trio(), [r(p, "played") for p in (A, B, C)], NOW).ack == 4


def test_voice_played_by_one_only_does_not_become_played_nor_read():
    r_ = evaluate(trio(), [r(A, "played")], NOW)
    assert r_.ack == 1 and r_.plays == 1


def test_without_recipients_the_ack_stays_at_sent():
    assert evaluate([], [r(A, "read")], NOW).ack == 1


def test_receipt_from_a_non_member_is_ignored():
    outsider = "5599900000000@s.whatsapp.net"
    r_ = evaluate([member(A, LID_A)], [r(outsider, "read"), r(outsider, "delivered")], NOW)
    assert r_.ack == 1 and r_.delivered == 0 and r_.rows == []


def test_unknown_stage_is_ignored():
    assert evaluate([member(A)], [r(A, "sent")], NOW).ack == 1


def test_the_same_person_through_two_addresses_counts_once():
    # the read receipt came through the LID, the delivery one through the number
    r_ = evaluate([member(A, LID_A)], [r(A, "delivered"), r(LID_A, "read")], NOW)
    assert r_.ack == 3 and r_.expected == 1 and r_.rows[0][0] == "5511911110001@c.us"
    assert {row[0] for row in r_.rows} == {"5511911110001@c.us"}


def test_the_resolver_crosses_the_lid_with_the_number_when_the_list_only_has_one():
    only_number = [member(A)]
    assert evaluate(only_number, [r(LID_A, "read")], NOW).ack == 1
    assert evaluate(only_number, [r(LID_A, "read")], NOW,
                    resolver=lambda q: A if q == LID_A else "").ack == 3


def test_a_resolver_that_fails_does_not_bring_down_the_evaluation():
    def breaks(_):
        raise RuntimeError("no client")
    assert evaluate([member(A)], [r(LID_A, "read")], NOW, resolver=breaks).ack == 1


def test_brazilian_number_with_and_without_the_ninth_digit_is_the_same_person():
    r_ = evaluate([member("5511911110001@s.whatsapp.net")],
                  [r("551111110001@c.us", "read")], NOW)
    assert r_.ack == 3


# ------------------------------------------------------------------ recipients
def test_the_own_account_is_not_a_recipient_by_number_nor_by_lid():
    mine = keys_of(ME, MY_LID)
    everyone = trio() + [member(ME), member("5500000000@c.us", MY_LID)]
    assert [m.id for m in recipients(everyone, mine)] == [m.id for m in trio()]
    assert recipients([member("x", MY_LID)], mine) == []      # only its LID


def test_whoever_joined_after_the_message_is_not_a_recipient():
    new = member(C, LID_C, since=2000)
    assert [m.id for m in recipients([member(A), new], frozenset(), 1500)] == [
        "5511911110001@c.us"]
    assert len(recipients([member(A), new], frozenset(), 2500)) == 2
    # a join the bridge did not see (since 0) or a message without a time: counts
    assert len(recipients([member(A), member(C, since=0)], frozenset(), 1500)) == 2
    assert len(recipients([member(A), new], frozenset(), 0)) == 2


def test_whoever_joined_later_does_not_hold_the_ack():
    expected = recipients([member(A), member(B, since=5000)], frozenset(), 1500)
    assert evaluate(expected, [r(A, "read")], NOW).ack == 3


# ------------------------------------------------------------- grace without read
def grace(receipts, now, **kw):
    return evaluate(trio(), receipts, now, grace=600, **kw)


def test_whoever_only_delivered_does_not_hold_read_after_the_grace():
    receipts = ([r(p, "delivered", 1000) for p in (A, B, C)]
                + [r(A, "read", 1010), r(B, "read", 1020)])
    assert grace(receipts, 1000 + 599).ack == 2          # still within the grace
    assert grace(receipts, 1000 + 600).ack == 3          # C only delivered 10 minutes ago


def test_the_grace_counts_from_each_ones_delivery():
    receipts = [r(A, "read", 1000), r(B, "read", 1000),
                r(C, "delivered", 5000)]                 # delivered just now
    assert grace(receipts, 5100).ack == 2
    assert grace(receipts, 5600).ack == 3


def test_the_grace_does_not_apply_without_any_real_read():
    receipts = [r(p, "delivered", 1000) for p in (A, B, C)]
    assert grace(receipts, 10_000).ack == 2


def test_the_grace_does_not_forgive_whoever_did_not_even_deliver():
    receipts = [r(A, "read", 1000), r(B, "read", 1000)]
    assert grace(receipts, 10_000).ack == 1              # C may be without signal


def test_the_grace_does_not_apply_to_played():
    receipts = [r(A, "played", 1000), r(B, "played", 1000), r(C, "delivered", 1000)]
    assert grace(receipts, 10_000).ack == 3
    assert grace(receipts + [r(C, "played", 11_000)], 11_000).ack == 4


def test_receipt_without_a_time_does_not_trigger_the_grace():
    receipts = [r(A, "read", 1000), r(B, "read", 1000), r(C, "delivered", 0)]
    assert grace(receipts, 10_000_000).ack == 2


def test_the_default_grace_is_ten_minutes():
    assert gr.READ_GRACE == 600


# -------------------------------------------------------------------- roster
def test_roster_keeps_the_since_of_whoever_was_already_known():
    e = gr.Roster()
    e.define(GROUP, [member(A, LID_A)], 10)
    e.joined(GROUP, member(B, LID_B, since=500))
    e.define(GROUP, [member(A, LID_A), member(B, LID_B)], 20)
    assert {m.id: m.since for m in e.members(GROUP)}["5511911110002@c.us"] == 500


def test_roster_joined_and_left():
    e = gr.Roster()
    e.joined(GROUP, member(A))                      # group not loaded: nothing
    assert e.members(GROUP) is None
    e.define(GROUP, [member(A, LID_A), member(B, LID_B)], 10)
    e.joined(GROUP, member(C, LID_C, since=50))
    e.joined(GROUP, member(C, LID_C, since=60))     # does not duplicate
    assert len(e.members(GROUP)) == 3
    e.left(GROUP, keys_of(LID_A))                   # leaves only by the LID
    assert [m.id for m in e.members(GROUP)] == ["5511911110002@c.us", "5511911110003@c.us"]


def test_roster_expires_after_the_ttl_and_invalidates():
    e = gr.Roster(ttl=100)
    assert e.stale(GROUP, 0)
    e.define(GROUP, [member(A)], 10)
    assert not e.stale(GROUP, 109) and e.stale(GROUP, 110)
    e.invalidate(GROUP)
    assert e.members(GROUP) is None


def test_member_from_participant_prefers_the_number_and_keeps_both_addresses():
    def text(j):
        return f"{j.User}@{j.Server}" if j is not None and j.User else ""
    p = N(JID=N(User="1001", Server="lid"), LID=N(User="1001", Server="lid"),
          PhoneNumber=N(User="5511911110001", Server="s.whatsapp.net"))
    m = gr.member_from_participant(p, text)
    assert m.id == "5511911110001@c.us"
    assert m.keys >= keys_of(A) | keys_of(LID_A)
    without_number = N(JID=N(User="1001", Server="lid"), LID=N(User="1001", Server="lid"),
                       PhoneNumber=N(User="", Server=""))
    assert gr.member_from_participant(without_number, text).id == "1001@lid"
    assert gr.member_from_participant(N(JID=None, LID=None, PhoneNumber=None), text) is None


def jid_n(address):
    user, _, server = address.partition("@")
    return N(User=user, Server=server)


def text_n(j):
    return f"{j.User}@{j.Server}" if j is not None and j.User else ""


def test_apply_event_records_the_join_with_its_time_and_the_leave():
    e = gr.Roster()
    e.define(GROUP, [member(A, LID_A), member(B, LID_B)], 10)
    event = N(Join=[jid_n(C)], Leave=[jid_n(B)], Timestamp=3000)
    gr.apply_event(e, GROUP, event, 9999, text_n,
                   lambda q: LID_C if q == C else "")
    by_id = {m.id: m for m in e.members(GROUP)}
    assert set(by_id) == {"5511911110001@c.us", "5511911110003@c.us"}
    assert by_id["5511911110003@c.us"].since == 3000
    assert by_id["5511911110003@c.us"].keys >= keys_of(LID_C)


def test_apply_event_without_a_time_uses_now_and_survives_a_failing_resolver():
    e = gr.Roster()
    e.define(GROUP, [member(A)], 10)

    def breaks(_):
        raise RuntimeError("x")
    gr.apply_event(e, GROUP, N(Join=[jid_n(C)], Leave=[]), 777, text_n, breaks)
    assert {m.id: m.since for m in e.members(GROUP)}["5511911110003@c.us"] == 777


# ------------------------------------------------- Session: the receipt handler
def participant(phone, lid):
    return N(JID=jid_n(lid), LID=jid_n(lid), PhoneNumber=jid_n(phone),
             IsAdmin=False, IsSuperAdmin=False)


def group_info(*people):
    mine = N(JID=jid_n(MY_LID), LID=jid_n(MY_LID), PhoneNumber=jid_n(ME),
             IsAdmin=True, IsSuperAdmin=True)
    return N(JID=jid_n(GROUP), Participants=[mine, *people],
             GroupName=N(Name="Group"))


THREE = (participant(A, LID_A), participant(B, LID_B), participant(C, LID_C))


class GroupClient(Fake):
    def __init__(self, info):
        super().__init__()
        self.info, self.queries = info, 0

    def get_group_info(self, jid):
        self.queries += 1
        if self.info is None:
            raise RuntimeError("no network")
        return self.info


class EventRegistry:
    """neonize's `cli.event`: keeps the handler of each event type."""

    def __init__(self):
        self.handlers = {}

    def __call__(self, kind):
        def keep(function):
            self.handlers[kind] = function
            return function
        return keep

    def qr(self, function):
        return function


class FakeEvents(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        kind = type(name, (), {})
        setattr(self, name, kind)
        return kind


@pytest.fixture
def session(tmp_path, monkeypatch):
    """A real Session, connected, with the ReceiptEv handler registered
    by `_run` on a fake NewClient."""
    import neonize
    import neonize.client
    registry = EventRegistry()
    # the installed neonize version may not have all the event types that the
    # bridge registers: a fake module hands out a distinct type for each name
    events = FakeEvents("neonize.events")
    monkeypatch.setitem(sys.modules, "neonize.events", events)
    monkeypatch.setattr(neonize, "events", events, raising=False)

    class FakeNewClient:
        def __init__(self, *args, **kw):
            self.event = registry

        def set_force_activate_delivery_receipts(self, active):
            pass

        def connect(self):
            pass
    monkeypatch.setattr(neonize.client, "NewClient", FakeNewClient)
    s = server.Session("account1", str(tmp_path / "account1"), None, None)
    s.emitted = []
    s.emit = lambda event, data: s.emitted.append((event, data))
    s._run(None, s.lifecycle.open())
    import neonize.events as nev
    s.receipt = registry.handlers[nev.ReceiptEv]
    s.handlers = registry.handlers
    s.client = GroupClient(group_info(*THREE))
    s.status, s.me, s.lid = "CONNECTED", ME, MY_LID
    s.emitted.clear()
    s.db.message(GROUP, _msg("M1", int(time.time()) - 5, GROUP, from_me=True))
    return s


KIND = {"DELIVERED": 1, "READ": 4, "PLAYED": 6}


def receive(s, kind, who, mid="M1", ts=None, mine=False):
    s.receipt(s.client, N(
        Type=KIND[kind], MessageIDs=[mid], Timestamp=ts or int(time.time()),
        MessageSource=N(Chat=jid_n(GROUP), Sender=jid_n(who), IsFromMe=mine)))


def acks(s):
    return [d["ack"] for ev, d in s.emitted if ev == "onack"]


def test_the_group_handler_only_emits_when_everyone_arrived(session):
    receive(session, "DELIVERED", A)
    receive(session, "READ", A)              # a single reader: no blue
    receive(session, "DELIVERED", B)
    assert acks(session) == []
    receive(session, "DELIVERED", C)
    assert acks(session) == [2]
    receive(session, "READ", B)
    assert acks(session) == [2]
    receive(session, "READ", C)
    assert acks(session) == [2, 3]


def test_the_group_onack_has_the_wppconnect_format(session):
    for who in (A, B, C):
        receive(session, "DELIVERED", who)
    _, data = session.emitted[-1]
    chat = "120363000000000001@g.us"
    assert data == {"session": "account1", "ack": 2, "to": chat, "from": chat,
                    "id": {"fromMe": True, "remote": chat, "id": "M1",
                           "_serialized": f"true_{chat}_M1"}}


def test_never_emits_an_ack_lower_or_equal_to_the_one_already_emitted(session):
    for who in (A, B, C):
        receive(session, "READ", who)
    assert acks(session) == [3]
    receive(session, "DELIVERED", A)         # late receipt, it was already 3
    receive(session, "READ", A)
    assert acks(session) == [3]


def test_read_receipt_before_the_delivery_one_does_not_go_backwards(session):
    receive(session, "READ", A)
    receive(session, "READ", B)
    receive(session, "DELIVERED", C)
    assert acks(session) == [2]
    receive(session, "READ", C)
    assert acks(session) == [2, 3]


def test_voice_in_a_group_only_becomes_played_when_everyone_played(session):
    for who in (A, B, C):
        receive(session, "READ", who)
    receive(session, "PLAYED", A)
    receive(session, "PLAYED", B)
    assert acks(session) == [3]
    receive(session, "PLAYED", C)
    assert acks(session) == [3, 4]


def test_receipt_from_someone_outside_the_group_does_not_count(session):
    outsider = "5599900000000@s.whatsapp.net"
    receive(session, "READ", outsider)
    receive(session, "READ", A)
    receive(session, "READ", B)
    assert acks(session) == []
    receive(session, "READ", C)
    assert acks(session) == [3]


def test_receipt_through_the_lid_counts_as_the_person_of_the_number(session):
    receive(session, "READ", A)
    receive(session, "READ", LID_B)
    receive(session, "READ", C)
    assert acks(session) == [3]


def test_the_receipt_is_still_stored_per_participant(session):
    receive(session, "READ", A)
    receive(session, "READ", B)
    assert {q for q, _, _ in session.db.receipts_of("M1")} == {"5511911110001@c.us",
                                                               "5511911110002@c.us"}


def test_unknown_members_emit_nothing(session):
    session.client = GroupClient(None)           # GetGroupInfo fails
    for who in (A, B, C):
        receive(session, "READ", who)
    assert acks(session) == []


def test_the_member_list_is_fetched_once_and_kept(session):
    receive(session, "DELIVERED", A)
    receive(session, "DELIVERED", B)
    receive(session, "READ", A)
    assert session.client.queries == 1


def test_a_fetch_that_fails_is_tried_again_on_the_next_receipt(session):
    session.client.info = None
    receive(session, "DELIVERED", A)
    session.client.info = group_info(*THREE)
    for who in (A, B, C):
        receive(session, "DELIVERED", who)
    assert acks(session) == [2]


def test_the_own_account_does_not_hold_the_ack(session):
    # the account is in the participant list; it never sends a receipt of itself
    for who in (A, B, C):
        receive(session, "DELIVERED", who)
    assert acks(session) == [2]
    assert session.me not in [q for q, _, _ in session.db.receipts_of("M1")]


def test_read_receipt_on_my_own_device_does_not_emit_a_group_ack(session):
    receive(session, "READ", A, mine=True)
    assert [ev for ev, _ in session.emitted if ev == "onack"] == []


def test_chat_with_one_person_still_emits_the_first_receipt(session):
    s = session
    s.receipt(s.client, N(Type=KIND["READ"], MessageIDs=["M9"], Timestamp=int(time.time()),
                          MessageSource=N(Chat=jid_n(A), Sender=jid_n(A), IsFromMe=False)))
    assert acks(s) == [3]


# ------------------------------------------------- group size changes
def test_whoever_left_the_group_is_no_longer_expected(session):
    receive(session, "READ", A)
    receive(session, "READ", B)
    assert acks(session) == []
    import neonize.events as nev
    session.handlers[nev.GroupInfoEv](session.client, N(
        JID=jid_n(GROUP), Join=[], Leave=[jid_n(C)], Promote=[], Demote=[],
        Name=None, Timestamp=int(time.time())))
    receive(session, "READ", A, ts=int(time.time()) + 1)     # any receipt reevaluates
    # A's receipt already existed: INSERT OR IGNORE changes nothing, but the evaluation
    # now only waits for A and B
    assert acks(session) == [3]


def test_whoever_joined_after_sending_does_not_hold_the_ack(session):
    import neonize.events as nev
    new = "5511911110004@s.whatsapp.net"
    receive(session, "READ", A)
    receive(session, "READ", B)
    session.handlers[nev.GroupInfoEv](session.client, N(
        JID=jid_n(GROUP), Join=[jid_n(new)], Leave=[], Promote=[], Demote=[],
        Name=None, Timestamp=int(time.time()) + 100))       # after the message
    receive(session, "READ", C)
    assert acks(session) == [3]


def test_whoever_joined_before_sending_holds_the_ack(session):
    import neonize.events as nev
    new = "5511911110004@s.whatsapp.net"
    session.group_members(GROUP)                           # loads the list
    session.handlers[nev.GroupInfoEv](session.client, N(
        JID=jid_n(GROUP), Join=[jid_n(new)], Leave=[], Promote=[], Demote=[],
        Name=None, Timestamp=int(time.time()) - 3600))
    for who in (A, B, C):
        receive(session, "READ", who)
    assert acks(session) == []
    receive(session, "READ", new)
    assert acks(session) == [3]


def test_new_group_arrives_through_joinedgroupev_and_load_names(session):
    import neonize.events as nev
    session.client.info = None                  # no network: only the kept list counts
    session.handlers[nev.JoinedGroupEv](session.client, N(
        GroupInfo=group_info(participant(A, LID_A), participant(B, LID_B))))
    receive(session, "READ", A)
    receive(session, "READ", B)
    assert acks(session) == [3]


def test_load_names_keeps_the_members_of_the_groups(session):
    session.client.info = None
    session.client.contact = N(get_all_contacts=lambda: [])
    session.client.get_joined_groups = lambda: [group_info(*THREE)]
    session._load_names()
    assert len(session.group_members(GROUP, fetch=False)) == 4   # the 3 and me


# ------------------------------------------------- grace and reevaluation
def test_the_grace_expires_on_the_next_evaluation_without_a_timer(session):
    now = int(time.time())
    receive(session, "DELIVERED", A, ts=now)
    receive(session, "READ", A, ts=now)
    receive(session, "DELIVERED", B, ts=now)
    receive(session, "READ", B, ts=now)
    receive(session, "DELIVERED", C, ts=now)           # C delivered and never reads
    assert acks(session) == [2]
    assert session.group_ack_to_emit(GROUP, "M1", now=now + 599) is None
    assert session.group_ack_to_emit(GROUP, "M1", now=now + 601) == 3
    assert session.group_ack_to_emit(GROUP, "M1", now=now + 700) is None


# ------------------------------------------------- message-ack and get-messages
async def _bridge(tmp_path, sessions, info=None):  # noqa: F811
    bridge = await _open(tmp_path, sessions)
    bridge.s.client = GroupClient(info if info is not None else group_info(*THREE))
    bridge.s.lid = MY_LID
    bridge.s.db.chat(GROUP)
    bridge.s.db.message(GROUP, _msg("M1", int(time.time()) - 5, GROUP, from_me=True))
    return bridge


@pytest.mark.asyncio
async def test_message_ack_of_a_group_counts_who_is_still_missing(tmp_path, sessions):  # noqa: F811
    bridge = await _bridge(tmp_path, sessions)
    try:
        db = bridge.s.db
        db.receipt("M1", "5511911110001@c.us", "delivered", 10)
        db.receipt("M1", "5511911110001@c.us", "read", 20)
        db.receipt("M1", "5511911110002@c.us", "delivered", 15)
        status, body = await bridge.post("message-ack", {"messageId": f"true_{GROUP}_M1"})
        assert status == 200
        answer = body["response"]
        assert {p["id"] for p in answer["participants"]} == {
            "5511911110001@c.us", "5511911110002@c.us"}
        assert answer["deliveryRemaining"] == 1       # C is missing
    finally:
        await bridge.http.close()


@pytest.mark.asyncio
async def test_message_ack_of_a_group_ignores_non_members_and_joins_addresses(tmp_path, sessions):  # noqa: F811
    bridge = await _bridge(tmp_path, sessions)
    try:
        db = bridge.s.db
        db.receipt("M1", "5511911110001@c.us", "delivered", 10)
        db.receipt("M1", LID_A, "read", 20)                  # the same person, through the LID
        db.receipt("M1", "5599900000000@c.us", "read", 30)   # not from the group
        status, body = await bridge.post("message-ack", {"messageId": f"true_{GROUP}_M1"})
        answer = body["response"]
        assert answer["participants"] == [
            {"id": "5511911110001@c.us", "deliveredAt": 10, "readAt": 20}]
        assert answer["deliveryRemaining"] == 2
    finally:
        await bridge.http.close()


@pytest.mark.asyncio
async def test_message_ack_of_a_group_never_returns_a_negative_remainder(tmp_path, sessions):  # noqa: F811
    # the group shrank: more people with receipts than recipients now -> 0
    bridge = await _bridge(tmp_path, sessions, group_info(THREE[0]))
    try:
        bridge.s.db.receipt("M1", "5511911110001@c.us", "read", 20)
        _, body = await bridge.post("message-ack", {"messageId": f"true_{GROUP}_M1"})
        assert body["response"]["deliveryRemaining"] == 0
    finally:
        await bridge.http.close()


@pytest.mark.asyncio
async def test_message_ack_of_a_one_person_chat_still_has_zero(tmp_path, sessions):  # noqa: F811
    bridge = await _bridge(tmp_path, sessions)
    try:
        bridge.s.db.message(A, _msg("M2", 100, A, from_me=True))
        bridge.s.db.receipt("M2", A, "read", 20)
        _, body = await bridge.post("message-ack", {"messageId": f"true_{A}_M2"})
        assert body["response"]["deliveryRemaining"] == 0
    finally:
        await bridge.http.close()


@pytest.mark.asyncio
async def test_message_ack_after_the_grace_does_not_change_the_participants(tmp_path, sessions):  # noqa: F811
    bridge = await _bridge(tmp_path, sessions)
    try:
        for who in ("5511911110001@c.us", "5511911110002@c.us", "5511911110003@c.us"):
            bridge.s.db.receipt("M1", who, "delivered", 10)
        _, body = await bridge.post("message-ack", {"messageId": f"true_{GROUP}_M1"})
        assert len(body["response"]["participants"]) == 3
        assert body["response"]["deliveryRemaining"] == 0
    finally:
        await bridge.http.close()


@pytest.mark.asyncio
async def test_get_messages_of_a_group_does_not_show_read_with_a_single_reader(tmp_path, sessions):  # noqa: F811
    bridge = await _bridge(tmp_path, sessions)
    try:
        await asyncio_in_thread(bridge.s.group_members, GROUP)    # loads the list
        bridge.s.db.receipt("M1", "5511911110001@c.us", "read", 20)
        _, body = await bridge.get(f"get-messages/{GROUP}?count=10")
        assert [m["ack"] for m in body["response"]] == [1]
        for who in ("5511911110002@c.us", "5511911110003@c.us"):
            bridge.s.db.receipt("M1", who, "read", 21)
        _, body = await bridge.get(f"get-messages/{GROUP}?count=10")
        assert [m["ack"] for m in body["response"]] == [3]
        _, body = await bridge.get(f"message-by-id/true_{GROUP}_M1")
        assert body["response"]["data"]["ack"] == 3
    finally:
        await bridge.http.close()


@pytest.mark.asyncio
async def test_get_messages_of_a_group_does_not_go_below_the_ack_already_emitted(tmp_path, sessions):  # noqa: F811
    bridge = await _bridge(tmp_path, sessions)
    try:
        await asyncio_in_thread(bridge.s.group_members, GROUP)
        for who in ("5511911110001@c.us", "5511911110002@c.us", "5511911110003@c.us"):
            bridge.s.db.receipt("M1", who, "read", 21)
        assert bridge.s.group_ack_to_emit(GROUP, "M1") == 3
        bridge.s.roster_changed(N(JID=jid_n(GROUP), Leave=[], Timestamp=1,
                                  Join=[jid_n("5511911110009@s.whatsapp.net")]))
        # the newcomer (time 1: before the message) would become expected
        _, body = await bridge.get(f"get-messages/{GROUP}?count=10")
        assert [m["ack"] for m in body["response"]] == [3]
    finally:
        await bridge.http.close()


@pytest.mark.asyncio
async def test_get_messages_of_a_group_with_unknown_members_stays_at_sent(tmp_path, sessions):  # noqa: F811
    bridge = await _bridge(tmp_path, sessions)
    try:
        bridge.s.db.receipt("M1", "5511911110001@c.us", "read", 20)
        _, body = await bridge.get(f"get-messages/{GROUP}?count=10")
        assert [m["ack"] for m in body["response"]] == [1]
    finally:
        await bridge.http.close()


async def asyncio_in_thread(function, *args):
    import asyncio
    return await asyncio.get_running_loop().run_in_executor(None, function, *args)
