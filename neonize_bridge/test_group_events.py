"""Live group changes: pure functions with fake events and the handlers
with a fake `neonize.events` (no DLL and no account)."""
import sys
import types
from types import SimpleNamespace as N

import group_events as ge


def jid(user, server="s.whatsapp.net"):
    return N(User=user, Server=server)


def group(**fields):
    base = dict(JID=jid("120", "g.us"), Join=[], Leave=[], Promote=[], Demote=[])
    base.update(fields)
    return N(**base)


def person(j):
    return ge.msgmap.jid_wpp(ge.jid_text(j))


def test_new_name_becomes_subject_with_the_group_id():
    e = group(Name=N(Name="  Family  "))
    assert ge.event_updates(e, person) == [{"id": "120@g.us", "subject": "Family"}]


def test_fields_are_joined_in_a_single_item():
    e = group(Name=N(Name="X"), Topic=N(Topic="rules", TopicDeleted=False),
              Locked=N(isLocked=True), Announce=N(IsAnnounce=False))
    assert ge.event_updates(e, person) == [{
        "id": "120@g.us", "subject": "X", "desc": "rules",
        "restrict": True, "announce": False}]


def test_deleted_description_becomes_empty():
    e = group(Topic=N(Topic="old", TopicDeleted=True))
    assert ge.event_updates(e, person) == [{"id": "120@g.us", "desc": ""}]


def test_empty_name_does_not_become_subject():
    assert ge.event_updates(group(Name=N(Name="  ")), person) == []


def test_members_become_one_item_per_action_in_order():
    e = group(Join=[jid("1"), jid("2"), jid("3")], Demote=[jid("4")], Leave=[jid("5")])
    assert ge.event_updates(e, person) == [
        {"id": "120@g.us", "participants": ["1@c.us", "2@c.us", "3@c.us"], "action": "add"},
        {"id": "120@g.us", "participants": ["5@c.us"], "action": "remove"},
        {"id": "120@g.us", "participants": ["4@c.us"], "action": "demote"}]


def test_event_outside_a_group_or_without_a_change_generates_nothing():
    assert ge.event_updates(group(JID=jid("5511")), person) == []
    assert ge.event_updates(group(), person) == []
    assert ge.event_updates(N(), person) == []


def test_joining_a_group_brings_name_description_and_settings():
    info = N(JID=jid("120", "g.us"), GroupName=N(Name="New"),
             GroupTopic=N(Topic="", TopicDeleted=False),
             GroupLocked=N(isLocked=False), GroupAnnounce=N(IsAnnounce=True))
    assert ge.join_updates(N(GroupInfo=info)) == [{
        "id": "120@g.us", "subject": "New", "desc": "", "restrict": False, "announce": True}]
    assert ge.join_updates(N(GroupInfo=N(JID=jid("5511")))) == []
    assert ge.join_updates(N()) == []


def test_has_uses_hasfield_when_it_exists():
    class Proto:
        def HasField(self, field):
            return field == "Name"
        Name = N(Name="a")
        Topic = N(Topic="b", TopicDeleted=False)
    assert ge._has(Proto(), "Name") and not ge._has(Proto(), "Topic")


# ------------------------------------------------- handlers, without neonize
class _Cli:
    def __init__(self, pairs=None):
        self.handlers = {}
        self.pairs = pairs or {}

    def event(self, kind):
        def bind(f):
            self.handlers[kind] = f
            return f
        return bind

    def get_pn_from_lid(self, j):
        return self.pairs[f"{j.User}@{j.Server}"]


class _Session:
    name = "s1"

    def __init__(self):
        self.sent = []

    def emit(self, event, data):
        self.sent.append((event, data))


def _bind(monkeypatch, pairs=None):
    events = types.ModuleType("neonize.events")
    events.GroupInfoEv = type("GroupInfoEv", (), {})
    events.JoinedGroupEv = type("JoinedGroupEv", (), {})
    package = types.ModuleType("neonize")
    package.events = events
    monkeypatch.setitem(sys.modules, "neonize", package)
    monkeypatch.setitem(sys.modules, "neonize.events", events)
    cli, s = _Cli(pairs), _Session()
    ge.register_events(cli, s)
    return cli, s, events


def test_group_changed_emits_groups_update_of_the_session(monkeypatch):
    cli, s, ev = _bind(monkeypatch)
    ge.group_changed(cli, s, group(Name=N(Name="New name")))
    assert s.sent == [("groups.update", {
        "session": "s1", "data": [{"id": "120@g.us", "subject": "New name"}]})]


def test_lid_member_becomes_a_number_when_known_and_stays_lid_when_not(monkeypatch):
    cli, s, ev = _bind(monkeypatch, {"99@lid": jid("5511")})
    ge.group_changed(cli, s, group(Join=[jid("99", "lid"), jid("77", "lid")]))
    # 77@lid has no pair: get_pn_from_lid raises KeyError and the LID stays
    assert s.sent[0][1]["data"] == [{
        "id": "120@g.us", "participants": ["5511@c.us", "77@lid"], "action": "add"}]


def test_event_without_a_change_or_with_an_error_neither_emits_nor_raises(monkeypatch):
    cli, s, ev = _bind(monkeypatch)
    ge.group_changed(cli, s, group())
    ge.group_changed(cli, s, None)
    cli.handlers[ev.JoinedGroupEv](cli, N())
    assert s.sent == []


def test_joining_emits_groups_update(monkeypatch):
    cli, s, ev = _bind(monkeypatch)
    info = N(JID=jid("120", "g.us"), GroupName=N(Name="New"))
    cli.handlers[ev.JoinedGroupEv](cli, N(GroupInfo=info))
    assert s.sent[0] == ("groups.update", {
        "session": "s1", "data": [{"id": "120@g.us", "subject": "New"}]})


def test_register_events_does_not_take_the_place_of_the_servers_handler(monkeypatch):
    """neonize keeps one handler per type: the GroupInfoEv one belongs to server.py."""
    cli, s, ev = _bind(monkeypatch)
    assert ev.GroupInfoEv not in cli.handlers
    assert ev.JoinedGroupEv in cli.handlers


def test_joining_by_link_stores_and_emits_the_accounts_own_notice(monkeypatch):
    from store import Store
    cli, s, ev = _bind(monkeypatch)
    s.me = "9:3@s.whatsapp.net"
    s.db = Store(":memory:")
    info = N(JID=jid("120", "g.us"), ParticipantVersionID="v1")
    cli.handlers[ev.JoinedGroupEv](cli, N(GroupInfo=info, Reason="invite"))
    cli.handlers[ev.JoinedGroupEv](cli, N(GroupInfo=info, Reason="invite"))  # repeated
    notices = [d["response"] for e, d in s.sent if e == "received-message"]
    assert len(notices) == 1
    assert (notices[0]["subtype"], notices[0]["recipients"]) == ("invite", ["9@c.us"])
