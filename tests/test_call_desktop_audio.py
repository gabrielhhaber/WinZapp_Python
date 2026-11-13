import base64
from types import SimpleNamespace

import numpy as np

from core import call_desktop_audio
from core.call_audio import CallAudioConfig, CallAudioSession
from core.desktop_audio_sources import DesktopAudioApp, app_render_roots, shareable_processes
from core.wasapi_capture import WasapiError
from main_window.call_desktop_audio import DesktopCallAudioMixin


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
    import pytest

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


def test_replacing_the_call_while_the_source_picker_is_open_cancels_start(monkeypatch):
    from main_window import call_desktop_audio as ui

    monkeypatch.setattr(ui, "desktop_audio_supported", lambda: True)
    audio = SimpleNamespace(stop_desktop_audio=lambda: False)
    stub = SimpleNamespace(_active_voice_call={"is_video": False}, _call_audio_session=audio,
                           _voice_call_last_announced_state="ACTIVE",
                           i18n=SimpleNamespace(t=str))

    def choose():
        stub._active_voice_call = {"is_video": False}
        return True, False, None, 200

    stub._choose_call_desktop_audio = choose
    DesktopCallAudioMixin._toggle_call_desktop_audio(stub)
