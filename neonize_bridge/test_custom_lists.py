"""WhatsApp's custom chat lists, read from and written to the app state."""
import asyncio
from types import SimpleNamespace as N

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import app_state_mirror
import custom_lists as cl


def test_chat_keys():
    assert cl.chat_key("5511999998888@c.us") == "5511999998888@s.whatsapp.net"
    assert cl.chat_key("5511999998888:3@s.whatsapp.net") == "5511999998888@s.whatsapp.net"
    assert cl.chat_key("120363-1@g.us") == "120363-1@g.us"
    assert cl.chat_key("123@lid") == "123@lid"
    for other in ("1@newsletter", "status@broadcast", "", None, 5):
        assert cl.chat_key(other) == ""


def test_names_count_utf16_code_units():
    assert cl.name_ok("  Trabalho ")
    assert not cl.name_ok("   ") and not cl.name_ok(None)
    assert cl.name_ok("a" * 100) and not cl.name_ok("a" * 101)
    assert not cl.name_ok("\U0001F600" * 51)      # 102 code units


LABELS = {
    "1": {"name": "Cliente novo", "type": 4, "deleted": False},   # predefined, not a list
    "5": {"name": "Família", "type": cl.CUSTOM, "deleted": False},
    "6": {"name": "Velha", "type": cl.CUSTOM, "deleted": True},
    "12": {"name": "Trabalho", "type": cl.CUSTOM, "deleted": False},
}


def test_the_snapshot_has_only_custom_lists_still_in_use():
    links = [("5", "5511999998888@c.us", True), ("5", "120363-1@g.us", True),
             ("5", "123@lid", False), ("12", "1@newsletter", True), ("1", "9@g.us", True),
             ("5", "5511999998888@s.whatsapp.net", True)]
    assert cl.snapshot(LABELS, links, True) == {
        "canEdit": True, "editingReason": "", "lists": [
            {"id": "5", "name": "Família",
             "members": ["5511999998888@s.whatsapp.net", "120363-1@g.us"]},
            {"id": "12", "name": "Trabalho", "members": []}]}
    assert cl.snapshot({}, [], False) == {"canEdit": False, "editingReason": "account_disabled",
                                          "lists": []}


def test_a_new_list_takes_the_next_id_even_after_a_deleted_one():
    assert cl.next_label_id(LABELS) == "13"
    assert cl.next_label_id({}) == "1"
    assert cl.next_label_id({"a": {}, "2": {}}) == "3"


@pytest.mark.parametrize("command,code", [
    ({"action": "drop"}, "list_command_invalid"),
    ({"action": "rename", "id": "bad id", "name": "x"}, "list_command_invalid"),
    ({"action": "rename", "id": "1", "name": "x"}, "list_not_found"),       # not a list
    ({"action": "remove", "id": "6"}, "list_not_found"),                    # deleted
    ({"action": "create", "name": " "}, "list_name_required"),
    ({"action": "addChats", "id": "5", "chatIds": []}, "list_command_invalid"),
    ({"action": "addChats", "id": "5", "chatIds": ["1@newsletter"]}, "list_command_invalid"),
    ({"action": "addChats", "id": "5", "chatIds": ["1@g.us"] * 501}, "list_command_invalid"),
])
def test_invalid_commands(command, code):
    with pytest.raises(cl.ListError) as caught:
        cl.check_command(command, LABELS)
    assert caught.value.code == code


def test_valid_commands():
    for command in ({"action": "create", "name": "Nova"},
                    {"action": "rename", "id": "5", "name": "Casa"},
                    {"action": "removeChats", "id": "12", "chatIds": ["1@g.us"]}):
        cl.check_command(command, LABELS)


def test_removing_uses_the_exact_entries_and_adding_whatsapp_webs_index():
    links = [("5", "123@lid", True), ("5", "120363-1@g.us", True), ("5", "9@g.us", False)]
    remove = {"action": "removeChats", "id": "5", "chatIds": ["120363-1@g.us", "777@g.us"]}
    assert cl.chats_to_change(remove, links, str) == ["120363-1@g.us"]
    add = {"action": "addChats", "id": "5",
           "chatIds": ["5511999998888@c.us", "120363-1@g.us", "9@g.us"]}
    to_lid = {"5511999998888@s.whatsapp.net": "456@lid"}.get
    assert cl.chats_to_change(add, links, lambda k: to_lid(k, k)) == ["456@lid", "9@g.us"]


class Client:
    def __init__(self, business=""):
        self.patches = []
        self.business = business

    def send_app_state(self, patch):
        self.patches.append(patch)

    def get_me(self):
        return N(BussinessName=self.business)

    def get_lid_from_pn(self, jid):
        return N(User="")


def seeded_session(tmp_path, business=""):
    from neonize.proto.waSyncAction.WAWebProtobufSyncAction_pb2 import (
        LabelAssociationAction, LabelEditAction, SyncActionValue)
    s = N(client=Client(business), status="CONNECTED", folder=str(tmp_path),
          db=N(chat_exists=lambda jid: jid in {"120363-1@g.us", "5511999998888@s.whatsapp.net"}))
    mirror = app_state_mirror.mirror_of(s)
    # what the first full sync of `regular` delivers
    mirror.apply("regular", ["label_edit", "5"], 3, False, SyncActionValue(
        labelEditAction=LabelEditAction(name="Família", type=5, isActive=True,
                                        color=3)).SerializeToString(), full_sync=True)
    mirror.apply("regular", ["label_jid", "5", "120363-1@g.us"], 3, False, SyncActionValue(
        labelAssociationAction=LabelAssociationAction(labeled=True)).SerializeToString(),
        full_sync=True)
    mirror.full_sync_completed("regular")
    return s


def run(s, *requests):
    async def main():
        app = web.Application()
        cl.register_routes(app.router, session=lambda req: s,
                           jid_from_text=lambda t: t, jid_text=lambda j: "")
        async with TestClient(TestServer(app)) as http:
            out = []
            for body in requests:
                if body is None:
                    resp = await http.get("/api/tok/custom-lists")
                else:
                    resp = await http.post("/api/tok/custom-lists", json=body)
                out.append((resp.status, await resp.json()))
            return out
    return asyncio.run(main())


@pytest.fixture(autouse=True)
def supported(monkeypatch):
    monkeypatch.setattr(app_state_mirror, "supported", lambda: True)


def test_read(tmp_path):
    (status, body), = run(seeded_session(tmp_path), None)
    assert status == 200 and body["response"]["lists"] == [
        {"id": "5", "name": "Família", "members": ["120363-1@g.us"]}]
    assert body["response"]["canEdit"] is True


def test_create_and_read_back(tmp_path):
    s = seeded_session(tmp_path)
    (status, ack), (_, read) = run(s, {"action": "create", "name": " Trabalho "}, None)
    assert (status, ack["response"]) == (200, {"action": "create", "createdId": "6"})
    (mutation,) = s.client.patches[0].Mutations
    assert (list(mutation.Index), mutation.Version) == (["label_edit", "6"], 3)
    edit = mutation.Value.labelEditAction
    assert (edit.name, edit.type, edit.isActive, edit.deleted) == ("Trabalho", 5, True, False)
    assert {"id": "6", "name": "Trabalho", "members": []} in read["response"]["lists"]


def test_rename_keeps_the_other_fields_and_delete_keeps_the_color(tmp_path):
    s = seeded_session(tmp_path)
    run(s, {"action": "rename", "id": "5", "name": "Casa"}, {"action": "remove", "id": "5"})
    renamed = s.client.patches[0].Mutations[0].Value.labelEditAction
    assert (renamed.name, renamed.type, renamed.color, renamed.isActive) == ("Casa", 5, 3, True)
    deleted = s.client.patches[1].Mutations[0].Value.labelEditAction
    assert (deleted.name, deleted.color, deleted.deleted) == ("Casa", 3, True)


def test_deleting_a_list_unlabels_its_chats_in_the_same_patch(tmp_path):
    """As WhatsApp Web's labelDeleteAction (captured over CDP, 2026-11-18):
    the deletion and one labeled=false per chat of the list, one patch."""
    s = seeded_session(tmp_path)
    (status, ack), (_, read) = run(s, {"action": "remove", "id": "5"}, None)
    assert (status, ack["response"]) == (200, {"action": "remove", "id": "5"})
    (patch,) = s.client.patches
    edit, unlabel = patch.Mutations
    assert list(edit.Index) == ["label_edit", "5"] and edit.Value.labelEditAction.deleted
    assert list(unlabel.Index) == ["label_jid", "5", "120363-1@g.us"]
    assert unlabel.Version == 3
    assert unlabel.Value.labelAssociationAction.labeled is False
    assert read["response"]["lists"] == []


def test_the_members_of_a_list_are_its_labeled_entries():
    links = [("5", "1@g.us", True), ("5", "2@lid", False), ("6", "3@g.us", True),
             ("5", "1@g.us", True)]
    assert cl.members_of("5", links) == ["1@g.us"]


def test_add_and_remove_chats(tmp_path):
    s = seeded_session(tmp_path)
    (status, ack), (_, read) = run(
        s, {"action": "addChats", "id": "5", "chatIds": ["5511999998888@c.us"]}, None)
    assert (status, ack["response"]) == (200, {"action": "addChats", "id": "5"})
    (mutation,) = s.client.patches[0].Mutations
    assert list(mutation.Index) == ["label_jid", "5", "5511999998888@s.whatsapp.net"]
    assert mutation.Value.labelAssociationAction.labeled is True
    assert read["response"]["lists"][0]["members"] == [
        "120363-1@g.us", "5511999998888@s.whatsapp.net"]
    run(s, {"action": "removeChats", "id": "5", "chatIds": ["120363-1@g.us"]})
    (mutation,) = s.client.patches[1].Mutations
    assert list(mutation.Index) == ["label_jid", "5", "120363-1@g.us"]
    assert mutation.Value.labelAssociationAction.labeled is False


def test_a_chat_the_bridge_does_not_hold_is_refused(tmp_path):
    s = seeded_session(tmp_path)
    (status, body), = run(s, {"action": "addChats", "id": "5", "chatIds": ["777@g.us"]})
    assert (status, body["code"]) == (400, "list_chat_not_found") and s.client.patches == []


def test_a_business_account_reads_but_does_not_edit(tmp_path):
    s = seeded_session(tmp_path, business="Loja")
    (_, read), (status, body) = run(s, None, {"action": "create", "name": "x"})
    assert read["response"]["canEdit"] is False
    assert (status, body["code"]) == (403, "list_editing_not_available")
    assert s.client.patches == []


def test_lists_not_learned_yet_are_not_answered_as_none(tmp_path):
    s = N(client=Client(), status="CONNECTED", folder=str(tmp_path),
          repair_collection=lambda name, force=True: False)
    (status, body), = run(s, None)
    assert (status, body["code"]) == (503, "lists_not_available")
