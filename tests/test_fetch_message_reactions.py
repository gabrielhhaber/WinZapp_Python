"""Tests for MainWindow.fetch_message_reactions() — GET /reactions/{msgId},
wppconnect-server's own (unmodified) DeviceController.getReactions(),
already registered at /api/:session/reactions/:id but never called from
WinZapp's Python side before this.

See tests/test_reaction_backfill.py for the ConversationsPanel side that
actually uses this — this file is the network-call boundary alone: any
non-2xx, timeout, or malformed body must come back as None, never raise,
since a caller that can't tell "nothing to report" apart from "the request
blew up" would risk treating a failure as "no reaction exists" and erasing
one it doesn't actually know is gone.

MainWindow is a wx.Frame; the method is exercised unbound against a stub
carrying only what it touches, in the style CLAUDE.md prescribes.
"""

import main as main_module
from main import MainWindow


class _Response:
    def __init__(self, status_code=200, body=None, raise_on_json=False):
        self.status_code = status_code
        self._body = body
        self._raise_on_json = raise_on_json

    def json(self):
        if self._raise_on_json:
            raise ValueError("not json")
        return self._body


class _Stub:
    fetch_message_reactions = MainWindow.fetch_message_reactions

    def __init__(self, wa_connected=True):
        self.wpp_server = "http://127.0.0.1"
        self.wpp_port = 6300
        self.token = "sess1:hash1"
        self._wa_connected = wa_connected


class TestNeverCallsOutWithNothingToAsk:
    def test_empty_msg_id_short_circuits(self, monkeypatch):
        calls = []
        monkeypatch.setattr(main_module, "api_get", lambda *a, **kw: calls.append(1))

        assert _Stub().fetch_message_reactions("") is None
        assert calls == []

    def test_not_connected_short_circuits(self, monkeypatch):
        calls = []
        monkeypatch.setattr(main_module, "api_get", lambda *a, **kw: calls.append(1))

        assert _Stub(wa_connected=False).fetch_message_reactions("msg1") is None
        assert calls == []


class TestSuccessfulResponse:
    def test_returns_the_response_payload(self, monkeypatch):
        payload = {"reactionByMe": None, "reactions": [{"aggregateEmoji": "👍"}]}
        monkeypatch.setattr(
            main_module, "api_get",
            lambda *a, **kw: _Response(200, {"status": "success", "response": payload}),
        )

        assert _Stub().fetch_message_reactions("msg1") == payload

    def test_url_carries_the_session_and_message_id(self, monkeypatch):
        seen = {}

        def _fake_api_get(url, **kw):
            seen["url"] = url
            return _Response(200, {"status": "success", "response": {}})

        monkeypatch.setattr(main_module, "api_get", _fake_api_get)

        _Stub().fetch_message_reactions("true_123@s.whatsapp.net_ABC")

        assert seen["url"] == (
            "http://127.0.0.1:6300/api/sess1:hash1/reactions/true_123@s.whatsapp.net_ABC"
        )


class TestFailureModesAllReturnNoneRatherThanRaise:
    def test_non_200_status(self, monkeypatch):
        monkeypatch.setattr(main_module, "api_get", lambda *a, **kw: _Response(404))

        assert _Stub().fetch_message_reactions("msg1") is None

    def test_malformed_json_body(self, monkeypatch):
        monkeypatch.setattr(
            main_module, "api_get",
            lambda *a, **kw: _Response(200, raise_on_json=True),
        )

        assert _Stub().fetch_message_reactions("msg1") is None

    def test_response_field_is_not_a_dict(self, monkeypatch):
        monkeypatch.setattr(
            main_module, "api_get",
            lambda *a, **kw: _Response(200, {"status": "success", "response": "nope"}),
        )

        assert _Stub().fetch_message_reactions("msg1") is None

    def test_request_raises(self, monkeypatch):
        def _boom(*a, **kw):
            raise ConnectionError("offline")
        monkeypatch.setattr(main_module, "api_get", _boom)

        assert _Stub().fetch_message_reactions("msg1") is None
