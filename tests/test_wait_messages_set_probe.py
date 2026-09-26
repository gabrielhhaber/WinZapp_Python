"""Tests for the list-chats probe behind wait_messages_set()'s fallback poll.

The bug family: WinZapp starts, WhatsApp is not actually connected, and the
app spends a minute talking to a session that cannot answer and then starts a
full sync against it anyway.

`_probe_chats_and_start_sync()` is the last of the three list-chats consumers
to learn about the 404 `{"status": "Disconnected"}` that
`api_patches/src/middleware/statusConnection.ts` returns for a session with no
live client — `get_remote_chats()` and `_run_sync()` already bail out at the
first one. Until it did, that answer fell through to "probe failed", so the
poll burned all 13 attempts, 5 s apart (seen verbatim in a user's log as
`POST /list-chats -> 404` every five seconds), and then force-started a sync
that could not succeed.

The other half of the fix is what must *not* change: a read timeout or a
dropped connection is a WPPConnect still warming up, not a dead session, and
has to keep the poll running — treating those like a Disconnected answer would
make a brief hiccup skip the sync entirely.

MainWindow is a wx.Frame, so the method is bound onto a stub carrying only the
attributes it touches — the same pattern the other main.py tests use.
"""

import json

import pytest
import requests
import wx

import main
from main import MainWindow
from tests.god_modules import patch_main_global


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _Stub:
    """Minimum surface _probe_chats_and_start_sync() actually reads."""

    _probe_chats_and_start_sync = MainWindow._probe_chats_and_start_sync
    # The real classifier, deliberately: the whole point of the fix is that
    # this probe recognises "Disconnected" the same way every other caller
    # does, so stubbing it out here would test nothing.
    _check_wa_connection_closed = MainWindow._check_wa_connection_closed

    def __init__(self, **kwargs):
        self.wpp_server = "http://127.0.0.1"
        self.wpp_port = 6300
        self.token = "tok"
        # A plain attribute here; on MainWindow it is a DB-backed property.
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
    def _try_start_sync_thread(self):
        self.sync_starts += 1
        return True

    def _set_status(self, text):
        self.status_calls.append(text)

    def _set_wa_connected(self, connected, reason="", **kwargs):
        self.connection_flags.append(bool(connected))

    def check_wa_connection_http(self):
        self.http_check_calls += 1


@pytest.fixture(autouse=True)
def _synchronous_call_after(monkeypatch):
    """Run wx.CallAfter(fn, *args) immediately instead of queuing it onto a
    (nonexistent, in these tests) wx event loop."""
    monkeypatch.setattr(wx, "CallAfter", lambda fn, *a, **kw: fn(*a, **kw))


def _answer(monkeypatch, response=None, exc=None):
    """Make the probe's single api_post() call return `response` or raise `exc`,
    and record how it was called."""
    calls = []

    def _fake_api_post(url, **kwargs):
        calls.append((url, kwargs))
        if exc is not None:
            raise exc
        return response

    patch_main_global(monkeypatch, "api_post", _fake_api_post)
    return calls


class TestProbeAnswered:
    def test_chat_list_starts_the_sync(self, monkeypatch):
        stub = _Stub()
        calls = _answer(monkeypatch, _Response([{"id": "a@c.us"}]))

        assert stub._probe_chats_and_start_sync() is True
        assert stub.messages_set_completed is True
        assert stub.sync_starts == 1
        # The metadata prefetch must stay off, or a "failed" probe leaves a
        # serial per-group loop running inside the page (see get_remote_chats).
        assert calls[0][1]["json"] == {"ignoreGroupMetadata": True}

    def test_already_syncing_makes_no_request(self, monkeypatch):
        stub = _Stub(messages_set_completed=True)
        calls = _answer(monkeypatch, _Response([]))

        assert stub._probe_chats_and_start_sync() is True
        assert calls == []
        assert stub.sync_starts == 0

    def test_finished_sync_clears_the_preparing_status(self, monkeypatch):
        stub = _Stub(_sync_completed=True)
        _answer(monkeypatch, _Response([]))

        assert stub._probe_chats_and_start_sync() is True
        assert stub.status_calls == [""]


class TestDisconnectedEndsThePoll:
    def test_disconnected_404_stops_probing_without_starting_a_sync(self, monkeypatch):
        stub = _Stub()
        _answer(monkeypatch, _Response({"status": "Disconnected"}, status_code=404))

        # True means "the poll is over", not "a sync started" — nothing was
        # started, and messages_set_completed stays False so no later code
        # believes WhatsApp Web's chat store was ever loaded.
        assert stub._probe_chats_and_start_sync() is True
        assert stub.sync_starts == 0
        assert stub.messages_set_completed is False

    def test_disconnected_404_flags_the_connection_down(self, monkeypatch):
        """The recovery path the probe hands off to: the connection is marked
        down and an HTTP session check is scheduled, so the health checker's
        trigger_sync_if_needed() can start the sync once WhatsApp is back."""
        stub = _Stub()
        _answer(monkeypatch, _Response({"status": "Disconnected"}, status_code=404))

        stub._probe_chats_and_start_sync()
        assert stub.connection_flags == [False]
        assert stub.http_check_calls == 1

    def test_target_close_error_stops_probing(self, monkeypatch):
        """The other shape _check_wa_connection_closed() calls disconnected —
        a closed Puppeteer target is as dead as an offline session."""
        stub = _Stub()
        _answer(monkeypatch, _Response(
            {"error": {"name": "TargetCloseError"}}, status_code=500))

        assert stub._probe_chats_and_start_sync() is True
        assert stub.sync_starts == 0


class TestTransientFailureKeepsProbing:
    def test_read_timeout(self, monkeypatch):
        stub = _Stub()
        _answer(monkeypatch, exc=requests.exceptions.ReadTimeout("timed out"))

        assert stub._probe_chats_and_start_sync() is False
        assert stub.sync_starts == 0
        assert stub.connection_flags == []

    def test_connection_error(self, monkeypatch):
        stub = _Stub()
        _answer(monkeypatch, exc=requests.exceptions.ConnectionError("refused"))

        assert stub._probe_chats_and_start_sync() is False
        assert stub.connection_flags == []

    def test_plain_server_error(self, monkeypatch):
        """A 500 that is not a closed target: the server is up but not ready.
        Still a retry, never an exit — the sync must not be skipped for it."""
        stub = _Stub()
        _answer(monkeypatch, _Response({"error": "boom"}, status_code=500))

        assert stub._probe_chats_and_start_sync() is False
        assert stub.sync_starts == 0
        assert stub.connection_flags == []

    def test_non_json_body(self, monkeypatch):
        """A 200 whose body isn't the expected list (an HTML error page from a
        proxy, "undefined", …) is not an answer — keep asking."""
        stub = _Stub()
        _answer(monkeypatch, _Response({"response": "undefined"}))

        assert stub._probe_chats_and_start_sync() is False
        assert stub.sync_starts == 0
