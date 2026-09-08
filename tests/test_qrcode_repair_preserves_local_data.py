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

import threading

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
        # _bg_pairing_flow() clears these and then waits up to 90 s on the
        # event, so they have to be the real thing or the flow never returns.
        self._phone_code_event = threading.Event()
        self._phone_code_value = ""


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
        self.abandoned = []
        # Ordered trace of every step that touches the session's browser
        # profile — the close-session/flush/profile-release handshake and the
        # start-session that must come last. Appended to by the stubs below
        # and by the recording api_post in
        # TestReusingTheJustClosedSessionWaitsForItsProfile.
        self.events = []

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

    def _abandon_closed_session(self, token):
        # _close_active_session() marks the session it just closed as
        # abandoned in this account's SessionStore. Recorded rather than
        # swallowed so the tests below can assert it was the live token that
        # went, not a leftover from somewhere else.
        self.abandoned.append(token)

    def _register_abandoned_session(self, token):
        # Reached from _bg_pairing_flow()'s failure paths.
        self.abandoned.append(token)

    def _wait_for_session_flushed(self, token):
        self.events.append(("flush", token))
        return True

    def wait_for_profile_release(self, session_name, timeout=15.0):
        self.events.append(("profile-release", session_name))
        return True


class _Sound:
    def play(self):
        pass


class _Panel:
    def Hide(self):
        pass

    def Show(self):
        pass


class _Dial:
    def Layout(self):
        pass


class _Field:
    def __init__(self, value=""):
        self._value = value

    def GetValue(self):
        return self._value

    def SetFocus(self):
        pass

    def SetInsertionPointEnd(self):
        pass


class _Button:
    def Disable(self):
        pass

    def Enable(self):
        pass

    def SetLabel(self, label):
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
    def test_paired_account_preserves_data_across_the_switch(self):
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
        # The real _close_active_session() ran: the account's live session is
        # the one it abandoned.
        assert mw.abandoned == ["sess1:hash1"]

    def test_never_paired_account_still_wipes(self):
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
        assert mw.abandoned == ["sess1:hash1"]

    def test_no_prior_token_leaves_the_capture_empty(self):
        mw = _FakeMainWindow(paired=False, token="")
        c = Connect(mw)
        c.qrcode_panel = _Panel()
        c.phone_panel = _Panel()
        c.phone_field = _Field()
        c.connection_dial = _Dial()

        c.on_switch_to_phone(None)

        assert c._token_before_mode_switch == ""


class TestModeRoundTripLeavesNothingBogusReusable:
    """A detour through QR mode mints a brand-new session and writes it into
    WA_token. `paired` is still True from the account's previous life and the
    stored number still matches, so carrying that token forward would make
    _can_reuse_existing_session() "resume" a session that never authenticated
    — no crash, but a semantically wrong resume that also skips the wipe."""

    def test_a_session_this_dialog_minted_itself_is_not_carried_forward(self):
        mw = _FakeMainWindow(paired=True, token="sess1:hash1")
        mw.settings["privateinfo"]["WA_phone_number"] = "5511999999999"
        c = Connect(mw)
        c.qrcode_panel = _Panel()
        c.phone_panel = _Panel()
        c.phone_field = _Field()
        c.connection_dial = _Dial()
        c._create_instance = lambda token: None

        c.on_switch_to_qrcode(None)
        # Sanity: the QR switch really did mint a fresh session over WA_token.
        assert mw._get_wa_token() == c._started_new_session_token != ""

        c.on_switch_to_phone(None)

        # These two, in this order, are exactly what _bg_pairing_flow() feeds
        # into _can_reuse_existing_session().
        existing_token = mw._get_wa_token() or c._token_before_mode_switch
        assert existing_token == ""
        assert not c._can_reuse_existing_session(
            mw.settings["privateinfo"], "5511999999999", existing_token
        )

    def test_a_pre_existing_paired_session_is_still_carried_forward(self):
        """The case the PR exists to fix must survive the guard above: no QR
        detour happened, so the token predates anything this dialog minted."""
        mw = _FakeMainWindow(paired=True, token="sess1:hash1")
        mw.settings["privateinfo"]["WA_phone_number"] = "5511999999999"
        c = Connect(mw)
        c.qrcode_panel = _Panel()
        c.phone_panel = _Panel()
        c.phone_field = _Field()
        c.connection_dial = _Dial()

        c.on_switch_to_phone(None)

        assert c._can_reuse_existing_session(
            mw.settings["privateinfo"], "5511999999999",
            mw._get_wa_token() or c._token_before_mode_switch,
        )

    def test_switching_back_to_qrcode_drops_the_capture(self):
        mw = _FakeMainWindow(paired=True, token="sess1:hash1")
        c = Connect(mw)
        c.qrcode_panel = _Panel()
        c.phone_panel = _Panel()
        c.phone_field = _Field()
        c.connection_dial = _Dial()
        c._create_instance = lambda token: None

        c.on_switch_to_phone(None)
        assert c._token_before_mode_switch == "sess1:hash1"

        c.on_switch_to_qrcode(None)

        assert c._token_before_mode_switch == ""


class TestTheCaptureIsSpentByOnePairingAttempt:
    def test_on_continue_consumes_it_and_clears_it(self, monkeypatch):
        """It must not outlive the attempt that reads it: a failed attempt
        abandons that session and clears WA_token, so a later Continue would
        otherwise resume a token that is already dead."""
        # on_continue() rebinds wx.GetApp on the module itself; going through
        # monkeypatch keeps that out of the rest of the suite.
        monkeypatch.setattr(connect_module.wx, "GetApp", lambda: None)

        mw = _FakeMainWindow(paired=True, token="sess1:hash1")
        mw.settings["privateinfo"]["WA_phone_number"] = "5511999999999"
        c = Connect(mw)
        c.qrcode_panel = _Panel()
        c.phone_panel = _Panel()
        c.phone_field = _Field("5511999999999")
        c.connection_dial = _Dial()
        c.continue_btn = _Button()

        seen = []
        reached = threading.Event()

        def _fake_reuse(privateinfo, phone_number, existing_token):
            seen.append(existing_token)
            reached.set()
            # Abort the rest of _bg_pairing_flow() (start-session, the 90 s
            # phoneCode wait) — its own except clause handles this.
            raise RuntimeError("stop the pairing flow here")

        c._can_reuse_existing_session = _fake_reuse

        c.on_switch_to_phone(None)
        c.on_continue(None)

        assert reached.wait(5)
        assert seen == ["sess1:hash1"]
        assert c._token_before_mode_switch == ""


class TestReusingTheJustClosedSessionWaitsForItsProfile:
    """The reuse branch this PR opened up re-starts the SAME session name that
    on_switch_to_phone() just closed, so it lands on the same userDataDir.

    _bg_pairing_flow()'s close/flush/profile-release handshake keys on
    _old_token = main_window.token, and _close_active_session() cleared that on
    the way into phone mode — so the handshake would be skipped entirely and
    /start-session would race the Chrome still shutting down on that profile.
    Puppeteer answers "The browser is already running for <dir>" and the
    recovery kills Chrome by userDataDir, mid-LevelDB-flush, on the only copy
    of the WhatsApp login (see CLAUDE.md and core/profile_recovery.py). The
    same reuse reached WITHOUT a mode switch has always taken that wait; this
    is that wait, on the second route to the same place.
    """

    def test_the_profile_is_released_before_start_session(self, monkeypatch):
        monkeypatch.setattr(connect_module.wx, "GetApp", lambda: None)

        mw = _FakeMainWindow(paired=True, token="sess1:hash1")
        mw.settings["privateinfo"]["WA_phone_number"] = "5511999999999"

        started = threading.Event()

        def _recording_post(url, *a, **kw):
            if "/close-session" in url:
                mw.events.append(("close-session", url))
            elif "/start-session" in url:
                mw.events.append(("start-session", url))
                started.set()
                # Inline phoneCode: unblocks the 90 s _phone_code_event wait
                # so the flow finishes instead of holding the test open.
                return _Response(201, {"phoneCode": "ABCD1234"})
            return _Response(201, {"token": "hash123"})

        monkeypatch.setattr(connect_module, "api_post", _recording_post)

        c = Connect(mw)
        c.qrcode_panel = _Panel()
        c.phone_panel = _Panel()
        c.phone_field = _Field("5511999999999")
        c.connection_dial = _Dial()
        c.continue_btn = _Button()

        c.on_switch_to_phone(None)
        c.on_continue(None)

        assert started.wait(10)
        # The reuse really happened — otherwise a fresh session name would
        # have been minted and there would be no profile collision to avoid.
        assert mw.token == "sess1:hash1"

        steps = [name for name, _ in mw.events]
        assert ("profile-release", "sess1") in mw.events
        assert ("flush", "sess1:hash1") in mw.events
        assert steps.index("profile-release") < steps.index("start-session")
        assert steps.index("flush") < steps.index("profile-release")
