"""The client side of a pairing the phone accepted (macOS report, 2026-11-24).

Two cracks, both found in the same report:

* the pairing dialog closes only on session-logged, so when host.layer.js
  gives up on a pairing it reports `PairingNotCompleted` and the dialog has
  to say so out loud (wppconnect_pairing_completion_patch.py);
* `paired` is only written on session-logged. A relaunch before it came read
  check-connection-session=false + paired=False as "unlinked or incomplete",
  cleared the token and closed the session — discarding a device the phone
  listed as linked. The server now emits `pairing-accepted` at qrReadSuccess
  and the client keeps that session.
"""

import threading
from pathlib import Path

import pytest

import core.websocket_client as ws_module
import ui.dialogs.connect as connect_module
from core.websocket_client import WebSocketClient
from ui.dialogs.connect import Connect, pairing_was_accepted

ROOT = Path(__file__).resolve().parent.parent
SESSION = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
OTHER = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


class TestPairingWasAccepted:
    def test_the_session_the_token_names(self):
        assert pairing_was_accepted({"link_accepted_session": SESSION}, f"{SESSION}:hash")

    def test_any_other_session(self):
        assert not pairing_was_accepted({"link_accepted_session": OTHER}, f"{SESSION}:hash")

    def test_nothing_recorded_or_no_token(self):
        assert not pairing_was_accepted({}, f"{SESSION}:hash")
        assert not pairing_was_accepted({"link_accepted_session": ""}, "")
        assert not pairing_was_accepted({"link_accepted_session": SESSION}, None)


# --- the relaunch ------------------------------------------------------------

class _Response:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body


class _MainWindow:
    def __init__(self, privateinfo):
        self.settings = {"general": {"language": "en-US"}, "privateinfo": privateinfo}
        self._token = f"{SESSION}:hash"
        self.wpp_server = "http://127.0.0.1"
        self.wpp_port = 6300
        self.app_name = "WinZapp"
        self.cleared = 0
        self.tokens = []

    def _get_wa_token(self):
        return self._token

    def _set_wa_token(self, value):
        self.tokens.append(value)
        self._token = value

    def save_settings(self):
        pass

    def clear_local_data(self):
        self.cleared += 1


@pytest.fixture
def fresh_node(monkeypatch):
    """Node just started: the session is not running, so check-connection
    answers false, as at 03:31:48 in the report."""
    closed = []
    monkeypatch.setattr(connect_module.wx, "CallAfter", lambda fn, *a, **kw: fn(*a, **kw))
    monkeypatch.setattr(connect_module.wx, "MessageBox", lambda *a, **kw: None)
    monkeypatch.setattr(connect_module.wx, "IsMainThread", lambda: True)
    monkeypatch.setattr(connect_module, "api_post", lambda *a, **kw: closed.append(a) or _Response(200))

    def fake_get(url, headers=None, timeout=None):
        if "check-connection-session" in url:
            return _Response(200, {"status": False})
        return _Response(200, {"status": "CLOSED"})

    monkeypatch.setattr(connect_module, "api_get", fake_get)
    return closed


class TestRelaunchAfterAcceptance:
    def test_a_session_the_phone_accepted_is_kept(self, fresh_node):
        mw = _MainWindow({"link_accepted_session": SESSION})
        assert Connect(mw).check_connection_status() is True
        assert mw.cleared == 0
        assert mw.tokens == []

    def test_control_an_unfinished_pairing_is_still_discarded(self, fresh_node):
        """The case the wipe exists for: nobody ever accepted this session."""
        mw = _MainWindow({"link_accepted_session": OTHER})
        assert Connect(mw).check_connection_status() is False
        assert mw.cleared == 1
        assert mw.tokens == [""]


# --- the socket events -------------------------------------------------------

class _Sound:
    def __init__(self):
        self.plays = 0

    def play(self):
        self.plays += 1


class _Speech:
    def __init__(self):
        self.spoken = []

    def output(self, text):
        self.spoken.append(text)


class _WsMainWindow:
    app_name = "WinZapp"

    def __init__(self, dialog_active):
        self._dialog_active = dialog_active
        self.error_sound = _Sound()
        self.speak_output = _Speech()
        self.settings = {"privateinfo": {}}
        self.saves = 0

    def _is_pairing_dialog_active(self):
        return self._dialog_active

    def save_settings(self):
        self.saves += 1


class _I18n:
    def t(self, key):
        return {"pairing_failed_msg": "Could not finish pairing."}.get(key, key)


class _Ws:
    on_wpp_phone_code_error = WebSocketClient.on_wpp_phone_code_error
    on_wpp_pairing_accepted = WebSocketClient.on_wpp_pairing_accepted
    _announce_pairing_not_completed = WebSocketClient._announce_pairing_not_completed
    _announce_pairing_code_expired = WebSocketClient._announce_pairing_code_expired
    _belongs_to_this_session = WebSocketClient._belongs_to_this_session

    def __init__(self, dialog_active=True):
        self.instance_name = SESSION
        self._phone_code_error = ""
        self._phone_code_value = "1LE591ZK"
        self._phone_code_event = threading.Event()
        self.main_window = _WsMainWindow(dialog_active)
        self.i18n = _I18n()


@pytest.fixture
def synchronous_call_after(monkeypatch):
    monkeypatch.setattr(ws_module.wx, "CallAfter", lambda fn, *a, **kw: fn(*a, **kw))


class TestPairingNotCompleted:
    def test_the_dialog_says_the_pairing_failed(self, synchronous_call_after):
        ws = _Ws()
        ws.on_wpp_phone_code_error({"session": SESSION, "name": "PairingNotCompleted",
                                    "message": "WhatsApp Web did not connect"})
        assert ws.main_window.speak_output.spoken == ["Could not finish pairing."]
        assert ws.main_window.error_sound.plays == 1
        assert ws._phone_code_error.startswith("PairingNotCompleted")

    def test_silent_without_a_dialog(self, synchronous_call_after):
        ws = _Ws(dialog_active=False)
        ws.on_wpp_phone_code_error({"session": SESSION, "name": "PairingNotCompleted"})
        assert ws.main_window.speak_output.spoken == []

    def test_another_sessions_report_is_ignored(self, synchronous_call_after):
        ws = _Ws()
        ws.on_wpp_phone_code_error({"session": OTHER, "name": "PairingNotCompleted"})
        assert ws.main_window.speak_output.spoken == []


class TestPairingAccepted:
    def test_records_this_session(self):
        ws = _Ws()
        ws.on_wpp_pairing_accepted({"session": SESSION})
        assert ws.main_window.settings["privateinfo"]["link_accepted_session"] == SESSION
        assert ws.main_window.saves == 1

    def test_ignores_another_session(self):
        ws = _Ws()
        ws.on_wpp_pairing_accepted({"session": OTHER})
        assert "link_accepted_session" not in ws.main_window.settings["privateinfo"]

    def test_survives_a_malformed_payload(self):
        ws = _Ws()
        ws.on_wpp_pairing_accepted(None)
        ws.on_wpp_pairing_accepted({})
        assert ws.main_window.saves == 0


class TestTheWiring:
    def test_the_client_listens_for_it(self):
        source = (ROOT / "client" / "core" / "websocket_client.py").read_text(encoding="utf-8")
        assert 'self.sio.on("pairing-accepted", self.on_wpp_pairing_accepted)' in source

    def test_the_server_emits_it_on_qr_read_success(self):
        source = (ROOT / "client" / "api_patches" / "src" / "util" / "createSessionUtil.ts").read_text(encoding="utf-8")
        branch = source.index("statusFind === StatusFind.qrReadSuccess")
        assert source.index("req.io.emit('pairing-accepted', { session: client.session })", branch) > branch
