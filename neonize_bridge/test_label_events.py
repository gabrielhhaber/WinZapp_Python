"""Phone label mutations must agree with both socket notifications and reads."""
import asyncio
from types import SimpleNamespace as N

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from neonize.proto.waSyncAction.WAWebProtobufSyncAction_pb2 import (
    LabelAssociationAction, LabelEditAction, SyncActionValue)

import label_events
import app_state_mirror
import labels
from test_group_receipts import session, GROUP


def mutate(s, index, action, full=False, remove=False):
    import neonize.events as nev
    s.handlers[nev.AppStateMutationEv](s.client, N(
        Name="regular", Index=index, Version=3, Remove=remove,
        FromFullSync=full, Action=action, HasField=lambda field: action is not None))


def metadata(s, label_id="3", **extra):
    mutate(s, ["label_edit", label_id], SyncActionValue(labelEditAction=LabelEditAction(
        name="Work", color=5, **extra)), full=True)


def link(s, attached, full=False, chat=GROUP):
    mutate(s, ["label_jid", "3", chat], SyncActionValue(
        labelAssociationAction=LabelAssociationAction(labeled=attached)), full=full)


def test_phone_label_add_remove_payload_matches_the_current_read(session):
    s = session
    metadata(s)
    link(s, True)
    assert s.emitted == [("onupdatelabel", {
        "session": "account1", "chat": {"id": GROUP,
            "labels": ["3"]}, "ids": ["3"],
        "labels": [{"id": "3", "name": "Work", "colorIndex": 5, "count": 1}], "type": "add"})]
    link(s, False)
    assert s.emitted[-1][1]["type"] == "remove"
    assert s.emitted[-1][1]["chat"]["labels"] == []
    assert s.emitted[-1][1]["labels"][0]["count"] == 0
    assert label_events.registry(s)["3"]["chats"] == []


def test_full_sync_hydrates_without_announcing_old_associations(session):
    metadata(session)
    link(session, True, full=True)
    assert session.emitted == []
    assert label_events.registry(session)["3"]["chats"] == [GROUP]
    session.app_state_synced("regular")
    assert label_events.registry(session)["3"]["chats"] == [GROUP]


def test_label_message_associations_and_other_mutations_are_not_chat_events(session):
    metadata(session)
    mutate(session, ["label_message", "3", GROUP, "MID"], SyncActionValue())
    link(session, True, chat="status@broadcast")
    assert session.emitted == []


def test_lid_association_is_preserved_in_the_event(session):
    metadata(session)
    link(session, True, chat="901@lid")
    data = session.emitted[-1][1]
    assert data["chat"]["id"] == "901@lid"
    assert label_events.registry(session)["3"]["chats"] == ["901@lid"]


def test_a_phone_association_does_not_duplicate_its_legacy_c_us_spelling(session):
    labels.save(session.folder, {"3": {"name": "Work", "chats": ["901@c.us"]}})
    metadata(session)
    link(session, True, chat="901@s.whatsapp.net")
    assert session.emitted[-1][1]["labels"][0]["count"] == 1
    link(session, False, chat="901@s.whatsapp.net")
    assert label_events.registry(session)["3"]["chats"] == []


def test_socket_failure_does_not_fail_an_already_accepted_label_patch(session):
    s = session
    metadata(s)
    sent = []
    s.client.send_app_state = sent.append
    def fail(*args):
        raise RuntimeError("socket unavailable")
    s.emit = fail
    value = SyncActionValue(labelAssociationAction=LabelAssociationAction(labeled=True))
    app_state_mirror.send(s, "regular", [(["label_jid", "3", GROUP], 3, value, False)])
    assert len(sent) == 1
    assert label_events.registry(s)["3"]["chats"] == [GROUP]


def test_a_deleted_label_is_not_returned_and_complete_sync_drops_stale_legacy(session):
    labels.save(session.folder, {"1000": {"name": "Old", "chats": []}})
    metadata(session)
    assert set(label_events.registry(session)) == {"3", "1000"}
    session.app_state_synced("regular")
    assert set(label_events.registry(session)) == {"3"}
    mutate(session, ["label_edit", "3"], SyncActionValue(
        labelEditAction=LabelEditAction(name="Work", deleted=True)))
    assert label_events.registry(session) == {}


def test_label_routes_read_phone_metadata_and_mirror_their_own_writes(session):
    s = session
    metadata(s)
    link(s, True, full=True)
    s.app_state_synced("regular")
    sent = []
    s.client.send_app_state = sent.append

    async def run():
        app = web.Application()
        labels.register_routes(app.router, session=lambda req: s,
            ok=lambda **kw: web.json_response(kw), jid_from_text=lambda text: text,
            jid_text=str)
        async with TestClient(TestServer(app)) as http:
            response = await http.get('/api/t/get-all-labels')
            assert (await response.json())["response"] == [
                {"id": "3", "name": "Work", "colorIndex": 5, "count": 1}]
            response = await http.post('/api/t/add-new-label', json={"name": "New"})
            assert response.status == 201
            response = await http.post('/api/t/add-or-remove-label', json={
                "chatIds": [GROUP], "options": [{"labelId": "1000", "type": "add"}]})
            assert response.status == 200
            response = await http.get('/api/t/get-all-labels')
            result = (await response.json())["response"]
            assert {row["id"]:row["count"] for row in result} == {"3": 1, "1000": 1}
            assert s.emitted[-1][1]["ids"] == ["1000"]
            assert set(s.emitted[-1][1]["chat"]["labels"]) == {"3", "1000"}
            response = await http.put('/api/t/delete-label/3')
            assert response.status == 200
            response = await http.get('/api/t/get-all-labels')
            assert [row["id"] for row in (await response.json())["response"]] == ["1000"]
    asyncio.run(run())
    assert len(sent) == 3


def test_business_labels_drop_the_accounts_chat_lists():
    registry = {"3": {"type": 0}, "4": {"type": 4, "isImmutable": False, "predefinedID": 2},
                "8": {"type": 4, "predefinedID": 0}, "5": {"type": 5},
                "6": {"type": 3, "isImmutable": True}, "7": {"type": 0, "isImmutable": True},
                "1000": {"name": "legacy, no type"}}
    assert set(label_events.business_labels(registry)) == {"3", "4", "1000"}


def test_label_routes_never_list_or_delete_chat_lists(session):
    s = session
    metadata(s)
    metadata(s, "5", type=LabelEditAction.CUSTOM)
    metadata(s, "6", type=LabelEditAction.FAVORITES, isImmutable=True)
    s.app_state_synced("regular")
    sent = []
    s.client.send_app_state = sent.append

    async def run():
        app = web.Application()
        labels.register_routes(app.router, session=lambda req: s,
            ok=lambda **kw: web.json_response(kw), jid_from_text=lambda text: text,
            jid_text=str)
        async with TestClient(TestServer(app)) as http:
            response = await http.get('/api/t/get-all-labels')
            assert [row["id"] for row in (await response.json())["response"]] == ["3"]
            response = await http.put('/api/t/delete-label/5')
            assert response.status == 404
            response = await http.put('/api/t/delete-all-labels')
            assert (await response.json())["response"] == {"ids": ["3"]}
    asyncio.run(run())
    assert len(sent) == 1  # only the Business label's deletion
    assert set(label_events.registry(s)) == {"5", "6"}
