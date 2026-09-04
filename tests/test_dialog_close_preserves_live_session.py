"""Tests for Connect.on_dialog_close()/on_quit_from_connect() not tearing
down a currently-live WhatsApp connection.

Reported live (issue #155): an account paired and worked normally for an
entire session — messages synced, nothing visibly wrong — yet the very next
launch showed the pairing dialog again, as if never paired. The account's
WPPConnect session, its Chrome userDataDir profile, and its sessions.json
entry were all still intact and reusable (confirmed by manually restoring
the encrypted token from sessions.json into settings.json, after which the
same session kept working across further restarts) — only the token
reference in settings.json had disappeared, with `paired` left `true`.

The connection dialog (client/ui/dialogs/connect.py) can now open on its
own while an account is already paired — websocket_client.py's
_show_repair_dialog(), the proactive re-pair dialog — and WhatsApp can
reconnect in the background in the time before a user reacts to it (the
dialog is sometimes not even seen for many seconds — see
tests/test_qrcode_auto_repair_dialog.py). Both on_dialog_close() and
on_quit_from_connect() used to assume the opposite: that a dialog on screen
always means nothing usable is connected yet, so closing or quitting from
it unconditionally disconnected the live WebSocket and cleared the saved
token via _close_active_session() — exactly the observed symptom, since
_close_active_session() never touches `paired` or the SessionStore entry,
only the token reference (see _set_wa_token("") in main.py).

Connect is a plain class — same approach as tests/test_pairing_startup_grace.py.
"""

import pytest

import ui.dialogs.connect as connect_module
from ui.dialogs.connect import Connect


class _FakeSio:
    def __init__(self):
        self.connected = True
        self.disconnect_calls = 0

    def disconnect(self):
        self.disconnect_calls += 1


class _FakeWs:
    def __init__(self):
        self.sio = _FakeSio()


class _FakeCloseEvent:
    def __init__(self, can_veto=True):
        self._can_veto = can_veto
        self.skip_calls = 0
        self.veto_calls = 0

    def CanVeto(self):
        return self._can_veto

    def Skip(self):
        self.skip_calls += 1

    def Veto(self):
        self.veto_calls += 1


class _FakeMainWindow:
    def __init__(self, wa_connected, pairing_in_progress=False):
        self.settings = {"general": {"language": "pt-BR"}}
        self._wa_connected = wa_connected
        self._pairing_in_progress = pairing_in_progress
        self.ws = _FakeWs()
        self.token = "sess1:hash1"
        self.wpp_server = "http://127.0.0.1"
        self.wpp_port = 6300
        self.real_exit_calls = 0
        self.close_active_session_calls = 0

    def output(self, *a, **kw):
        pass

    def real_exit(self):
        self.real_exit_calls += 1


@pytest.fixture(autouse=True)
def _no_sys_exit(monkeypatch):
    """on_quit_from_connect()'s not-connected branch really does call
    sys.exit() — that is the behaviour under test in that branch, but it
    must not actually kill the test process."""
    monkeypatch.setattr(connect_module.sys, "exit", lambda: (_ for _ in ()).throw(SystemExit))


def _make_connect(mw):
    c = Connect(mw)
    calls = []
    c._close_active_session = lambda *a, **kw: calls.append((a, kw))
    return c, calls


class TestOnDialogCloseWhileConnected:
    def test_does_not_disconnect_or_clear_the_session(self):
        mw = _FakeMainWindow(wa_connected=True)
        c, close_calls = _make_connect(mw)
        event = _FakeCloseEvent()

        c.on_dialog_close(event)

        assert close_calls == []
        assert mw.ws is not None
        assert mw.ws.sio.disconnect_calls == 0
        assert event.skip_calls == 1
        assert event.veto_calls == 0


class TestOnDialogCloseWhileNotConnected:
    def test_disconnects_and_clears_as_before(self):
        """Unchanged behaviour: nothing usable is connected, so closing the
        dialog still tears the attempt down."""
        mw = _FakeMainWindow(wa_connected=False)
        c, close_calls = _make_connect(mw)
        event = _FakeCloseEvent()

        c.on_dialog_close(event)

        assert len(close_calls) == 1
        assert mw.ws is None
        assert event.skip_calls == 1


class TestOnQuitFromConnectWhileConnected:
    def test_quits_via_real_exit_without_tearing_down_the_session(self):
        mw = _FakeMainWindow(wa_connected=True)
        c, close_calls = _make_connect(mw)

        c.on_quit_from_connect(None)

        assert close_calls == []
        assert mw.ws is not None
        assert mw.ws.sio.disconnect_calls == 0
        assert mw.real_exit_calls == 1


class TestOnQuitFromConnectWhileNotConnected:
    def test_disconnects_clears_and_exits_as_before(self):
        mw = _FakeMainWindow(wa_connected=False)
        c, close_calls = _make_connect(mw)

        with pytest.raises(SystemExit):
            c.on_quit_from_connect(None)

        assert len(close_calls) == 1
        assert close_calls[0] == ((), {"sync": True})
        assert mw.ws is None
        assert mw.real_exit_calls == 0
