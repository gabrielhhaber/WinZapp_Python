"""Archived chats and new messages: WhatsApp's "Keep chats archived" (chat_archive.py)."""
from types import SimpleNamespace as N

import pytest
from neonize.proto.waSyncAction.WAWebProtobufSyncAction_pb2 import SyncActionValue

import app_state_mirror
import chat_archive
from store import Store

GROUP = "120363000000000001@g.us"


def _setting(unarchive, version=4):
    value = SyncActionValue()
    value.unarchiveChatsSetting.unarchiveChats = unarchive
    return (["setting_unarchiveChats"], version, value.SerializeToString())


def test_never_seen_the_chat_stays_archived():
    """WhatsApp's default is to keep chats archived."""
    assert chat_archive.unarchives_on_message([]) is False


def test_the_setting_off_unarchives():
    assert chat_archive.unarchives_on_message([_setting(True)]) is True
    assert chat_archive.unarchives_on_message([_setting(False)]) is False


def test_the_newest_version_decides():
    assert chat_archive.unarchives_on_message([_setting(True, 3), _setting(False, 5)]) is False


def test_an_unreadable_value_keeps_it_archived():
    assert chat_archive.unarchives_on_message([(["setting_unarchiveChats"], 1, b"\xff\xff")]) is False


@pytest.mark.parametrize("message, leaves", [
    ({"fromMe": False, "type": "chat"}, True),
    ({"fromMe": True, "type": "chat"}, False),
    ({"fromMe": False, "type": "gp2"}, False),
    ({"fromMe": False, "type": "call_log"}, False),
    ({"fromMe": False, "type": "revoked"}, False),
])
def test_what_takes_a_chat_out(message, leaves):
    assert chat_archive.leaves_archive(True, message, True) is leaves
    assert chat_archive.leaves_archive(False, message, True) is False
    assert chat_archive.leaves_archive(True, message, False) is False


class _Session:
    def __init__(self, tmp_path, unarchive=None, seeded=True):
        self.db = Store(str(tmp_path / "bridge.db"))
        self.name = "s"
        self.folder = str(tmp_path)
        self.events = []
        mirror = app_state_mirror.mirror_of(self)
        if unarchive is not None:
            index, version, value = _setting(unarchive)
            mirror.apply("regular_low", index, version, False, value, full_sync=seeded)
        if seeded:
            mirror.full_sync_completed("regular_low")
        self.client, self.status = None, "CLOSED"
        self.aliases = {}

    def chat_keys(self, jid):
        return self.aliases.get(jid, [jid])

    def emit(self, event, data):
        self.events.append((event, data))

    def close(self):
        self.app_state.close()
        self.db.close()


@pytest.fixture
def supported(monkeypatch):
    monkeypatch.setattr(app_state_mirror, "supported", lambda: True)


def test_a_message_in_an_archived_chat_unarchives_it_with_the_setting_off(tmp_path, supported):
    s = _Session(tmp_path, unarchive=True)
    try:
        s.db.chat(GROUP, archived=1)
        chat_archive.on_arrival(s, GROUP, {"fromMe": False, "type": "chat"})
        assert s.db.archived(GROUP) is False
        event, data = s.events[0]
        assert event == "chats-update" and data["data"][0]["archive"] is False
    finally:
        s.close()


def test_a_chat_archived_under_its_other_address_comes_out_too(tmp_path, supported):
    """The phone's notice named the number; the message came under the LID."""
    s = _Session(tmp_path, unarchive=True)
    try:
        number, lid = "5511999990000@s.whatsapp.net", "111222333@lid"
        s.aliases = {lid: [lid, number]}
        s.db.chat(number, archived=1)
        s.db.chat(lid, archived=0)
        chat_archive.on_arrival(s, lid, {"fromMe": False, "type": "chat"})
        assert s.db.archived(number) is False
        assert [data["data"][0]["id"] for _event, data in s.events] == ["5511999990000@c.us"]
    finally:
        s.close()


def test_with_the_setting_on_it_stays(tmp_path, supported):
    s = _Session(tmp_path, unarchive=False)
    try:
        s.db.chat(GROUP, archived=1)
        chat_archive.on_arrival(s, GROUP, {"fromMe": False, "type": "chat"})
        assert s.db.archived(GROUP) is True and s.events == []
    finally:
        s.close()


def test_a_chat_not_archived_is_left_alone(tmp_path, supported):
    s = _Session(tmp_path, unarchive=True)
    try:
        s.db.chat(GROUP, archived=0)
        chat_archive.on_arrival(s, GROUP, {"fromMe": False, "type": "chat"})
        assert s.events == []
    finally:
        s.close()


def test_a_copy_not_complete_asks_for_the_setting_once(tmp_path, supported, monkeypatch):
    asked = []
    monkeypatch.setattr(app_state_mirror, "ensure_seeded", lambda s, c: asked.append(c))
    monkeypatch.setattr(chat_archive.threading, "Thread",
                        lambda target, args, **kw: N(start=lambda: target(*args)))
    s = _Session(tmp_path, seeded=False)
    try:
        s.db.chat(GROUP, archived=1)
        for _ in range(2):
            chat_archive.on_arrival(s, GROUP, {"fromMe": False, "type": "chat"})
        assert asked == ["regular_low"]
        assert s.db.archived(GROUP) is True
    finally:
        s.close()


def test_an_older_neonize_build_changes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(app_state_mirror, "supported", lambda: False)
    s = _Session(tmp_path, unarchive=True)
    try:
        s.db.chat(GROUP, archived=1)
        chat_archive.on_arrival(s, GROUP, {"fromMe": False, "type": "chat"})
        assert s.db.archived(GROUP) is True
    finally:
        s.close()


def test_the_bridge_calls_it_for_every_message_stored():
    import pathlib
    source = (pathlib.Path(__file__).parent / "server.py").read_text(encoding="utf-8")
    at = source.index('self.db.on_arrival(chat, w["t"], w["fromMe"])')
    assert "chat_archive.on_arrival(self, chat, w)" in source[at:at + 200]



# ── the setting itself, read and changed from WinZapp ────────────────────────

def test_keep_archived_is_the_opposite_of_unarchive():
    assert chat_archive.keep_archived([]) is None
    assert chat_archive.keep_archived([_setting(True)]) is False
    assert chat_archive.keep_archived([_setting(False)]) is True


@pytest.mark.parametrize("keep", [True, False])
def test_the_value_sent_is_whatsapp_webs(keep):
    value = chat_archive.setting_value(keep)
    assert value.unarchiveChatsSetting.unarchiveChats is (not keep)


@pytest.mark.parametrize("body, keep", [
    ({"keepArchived": True}, True), ({"keepArchived": False}, False),
    ({"keepArchived": "yes"}, None), ({}, None), (None, None)])
def test_the_request(body, keep):
    assert chat_archive.parse_request(body) is keep


def _run(s, requests):
    import asyncio
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    async def main():
        app = web.Application()
        chat_archive.register_routes(app.router, session=lambda req: s)
        async with TestClient(TestServer(app)) as http:
            outputs = []
            for method, body in requests:
                resp = await http.request(method, "/api/tok/keep-chats-archived", json=body)
                outputs.append((resp.status, await resp.json()))
            return outputs
    return asyncio.run(main())


def test_reading_and_changing_it(tmp_path, supported, monkeypatch):
    s = _Session(tmp_path, unarchive=True)
    s.client, s.status = object(), "CONNECTED"
    sent = []

    def send(session, collection, mutations):
        sent.append((collection, mutations[0][0], mutations[0][1]))
        index, version, value, remove = mutations[0]
        app_state_mirror.mirror_of(session).apply(
            collection, index, version, remove, value.SerializeToString())

    monkeypatch.setattr(app_state_mirror, "send", send)
    try:
        (status, body), (status2, body2), (status3, body3) = _run(s, [
            ("GET", None), ("POST", {"keepArchived": True}), ("GET", None)])
        assert (status, body["response"]["keepArchived"]) == (200, False)
        assert (status2, body2["response"]["keepArchived"]) == (200, True)
        assert sent == [("regular_low", ["setting_unarchiveChats"], 4)]
        assert body3["response"]["keepArchived"] is True
    finally:
        s.close()


def test_a_bad_request_and_a_closed_session(tmp_path, supported):
    s = _Session(tmp_path)
    try:
        (status, body), (status2, body2) = _run(s, [
            ("POST", {"keepArchived": "on"}), ("GET", None)])
        assert (status, body["code"]) == (400, "invalid_keep_archived")
        assert (status2, body2["code"]) == (503, "keep_archived_unavailable")
    finally:
        s.close()

