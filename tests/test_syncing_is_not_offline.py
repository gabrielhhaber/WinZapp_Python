"""WhatsApp Web catching up is not a dropped connection.

After days without WPPConnect, the stored login is accepted in seconds but the
page takes minutes to finish its sync, and the status stays INITIALIZING until
then. Past the 45 s startup grace that read as "disconnected", with the offline
sound, on an account that was fine (measured 2026-11-02: authenticated, 65% then
73% 25 s later). status-session now says so, and the app shows its own state.
"""

import time
from pathlib import Path

import pytest

import connection_state as cs
from tests.test_no_offline_during_wpp_update import _Stub

PATCHES = Path(__file__).resolve().parents[1] / "client" / "api_patches" / "src"


@pytest.fixture(autouse=True)
def _sync_callafter(monkeypatch):
    import main as main_module

    monkeypatch.setattr(main_module.wx, "CallAfter", lambda fn, *a, **k: fn(*a, **k))
    monkeypatch.setattr(main_module.wx, "IsMainThread", lambda: True)


class _Window(_Stub):
    _WA_STARTUP_GRACE_SECONDS = 45


def _past_the_grace(reported_at):
    stub = _Window()
    stub._wa_connected = False
    stub._wa_connect_announced = False
    stub._wa_startup_time = 0.0
    stub._wa_sync_reported_at = reported_at
    return stub


class TestWhatStatusSessionSays:
    def test_syncing_while_initializing(self):
        assert cs.session_reports_syncing({"status": "INITIALIZING", "syncing": True})

    def test_a_server_without_the_field_is_not_syncing(self):
        assert not cs.session_reports_syncing({"status": "INITIALIZING"})

    def test_any_other_status_is_not_syncing(self):
        assert not cs.session_reports_syncing({"status": "CONNECTED", "syncing": True})

    def test_a_report_holds_across_one_lost_probe(self):
        assert cs.sync_report_is_fresh(100.0, 160.0)

    def test_a_report_nothing_renewed_runs_out(self):
        assert not cs.sync_report_is_fresh(100.0, 100.0 + cs.SYNC_REPORT_LIFETIME_SECONDS)

    def test_no_report_is_not_fresh(self):
        assert not cs.sync_report_is_fresh(None, 100.0)


class TestTheStateTheWindowShows:
    def test_it_says_syncing_past_the_startup_grace(self):
        stub = _past_the_grace(time.monotonic())
        stub._set_wa_connected(False, "status-session INITIALIZING", False)
        assert stub.statuses == ["tray_syncing_whatsapp"]

    def test_it_makes_no_sound_and_says_nothing(self):
        stub = _past_the_grace(time.monotonic())
        stub._set_wa_connected(False, "status-session INITIALIZING", False)
        assert stub.offline_mode_sound.played == 0 and stub.spoken == []

    def test_without_a_report_it_is_still_a_disconnection(self):
        stub = _past_the_grace(None)
        stub._set_wa_connected(False, "status-session INITIALIZING", False)
        assert stub.statuses == ["desconectado do WhatsApp"]

    def test_a_node_that_died_mid_sync_ends_in_a_disconnection(self):
        stub = _past_the_grace(time.monotonic() - cs.SYNC_REPORT_LIFETIME_SECONDS - 1)
        stub._set_wa_connected(False, "health check failed", False)
        assert stub.statuses == ["desconectado do WhatsApp"]

    def test_a_definite_answer_is_not_held_back(self):
        stub = _past_the_grace(time.monotonic())
        stub._set_wa_connected(False, "status-session notLogged", confirmed=True)
        assert "tray_syncing_whatsapp" not in stub.statuses

    def test_a_session_that_was_up_holds_sends_quietly(self):
        stub = _past_the_grace(time.monotonic())
        stub._wa_connected = True
        stub._wa_connect_announced = True
        stub._set_wa_connected(False, "status-session INITIALIZING", False)
        assert stub.statuses == ["tray_syncing_whatsapp"]
        assert stub.offline_mode is True and stub.offline_mode_sound.played == 0

    def test_connecting_clears_it(self):
        stub = _past_the_grace(time.monotonic())
        stub._wa_connect_announced = True
        stub._tray_status = "tray_syncing_whatsapp"
        stub._set_wa_connected(True, "status-session CONNECTED", False)
        assert stub.statuses[-1] == ""


def test_the_server_reports_it_from_the_login_to_connected():
    session = (PATCHES / "util" / "createSessionUtil.ts").read_text(encoding="utf-8")
    status = (PATCHES / "controller" / "sessionController.ts").read_text(encoding="utf-8")
    assert "statusFind === StatusFind.isLogged" in session
    assert "(client as any).syncing = false;" in session, "a new start begins unsynced"
    assert "client.status === 'INITIALIZING' &&" in status
