"""Tests for start_qrcode_connection()'s preserve_local_data parameter and its
threading through on_switch_to_qrcode()/on_switch_to_phone().

Reported live via a real log: an already-paired account whose QR flow was
re-entered (on_switch_to_qrcode, e.g. from the proactive re-pair dialog)
always ran clear_local_data() and wiped the entire local database — even
though the account being re-linked was, in the overwhelming common case, the
exact same one that had just been working. The cause was dead code:
on_switch_to_qrcode() calls _close_active_session() first, which clears
WA_token — so by the time start_qrcode_connection() checked "is there a
stored token" to decide whether this was a resume or a fresh pairing, the
answer was always empty, and the resume branch (whose own comments describe
preserving the token) could never be reached from its only caller.

on_switch_to_phone()'s analogous fix is that on_continue()'s
_can_reuse_existing_session() (tests/test_pairing_session_reuse.py) suffers
the exact same problem: it needs the pre-close token to recognise a
same-number resume, and _close_active_session() already erased it by the
time the user clicks Continue.

Connect is a plain class — same approach as tests/test_pairing_startup_grace.py.
"""

import pytest

import ui.dialogs.connect as connect_module
from ui.dialogs.connect import Connect


class _Response:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body

    @property
    def text(self):
        return ""


class _FakeWs:
    class _Sio:
        connected = False

        def disconnect(self):
            pass

    def __init__(self, *a, **kw):
        self.sio = self._Sio()


class _FakeMainWindow:
    def __init__(self, paired=False, token=""):
        self.settings = {
            "general": {"language": "pt-BR"},
            "privateinfo": {"paired": paired},
        }
        self._token = token
        self.token = token
        self.wpp_server = "http://127.0.0.1"
        self.wpp_port = 6300
        self.wpp_api_key = "api-key"
        self.app_name = "WinZapp"
        self.clear_local_data_calls = 0
        self.messages_set_completed = True
        self.qrcode_loaded_sound = _Sound()
        self.error_sound = _Sound()

    def _get_wa_token(self):
        return self._token

    def _set_wa_token(self, value):
        self._token = value

    def save_settings(self):
        pass

    def clear_local_data(self):
        self.clear_local_data_calls += 1

    def output(self, *a, **kw):
        pass

    def connect_websocket(self):
        pass


class _Sound:
    def play(self):
        pass


@pytest.fixture(autouse=True)
def _no_real_io(monkeypatch):
    monkeypatch.setattr(connect_module, "api_get",
                         lambda *a, **kw: _Response(200, {}))
    monkeypatch.setattr(connect_module, "api_post",
                         lambda *a, **kw: _Response(201, {"token": "hash123"}))
    monkeypatch.setattr(connect_module, "WebSocketClient", _FakeWs)
    monkeypatch.setattr(connect_module.wx, "CallAfter", lambda fn, *a, **kw: None)
    monkeypatch.setattr(connect_module.wx, "MessageBox", lambda *a, **kw: None)


class TestStartQrcodeConnectionPreservesOnRequest:
    def test_default_wipes_local_data_on_a_fresh_pairing(self):
        """Back-compat: nothing asked for preservation, no stored token —
        this is the original "brand-new pairing" case, unchanged."""
        mw = _FakeMainWindow(paired=False, token="")
        c = Connect(mw)
        c._create_instance = lambda token: None

        c.start_qrcode_connection()

        assert mw.clear_local_data_calls == 1

    def test_preserve_local_data_skips_the_wipe(self):
        mw = _FakeMainWindow(paired=True, token="")
        c = Connect(mw)
        c._create_instance = lambda token: None

        c.start_qrcode_connection(preserve_local_data=True)

        assert mw.clear_local_data_calls == 0

    def test_an_actually_resumable_token_is_never_wiped_either_way(self):
        """If a token somehow does survive to this point, the original
        resume branch already skipped the wipe — preserve_local_data must
        not change that."""
        mw = _FakeMainWindow(paired=True, token="sess1:hash1")
        c = Connect(mw)
        c._create_instance = lambda token: None

        c.start_qrcode_connection(preserve_local_data=False)

        assert mw.clear_local_data_calls == 0


class TestOnSwitchToQrcodeThreadsThePairedFlag:
    def test_paired_account_preserves_data_across_the_switch(self, monkeypatch):
        mw = _FakeMainWindow(paired=True, token="sess1:hash1")
        c = Connect(mw)
        c.qrcode_panel = _Panel()
        c.phone_panel = _Panel()
        c.connection_dial = _Dial()
        c._create_instance = lambda token: None
        # _close_active_session() posts close-session and clears WA_token —
        # exercise the real method so this test proves the fix survives it,
        # not a shortcut around it.
        c.on_switch_to_qrcode(None)

        assert mw.clear_local_data_calls == 0

    def test_never_paired_account_still_wipes(self, monkeypatch):
        mw = _FakeMainWindow(paired=False, token="")
        c = Connect(mw)
        c.qrcode_panel = _Panel()
        c.phone_panel = _Panel()
        c.connection_dial = _Dial()
        c._create_instance = lambda token: None

        c.on_switch_to_qrcode(None)

        assert mw.clear_local_data_calls == 1


class TestOnSwitchToPhoneCarriesTheTokenForward:
    def test_captures_the_token_before_close_active_session_clears_it(self):
        mw = _FakeMainWindow(paired=True, token="sess1:hash1")
        c = Connect(mw)
        c.qrcode_panel = _Panel()
        c.phone_panel = _Panel()
        c.phone_field = _Field()
        c.connection_dial = _Dial()

        c.on_switch_to_phone(None)

        assert c._token_before_mode_switch == "sess1:hash1"
        # _close_active_session() has now cleared the live token, same as
        # before this fix — only the captured copy is new.
        assert mw._get_wa_token() == ""

    def test_no_prior_token_leaves_the_capture_empty(self):
        mw = _FakeMainWindow(paired=False, token="")
        c = Connect(mw)
        c.qrcode_panel = _Panel()
        c.phone_panel = _Panel()
        c.phone_field = _Field()
        c.connection_dial = _Dial()

        c.on_switch_to_phone(None)

        assert c._token_before_mode_switch == ""


class _Panel:
    def Hide(self):
        pass

    def Show(self):
        pass


class _Dial:
    def Layout(self):
        pass


class _Field:
    def SetFocus(self):
        pass

    def SetInsertionPointEnd(self):
        pass
