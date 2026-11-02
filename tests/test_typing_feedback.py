"""Optional cadence: fake clock/timers only, no wx.App or audio device."""
from types import SimpleNamespace
import copy
import pytest

from core.typing_feedback import TypingCadence, interval, mode, save_preferences
from core.utils import DEFAULT_SETTINGS, backfill_missing_defaults


@pytest.mark.parametrize(("spoken", "expected"), [(True, "speech"), (False, "off")])
def test_upgrading_preserves_existing_typing_choice_and_never_enables_sound(spoken, expected):
    settings = {"speech_content": {"announce_typing": spoken}}
    before = copy.deepcopy(settings)
    assert mode(settings) == expected
    assert settings == before  # Reading preferences never migrates a choice.
    backfill_missing_defaults(settings, DEFAULT_SETTINGS)
    assert settings["speech_content"]["announce_typing"] is spoken
    assert settings["speech_content"]["typing_sound"] is False
    assert mode(settings) == expected


@pytest.mark.parametrize(("selected", "spoken", "audible"), [
    ("off", False, False), ("speech", True, False),
    ("sound", False, True), ("both", True, True),
])
def test_all_four_modes_round_trip_without_changing_recording_or_visual_row(selected, spoken, audible):
    settings = {"speech_content": {"announce_recording": False}, "user_interface": {"show_typing_row": True}}
    save_preferences(settings, selected, 5)
    assert mode(settings) == selected
    assert settings["speech_content"]["announce_typing"] is spoken
    assert settings["speech_content"]["typing_sound"] is audible
    assert settings["speech_content"]["announce_recording"] is False
    assert settings["user_interface"]["show_typing_row"] is True
    assert interval(settings) == 5


@pytest.mark.parametrize("value", [None, True, False, "3", {}, [], -1, 0, 31, float("nan"), float("inf")])
def test_bad_interval_is_bounded(value):
    assert interval({"speech_content": {"typing_sound_interval": value}}) == 3


class Timer:
    def __init__(self, delay, callback):
        self.delay = delay
        self.callback = callback
        self.stopped = False

    def Stop(self):
        self.stopped = True


@pytest.fixture
def rig():
    r = SimpleNamespace(now=0, context=("visit", 1), played=[], stopped=[], timers=[])
    def schedule(delay, callback):
        timer = Timer(delay, callback)
        r.timers.append(timer)
        return timer
    r.cadence = TypingCadence(schedule, lambda: r.context,
                             lambda: r.played.append(r.now), lambda: r.stopped.append(r.now),
                             lambda: 3, clock=lambda: r.now)
    def advance(seconds):
        r.now += seconds
        r.timers[-1].callback()
    r.advance = advance
    return r


def test_heartbeat_repeats_sound_while_refreshes_never_postpone_it(rig):
    c = rig.cadence
    c.observe(rig.context, "one", True)
    first = c.timer
    rig.now = 2
    c.observe(rig.context, "one", True)
    assert c.timer is first
    rig.advance(1)
    rig.advance(3)
    assert rig.played == [0, 3, 6]
    assert len(rig.timers) == 3


def test_one_group_cadence_not_one_sound_per_participant(rig):
    for person in ("one", "two", "three"):
        rig.cadence.observe(rig.context, person, True)
    assert rig.played == [0]
    assert len(rig.timers) == 1
    rig.cadence.dismiss("one")
    rig.advance(3)
    assert rig.played == [0, 3]
    rig.cadence.dismiss("two")
    rig.cadence.dismiss("three")
    assert rig.cadence.timer is None
    assert not rig.cadence.people


@pytest.mark.parametrize("ending", ["paused", "recording", "message", "stop", "ineligible"])
def test_endings_stop_sound_and_fence_queued_callback(rig, ending):
    c = rig.cadence
    c.observe(rig.context, "one", True)
    old = c.timer
    if ending in ("paused", "recording"):
        c.observe(rig.context, "one", False)
    elif ending == "message":
        c.dismiss()
    elif ending == "ineligible":
        c.observe(None, "one", True)
    else:
        c.stop()
    assert old.stopped
    rig.now = 3
    old.callback()  # A callback already queued before Stop cannot revive work.
    assert rig.played == [0]
    assert c.timer is None


def test_expiry_stops_at_ten_seconds_without_final_push(rig):
    rig.cadence.observe(rig.context, "one", True)
    for seconds in (3, 3, 3):
        rig.advance(seconds)
    assert rig.timers[-1].delay == 1000
    rig.advance(1)
    assert rig.played == [0, 3, 6, 9]
    assert rig.cadence.timer is None


@pytest.mark.parametrize("replacement", [None, ("visit", 2), ("other chat", 1)])
def test_callback_revalidates_current_context(rig, replacement):
    rig.cadence.observe(rig.context, "one", True)
    rig.context = replacement
    rig.advance(3)
    assert rig.played == [0]
    assert rig.cadence.timer is None


def test_old_callback_cannot_retire_successor_timer(rig):
    c = rig.cadence
    c.observe(rig.context, "one", True)
    old = c.timer
    c.stop()
    c.observe(rig.context, "one", True)
    successor = c.timer
    old.callback()
    assert c.timer is successor
    assert not successor.stopped


def test_playback_failure_is_isolated_and_does_not_rearm(rig):
    def broken():
        raise RuntimeError("unavailable device")
    rig.cadence.play = broken
    rig.cadence.observe(rig.context, "one", True)
    assert rig.cadence.timer is None
    assert not rig.cadence.people


def test_shutdown_during_play_does_not_create_a_new_timer(rig):
    rig.cadence.play = lambda: rig.cadence.stop()
    rig.cadence.observe(rig.context, "one", True)
    assert not rig.timers


def test_a_duplicate_old_tick_does_not_create_a_second_chain(rig):
    rig.cadence.observe(rig.context, "one", True)
    old = rig.cadence.timer
    rig.advance(3)
    successor = rig.cadence.timer
    old.callback()
    assert rig.played == [0, 3]
    assert rig.cadence.timer is successor


def test_reentrant_successor_is_not_stopped_by_old_play_completion(rig):
    c = rig.cadence
    def replace():
        c.stop()
        c.play = lambda: rig.played.append(rig.now)
        c.observe(rig.context, "successor", True)
    c.play = replace
    c.observe(rig.context, "one", True)
    assert set(c.people) == {"successor"}
    assert c.timer is not None and not c.timer.stopped
