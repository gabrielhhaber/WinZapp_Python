"""Tests for surfacing *why* a pairing-code request failed.

WhatsApp can refuse to issue a link-by-code — CompanionHelloError is the one
seen in practice — while the session itself stays healthy and goes on rotating
auth codes. Before this the refusal never left the Node process: host.layer.js
logged it, the browser quietly retried, and the Python side simply waited out
its full 90-second phoneCode timeout and reported "no pairing code received",
which told the person trying to pair nothing at all.

The chain under test:

    host.layer.js checkQrCode() catch  ->  options.catchLinkCodeError
      ->  createSessionUtil.ts exportPhoneCodeError  ->  Socket.IO
      ->  WebSocketClient.on_wpp_phone_code_error    (records it)
      ->  Connect._on_pairing_code_error(reason)     (shows it)

WebSocketClient talks to a real Socket.IO client and Connect is wx UI, so both
methods are exercised as plain functions against small stubs carrying only the
attributes they actually touch — the idiom the rest of this suite uses.
"""

import pytest

from core.websocket_client import WebSocketClient
from ui.dialogs.connect import Connect


SESSION = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
OTHER_SESSION = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


class _WsStub:
    """Stand-in for WebSocketClient for the phoneCodeError handler."""

    on_wpp_phone_code_error = WebSocketClient.on_wpp_phone_code_error
    _belongs_to_this_session = WebSocketClient._belongs_to_this_session

    def __init__(self, instance_name=SESSION):
        self.instance_name = instance_name
        self._phone_code_error = ""
        self._phone_code_value = ""


class TestOnWppPhoneCodeError:
    def test_records_name_and_message(self):
        ws = _WsStub()
        ws.on_wpp_phone_code_error(
            {"session": SESSION, "name": "SomeError", "message": "it broke"}
        )
        assert ws._phone_code_error == "SomeError: it broke"

    def test_name_only_is_not_doubled_up(self):
        """WhatsApp's errors are routinely name-only — the message repeats the
        class name. Rendering "CompanionHelloError: CompanionHelloError" at a
        user (or a screen reader) is noise."""
        ws = _WsStub()
        ws.on_wpp_phone_code_error(
            {
                "session": SESSION,
                "name": "CompanionHelloError",
                "message": "CompanionHelloError",
            }
        )
        assert ws._phone_code_error == "CompanionHelloError"

    def test_empty_message_falls_back_to_the_name(self):
        ws = _WsStub()
        ws.on_wpp_phone_code_error({"session": SESSION, "name": "CompanionHelloError"})
        assert ws._phone_code_error == "CompanionHelloError"

    def test_missing_name_still_produces_something(self):
        ws = _WsStub()
        ws.on_wpp_phone_code_error({"session": SESSION, "message": "no name given"})
        assert ws._phone_code_error == "Error: no name given"

    def test_another_sessions_failure_is_ignored(self):
        """A stale session left over from a previous pairing attempt must not
        put its error in front of the attempt the user is actually looking
        at — the same guard every other event handler uses."""
        ws = _WsStub()
        ws.on_wpp_phone_code_error(
            {"session": OTHER_SESSION, "name": "CompanionHelloError"}
        )
        assert ws._phone_code_error == ""

    def test_does_not_unblock_the_phone_code_wait(self):
        """The core design property: host.layer.js retries on its own backoff
        schedule, so a failure inside the 90s window may still be followed by a
        code that works. Recording the error must not cut that wait short — if
        it did, one transient refusal would abort a pairing that was about to
        succeed."""
        ws = _WsStub()
        import threading

        ws._phone_code_event = threading.Event()
        ws.on_wpp_phone_code_error({"session": SESSION, "name": "CompanionHelloError"})
        assert not ws._phone_code_event.is_set()

    def test_a_malformed_payload_is_survivable(self):
        ws = _WsStub()
        ws.on_wpp_phone_code_error("not a dict")
        ws.on_wpp_phone_code_error(None)
        assert ws._phone_code_error == ""


class _I18nStub:
    def __init__(self, table):
        self._table = table

    def t(self, key):
        return self._table.get(key, key)


class _MainWindowStub:
    app_name = "WinZapp"


class _ButtonStub:
    def __init__(self):
        self.enabled = False
        self.label = ""

    def Enable(self):
        self.enabled = True

    def SetLabel(self, label):
        self.label = label

    def __bool__(self):
        return True


class _ConnectStub:
    """Stand-in for Connect for the pairing-code error dialog."""

    _on_pairing_code_error = Connect._on_pairing_code_error

    def __init__(self):
        self.continue_btn = _ButtonStub()
        self.main_window = _MainWindowStub()
        self.i18n = _I18nStub(
            {
                "continue": "Continuar",
                "connection_error": "Erro de conexao",
                "no_pairing_code_received": (
                    "Nao foi possivel conectar ao {app_name}. Nenhum codigo "
                    "de pareamento foi recebido."
                ),
                "no_pairing_code_received_reason": (
                    "Nao foi possivel conectar ao {app_name}. O WhatsApp "
                    "recusou o pedido de codigo de pareamento: {reason}."
                ),
            }
        )

    def __bool__(self):
        return True


@pytest.fixture
def captured_messagebox(monkeypatch):
    seen = {}

    def _fake_messagebox(message, caption="", style=0, parent=None):
        seen["message"] = message
        seen["caption"] = caption
        return 0

    import ui.dialogs.connect as connect_module

    monkeypatch.setattr(connect_module.wx, "MessageBox", _fake_messagebox)
    return seen


class TestPairingCodeErrorDialog:
    def test_a_known_reason_is_shown_to_the_user(self, captured_messagebox):
        dlg = _ConnectStub()
        dlg._on_pairing_code_error("CompanionHelloError")
        assert "CompanionHelloError" in captured_messagebox["message"]
        assert "recusou" in captured_messagebox["message"]

    def test_no_reason_falls_back_to_the_generic_message(self, captured_messagebox):
        """The reason is best-effort: an older WPPConnect that never emits
        phoneCodeError, or a code that simply never arrived, must still get the
        original wording rather than an empty placeholder."""
        dlg = _ConnectStub()
        dlg._on_pairing_code_error("")
        assert "Nenhum codigo" in captured_messagebox["message"]
        assert "{reason}" not in captured_messagebox["message"]

    def test_reason_defaults_to_empty_when_omitted(self, captured_messagebox):
        """Called with no argument at all — the signature must stay backwards
        compatible, since this is also reached from paths that know no
        reason."""
        dlg = _ConnectStub()
        dlg._on_pairing_code_error()
        assert "Nenhum codigo" in captured_messagebox["message"]

    def test_the_continue_button_is_restored_either_way(self, captured_messagebox):
        for reason in ("CompanionHelloError", ""):
            dlg = _ConnectStub()
            dlg._on_pairing_code_error(reason)
            assert dlg.continue_btn.enabled is True
            assert dlg.continue_btn.label == "Continuar"


class TestLocalesCarryTheReasonKey:
    """CLAUDE.md's rule: a user-facing string exists in all five locales or it
    renders as the raw key name. Placeholders must survive translation too — a
    dropped {reason} would silently hide the very detail this change adds."""

    def test_every_locale_has_both_placeholders(self):
        import glob
        import json
        import os

        files = [
            f
            for f in glob.glob(os.path.join("client", "languages", "*.json"))
            if os.path.basename(f) != "language_map.json"
        ]
        assert len(files) == 5

        for path in files:
            with open(path, encoding="utf-8") as fh:
                table = json.load(fh)
            text = table.get("no_pairing_code_received_reason")
            assert text, f"{path} is missing no_pairing_code_received_reason"
            assert "{app_name}" in text, path
            assert "{reason}" in text, path


class TestBackoffMetadataIsCarried:
    """host.layer.js v5 attaches which attempt this was and when the next one
    is due. Those ride along to the log — not to the dialog, where the reason
    is the only part a user can act on."""

    def test_attempt_and_retry_are_logged(self, caplog):
        ws = _WsStub()
        with caplog.at_level("WARNING"):
            ws.on_wpp_phone_code_error(
                {
                    "session": SESSION,
                    "name": "CompanionHelloError",
                    "message": "CompanionHelloError",
                    "attempt": 4,
                    "retryInSeconds": 160,
                }
            )
        assert "attempt 4" in caplog.text
        assert "160s" in caplog.text

    def test_the_stored_reason_stays_clean(self):
        """What reaches the user must not grow a retry schedule on the end."""
        ws = _WsStub()
        ws.on_wpp_phone_code_error(
            {
                "session": SESSION,
                "name": "CompanionHelloError",
                "message": "CompanionHelloError",
                "attempt": 4,
                "retryInSeconds": 160,
            }
        )
        assert ws._phone_code_error == "CompanionHelloError"

    def test_it_still_works_without_the_metadata(self, caplog):
        """An older Node side that never sends these fields must still log."""
        ws = _WsStub()
        with caplog.at_level("WARNING"):
            ws.on_wpp_phone_code_error({"session": SESSION, "name": "SomeError"})
        assert "SomeError" in caplog.text
        assert ws._phone_code_error == "SomeError"


class TestFullErrorDetailIsCaptured:
    """CompanionHelloError is thrown by WhatsApp Web's own bundle — no code we
    ship contains that string. Its class name was all that survived to the log,
    which is why three separate hypotheses about its cause could be neither
    confirmed nor ruled out. The page-context stack and every other own
    property of the error are the only remaining places an answer could be."""

    def test_stack_is_logged_when_present(self, caplog):
        ws = _WsStub()
        with caplog.at_level("WARNING"):
            ws.on_wpp_phone_code_error(
                {
                    "session": SESSION,
                    "name": "CompanionHelloError",
                    "stack": "Error\n    at n (https://web.whatsapp.com/x.js:1:2)",
                }
            )
        assert "web.whatsapp.com/x.js" in caplog.text

    def test_details_are_logged_when_present(self, caplog):
        ws = _WsStub()
        with caplog.at_level("WARNING"):
            ws.on_wpp_phone_code_error(
                {
                    "session": SESSION,
                    "name": "CompanionHelloError",
                    "details": {"code": "429", "reason": "rate_limited"},
                }
            )
        assert "429" in caplog.text
        assert "rate_limited" in caplog.text

    def test_absent_diagnostics_are_not_logged_as_empty(self, caplog):
        ws = _WsStub()
        with caplog.at_level("WARNING"):
            ws.on_wpp_phone_code_error({"session": SESSION, "name": "SomeError"})
        assert "failure stack" not in caplog.text
        assert "failure details" not in caplog.text

    def test_diagnostics_never_reach_the_user_facing_reason(self):
        ws = _WsStub()
        ws.on_wpp_phone_code_error(
            {
                "session": SESSION,
                "name": "CompanionHelloError",
                "stack": "Error\n    at n (x.js:1:2)",
                "details": {"code": "429"},
            }
        )
        assert ws._phone_code_error == "CompanionHelloError"


class TestPatchSerializesTheWholeError:
    """The in-page serializer is what decides how much survives the CDP
    boundary. It hand-picked three fields; anything else the error carried was
    dropped before it could ever be read."""

    def test_all_own_properties_are_collected(self):
        from core.wppconnect_host_layer_patch import PATCHED_LOGIN_BY_CODE

        assert "Object.getOwnPropertyNames(Object(error))" in PATCHED_LOGIN_BY_CODE
        assert "details: details," in PATCHED_LOGIN_BY_CODE

    def test_message_fallbacks_use_or_not_nullish_coalescing(self):
        """`??` only falls through on null/undefined, and an Error with an
        empty message is not nullish — so the reason/text fallbacks could never
        fire, and an empty message was indistinguishable from a message equal
        to the class name."""
        from core.wppconnect_host_layer_patch import PATCHED_LOGIN_BY_CODE

        assert (
            "String(error?.message || error?.reason || error?.text || error)"
            in PATCHED_LOGIN_BY_CODE
        )
        assert "error?.message ??" not in PATCHED_LOGIN_BY_CODE

    def test_serialization_failure_cannot_break_the_report(self):
        """A getter that throws, or a circular value, must not turn a
        diagnostic into a second error that loses the first one."""
        from core.wppconnect_host_layer_patch import PATCHED_LOGIN_BY_CODE

        assert "'[unserializable]'" in PATCHED_LOGIN_BY_CODE
        assert PATCHED_LOGIN_BY_CODE.count("catch (e)") >= 2

    def test_the_hook_forwards_stack_and_details(self):
        from core.wppconnect_host_layer_patch import PATCHED_CHECK_QR_CODE

        assert "stack: String(error?.stack || '')," in PATCHED_CHECK_QR_CODE
        assert "details: error?.winzappDetails || {}," in PATCHED_CHECK_QR_CODE


class _FakeSound:
    def __init__(self):
        self.plays = 0

    def play(self):
        self.plays += 1


class _FakeSpeakOutput:
    def __init__(self):
        self.spoken = []

    def output(self, text):
        self.spoken.append(text)


class _AnnouncingMainWindowStub:
    app_name = "WinZapp"

    def __init__(self, pairing_dialog_active=True):
        self._pairing_dialog_active = pairing_dialog_active
        self.error_sound = _FakeSound()
        self.speak_output = _FakeSpeakOutput()

    def _is_pairing_dialog_active(self):
        return self._pairing_dialog_active


class _AnnouncingWsStub(_WsStub):
    """_WsStub plus the two attributes the announcement itself touches."""

    _announce_pairing_code_expired = WebSocketClient._announce_pairing_code_expired

    def __init__(self, pairing_dialog_active=True, instance_name=SESSION):
        super().__init__(instance_name=instance_name)
        self.main_window = _AnnouncingMainWindowStub(pairing_dialog_active)
        self.i18n = _I18nStub(
            {
                "pairing_code_expired": "O codigo expirou.",
                "pairing_code_rate_limited": (
                    "O WhatsApp esta limitando os pedidos. Aguarde alguns "
                    "minutos e tente novamente no {app_name}."
                ),
            }
        )


@pytest.fixture
def synchronous_call_after(monkeypatch):
    import core.websocket_client as ws_module

    monkeypatch.setattr(
        ws_module.wx, "CallAfter", lambda fn, *a, **kw: fn(*a, **kw)
    )


class TestExpiredPairingCodeIsAnnounced:
    """The blocker this class was written for: wa-js stops refreshing after
    five re-mints, so an attended dialog gets ~six codes over ~19.5 minutes and
    then `LinkCodeExpired`. Recorded in `_phone_code_error` it reaches nobody —
    the only reader is connect.py's 90 s phoneCode wait, which returned long
    before the dialog even opened. The dialog therefore kept showing the last,
    now-dead code; the user typed it, WhatsApp refused, and the retry minted a
    fresh session and another six codes.

    `LinkCodeRefreshFailed` is the same ending arrived at from the other side —
    a refresh that fails rather than a refresh budget that runs out — and it is
    announced for the same reason. The two names exist only because the *first*
    mint fails through the same wa-js event, and that case is already handled
    elsewhere.
    """

    def test_it_speaks_and_plays_the_error_sound(self, synchronous_call_after):
        ws = _AnnouncingWsStub(pairing_dialog_active=True)

        ws.on_wpp_phone_code_error({"session": SESSION, "name": "LinkCodeExpired"})

        assert ws.main_window.speak_output.spoken == ["O codigo expirou."]
        assert ws.main_window.error_sound.plays == 1

    def test_it_says_nothing_with_no_dialog_on_screen(self, synchronous_call_after):
        """Same reasoning as on_qrcode_update()'s refresh branches: an event
        for a session nobody is watching must not talk over whatever the user
        is actually doing."""
        ws = _AnnouncingWsStub(pairing_dialog_active=False)

        ws.on_wpp_phone_code_error({"session": SESSION, "name": "LinkCodeExpired"})

        assert ws.main_window.speak_output.spoken == []
        assert ws.main_window.error_sound.plays == 0

    def test_another_sessions_expiry_is_ignored(self, synchronous_call_after):
        ws = _AnnouncingWsStub(pairing_dialog_active=True)

        ws.on_wpp_phone_code_error(
            {"session": OTHER_SESSION, "name": "LinkCodeExpired"}
        )

        assert ws.main_window.speak_output.spoken == []

    def test_a_failed_refresh_is_announced_too(self, synchronous_call_after):
        """A refresh that fails ends the stream just as surely, and sooner.

        wa-js clears its 195 s timer before minting and re-arms it only from a
        successful mint, so a failed refresh leaves no timer, no code and a
        rejection its own caller swallows — `conn.link_code_expired` cannot
        fire afterwards either. Left as a log line, this stranded the user at
        t+195 s with a code wa-js had already discarded, where the announced
        case reaches them at ~19.5 minutes: the quiet failure was the earlier
        one. The host.layer.js hook names it apart precisely so it can be
        announced without dragging the first mint's failure in with it."""
        ws = _AnnouncingWsStub(pairing_dialog_active=True)

        ws.on_wpp_phone_code_error(
            {
                "session": SESSION,
                "name": "LinkCodeRefreshFailed",
                "message": "CompanionHelloError",
            }
        )

        assert ws.main_window.speak_output.spoken == ["O codigo expirou."]
        assert ws.main_window.error_sound.plays == 1
        assert ws._phone_code_error == "LinkCodeRefreshFailed: CompanionHelloError"

    def test_a_quota_refusal_says_to_wait_instead_of_to_retry(
        self, synchronous_call_after
    ):
        """"Cancel and try again" is the one piece of advice that must not be
        given here. A refresh refused on quota grounds is reachable — every
        manual retry mints a fresh session and up to six codes — and sending
        the user round that loop immediately is what keeps the quota spent.
        connect.py's _on_pairing_code_error() reached the same conclusion for
        the first mint; this is the channel that carries every later one."""
        ws = _AnnouncingWsStub(pairing_dialog_active=True)

        ws.on_wpp_phone_code_error(
            {
                "session": SESSION,
                "name": "LinkCodeRefreshFailed",
                "message": "rate-overlimit",
                "rateLimited": True,
            }
        )

        assert ws.main_window.speak_output.spoken == [
            "O WhatsApp esta limitando os pedidos. Aguarde alguns minutos e "
            "tente novamente no WinZapp."
        ]
        assert ws.main_window.error_sound.plays == 1

    def test_an_expiry_after_a_quota_refusal_is_read_at_the_call_site(
        self, synchronous_call_after
    ):
        """`_phone_code_rate_limited` is a single slot overwritten by every
        event, and CallAfter runs on the wx thread whenever it gets there. Read
        inside the announcement instead of at the call site, a later non-quota
        expiry would be announced with the previous event's verdict."""
        ws = _AnnouncingWsStub(pairing_dialog_active=True)

        ws.on_wpp_phone_code_error(
            {
                "session": SESSION,
                "name": "LinkCodeRefreshFailed",
                "rateLimited": True,
            }
        )
        ws.on_wpp_phone_code_error({"session": SESSION, "name": "LinkCodeExpired"})

        assert ws.main_window.speak_output.spoken[-1] == "O codigo expirou."

    def test_a_first_mint_failure_stays_in_the_log(self, synchronous_call_after):
        """`LinkCodeError` is what the hook reports when no code ever reached
        the screen — the initial mint failing. loginByCode()'s retry ladder
        owns that one and connect.py's 90 s wait reports it, so announcing
        "your code expired" during the initial wait would name a code that
        never existed."""
        ws = _AnnouncingWsStub(pairing_dialog_active=True)

        ws.on_wpp_phone_code_error(
            {"session": SESSION, "name": "LinkCodeError", "message": "timeout"}
        )

        assert ws.main_window.speak_output.spoken == []
        assert ws._phone_code_error == "LinkCodeError: timeout"

    def test_a_failed_refresh_says_nothing_with_no_dialog_on_screen(
        self, synchronous_call_after
    ):
        ws = _AnnouncingWsStub(pairing_dialog_active=False)

        ws.on_wpp_phone_code_error(
            {"session": SESSION, "name": "LinkCodeRefreshFailed"}
        )

        assert ws.main_window.speak_output.spoken == []
        assert ws.main_window.error_sound.plays == 0

    def test_it_still_records_the_reason(self, synchronous_call_after):
        """Announcing is additional, not instead of: a retry that goes on to
        time out must still be able to report why."""
        ws = _AnnouncingWsStub(pairing_dialog_active=True)

        ws.on_wpp_phone_code_error({"session": SESSION, "name": "LinkCodeExpired"})

        assert ws._phone_code_error == "LinkCodeExpired"

    def test_a_destroyed_dialog_cannot_crash_the_handler(
        self, synchronous_call_after
    ):
        """CallAfter runs later than the event, so the dialog can be gone by
        then — a wx wrapper whose C++ object died raises RuntimeError on any
        call, and this handler must survive it."""
        ws = _AnnouncingWsStub(pairing_dialog_active=True)

        def _boom():
            raise RuntimeError("wrapped C/C++ object has been deleted")

        ws.main_window._is_pairing_dialog_active = _boom
        ws.on_wpp_phone_code_error({"session": SESSION, "name": "LinkCodeExpired"})

        assert ws.main_window.speak_output.spoken == []
