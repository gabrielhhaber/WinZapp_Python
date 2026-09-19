"""Tests for StatusPanel._fetch_statuses_from_api() (client/status_panel.py).

The Status tab must pull the user's own posted statuses straight from the
account (WPPConnect's StatusV3Store via GET /api/{session}/statuses) instead
of relying solely on status@broadcast messages over the Socket.IO channel,
which WhatsApp Web only emits once the Status view was opened in the browser
— leaving the tab empty on fresh sessions. This test verifies the API path:
raw store messages are normalized (WebSocketClient._normalize_wpp_message)
and split into my statuses vs. contacts' statuses by _parse_statuses().

StatusPanel is a wx.Panel and cannot be instantiated without a running
wx.App, so _fetch_statuses_from_api() is exercised as a plain function bound
to a lightweight stub — same approach as tests/test_status_panel.py.
"""

import pytest

import requests

from core.websocket_client import WebSocketClient
from status_panel import StatusPanel


class _FakeWS:
    _normalize_wpp_message = WebSocketClient._normalize_wpp_message
    _clean_jid = WebSocketClient._clean_jid


class _MWStub:
    def __init__(self):
        self.wpp_server = "http://127.0.0.1"
        self.wpp_port = "6300"
        self.token = "sessao-teste"
        self.ws = _FakeWS()
        self._lid_to_phone = {}
        self._phone_to_lid = {}

        class _I18n:
            def t(self, key, **kw):
                return key

        self.i18n = _I18n()
        self.settings = {"status_panel": {"viewed_status_ids": []}}
        self._is_self_jid_calls = []

    def _is_self_jid(self, jid):
        # "62655318482954@lid" is the account's own LID in the fixtures.
        self._is_self_jid_calls.append(jid)
        return jid in ("62655318482954@lid", "5511919177719@s.whatsapp.net")


class _PanelStub:
    _fetch_statuses_from_api = StatusPanel._fetch_statuses_from_api
    _parse_statuses = StatusPanel._parse_statuses

    def __init__(self, mw):
        self.main_window = mw

    def _resolve_name(self, jid):
        return ""


def _text_status(jid: str, body: str, from_me: bool, msg_id: str) -> dict:
    prefix = "true" if from_me else "false"
    return {
        "id": f"{prefix}_{jid}_{msg_id}",
        "body": body,
        "type": "chat",
        "t": 1786000000,
        "from": "62655318482954@lid" if from_me else jid,
        "to": "status@broadcast",
        "fromMe": from_me,
        "isSentByMe": from_me,
        "author": "62655318482954@lid" if from_me else jid,
        "participant": "62655318482954@lid" if from_me else jid,
    }


def test_fetch_from_api_returns_my_and_contacts_statuses(monkeypatch):
    api_payload = {
        "status": "success",
        "response": {
            "myStatus": [_text_status("62655318482954@lid", "meu status", True, "M1")],
            "contacts": [
                {
                    "jid": "73504238084102@lid",
                    "msgs": [_text_status("73504238084102@lid", "status dele", False, "C1")],
                }
            ],
        },
    }

    class _Resp:
        status_code = 200

        def json(self):
            return api_payload

    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())

    panel = _PanelStub(_MWStub())
    my_statuses, contacts = panel._fetch_statuses_from_api()

    assert len(my_statuses) == 1
    assert my_statuses[0]["key"]["fromMe"] is True
    assert len(contacts) == 1
    assert contacts[0]["jid"] == "73504238084102@lid"
    assert len(contacts[0]["statuses"]) == 1


def test_fetch_from_api_marks_genuinely_empty_my_status_as_ready(monkeypatch):
    api_payload = {
        "status": "success",
        "response": {"myStatus": [], "contacts": [], "myStatusReady": True},
    }

    class _Resp:
        status_code = 200

        def json(self):
            return api_payload

    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())

    panel = _PanelStub(_MWStub())
    my_statuses, contacts = panel._fetch_statuses_from_api()

    assert my_statuses == []
    assert contacts == []
    assert panel._last_status_api_ok is True
    assert panel._last_my_status_ready is True


def test_fetch_from_old_api_keeps_empty_my_status_non_authoritative(monkeypatch):
    api_payload = {
        "status": "success",
        "response": {"myStatus": [], "contacts": []},
    }

    class _Resp:
        status_code = 200

        def json(self):
            return api_payload

    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())

    panel = _PanelStub(_MWStub())
    panel._fetch_statuses_from_api()

    assert panel._last_status_api_ok is True
    assert panel._last_my_status_ready is False


def test_fetch_from_api_http_error_falls_back_empty(monkeypatch):
    class _Resp:
        status_code = 500

        def json(self):
            return {}

    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())

    panel = _PanelStub(_MWStub())
    my_statuses, contacts = panel._fetch_statuses_from_api()
    assert my_statuses == []
    assert contacts == []


def test_fetch_from_api_exception_falls_back_empty(monkeypatch):
    def _boom(*a, **k):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(requests, "get", _boom)

    panel = _PanelStub(_MWStub())
    my_statuses, contacts = panel._fetch_statuses_from_api()
    assert my_statuses == []
    assert contacts == []


def test_post_was_rejected_detects_error_unknown():
    from status_panel import _post_was_rejected
    body = {
        "status": "success",
        "response": [
            {"id": "true_status@broadcast_X", "ack": 0,
             "sendMsgResult": {"messageSendResult": "ERROR_UNKNOWN"}}
        ],
    }
    assert _post_was_rejected(body) is True


def test_post_was_rejected_accepts_success():
    from status_panel import _post_was_rejected
    body = {
        "status": "success",
        "response": [
            {"id": "true_status@broadcast_X", "ack": 3,
             "sendMsgResult": {"messageSendResult": "SUCCESS"}}
        ],
    }
    assert _post_was_rejected(body) is False


def test_post_was_rejected_null_response():
    from status_panel import _post_was_rejected
    assert _post_was_rejected({"response": None}) is True
    assert _post_was_rejected({"response": []}) is False
    assert _post_was_rejected({}) is True


def test_merge_status_contacts_dedup_by_jid_and_id():
    from status_panel import StatusPanel
    a = [{"name": "X", "jid": "a@lid", "statuses": [
        {"key": {"id": "S1", "participant": "a@lid"}},
        {"key": {"id": "S2", "participant": "a@lid"}},
    ]}]
    b = [{"name": "X", "jid": "a@lid", "statuses": [
        {"key": {"id": "S2", "participant": "a@lid"}},
        {"key": {"id": "S3", "participant": "a@lid"}},
    ]}]
    merged = StatusPanel._merge_status_contacts(a, b)
    assert len(merged) == 1
    ids = [s["key"]["id"] for s in merged[0]["statuses"]]
    assert ids == ["S1", "S2", "S3"]


def test_merge_status_lists_dedup():
    from status_panel import StatusPanel
    a = [{"key": {"id": "M1"}}, {"key": {"id": "M2"}}]
    b = [{"key": {"id": "M2"}}, {"key": {"id": "M3"}}]
    merged = StatusPanel._merge_status_lists(a, b)
    assert [m["key"]["id"] for m in merged] == ["M1", "M2", "M3"]
