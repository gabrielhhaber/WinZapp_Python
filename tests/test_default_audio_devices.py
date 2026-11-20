"""What is on "system default" follows Windows' default device.

Report of 2026-11-18: during a call, plugging a wired headset in moved every
program's sound to it except WinZapp's call, which stayed on the speakers (and
on the headset when it was taken out). The driver makes a new endpoint the
default while the old one stays valid, so the call's PortAudio stream never
fails and nothing moved it. core/default_audio_endpoints.py watches the
default; main_window/default_audio_devices.py reopens the call on it and
moves the program sounds (SoundSystem.follow_default_output, tested in
test_audio_devices.py).
"""

import core.default_audio_endpoints as endpoints
from core.default_audio_endpoints import (
    DefaultEndpointWatcher,
    followed_change,
    refresh_portaudio_devices,
)
from main_window.default_audio_devices import DefaultAudioDevicesMixin

SPEAKERS, HEADSET, MIC, HEADSET_MIC = "{spk}", "{hp}", "{mic}", "{hpmic}"


# -- followed_change ---------------------------------------------------------

def test_a_new_default_output_moves_a_call_on_the_default():
    assert followed_change((SPEAKERS, MIC), (HEADSET, MIC), follow_output=True, follow_input=True)


def test_a_device_chosen_by_name_stays_where_it_is():
    assert not followed_change((SPEAKERS, MIC), (HEADSET, MIC),
                               follow_output=False, follow_input=True)


def test_a_new_default_microphone_moves_a_call_whose_microphone_is_the_default():
    assert followed_change((SPEAKERS, MIC), (SPEAKERS, HEADSET_MIC),
                           follow_output=False, follow_input=True)


def test_no_default_at_all_is_not_a_place_to_move_to():
    assert not followed_change((SPEAKERS, MIC), (None, MIC), follow_output=True, follow_input=True)


def test_an_unchanged_default_moves_nothing():
    assert not followed_change((SPEAKERS, MIC), (SPEAKERS, MIC),
                               follow_output=True, follow_input=True)


# -- DefaultEndpointWatcher.poll ---------------------------------------------

class _Readings:
    """Returns the readings in order, then keeps returning the last one."""

    def __init__(self, *values):
        self.values = list(values)

    def __call__(self):
        return self.values.pop(0) if len(self.values) > 1 else self.values[0]


def _watcher(readings, *, follows=(True, True), answer=True):
    changes = []

    def on_change():
        changes.append(True)
        return answer
    watcher = DefaultEndpointWatcher(on_change, lambda: follows, reader=readings)
    watcher._acted_on = watcher._last = readings()
    return watcher, changes


def test_the_change_is_acted_on_once_it_held_for_two_readings():
    watcher, changes = _watcher(_Readings((SPEAKERS, MIC), (HEADSET, MIC), (HEADSET, MIC),
                                          (HEADSET, MIC)))
    assert watcher.poll() is False      # first reading of the headset: may flicker
    assert watcher.poll() is True       # held: the call moves
    assert watcher.poll() is False      # and only once
    assert changes == [True]


def test_a_default_that_flickers_back_moves_nothing():
    watcher, changes = _watcher(_Readings((SPEAKERS, MIC), (HEADSET, MIC), (SPEAKERS, MIC),
                                          (SPEAKERS, MIC)))
    for _ in range(3):
        watcher.poll()
    assert changes == []


def test_taking_the_headset_out_moves_the_call_back():
    watcher, changes = _watcher(_Readings((SPEAKERS, MIC), (HEADSET, MIC), (HEADSET, MIC),
                                          (SPEAKERS, MIC), (SPEAKERS, MIC)))
    for _ in range(4):
        watcher.poll()
    assert changes == [True, True]


def test_a_change_refused_while_a_switch_runs_is_offered_again():
    watcher, changes = _watcher(_Readings((SPEAKERS, MIC), (HEADSET, MIC), (HEADSET, MIC),
                                          (HEADSET, MIC)), answer=False)
    for _ in range(3):
        watcher.poll()
    assert changes == [True, True]


def test_a_change_the_call_does_not_follow_is_not_reconsidered():
    follows = [False, False]
    watcher = DefaultEndpointWatcher(lambda: True, lambda: tuple(follows),
                                     reader=_Readings((HEADSET, MIC)))
    watcher._acted_on = watcher._last = (SPEAKERS, MIC)
    watcher.poll()
    watcher.poll()
    follows[0] = True   # the user goes back to the default afterwards
    assert watcher.poll() is False


def test_unreadable_defaults_never_move_the_call():
    watcher, changes = _watcher(_Readings((SPEAKERS, MIC), (None, None), (None, None)))
    watcher.poll()
    watcher.poll()
    assert changes == []


# -- refresh_portaudio_devices -----------------------------------------------

class _PortAudio:
    def __init__(self, fail_terminate=False, fail_initialize=0):
        self.calls = []
        self.fail_terminate = fail_terminate
        self.fail_initialize = fail_initialize

    def _terminate(self):
        self.calls.append("terminate")
        if self.fail_terminate:
            raise RuntimeError("busy")

    def _initialize(self):
        self.calls.append("initialize")
        if self.fail_initialize:
            self.fail_initialize -= 1
            raise RuntimeError("no audio")


def test_portaudio_is_closed_and_started_again():
    sd = _PortAudio()
    assert refresh_portaudio_devices(sd) is True
    assert sd.calls == ["terminate", "initialize"]


def test_portaudio_that_would_not_close_is_left_running():
    sd = _PortAudio(fail_terminate=True)
    assert refresh_portaudio_devices(sd) is False
    assert sd.calls == ["terminate"]


def test_portaudio_that_failed_to_start_again_gets_one_more_try():
    sd = _PortAudio(fail_initialize=1)
    assert refresh_portaudio_devices(sd) is False
    assert sd.calls == ["terminate", "initialize", "initialize"]


def test_no_portaudio_no_refresh():
    assert refresh_portaudio_devices(None) is False


def test_the_defaults_are_unknown_off_windows(monkeypatch):
    monkeypatch.setattr(endpoints.sys, "platform", "darwin")
    assert endpoints.default_endpoint_ids() == (None, None)


# -- DefaultAudioDevicesMixin (unbound methods on a plain stub) --------------

class _Window:
    _call_devices_following_default = DefaultAudioDevicesMixin._call_devices_following_default
    _on_call_default_device_changed = DefaultAudioDevicesMixin._on_call_default_device_changed
    _stop_call_default_device_follow = DefaultAudioDevicesMixin._stop_call_default_device_follow

    def __init__(self, output="", input_="", active=True, started=True):
        self.devices = {"output": output, "input": input_}
        self._active_voice_call = {"identity": "CALL1"} if active else None
        self.restarts = []
        self.started = started

    def call_audio_device(self, kind):
        return self.devices[kind]

    def _restart_active_voice_call_audio(self, *, refresh_device_list=False):
        self.restarts.append(refresh_device_list)
        return self.started


def test_a_call_device_with_no_name_follows_the_default():
    assert _Window()._call_devices_following_default() == (True, True)
    assert _Window(output="Fone de ouvido")._call_devices_following_default() == (False, True)


def test_a_default_change_reopens_the_call_with_a_fresh_device_list():
    window = _Window()
    assert window._on_call_default_device_changed() is True
    assert window.restarts == [True]


def test_a_switch_already_running_asks_the_watcher_to_offer_the_change_again():
    window = _Window(started=False)
    assert window._on_call_default_device_changed() is False


def test_after_the_call_nothing_is_reopened():
    window = _Window(active=False)
    assert window._on_call_default_device_changed() is True
    assert window.restarts == []


def test_stopping_the_follow_stops_the_watcher():
    stopped = []

    class _Watcher:
        def stop(self):
            stopped.append(True)
    window = _Window()
    window._call_default_device_watcher = _Watcher()
    window._stop_call_default_device_follow()
    assert stopped == [True]
    assert window._call_default_device_watcher is None


# -- program sounds: the mixin side ------------------------------------------

class _SoundsWindow:
    _on_default_output_changed = DefaultAudioDevicesMixin._on_default_output_changed
    _follow_default_output = DefaultAudioDevicesMixin._follow_default_output
    _voice_playback_active = DefaultAudioDevicesMixin._voice_playback_active

    def __init__(self, shutting_down=False):
        self._shutting_down = shutting_down
        self.moves = 0
        window = self

        class _Sounds:
            def follow_default_output(self):
                window.moves += 1
                return True
        self.sound_system = _Sounds()


def test_the_sounds_are_moved_on_the_ui_thread(monkeypatch):
    import main_window.default_audio_devices as module
    queued = []
    monkeypatch.setattr(module.wx, "CallAfter", lambda fn, *a: queued.append(fn))
    window = _SoundsWindow()

    assert window._on_default_output_changed() is True
    assert window.moves == 0          # not on the watcher's thread
    queued[0]()
    assert window.moves == 1


def test_nothing_is_moved_while_shutting_down(monkeypatch):
    import main_window.default_audio_devices as module
    queued = []
    monkeypatch.setattr(module.wx, "CallAfter", lambda fn, *a: queued.append(fn))
    window = _SoundsWindow(shutting_down=True)

    window._on_default_output_changed()
    window._follow_default_output()
    assert queued == [] and window.moves == 0


class _Panel:
    def __init__(self, playing=False, stream=object()):
        self._is_audio_playing = playing
        self._audio_stream = stream


class _PlaybackWindow:
    _voice_playback_active = DefaultAudioDevicesMixin._voice_playback_active
    _on_default_output_changed = DefaultAudioDevicesMixin._on_default_output_changed

    def __init__(self, **panels):
        self._shutting_down = False
        for name, panel in panels.items():
            setattr(self, name, panel)
        self.followed = []

    def _follow_default_output(self):
        self.followed.append(True)


def test_the_sounds_move_when_no_voice_message_plays(monkeypatch):
    monkeypatch.setattr("main_window.default_audio_devices.wx.CallAfter",
                        lambda fn, *a: fn(*a))
    window = _PlaybackWindow(conversations_panel=_Panel())
    assert window._on_default_output_changed() is True
    assert window.followed == [True]


def test_a_playing_voice_message_defers_the_move_to_the_next_reading(monkeypatch):
    monkeypatch.setattr("main_window.default_audio_devices.wx.CallAfter",
                        lambda fn, *a: fn(*a))
    window = _PlaybackWindow(archived_conversations_panel=_Panel(playing=True))
    assert window._on_default_output_changed() is False
    assert window.followed == []


def test_the_status_voice_preview_defers_the_move_too():
    class Sound:
        is_playing = True
    window = _PlaybackWindow(status_panel=type("S", (), {"_recorded_audio_sound": Sound()})())
    assert window._voice_playback_active() is True


def test_nothing_is_read_while_both_devices_are_chosen_by_name():
    reads = []

    def reader():
        reads.append(True)
        return (SPEAKERS, MIC)
    follows = [False, False]
    watcher = DefaultEndpointWatcher(lambda: True, lambda: tuple(follows), reader=reader)
    assert watcher.poll() is False and watcher.poll() is False
    assert reads == []


def test_following_again_takes_a_new_baseline_instead_of_moving():
    follows = [False, False]
    watcher = DefaultEndpointWatcher(lambda: True, lambda: tuple(follows),
                                     reader=_Readings((HEADSET, MIC)))
    watcher.poll()
    follows[0] = True
    assert watcher.poll() is False      # the headset is the baseline, not news


def test_the_sounds_watcher_has_its_own_thread_name_and_log_tag():
    watcher = DefaultEndpointWatcher(lambda: True, lambda: (True, False),
                                     reader=_Readings((SPEAKERS, MIC)),
                                     name="WinZappSoundsDefaultDevice", tag="sound_system")
    watcher.start()
    try:
        assert watcher._thread.name == "WinZappSoundsDefaultDevice"
    finally:
        watcher.stop()


def test_stopping_the_sounds_follow_stops_the_watcher():
    stopped = []

    class W:
        stop = staticmethod(lambda: stopped.append(True))
    window = _PlaybackWindow()
    window._stop_default_output_follow = DefaultAudioDevicesMixin._stop_default_output_follow.__get__(window)
    window._default_output_watcher = W()
    window._stop_default_output_follow()
    assert stopped == [True] and window._default_output_watcher is None
