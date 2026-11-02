"""Alternative background/order/file scenarios, with all native surfaces fake."""
import sys
import threading
from types import SimpleNamespace

import pytest

from core import typing_sound
from main_window.window_lifecycle import WindowLifecycleMixin
from main_window.session_lifecycle import SessionLifecycleMixin
from tests.test_typing_feedback_integration import rig, GROUP_JID, PARTICIPANT_JID, OTHER  # noqa: F401


@pytest.mark.parametrize("entry", ["hide_to_tray", "close_to_tray"])
def test_native_tray_hide_stops_a_long_cue_without_relying_on_wx_events(rig, monkeypatch, entry):
    w = rig.window
    rig.presence("composing")
    old = w._typing_sound_cadence.timer
    w.typing_sound.is_playing = True
    w.tray_icon = SimpleNamespace(update_tooltip=lambda: None)
    w.lock_chat_vault = lambda **kwargs: None
    w._sync_online_presence = lambda: None
    w.GetHandle = lambda: 123
    # Actual source uses Win32 SW_HIDE directly, deliberately bypassing wx's
    # visibility bookkeeping. This fake sends no activation/show event.
    fake_ctypes = SimpleNamespace(windll=SimpleNamespace(user32=SimpleNamespace(ShowWindow=lambda *args: None)))
    monkeypatch.setitem(sys.modules, "ctypes", fake_ctypes)
    if entry == "hide_to_tray":
        WindowLifecycleMixin.hide_to_tray(w)
    else:
        WindowLifecycleMixin._on_close(w, SimpleNamespace(Veto=lambda: None))
    assert w._window_hidden is True
    assert w._typing_sound_cadence.timer is None
    assert old.stopped
    old.callback()
    assert rig.played == [0]


def test_offline_flag_blocks_an_already_queued_tick_before_invalidation_is_delivered(rig):
    w = rig.window
    w._wa_connected = True
    rig.presence("composing")
    old = w._typing_sound_cadence.timer
    w._wa_connected = False
    rig.now = 3
    old.callback()  # wx.CallAfter(_invalidate_contact_presence) is still queued.
    assert rig.played == [0]
    assert w._typing_sound_cadence.timer is None


def test_windows_session_end_stops_cue_before_waiting_for_another_teardown_owner(rig):
    w = rig.window
    rig.presence("composing")
    old = w._typing_sound_cadence.timer
    w._teardown_started_lock = threading.Lock()
    w._teardown_complete_event = SimpleNamespace(wait=lambda **kwargs: True)
    w._WINDOWS_SHUTDOWN_BUDGET = 1
    w._shutting_down = True
    assert SessionLifecycleMixin._run_windows_session_teardown(w, "fake-session-end") is False
    assert old.stopped and w._typing_sound_cadence.timer is None


@pytest.mark.parametrize("new_person_first", [False, True])
def test_group_handover_in_one_payload_does_not_restart_the_cadence(rig, new_person_first):
    w = rig.window
    rig.presence("composing")
    old = w._typing_sound_cadence.timer
    rig.now = .1
    changes = [(PARTICIPANT_JID, {"lastKnownPresence": "paused"}),
               (OTHER, {"lastKnownPresence": "composing"})]
    if new_person_first:
        changes.reverse()
    w.on_presence_update(GROUP_JID, dict(changes))
    assert rig.played == [0]
    assert w._typing_sound_cadence.timer is old
    assert set(w._typing_sound_cadence.people) == {OTHER}


@pytest.mark.parametrize("override", [[], {"path": "broken"}])
def test_malformed_imported_custom_path_falls_back_instead_of_breaking_sound_loading(tmp_path, monkeypatch, override):
    default_file = tmp_path / "typing.wav"
    default_file.write_bytes(b"fake sound; the decoder is mocked")
    pack = {"dir": str(tmp_path), "events": {"typing": "typing.wav"}}
    expected = SimpleNamespace()
    monkeypatch.setattr(typing_sound, "load_sound", lambda *args, **kwargs: expected)
    assert typing_sound.load_typing_sound(None, pack, pack, override, "default") is expected
