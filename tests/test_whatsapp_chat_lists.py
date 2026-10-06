"""Read/mutation list contracts on synthetic data, with no wx or real HTTP."""

import copy
import pytest

from core.chat_lists import (
    ListSnapshot, WhatsAppList, list_contains, list_identity,
    parse_list_snapshot, list_change_verified,
)
from core import chat_list_transport as transport

PN = "551199990001@s.whatsapp.net"
LID = "900000000000001@lid"
GROUP = "120363000000001@g.us"


def body(members=(PN,), name="Friends", editable=True):
    return {"status": "success", "response": {"canEdit": editable,
            "lists": [{"id": "42", "name": name, "members": list(members)}]}}


def test_snapshot_keeps_identity_and_members_separate_from_name():
    snapshot = parse_list_snapshot(body())
    assert snapshot.can_edit and snapshot.find("42") == WhatsAppList("42", "Friends", frozenset({PN}))
    assert snapshot.find("Friends") is None
    assert parse_list_snapshot({"status": "success", "response": {"canEdit": False, "lists": []}}) == ListSnapshot()


@pytest.mark.parametrize("mutate", [
    lambda data: data.update(status="error"),
    lambda data: data.update(response=[]),
    lambda data: data["response"].update(canEdit="true"),
    lambda data: data["response"].pop("canEdit"),
    lambda data: data["response"].update(lists={}),
    lambda data: data["response"]["lists"].append(copy.deepcopy(data["response"]["lists"][0])),
    lambda data: data["response"]["lists"][0].update(id="../42"),
    lambda data: data["response"]["lists"][0].update(id=42),
    lambda data: data["response"]["lists"][0].update(name=" "),
    lambda data: data["response"]["lists"][0].update(name=None),
    lambda data: data["response"]["lists"][0].update(members=PN),
    lambda data: data["response"]["lists"][0].update(members=["status@broadcast"]),
    lambda data: data["response"]["lists"][0].update(members=["phone@newsletter"]),
    lambda data: data["response"]["lists"][0].update(members=[{}]),
])
def test_malformed_snapshot_never_becomes_an_empty_success(mutate):
    data = body()
    mutate(data)
    with pytest.raises(ValueError):
        parse_list_snapshot(data)


def test_membership_bridges_only_explicit_phone_lid_mapping():
    item = WhatsAppList("42", "Friends", frozenset({LID, GROUP}))
    assert not list_contains(item, PN, {})
    assert list_contains(item, PN.replace("@s.whatsapp.net", ":3@c.us"), {LID: PN})
    assert list_contains(item, GROUP, {LID: PN})
    assert not list_contains(item, GROUP.replace("@g.us", "@s.whatsapp.net"), {})
    assert not list_contains(item, "status@broadcast", {})
    assert list_identity(LID, {LID: GROUP}) == LID


@pytest.mark.parametrize("command,ack,data", [
    ({"action": "create", "name": "Friends"}, {"action": "create", "createdId": "42"}, body()),
    ({"action": "rename", "id": "42", "name": "Work"}, {"action": "rename", "id": "42"}, body(name="Work")),
    ({"action": "addChats", "id": "42", "chatIds": [PN]}, {"action": "addChats", "id": "42"}, body()),
    ({"action": "removeChats", "id": "42", "chatIds": [PN]}, {"action": "removeChats", "id": "42"}, body(members=[])),
    ({"action": "remove", "id": "42"}, {"action": "remove", "id": "42"}, {"status": "success", "response": {"canEdit": True, "lists": []}}),
])
def test_every_write_needs_a_matching_acknowledgement_and_read_state(command, ack, data):
    snapshot = parse_list_snapshot(data)
    assert list_change_verified(command, ack, snapshot)
    assert not list_change_verified(command, None, snapshot)
    assert not list_change_verified(command, {**ack, "action": "other"}, snapshot)
    assert not list_change_verified(command, {**ack, "id": "wrong", "createdId": "wrong"}, snapshot)


class Response:
    def __init__(self, status=200, payload=None):
        self.status_code, self.payload = status, payload
    def json(self):
        return self.payload


@pytest.fixture
def http(monkeypatch):
    calls = []
    read = Response(payload=body())
    write = Response(payload={"status": "success", "response": {"action": "addChats", "id": "42"}})
    def post(*args, **kwargs):
        calls.append(("POST", kwargs))
        if isinstance(write, Exception):
            raise write
        return write
    def get(*args, **kwargs):
        calls.append(("GET", kwargs))
        if isinstance(read, Exception):
            raise read
        return read
    def set_response(kind, response):
        nonlocal read, write
        if kind == "GET":
            read = response
        else:
            write = response
    monkeypatch.setattr(transport, "api_post", post)
    monkeypatch.setattr(transport, "api_get", get)
    return calls, set_response


COMMAND = {"action": "addChats", "id": "42", "chatIds": [PN]}


def test_a_read_never_mutates_whatsapp(http):
    result = transport.request_lists("synthetic", {})
    assert result.outcome == "loaded" and result.snapshot.find("42")
    assert [kind for kind, _kwargs in http[0]] == ["GET"]


def test_write_uses_one_post_then_one_get(http):
    result = transport.request_lists("synthetic", {}, COMMAND)
    assert result.outcome == "changed"
    assert [kind for kind, _kwargs in http[0]] == ["POST", "GET"]
    assert http[0][0][1]["json"] == COMMAND


@pytest.mark.parametrize("write", [TimeoutError(), Response(503, {"status": "error"}), Response(200, None)])
def test_ambiguous_write_is_read_once_and_never_reposted(http, write):
    http[1]("POST", write)
    result = transport.request_lists("synthetic", {}, COMMAND)
    assert result.outcome == "unconfirmed"
    assert result.snapshot.find("42")
    assert [kind for kind, _kwargs in http[0]] == ["POST", "GET"]


def test_setter_success_with_previous_members_is_unconfirmed(http):
    http[1]("GET", Response(payload=body(members=[])))
    assert transport.request_lists("synthetic", {}, COMMAND).outcome == "unconfirmed"


def test_definite_refusal_cannot_become_success_from_coincidental_read_state(http):
    http[1]("POST", Response(403, {"status": "error"}))
    assert transport.request_lists("synthetic", {}, COMMAND).outcome == "refused"


@pytest.mark.parametrize("read,outcome", [
    (Response(404, {}), "unavailable"), (Response(501, {}), "unavailable"),
    (Response(503, {}), "failed"), (TimeoutError(), "failed"),
    (Response(payload={}), "failed"),
])
def test_read_error_has_no_snapshot(http, read, outcome):
    http[1]("GET", read)
    result = transport.request_lists("synthetic", {})
    assert result.snapshot is None and result.outcome == outcome


def test_failed_verification_does_not_claim_write_success(http):
    http[1]("GET", TimeoutError())
    result = transport.request_lists("synthetic", {}, COMMAND)
    assert result.snapshot is None and result.outcome == "unconfirmed"
