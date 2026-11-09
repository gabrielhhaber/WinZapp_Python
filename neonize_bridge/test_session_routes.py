"""The session routes of the real server (create_app), with the real Session and the
fake neonize client: start-session, status-session, check-connection-
session, close-session, host-device, statuses, status/{id}/delete and /healthz.
Not tested against a real account."""
import threading
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

import authentication as auth
import server


class FakeClient:
    def __init__(self, connected=True):
        self.is_connected = connected
        self.calls = []

    def disconnect(self):
        self.calls.append("disconnect")

    def stop(self):
        self.calls.append("stop")

    def logout(self):
        self.calls.append("logout")


@pytest.fixture
def sessions(monkeypatch):
    created = {}

    class Registered(server.Session):
        def __init__(self, name, *args, **kw):
            super().__init__(name, *args, **kw)
            created[name] = self

    monkeypatch.setattr(server, "Session", Registered)
    return created


async def _bridge(folder):
    client = TestClient(TestServer(server.create_app(str(folder))))
    await client.start_server()
    secret = (folder / auth.SECRET_FILE).read_text(encoding="utf-8")
    return client, f"account1:{auth.session_token(secret, 'account1')}", secret


@pytest.mark.asyncio
async def test_healthz_answers_like_node(tmp_path, sessions):
    client, _, _ = await _bridge(tmp_path)
    try:
        response = await client.get("/healthz")
        # wait_for_api (client/core/wpp_update_health.py) requires {"message": "OK"}
        assert response.status == 200 and await response.json() == {"message": "OK"}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_start_session_runs_begin_outside_the_loop(tmp_path, sessions, monkeypatch):
    """begin() waits for the old thread to finish (up to 10 s): in the aiohttp loop
    that froze all the routes and Socket.IO."""
    where = {}

    def fake_begin(self, phone_number=None):
        where["thread"] = threading.get_ident()
        return True

    monkeypatch.setattr(server.Session, "begin", fake_begin)
    client, token, _ = await _bridge(tmp_path)
    try:
        response = await client.post(f"/api/{token}/start-session", json={})
        assert response.status == 200
        assert where["thread"] != threading.get_ident()
        # the start-all (administration) too
        where.clear()
        response = await client.post(f"/api/{auth.load_secret(str(tmp_path))}/start-all")
        assert response.status == 201
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_start_session_after_close_reconnects(tmp_path, sessions, monkeypatch):
    """End to end of defect 1: close-session and start-session on the same session."""
    clients = []

    def fake_run(self, phone_number, generation):
        cli = self.client = FakeClient()
        clients.append(cli)
        try:
            while "stop" not in cli.calls:
                time.sleep(0.01)
        finally:
            self._apply(generation, "thread_ended")

    monkeypatch.setattr(server.Session, "_run", fake_run)
    client, token, _ = await _bridge(tmp_path)
    try:
        assert (await client.post(f"/api/{token}/start-session", json={})).status == 200
        assert (await client.post(f"/api/{token}/close-session")).status == 200
        response = await client.post(f"/api/{token}/start-session", json={})
        assert (await response.json())["status"] == "INITIALIZING"
        assert len(clients) == 2
        await client.post(f"/api/{token}/close-session")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_check_connection_looks_at_the_socket_and_not_only_at_the_cached_status(tmp_path, sessions):
    client, token, _ = await _bridge(tmp_path)
    try:
        await client.get(f"/api/{token}/status-session")       # creates the session
        s = sessions["account1"]
        s.client = FakeClient(connected=True)
        s.status = "CONNECTED"
        assert (await (await client.get(f"/api/{token}/check-connection-session")).json())[
            "status"] is True
        s.client.is_connected = False                          # network drop
        body = await (await client.get(f"/api/{token}/check-connection-session")).json()
        assert body["status"] is False and body["message"] == "Disconnected"
        s.status = "CLOSED"
        s.client.is_connected = True
        assert (await (await client.get(f"/api/{token}/check-connection-session")).json())[
            "status"] is False
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_host_device_by_state(tmp_path, sessions):
    client, token, _ = await _bridge(tmp_path)
    try:
        # freshly started bridge: it used to be 200 with an empty number, read as "unlinked"
        response = await client.get(f"/api/{token}/host-device")
        assert response.status == 503
        s = sessions["account1"]
        s.me, s.lid = "5511999990000@s.whatsapp.net", "123@lid"
        s.status = "CONNECTED"
        response = await client.get(f"/api/{token}/host-device")
        body = await response.json()
        assert response.status == 200
        assert body["response"]["phoneNumber"]["_serialized"] == "5511999990000@s.whatsapp.net"
        s.status = "INITIALIZING"                               # reconnecting: it does not know
        assert (await client.get(f"/api/{token}/host-device")).status == 503
        s.status = "notLogged"                                  # really unlinked
        response = await client.get(f"/api/{token}/host-device")
        assert response.status == 200
        assert (await response.json())["response"] == {}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_logout_from_the_phone_stays_visible_in_the_state_query(tmp_path, sessions):
    client, token, _ = await _bridge(tmp_path)
    try:
        await client.get(f"/api/{token}/status-session")
        s = sessions["account1"]
        s.emit = lambda *a, **kw: None
        s.me = "5511999990000@s.whatsapp.net"
        g = s.lifecycle.open()
        s.lifecycle.apply(g, "connected")
        s._on_unlinked(g, FakeClient())
        body = await (await client.get(f"/api/{token}/status-session")).json()
        assert body["status"] == "notLogged"
        host = await client.get(f"/api/{token}/host-device")
        assert (await host.json())["response"] == {}            # it does not return the old number
        await client.post(f"/api/{token}/close-session")       # what the client does afterwards
        body = await (await client.get(f"/api/{token}/status-session")).json()
        assert body["status"] == "notLogged"
    finally:
        await client.close()


def _status(id_, from_me=True, t=None):
    return {"id": {"id": id_, "fromMe": from_me, "remote": "status@broadcast"},
            "t": t or int(time.time()), "fromMe": from_me, "to": "status@broadcast",
            "type": "chat", "body": "x", "author": "5511@c.us"}


@pytest.mark.asyncio
async def test_statuses_empty_database_is_not_whatsapps_final_word(tmp_path, sessions):
    """myStatusReady:true with an empty database made the client delete the own
    stories it had cached."""
    client, token, _ = await _bridge(tmp_path)
    try:
        body = await (await client.get(f"/api/{token}/statuses")).json()
        assert body["response"]["myStatus"] == []
        assert body["response"]["myStatusReady"] is False
        sessions["account1"].db.message("status@broadcast", _status("ST1"))
        body = await (await client.get(f"/api/{token}/statuses")).json()
        assert [m["id"]["id"] for m in body["response"]["myStatus"]] == ["ST1"]
        assert body["response"]["myStatusReady"] is True
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_deleting_a_status_that_does_not_exist_or_is_not_mine_answers_not_found(tmp_path, sessions):
    client, token, _ = await _bridge(tmp_path)
    try:
        await client.get(f"/api/{token}/status-session")
        s = sessions["account1"]
        s.db.message("status@broadcast", _status("OTHERS", from_me=False))
        s.db.message("5511@s.whatsapp.net", {**_status("CHAT"), "to": "5511@c.us"})
        for id_ in ("DOESNOTEXIST", "OTHERS", "CHAT"):
            response = await client.post(f"/api/{token}/status/{id_}/delete")
            assert response.status == 404, id_
            body = await response.json()
            assert body["code"] == "not_found" and body["status"] == "error"
        # an existing status of mine follows the earlier path (here, without a connection)
        s.db.message("status@broadcast", _status("MINE"))
        response = await client.post(f"/api/{token}/status/MINE/delete")
        assert response.status == 404                 # "Disconnected", like WPPConnect
        assert (await response.json()).get("code") != "not_found"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_session_routes_accept_bearer_with_only_the_session_name(tmp_path, sessions):
    client, _, secret = await _bridge(tmp_path)
    try:
        response = await client.get("/api/account1/check-connection-session")
        assert response.status == 401
        response = await client.get("/api/account1/check-connection-session",
                                    headers={"Authorization": f"Bearer {secret}"})
        assert response.status == 200
        response = await client.post("/api/account1/close-session",
                                     headers={"Authorization": f"Bearer {secret}"})
        assert response.status == 200
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_pairing_history_is_stored_and_a_qr_on_screen_is_redone(tmp_path, sessions, monkeypatch):
    import pairing_history
    restarted = []
    monkeypatch.setattr(server.Session, "restart_pairing",
                        lambda self: restarted.append(self.name) or True)
    client, token, _ = await _bridge(tmp_path)
    try:
        response = await client.post(f"/api/{token}/pairing-history", json={"days": 7})
        assert response.status == 400
        s = sessions["account1"]
        s.status = "QRCODE"
        response = await client.post(f"/api/{token}/pairing-history", json={"days": 3650})
        body = await response.json()
        assert body["response"] == {"days": 3650, "reason": "stored", "restarted": True}
        assert pairing_history.load(s.folder, 0) == 3650 and restarted == ["account1"]
        s.status = "CONNECTED"
        response = await client.post(f"/api/{token}/pairing-history", json={"days": 30})
        assert (await response.json())["response"]["reason"] == "paired"
        assert pairing_history.load(s.folder, 0) == 3650
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_start_session_stores_the_history_chosen_before_pairing(tmp_path, sessions, monkeypatch):
    import pairing_history
    monkeypatch.setattr(server.Session, "begin", lambda self, phone_number=None: True)
    client, token, _ = await _bridge(tmp_path)
    try:
        await client.post(f"/api/{token}/start-session", json={"historyDays": 365})
        s = sessions["account1"]
        assert pairing_history.load(s.folder, 0) == 365
        await client.post(f"/api/{token}/start-session", json={"historyDays": 9})   # not a choice
        assert pairing_history.load(s.folder, 0) == 365
    finally:
        await client.close()
