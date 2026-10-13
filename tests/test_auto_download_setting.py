"""Configuracoes > Armazenamento > "Baixar midias automaticamente ao
sincronizar" has to be obeyed when it is changed, not at the next sync.

Three things were wrong at once:

* unticking it left the sweep already running to finish its whole queue —
  the setting was read once, before the phase started;
* ticking it (or ticking another category) fetched nothing until some later
  sync happened to run its media phase.

It stays ON by default on purpose (a design decision): with it off, a recent
audio or document is not on the computer when opened, so playing says
"baixando" and, offline, cannot play at all.

``MainWindow`` cannot be instantiated without a wx.App, so the methods are
bound onto plain stubs — same approach as tests/test_media_sync_count.py.
"""

import json
import os

from core.utils import (
    AUTO_DOWNLOAD_MEDIA_TYPES,
    DEFAULT_SETTINGS,
    auto_download_enabled,
    auto_download_newly_wanted,
)
from main import MainWindow


def _storage(enabled, types=None):
    storage = {"auto_download_media": enabled}
    if types is not None:
        storage["auto_download_media_types"] = list(types)
    return storage


class TestDefault:
    def test_a_fresh_install_starts_with_it_on(self):
        assert DEFAULT_SETTINGS["storage"]["auto_download_media"] is True

    def test_the_seed_file_agrees(self):
        path = os.path.join(os.path.dirname(__file__), "..", "client", "data",
                            "settings_default.json")
        with open(path, encoding="utf-8") as f:
            assert json.load(f)["storage"]["auto_download_media"] is True

    def test_the_categories_stay_all_ticked(self):
        """Off, but ready: turning it on without touching the list downloads
        everything, as before."""
        assert DEFAULT_SETTINGS["storage"]["auto_download_media_types"] == list(
            AUTO_DOWNLOAD_MEDIA_TYPES)


class TestAutoDownloadEnabled:
    def test_off_only_when_explicitly_off(self):
        assert auto_download_enabled({"storage": _storage(True)}) is True
        assert auto_download_enabled({"storage": _storage(False)}) is False

    def test_missing_or_corrupt_reads_as_the_default_on(self):
        """On by default is a design decision: a settings file that lost the
        key must not leave recent audios/documents undownloaded."""
        for settings in ({}, {"storage": {}}, {"storage": None}, None,
                         {"storage": {"auto_download_media": "yes"}}):
            assert auto_download_enabled(settings) is True


class TestAutoDownloadNewlyWanted:
    def test_turning_it_on(self):
        assert auto_download_newly_wanted(_storage(False), _storage(True)) is True

    def test_ticking_another_category(self):
        assert auto_download_newly_wanted(
            _storage(True, ["photos"]), _storage(True, ["photos", "videos"])) is True

    def test_unticking_a_category_needs_no_sweep(self):
        assert auto_download_newly_wanted(
            _storage(True, ["photos", "videos"]), _storage(True, ["photos"])) is False

    def test_turning_it_off(self):
        assert auto_download_newly_wanted(_storage(True), _storage(False)) is False

    def test_categories_changed_while_it_stays_off(self):
        assert auto_download_newly_wanted(
            _storage(False, ["photos"]), _storage(False, ["photos", "videos"])) is False

    def test_nothing_changed(self):
        assert auto_download_newly_wanted(_storage(True), _storage(True)) is False

    def test_turning_it_on_with_every_category_unticked(self):
        assert auto_download_newly_wanted(_storage(False), _storage(True, [])) is False

    def test_a_list_never_saved_counts_as_every_category(self):
        """What auto_download_allows() reads it as — so narrowing from it is
        not "newly wanted", and widening back to it is."""
        assert auto_download_newly_wanted(
            _storage(True), _storage(True, ["photos"])) is False
        assert auto_download_newly_wanted(
            _storage(True, ["photos"]), _storage(True)) is True


class _SweepStub:
    """The sweep itself, one worker so the order is the queue's order."""

    _MEDIA_SYNC_TIMEOUT = 60
    _MEDIA_SYNC_WORKERS = 1
    sync_media_for_all_chats = MainWindow.sync_media_for_all_chats
    _voice_call_in_progress = MainWindow._voice_call_in_progress
    _VOICE_CALL_PAUSE_MAX_SECONDS = MainWindow._VOICE_CALL_PAUSE_MAX_SECONDS
    _active_voice_call = None
    _voice_call_pause_since = 0.0

    def __init__(self, count, disable_after):
        self.settings = {"storage": _storage(True)}
        self.chats = {"a@s.whatsapp.net": {"messages": {"messages": {"records": [
            {"key": {"id": str(i)}, "messageType": "imageMessage"}
            for i in range(count)]}}}}
        self.fetched = []
        self._disable_after = disable_after

    def _media_sync_candidate(self, msg, explicit=False):
        # The real filter is _FunnelStub's; this queue is about stopping.
        return msg.get("messageType")

    def sync_if_media(self, msg, timeout=60, explicit=False):
        self.explicit = explicit
        self.fetched.append(msg["key"]["id"])
        if len(self.fetched) == self._disable_after:
            # The user unticks the box in Settings while this file downloads.
            self.settings["storage"]["auto_download_media"] = False
        return True

    def _save_media_failed_ids(self):
        pass


class TestUntickingStopsTheRunningSweep:
    def test_the_queue_ends_after_the_file_in_flight(self):
        stub = _SweepStub(count=10, disable_after=3)
        count = stub.sync_media_for_all_chats(
            should_stop=lambda: not auto_download_enabled(stub.settings))
        assert stub.fetched == ["0", "1", "2"]
        assert count == 3

    def test_the_menu_sweep_ignores_the_setting(self):
        """"Baixar midias" is an explicit request: no should_stop, and every
        download is marked explicit so sync_if_media() does not ask the
        switch, so it works with the option unchecked."""
        stub = _SweepStub(count=4, disable_after=1)
        assert stub.sync_media_for_all_chats(explicit=True) == 4
        assert stub.explicit is True

    def test_an_automatic_sweep_is_not_explicit(self):
        stub = _SweepStub(count=1, disable_after=0)
        stub.sync_media_for_all_chats()
        assert stub.explicit is False

    def test_the_menu_asks_for_an_explicit_sweep(self):
        from tests.god_modules import main_window_method_source
        assert "sync_media_for_all_chats(explicit=True)" in main_window_method_source(
            "_on_menu_sync_media")


class _FunnelStub:
    """sync_if_media() itself, with a download that always succeeds."""

    _MEDIA_MAX_AGE_SECONDS = MainWindow._MEDIA_MAX_AGE_SECONDS
    sync_if_media = MainWindow.sync_if_media
    _media_sync_candidate = MainWindow._media_sync_candidate
    _wa_connected = True
    offline_mode = False

    def __init__(self, enabled, types=None):
        self.settings = {"storage": _storage(enabled, types)}
        self._media_failed_ids = {}

    def _media_max_download_days(self):
        return 0

    def _media_max_download_bytes(self):
        return 0

    def _is_conversation_open_for(self, msg):
        return False

    def handle_audio_message(self, msg, timeout=60):
        return True

    def handle_media_message(self, msg, progress_callback=None, timeout=60):
        return True


def _live_msg(message_type="audioMessage"):
    import time
    return {"key": {"id": "3EB0AA"}, "messageType": message_type,
            "messageTimestamp": int(time.time())}


class TestTheSwitchIsTheMasterSwitch:
    """on_new_message() hands every arriving media message to sync_if_media(),
    so this is the live path too — a voice note included."""

    def test_off_fetches_nothing_on_its_own(self):
        for message_type in ("audioMessage", "imageMessage", "documentMessage"):
            assert _FunnelStub(False).sync_if_media(_live_msg(message_type)) is False

    def test_off_keeps_the_ticked_categories_out_of_it(self):
        stub = _FunnelStub(False, types=["audios", "voice_messages"])
        assert stub.sync_if_media(_live_msg()) is False

    def test_on_fetches(self):
        assert _FunnelStub(True).sync_if_media(_live_msg()) is True

    def test_an_explicit_request_does_not_ask_the_switch(self):
        assert _FunnelStub(False).sync_if_media(_live_msg(), explicit=True) is True

    def test_an_explicit_request_still_follows_the_category_list(self):
        stub = _FunnelStub(False, types=["photos"])
        assert stub.sync_if_media(_live_msg("audioMessage"), explicit=True) is False
        assert stub.sync_if_media(_live_msg("imageMessage"), explicit=True) is True


class _Control:
    def __init__(self, value=False):
        self.value, self.enabled = value, None

    def GetValue(self):
        return self.value

    def Enable(self, enabled=True):
        self.enabled = enabled


class TestTheCategoryListFollowsTheCheckbox:
    def _dialog(self, checked):
        from ui.dialogs.settings_dialog import SettingsDialog

        class _Dialog:
            _update_auto_download_types_state = (
                SettingsDialog._update_auto_download_types_state)

        dialog = _Dialog()
        dialog._auto_download_media_check = _Control(checked)
        dialog._auto_download_types_label = _Control()
        dialog._auto_download_types_list = _Control()
        dialog._update_auto_download_types_state()
        return dialog

    def test_disabled_while_the_auto_download_is_off(self):
        dialog = self._dialog(False)
        assert dialog._auto_download_types_list.enabled is False
        assert dialog._auto_download_types_label.enabled is False

    def test_enabled_while_it_is_on(self):
        dialog = self._dialog(True)
        assert dialog._auto_download_types_list.enabled is True
        assert dialog._auto_download_types_label.enabled is True


class _MainStub:
    _on_auto_download_settings_changed = MainWindow._on_auto_download_settings_changed
    _start_deferred_media_sync = MainWindow._start_deferred_media_sync

    def __init__(self, **state):
        self.settings = {"storage": _storage(True)}
        self._sync_completed = True
        self._wa_connected = True
        self.offline_mode = False
        self._media_sync_running = False
        self._history_still_landing = False
        self._media_sync_deferred = False
        self.__dict__.update(state)


class _ChangedStub(_MainStub):
    """Records the hand-off instead of starting the thread."""

    def __init__(self, **state):
        super().__init__(**state)
        self.started = 0

    def _start_deferred_media_sync(self):
        self.started += 1


class TestSettingsChangeStartsASweep:
    def test_turning_it_on_starts_one(self):
        stub = _ChangedStub()
        assert stub._on_auto_download_settings_changed(
            _storage(False), _storage(True)) is True
        assert stub.started == 1

    def test_turning_it_off_starts_nothing(self):
        stub = _ChangedStub()
        assert stub._on_auto_download_settings_changed(
            _storage(True), _storage(False)) is False
        assert stub.started == 0
        assert stub._media_sync_deferred is False

    def test_history_still_landing_queues_it_for_the_backfill_loop(self):
        stub = _ChangedStub(_history_still_landing=True)
        assert stub._on_auto_download_settings_changed(
            _storage(False), _storage(True)) is True
        assert stub.started == 0
        assert stub._media_sync_deferred is True

    def test_a_sweep_already_running_is_left_alone(self):
        stub = _ChangedStub(_media_sync_running=True)
        assert stub._on_auto_download_settings_changed(
            _storage(False), _storage(True)) is False
        assert stub.started == 0

    def test_nothing_is_queued_while_the_sync_itself_is_still_to_come(self):
        """Its own media phase reads the setting as it is now; a queued sweep
        would repeat it."""
        for state in ({"_sync_completed": False}, {"_wa_connected": False},
                      {"offline_mode": True}):
            stub = _ChangedStub(**state)
            assert stub._on_auto_download_settings_changed(
                _storage(False), _storage(True)) is False
            assert stub.started == 0
            assert stub._media_sync_deferred is False


class TestDeferredSweepRechecksTheSetting:
    def test_switched_off_while_waiting_starts_no_thread(self, monkeypatch):
        import threading

        def _no_thread(*args, **kwargs):
            raise AssertionError("the deferred sweep must not start")

        stub = _MainStub(_media_sync_deferred=True)
        stub.settings["storage"]["auto_download_media"] = False
        monkeypatch.setattr(threading, "Thread", _no_thread)
        stub._start_deferred_media_sync()
        # Consumed, so a later call cannot resurrect it either.
        assert stub._media_sync_deferred is False
