import base64
from types import SimpleNamespace

import numpy as np
import pytest

from core import call_desktop_audio, desktop_audio_sources, wasapi_process_capture
from core.call_audio import CallAudioConfig, CallAudioSession
from core.desktop_audio_sources import DesktopAudioApp, app_render_roots, shareable_processes
from core.system_audio_capture import TimelineMixer
from core.wasapi_capture import WasapiError
from main_window import call_desktop_audio as ui
from main_window.call_desktop_audio import DesktopCallAudioMixin
from main_window.calls import CallsMixin


def pcm(*samples):
    return np.array(samples, dtype="<i2").tobytes()


def test_system_capture_excludes_the_call_nvda_and_their_ancestors():
    rows = [(1, 0, "launcher.exe"), (2, 1, "WinZapp.exe"),
            (3, 2, "browser.exe"), (4, 1, "NVDA.EXE"), (5, 4, "voice.exe"),
            (6, 1, "media.exe"), (7, 6, "audio.exe"), (8, 1, "music.exe")]
    assert shareable_processes(rows, {1, 2, 3, 4, 5, 6, 7, 8, 999}, 2) == {6, 8}


def test_mix_clips_and_zero_fills_missing_desktop_audio():
    assert call_desktop_audio.mix_desktop_pcm(pcm(30000, -30000, 20), pcm(5000, -5000)) == pcm(32767, -32767, 20)
    assert call_desktop_audio.mix_desktop_pcm(pcm(5), pcm(1, 2)) == pcm(6)
    assert call_desktop_audio.mix_desktop_pcm(pcm(0, 0), pcm(1000, -1000), 200) == pcm(2000, -2000)
    assert call_desktop_audio.mix_desktop_pcm(pcm(31000, 10), pcm(20000, 2000), 400) == pcm(32767, 187)


def test_picker_groups_a_browser_renderer_and_excludes_the_call_and_nvda():
    rows = [(1, 0, "WinZapp.exe"), (2, 0, "Browser.exe"), (3, 2, "browser.exe"),
            (4, 0, "NVDA.exe"), (5, 4, "speech.exe"), (6, 0, "Player.exe")]
    assert app_render_roots(rows, {1, 3, 5, 6}, 1) == {2, 6}


def test_selected_application_cannot_capture_reused_pid(monkeypatch):
    app = DesktopAudioApp(12, 100, "player.exe")
    worker = call_desktop_audio.CallDesktopAudio(False, False, None, app=app)
    target = SimpleNamespace(creation_time=101, release=lambda: None)
    api = SimpleNamespace(process_tree=lambda: [(12, 0, "player.exe")])
    monkeypatch.setattr(call_desktop_audio, "pin_process", lambda *_: target)
    with pytest.raises(WasapiError, match="Selected audio application changed"):
        worker._open_selected_app(api)


def test_stop_discards_queued_and_partial_desktop_frames():
    worker = call_desktop_audio.CallDesktopAudio(True, False, None, volume=100)
    worker._offer(pcm(1, 2))
    assert worker.take(2) == pcm(1)
    worker._offer(pcm(3, 4))
    worker.stop()
    assert worker.take(6) == b""


def test_protected_render_process_does_not_stop_sharing_other_apps(monkeypatch):
    api = SimpleNamespace(process_tree=lambda: [
        (11, 0, "svchost.exe"), (12, 0, "player.exe")
    ])
    target = SimpleNamespace(acquire=lambda: None, release=lambda: None)
    stream = object()
    worker = call_desktop_audio.CallDesktopAudio(True, False, None)
    monkeypatch.setattr(call_desktop_audio, "active_render_pids", lambda _: {11, 12})
    monkeypatch.setattr(call_desktop_audio.os, "getpid", lambda: 1)

    def pin(_api, pid):
        if pid == 11:
            raise WasapiError("Cannot open audio process", hresult=0x80070005)
        return target

    monkeypatch.setattr(call_desktop_audio, "pin_process", pin)
    monkeypatch.setattr(worker, "_open", lambda _: stream)
    streams = {}
    worker._refresh_system(None, api, streams, None)
    assert streams == {12: (2, stream)}


def test_a_source_that_fails_to_close_does_not_end_the_share(monkeypatch):
    class _Stream:
        def close(self):
            raise OSError("device gone")

    worker = call_desktop_audio.CallDesktopAudio(True, False, None)
    mixer = TimelineMixer(48_000, 1, 0, source_count=3)
    monkeypatch.setattr(call_desktop_audio, "active_render_pids", lambda _: set())
    monkeypatch.setattr(call_desktop_audio.os, "getpid", lambda: 1)
    streams = {12: (1, _Stream())}
    worker._refresh_system(None, SimpleNamespace(process_tree=lambda: []), streams, mixer)
    assert streams == {}


def test_call_sender_sends_only_current_desktop_worker_and_preserves_epoch():
    sent = []
    sio = SimpleNamespace(emit=lambda event, data: sent.append((event, data)))
    audio = CallAudioSession(sio, CallAudioConfig(session="test"))
    worker = call_desktop_audio.CallDesktopAudio(True, False, None)
    audio._desktop_audio = worker
    worker._offer(pcm(6))
    audio._emit_microphone_frame(pcm(0), "stale")
    assert not sent
    audio._emit_microphone_frame(pcm(0), "")
    assert sent[0][0] == "call:audio:mic"
    assert base64.b64decode(sent[0][1]["pcm"]) == pcm(12)
    assert audio.stop_desktop_audio(expected=worker)
    audio._emit_microphone_frame(pcm(2), "")
    assert base64.b64decode(sent[1][1]["pcm"]) == pcm(2)


class _Audio:
    """CallAudioSession's desktop-share surface."""
    running = True

    def __init__(self):
        self.desktop_audio = None
        self.started = []
        self.on_state = None
        self.result = True

    def start_desktop_audio(self, system, nvda, on_state, *, app=None, volume=200):
        self.started.append((system, nvda, app, volume))
        self.on_state = on_state
        if self.result:
            self.desktop_audio = object()
        return self.result

    def stop_desktop_audio(self, expected=None):
        if self.desktop_audio is None or (expected is not None and expected is not self.desktop_audio):
            return False
        self.desktop_audio = None
        return True


class _Button:
    def __init__(self, shown=False, focused=False):
        self.shown, self.focused, self.label = shown, focused, None

    def IsShown(self):
        return self.shown

    def HasFocus(self):
        return self.focused

    def Show(self, show=True):
        self.shown = show

    def SetLabel(self, label):
        self.label = label

    def SetFocus(self):
        self.focused = True


class _Window(DesktopCallAudioMixin):
    def __init__(self):
        self._call_audio_session = _Audio()
        self._active_voice_call = {"is_video": False}
        self._voice_call_last_announced_state = "ACTIVE"
        self.i18n = SimpleNamespace(t=str)
        self.voice_call_window_desktop_audio_button = _Button()
        self.voice_call_window_mute_button = _Button()
        self.spoken = []
        self.synced = 0
        self.choice = (True, False, None, 150)
        self.picked = []

    def _sync_voice_call_bar(self):
        self.synced += 1

    def output(self, text, interrupt=False):
        self.spoken.append(text)

    def _choose_call_desktop_audio(self, apps):
        self.picked.append(apps)
        return self.choice


class _HeldThread:
    """Records the worker instead of running it, so a test runs it explicitly."""
    made = []

    def __init__(self, target=None, args=(), **_kw):
        self.target, self.args = target, args
        _HeldThread.made.append(self)

    def start(self):
        pass

    def run(self):
        self.target(*self.args)


@pytest.fixture
def share_ui(monkeypatch):
    _HeldThread.made = []
    monkeypatch.setattr(ui, "desktop_audio_supported", lambda: True)
    # Module-local stand-ins: the real threading.Thread and wx.CallAfter of
    # the rest of the process stay untouched.
    monkeypatch.setattr(ui, "threading", SimpleNamespace(Thread=_HeldThread))
    monkeypatch.setattr(ui, "wx", SimpleNamespace(CallAfter=lambda fn, *a, **k: fn(*a, **k)))
    return _Window()


def test_share_button_offered_only_on_a_connected_running_voice_call():
    voice = {"is_video": False}
    assert call_desktop_audio.share_button_visible(True, voice, True, False, "ACTIVE")
    assert not call_desktop_audio.share_button_visible(True, voice, True, False, "RINGING")
    # a share that is running keeps its Stop button whatever the state says
    assert call_desktop_audio.share_button_visible(True, voice, True, True, "RECONNECTING")
    assert not call_desktop_audio.share_button_visible(True, {"is_video": True}, True, True, "ACTIVE")
    assert not call_desktop_audio.share_button_visible(True, voice, False, False, "ACTIVE")
    assert not call_desktop_audio.share_button_visible(False, voice, True, False, "ACTIVE")
    assert not call_desktop_audio.share_button_visible(True, None, True, False, "ACTIVE")


def test_share_button_label_follows_the_share_and_hiding_moves_focus_to_mute(share_ui):
    window = share_ui
    button = window.voice_call_window_desktop_audio_button
    window._sync_call_desktop_audio_button(window._active_voice_call)
    assert button.shown and button.label == "call_desktop_share"

    window._call_audio_session.desktop_audio = object()
    window._sync_call_desktop_audio_button(window._active_voice_call)
    assert button.label == "call_desktop_stop"

    window._call_audio_session.desktop_audio = None
    window._voice_call_last_announced_state = "RECONNECTING"
    button.focused = True
    window._sync_call_desktop_audio_button(window._active_voice_call)
    assert not button.shown
    assert window.voice_call_window_mute_button.focused


def test_application_list_is_read_on_a_worker_thread_then_the_share_starts(share_ui, monkeypatch):
    """WasapiBackend asks COM for the multithreaded apartment; the wx UI
    thread is already single-threaded, so listing there always failed."""
    window = share_ui
    app = DesktopAudioApp(12, 100, "player.exe")
    monkeypatch.setattr(ui, "load_audio_apps", lambda: [app])
    window.choice = (False, True, app, 120)

    window._toggle_call_desktop_audio()

    assert window.picked == []  # nothing listed or shown on the UI thread yet
    (thread,) = _HeldThread.made
    assert thread.target == window._load_call_desktop_audio_apps
    thread.run()
    assert window.picked == [[app]]
    assert window._call_desktop_apps_loading is False
    audio = window._call_audio_session
    assert audio.started == [(False, True, app, 120)]
    assert window.spoken == []
    audio.on_state(audio.desktop_audio, True)
    assert window.spoken == ["call_desktop_started"]


def test_a_second_press_while_the_list_loads_opens_one_picker(share_ui):
    window = share_ui
    window._toggle_call_desktop_audio()
    window._toggle_call_desktop_audio()
    assert len(_HeldThread.made) == 1


def test_application_list_failure_still_opens_the_picker(share_ui, monkeypatch):
    window = share_ui

    def boom():
        raise ValueError("bad row")

    monkeypatch.setattr(ui, "load_audio_apps", boom)
    window._toggle_call_desktop_audio()
    _HeldThread.made[0].run()
    assert window.picked == [[]]


def test_toggle_while_sharing_stops_and_says_so(share_ui):
    window = share_ui
    window._call_audio_session.desktop_audio = object()
    window._toggle_call_desktop_audio()
    assert window._call_audio_session.desktop_audio is None
    assert window.spoken == ["call_desktop_stopped"]
    assert _HeldThread.made == []


def test_replacing_the_call_while_the_source_picker_is_open_cancels_start(share_ui):
    window = share_ui
    audio = window._call_audio_session

    def choose(apps):
        window._active_voice_call = {"is_video": False}
        return True, False, None, 200

    window._choose_call_desktop_audio = choose
    window._pick_call_desktop_audio(window._active_voice_call, audio, [])
    assert audio.started == []
    assert window.spoken == []


def test_a_call_that_ended_while_the_list_loaded_opens_no_picker(share_ui):
    window = share_ui
    call, audio = window._active_voice_call, window._call_audio_session
    window._active_voice_call = None
    window._pick_call_desktop_audio(call, audio, [])
    assert window.picked == []
    assert window._call_desktop_apps_loading is False


def test_picker_cancelled_or_empty_choice_starts_nothing(share_ui):
    window = share_ui
    audio = window._call_audio_session
    for choice in (None, (False, False, None, 200)):
        window.choice = choice
        window._pick_call_desktop_audio(window._active_voice_call, audio, [])
    assert audio.started == []


def test_a_share_that_does_not_start_is_spoken(share_ui):
    window = share_ui
    window._call_audio_session.result = False
    window._pick_call_desktop_audio(window._active_voice_call, window._call_audio_session, [])
    assert window.spoken == ["call_desktop_failed"]


def test_capture_failure_stops_the_share_and_a_stale_worker_is_ignored(share_ui):
    window = share_ui
    audio = window._call_audio_session
    window._pick_call_desktop_audio(window._active_voice_call, audio, [])
    worker = audio.desktop_audio

    window._on_call_desktop_audio(audio, object(), False)  # an older worker
    assert audio.desktop_audio is worker and window.spoken == []

    window._on_call_desktop_audio(audio, worker, False)
    assert audio.desktop_audio is None
    assert window.spoken == ["call_desktop_failed"]


def test_device_switch_shares_again_quietly_on_the_new_session(share_ui):
    window = share_ui
    settings = (True, True, None, 300)
    window._resume_call_desktop_audio(window._active_voice_call, settings)
    audio = window._call_audio_session
    assert audio.started == [settings]
    audio.on_state(audio.desktop_audio, True)
    assert window.spoken == []
    audio.on_state(audio.desktop_audio, False)
    assert window.spoken == ["call_desktop_failed"]


def test_device_switch_does_not_share_into_another_call(share_ui):
    window = share_ui
    window._resume_call_desktop_audio({"is_video": False}, (True, False, None, 200))
    assert window._call_audio_session.started == []
    assert window.spoken == []


def test_device_switch_while_reconnecting_says_the_share_stopped(share_ui):
    window = share_ui
    window._voice_call_last_announced_state = "REJOINING"
    window._resume_call_desktop_audio(window._active_voice_call, (True, False, None, 200))
    assert window._call_audio_session.started == []
    assert window.spoken == ["call_desktop_stopped"]


def test_share_settings_and_stop_helpers_tolerate_no_session():
    assert ui.stop_call_desktop_audio(SimpleNamespace()) is False
    assert ui.call_desktop_share_settings(SimpleNamespace()) is None
    worker = call_desktop_audio.CallDesktopAudio(False, True, None, volume=90)
    window = SimpleNamespace(_call_audio_session=SimpleNamespace(desktop_audio=worker))
    assert ui.call_desktop_share_settings(window) == (False, True, None, 90)


def test_upgrade_to_video_stops_the_share_and_says_so_after_the_upgrade():
    audio = _Audio()
    audio.desktop_audio = object()
    spoken = []
    window = SimpleNamespace(
        _call_audio_session=audio, _call_camera_capture=object(),
        i18n=SimpleNamespace(t=str), _sync_voice_call_bar=lambda: None,
        output=lambda text, interrupt=False: spoken.append((text, interrupt)),
    )
    CallsMixin._on_call_upgraded_to_video(window)
    assert audio.desktop_audio is None
    assert spoken == [("voice_call_upgraded_to_video", True), ("call_desktop_stopped", False)]


def test_hanging_up_stops_the_share_before_the_end_request(monkeypatch):
    from main_window import calls as calls_module

    monkeypatch.setattr(calls_module, "threading", SimpleNamespace(Thread=_HeldThread))
    audio = _Audio()
    audio.desktop_audio = object()
    window = SimpleNamespace(_call_audio_session=audio, _active_voice_call={"call_id": "C1"})
    CallsMixin.end_active_call(window)
    assert audio.desktop_audio is None


def test_stopping_call_audio_for_a_device_switch_stops_the_share():
    audio = _Audio()
    audio.desktop_audio = object()
    audio.stop = lambda **_kw: None
    window = SimpleNamespace(_call_audio_session=audio, _call_audio_stop_timer=None)
    CallsMixin._stop_voice_call_audio(window, keep_call=True)
    assert audio.desktop_audio is None
    assert window._call_audio_session is None


def test_mute_while_sharing_says_the_share_goes_on():
    audio = SimpleNamespace(microphone_muted=False, desktop_audio=object())
    audio.set_microphone_muted = lambda muted: setattr(audio, "microphone_muted", muted)
    spoken = []
    window = SimpleNamespace(
        _call_audio_session=audio, i18n=SimpleNamespace(t=str),
        voice_call_window_mute_button=_Button(),
        output=lambda text, interrupt=False: spoken.append(text),
    )
    CallsMixin.toggle_call_microphone(window)
    CallsMixin.toggle_call_microphone(window)
    assert spoken == ["voice_call_microphone_muted_sharing", "voice_call_microphone_unmuted"]


def test_session_rolls_back_a_desktop_worker_that_cannot_start(monkeypatch):
    audio = CallAudioSession(SimpleNamespace(emit=lambda *_: None), CallAudioConfig(session="test"))
    audio._input_stream = object()  # the microphone is open: the session runs

    def refuse(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(call_desktop_audio.CallDesktopAudio, "start", refuse)
    with pytest.raises(RuntimeError):
        audio.start_desktop_audio(True, False, None)
    assert audio.desktop_audio is None


def test_session_refuses_a_second_share_and_a_session_without_microphone():
    audio = CallAudioSession(SimpleNamespace(emit=lambda *_: None), CallAudioConfig(session="test"))
    assert audio.start_desktop_audio(True, False, None) is False
    audio._input_stream = object()
    current = audio._desktop_audio = object()
    assert audio.start_desktop_audio(True, False, None) is False
    assert audio.desktop_audio is current


def test_mixer_reset_source_forgets_the_previous_process():
    mixer = TimelineMixer(48_000, 1, 0, source_count=3)
    mixer._pending[2] = mixer._last_stamp[2] = mixer._last_end[2] = 7
    mixer._last_end[1] = 5
    mixer.reset_source(2)
    assert (mixer._pending[2], mixer._last_stamp[2], mixer._last_end[2]) == (None, None, None)
    assert mixer._last_end[1] == 5


def test_list_audio_apps_offers_this_session_roots_by_name(monkeypatch):
    rows = [(1, 0, "WinZapp.exe"), (2, 0, "zplayer.exe"), (3, 0, "Browser.exe"),
            (4, 3, "browser.exe"), (5, 0, "other-user.exe"), (6, 0, "gone.exe")]
    sessions = {2: 1, 3: 1, 5: 2}
    api = SimpleNamespace(process_tree=lambda: rows, current_session=lambda: 1,
                          session=lambda pid: sessions[pid])
    monkeypatch.setattr(desktop_audio_sources, "_ProcessAPI", lambda: api)
    monkeypatch.setattr(desktop_audio_sources, "active_render_pids",
                        lambda _backend, include_idle: {1, 2, 4, 5, 6})
    monkeypatch.setattr(desktop_audio_sources.os, "getpid", lambda: 1)
    released = []

    def pin(_api, pid):
        if pid == 6:
            raise WasapiError("exited")
        return SimpleNamespace(creation_time=pid * 10, release=lambda: released.append(pid))

    monkeypatch.setattr(desktop_audio_sources, "pin_process", pin)
    apps = desktop_audio_sources.list_audio_apps(object())
    assert apps == [DesktopAudioApp(3, 30, "Browser.exe"), DesktopAudioApp(2, 20, "zplayer.exe")]
    assert sorted(released) == [2, 3, 5]


def test_load_audio_apps_closes_its_backend_and_never_raises(monkeypatch):
    closed = []
    backend = SimpleNamespace(close=lambda: closed.append(True))
    monkeypatch.setattr(desktop_audio_sources, "list_audio_apps", lambda _b: ["app"])
    assert desktop_audio_sources.load_audio_apps(lambda: backend) == ["app"]

    def refuse(_b):
        raise WasapiError("RPC_E_CHANGED_MODE", hresult=0x80010106)

    monkeypatch.setattr(desktop_audio_sources, "list_audio_apps", refuse)
    assert desktop_audio_sources.load_audio_apps(lambda: backend) == []
    assert closed == [True, True]

    def no_com():
        raise OSError("ole32 missing")

    assert desktop_audio_sources.load_audio_apps(no_com) == []


def test_open_process_reports_the_windows_error_as_an_hresult(monkeypatch):
    api = object.__new__(wasapi_process_capture._ProcessAPI)
    api.k = SimpleNamespace(OpenProcess=lambda *_: 0)
    monkeypatch.setattr(wasapi_process_capture.C, "get_last_error", lambda: 5)
    with pytest.raises(WasapiError) as error:
        api.open(42)
    assert error.value.hresult == call_desktop_audio.PROCESS_ACCESS_DENIED
    assert error.value.operation == "OpenProcess"
