"""The 404 that means "the probe never answered", not "WhatsApp is gone".

`api_patches/src/middleware/statusConnection.ts` bounds its `isConnected()`
call at 8 s, because wppconnect 2.3.2 makes that call await `waitForPageLoad()`
on puppeteer's 30 s default — longer than main.py's own send timeouts. An
unanswered probe answers 404 `{"status": "Disconnected"}` so the send stays
queued instead of being dropped as ambiguous.

That was reasoned about as a *send* problem, and it is not only one: the same
middleware fronts `list-chats`, whose callers (`get_remote_chats()` from
`start_sync()`, the post-sync settling pass, `_probe_chats_and_start_sync()`)
are all background work. Undifferentiated, that 404 reached
`_check_wa_connection_closed()`, which flipped the connection to down and
re-probed — so an ordinary WhatsApp Web reload overlapping a sync round played
the offline sound and spoke "modo offline ativado automaticamente", then
"conexão restaurada" a few seconds later, over whatever the user was reading.
On 2.3.1 the same reload *threw* inside the page and left as a 503, which this
classifier ignores, so it passed in silence — the regression is the noise, not
the reload. It is the outcome `_OFFLINE_PROBE_STRIKES` was added for over the
session probe after a measured 28 s reload did exactly this.

So the middleware now names it (`reason: "probe_timeout"`) and the classifier
splits the two verdicts: still "this call was not delivered" for every caller,
never "WhatsApp is offline". Nothing is lost by staying quiet —
`check-connection-session` is deliberately not behind this middleware, and it
bounds its own `isConnected()` at 8 s (under the 10 s main.py gives that
request), answering `status: false` when the probe goes unanswered. That is
what makes the sentence above true: a page that really is stuck still lands on
`check_whatsapp_reachable()`'s consecutive-strike tally on the next
health-check tick. Unbounded, the client timed out first and the timeout raised
into an `except` that counts no strike at all — the two halves are pinned
together in tests/test_connection_probe_budget.py.

MainWindow is a wx.Frame, so the methods are bound onto a stub carrying only
the attributes they touch — the same pattern the other main.py tests use.
"""

import json

import pytest
import wx

import main
from main import MainWindow


# The two bodies statusConnection.ts really writes. `reason` is absent from the
# genuine one because JSON.stringify() drops the undefined argument.
PROBE_TIMEOUT_BODY = {
    "response": None,
    "status": "Disconnected",
    "reason": "probe_timeout",
    "message": "A sessão do WhatsApp não está ativa.",
}
DISCONNECTED_BODY = {
    "response": None,
    "status": "Disconnected",
    "message": "A sessão do WhatsApp não está ativa.",
}


class _Response:
    def __init__(self, payload, status_code=404):
        self._payload = payload
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _Stub:
    """The three real methods a 404 travels through, and nothing else."""

    _check_wa_connection_closed = MainWindow._check_wa_connection_closed
    _probe_chats_and_start_sync = MainWindow._probe_chats_and_start_sync
    send_text_message = MainWindow.send_text_message

    def __init__(self, **kwargs):
        self.wpp_server = "http://127.0.0.1"
        self.wpp_port = 6300
        self.token = "tok"
        self.messages_set_completed = False
        self.sync_thread = None
        self._sync_completed = False
        self.sync_starts = 0
        self.status_calls = []
        self.connection_flags = []
        self.http_check_calls = 0
        for key, value in kwargs.items():
            setattr(self, key, value)

    # ── collaborators ────────────────────────────────────────────────
    def _set_wa_connected(self, connected, reason="", **kwargs):
        self.connection_flags.append(bool(connected))

    def check_wa_connection_http(self):
        self.http_check_calls += 1

    def _try_start_sync_thread(self):
        self.sync_starts += 1
        return True

    def _set_status(self, text):
        self.status_calls.append(text)

    def _resolve_jid_for_send(self, jid):
        return jid

    def _legacy_phone_for_send(self, jid):
        return ""

    def _build_link_preview_options(self, link_preview):
        return {}


@pytest.fixture(autouse=True)
def _synchronous_call_after(monkeypatch):
    """Run wx.CallAfter(fn, *args) immediately instead of queuing it onto a
    (nonexistent, in these tests) wx event loop."""
    monkeypatch.setattr(wx, "CallAfter", lambda fn, *a, **kw: fn(*a, **kw))


def _answer(monkeypatch, response):
    """Make every api_post() in the path under test return `response`."""
    calls = []

    def _fake_api_post(url, **kwargs):
        calls.append((url, kwargs))
        return response

    monkeypatch.setattr(main, "api_post", _fake_api_post)
    return calls


class TestTheClassifierSplitsTheTwo404s:
    def test_an_unanswered_probe_is_still_a_call_that_did_not_land(self):
        """True is what makes every caller leave its retry ladder and every
        send report {"disconnected": True} — the message stays queued rather
        than being dropped as ambiguous. That half is unchanged."""
        stub = _Stub()

        assert stub._check_wa_connection_closed(_Response(PROBE_TIMEOUT_BODY)) is True

    def test_an_unanswered_probe_does_not_touch_the_connection_state(self):
        """The regression itself: no offline flip, so no sound, no "modo
        offline", and no re-probe to answer it with "conexão restaurada"."""
        stub = _Stub()

        stub._check_wa_connection_closed(_Response(PROBE_TIMEOUT_BODY))

        assert stub.connection_flags == []
        assert stub.http_check_calls == 0

    def test_a_real_disconnection_still_flips_the_state(self):
        """`connected === false` is the page itself answering that the session
        is down — the single most reliable offline signal the API gives us, and
        it must keep its meaning."""
        stub = _Stub()

        assert stub._check_wa_connection_closed(_Response(DISCONNECTED_BODY)) is True
        assert stub.connection_flags == [False]
        assert stub.http_check_calls == 1

    def test_an_unrecognised_reason_is_treated_as_a_real_disconnection(self):
        """If the middleware ever renames or drops the field, the classifier
        degrades to the old behaviour — a spurious announcement — never to a
        missed outage."""
        stub = _Stub()
        body = dict(PROBE_TIMEOUT_BODY, reason="something_else")

        assert stub._check_wa_connection_closed(_Response(body)) is True
        assert stub.connection_flags == [False]


class TestTheSendConsequence:
    def test_the_message_stays_queued_without_an_offline_announcement(
        self, monkeypatch
    ):
        """MessageQueue reads `disconnected` to break its 3 s retry loop and
        keep the message. It used to park the whole queue behind it too,
        because _wa_connected had just been set False; with the flag untouched
        it simply picks the message up on the next cycle — right for a send
        that provably never reached a controller."""
        stub = _Stub()
        _answer(monkeypatch, _Response(PROBE_TIMEOUT_BODY))

        result = stub.send_text_message("5511999999999@s.whatsapp.net", "oi")

        assert result["ok"] is False
        assert result["disconnected"] is True
        assert result["retry"] is False
        assert stub.connection_flags == []


class TestTheBackgroundSyncConsequence:
    def test_the_chat_probe_ends_quietly_instead_of_going_offline(
        self, monkeypatch
    ):
        """The path nobody asked for: list-chats carries this middleware too.
        The poll still ends (True) and no sync is started against a session
        that could not answer, but the user hears nothing about it."""
        stub = _Stub()
        _answer(monkeypatch, _Response(PROBE_TIMEOUT_BODY))

        assert stub._probe_chats_and_start_sync() is True
        assert stub.sync_starts == 0
        assert stub.messages_set_completed is False
        assert stub.connection_flags == []
        assert stub.http_check_calls == 0

    def test_a_real_disconnection_still_hands_off_to_the_recovery_path(
        self, monkeypatch
    ):
        """Same probe, the other 404 — the contrast is the whole point."""
        stub = _Stub()
        _answer(monkeypatch, _Response(DISCONNECTED_BODY))

        assert stub._probe_chats_and_start_sync() is True
        assert stub.connection_flags == [False]
        assert stub.http_check_calls == 1
