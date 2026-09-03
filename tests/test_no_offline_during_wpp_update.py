"""Tests that a WPPConnect update is not *announced* as an outage — while
still being treated as one everywhere the app holds work back.

`_update_wpp_server()` stops the server on purpose. The socket drops, every
probe fails, and nothing answers until the reinstall finishes — two or three
minutes on a real update. Announcing "desconectado do WhatsApp" through that
window is wrong, and it frightens people: two field reports describe the update
as leaving the app "flipping between offline and normal". That is the message,
not the mechanism.

There was already a `_wpp_updating` flag for exactly this, but it was consulted
in one place only — the health checker. `WebSocketClient`'s confirmed-socket-drop
path went straight to `_set_wa_connected(False, "socket disconnected")`, and that
is the one that fired: observed at 17:48:26 during a real 2.10.6 -> 2.10.10
update, seconds after the server was stopped deliberately.

The guard now sits at the single place that decides the message, so it cannot be
half-applied across callers again.

**Why two tests here changed their expectation.** The guard first returned
*before* `_auto_offline = True` / `_apply_offline_state()`, and those two tests
asserted that (`offline_applied is False`). That over-shot the intent: the fix
was meant to be presentational, but skipping the recompute also left
`self.offline_mode` False, and `MessageQueue._run` holds sends only while it is
True. A message sent moments before the user accepted the update was therefore
still attempted, against the already-stopped server; the
`requests.exceptions.ConnectionError` that came back is classified *ambiguous*
by `_classify_send_exception()` — a timeout can mean WhatsApp Web accepted the
message into its own outbox, where a resend would duplicate it — so the queue
dropped it without resending. The pending bubble resolved to nothing and the
message never arrived. That ambiguity does not apply to an update:
`_stop_wpp_server()` has just killed Node and Chrome, so nothing anywhere holds
the message. Offline mode is now engaged exactly as a real outage engages it,
and only the sound/speech/"desconectado" text stay suppressed.

Suppressing the announcement while the state *is* offline needs one more thing:
if the server never comes back, the offline state is already in place, so the
"nothing changed" early return at the top of `_set_wa_connected()` would swallow
the health check that finally notices — a genuine outage silent for the rest of
the session, with the status stuck on "conectando".
`_offline_announce_deferred` is what lets that one call through.
"""

import pytest

from main import MainWindow


class _I18n:
    def t(self, key):
        return {"tray_connecting": "conectando...",
                "tray_wa_disconnected": "desconectado do WhatsApp"}.get(key, key)


class _Sound:
    def __init__(self):
        self.played = 0

    def play(self):
        self.played += 1


class _Queue:
    """Stands in for MessageQueue — only flush() matters at this layer."""

    def __init__(self):
        self.flushes = 0

    def flush(self):
        self.flushes += 1


class _Stub:
    _set_wa_connected = MainWindow._set_wa_connected
    _self_inflicted_teardown_expected = MainWindow._self_inflicted_teardown_expected
    _reset_startup_probe = MainWindow._reset_startup_probe
    # The real recompute, not a spy: self.offline_mode is precisely what
    # MessageQueue._run reads before sending, so the tests assert the thing
    # that actually holds the message rather than a flag they invented.
    _apply_offline_state = MainWindow._apply_offline_state

    def __init__(self, updating=False):
        self.token = "tok"
        self._wpp_updating = updating
        self._wa_connected = True
        self._wa_offline_strikes = 0
        self._wa_connect_announced = True
        self._auto_offline = False
        self._user_offline = False
        self.offline_mode = False
        self.i18n = _I18n()
        self.statuses = []
        self.spoken = []
        self.background_mode = False
        self.offline_mode_sound = _Sound()
        self.connected_sound = _Sound()
        self.message_queue = _Queue()
        self._tray_status = ""
        self._sync_offline_menu_item = None
        self._sync_completed = True
        self._last_sync_attempt_ts = 123.0
        self._sync_retry_count = 0
        self.sync_triggered = 0
        # Only the connected branch reaches these.
        self._dead_browser_strikes = 0
        self._auto_repair_dialog_shown = False
        self.ws = None

    # --- collaborators the method reaches for ---
    def _set_status(self, text):
        self.statuses.append(text)
        self._tray_status = text

    def _update_title(self):
        pass

    def output(self, text, interrupt=False):
        self.spoken.append(text)

    def _announce_sync_events_enabled(self):
        return True

    def _startup_offline_confirmed(self):
        return False

    def _set_preparing_status_if_idle(self):
        pass

    def trigger_sync_if_needed(self):
        self.sync_triggered += 1


@pytest.fixture(autouse=True)
def _sync_callafter(monkeypatch):
    """wx.CallAfter would otherwise need a running app; run inline. The same
    goes for IsMainThread, which _apply_offline_state() consults."""
    import main as main_module

    monkeypatch.setattr(main_module.wx, "CallAfter",
                        lambda fn, *a, **k: fn(*a, **k))
    monkeypatch.setattr(main_module.wx, "IsMainThread", lambda: True)


class TestDuringAnUpdate:
    def test_it_shows_connecting_not_disconnected(self):
        stub = _Stub(updating=True)
        stub._set_wa_connected(False, "socket disconnected", False)

        assert stub.statuses == ["conectando..."]
        assert "desconectado do WhatsApp" not in stub.statuses

    def test_it_says_nothing_at_all(self):
        """No sound, no speech — the announcement is the whole defect this
        guard was written for."""
        stub = _Stub(updating=True)
        stub._set_wa_connected(False, "socket disconnected", False)

        assert stub.offline_mode_sound.played == 0
        assert stub.spoken == []

    def test_it_does_enter_the_offline_state(self):
        """Changed expectation: this used to assert the opposite.

        Offline mode is what pauses MessageQueue, and the server is genuinely
        down — a message sent moments earlier was otherwise attempted against
        it, failed with ConnectionError, was classified ambiguous and dropped
        without a resend. See the module docstring.
        """
        stub = _Stub(updating=True)
        stub._set_wa_connected(False, "socket disconnected", False)

        assert stub._auto_offline is True
        assert stub.offline_mode is True

    def test_the_health_check_path_is_covered_too(self):
        """The health checker already consulted the flag itself; the point of
        guarding centrally is that both callers now behave the same."""
        stub = _Stub(updating=True)
        stub._set_wa_connected(False, "status-session CLOSED", True)

        assert stub.statuses == ["conectando..."]
        assert stub.offline_mode is True
        assert stub.spoken == []

    def test_the_user_toggle_is_left_alone(self):
        """Only the automatic half moves — the tray checkbox reflects the
        user's own choice and must not start ticking itself during an
        update."""
        stub = _Stub(updating=True)
        stub._set_wa_connected(False, "socket disconnected", False)

        assert stub._user_offline is False


class TestTheQueueIsPausedAndFlushed:
    """The point of engaging offline mode: what was queued during the update
    goes out afterwards instead of being silently lost."""

    def test_the_server_coming_back_flushes_the_queue(self):
        stub = _Stub(updating=True)
        stub._set_wa_connected(False, "socket disconnected", False)
        assert stub.message_queue.flushes == 0

        stub._wpp_updating = False
        stub._set_wa_connected(True, "status-session CONNECTED")

        assert stub.offline_mode is False
        assert stub.message_queue.flushes == 1

    def test_a_user_toggled_offline_survives_the_update(self):
        """The automatic half clearing must not undo a deliberate choice."""
        stub = _Stub(updating=True)
        stub._user_offline = True
        stub._set_wa_connected(False, "socket disconnected", False)

        stub._wpp_updating = False
        stub._set_wa_connected(True, "status-session CONNECTED")

        assert stub.offline_mode is True
        assert stub.message_queue.flushes == 0


class TestAnOutageThatOutlivesTheUpdate:
    """Suppressing the announcement while the state is already offline would
    otherwise hide a failed update forever: the "nothing changed" early return
    matches, and the user is left on "conectando" with no idea why nothing
    sends."""

    def test_it_is_announced_once_the_flag_clears(self):
        stub = _Stub(updating=True)
        stub._set_wa_connected(False, "socket disconnected", False)
        assert stub.spoken == []

        stub._wpp_updating = False
        stub._set_wa_connected(False, "status-session CLOSED", True)

        assert stub.statuses[-1] == "desconectado do WhatsApp"
        assert stub.offline_mode_sound.played == 1
        assert "offline_mode_auto_enabled" in stub.spoken

    def test_it_is_announced_only_once(self):
        """The health checker fires every 30 s for as long as the outage
        lasts; only the first of those may speak."""
        stub = _Stub(updating=True)
        stub._set_wa_connected(False, "socket disconnected", False)
        stub._wpp_updating = False
        for _ in range(5):
            stub._set_wa_connected(False, "status-session CLOSED", True)

        assert stub.offline_mode_sound.played == 1
        assert stub.spoken.count("offline_mode_auto_enabled") == 1

    def test_a_check_still_inside_the_update_stays_silent(self):
        """Repeated failures while the update is running are still the update,
        not an outage."""
        stub = _Stub(updating=True)
        for _ in range(5):
            stub._set_wa_connected(False, "socket disconnected", False)

        assert stub.offline_mode_sound.played == 0
        assert stub.spoken == []
        assert stub.statuses == ["conectando..."]


class TestOutsideAnUpdate:
    def test_a_real_outage_is_still_announced(self):
        """The guard must not swallow genuine disconnections — that would be a
        worse bug than the one it fixes."""
        stub = _Stub(updating=False)
        stub._set_wa_connected(False, "socket disconnected", True)

        assert stub.statuses == ["desconectado do WhatsApp"]
        assert stub.offline_mode is True
        assert stub.offline_mode_sound.played == 1

    def test_a_missing_flag_is_treated_as_not_updating(self):
        """_wpp_updating is set in __init__; anything reaching this before then
        must fall through to the normal path, not be silently suppressed."""
        stub = _Stub(updating=False)
        del stub._wpp_updating
        stub._set_wa_connected(False, "socket disconnected", True)

        assert stub.statuses == ["desconectado do WhatsApp"]


class TestTheFlagIsActuallyCleared:
    """The suppression is only safe because the flag is guaranteed to clear —
    otherwise a failed update would hide every outage from then on."""

    def test_update_clears_it_in_a_finally(self):
        import inspect

        source = inspect.getsource(MainWindow._update_wpp_server)
        finally_at = source.index("finally:")
        assert "self._wpp_updating = False" in source[finally_at:]
