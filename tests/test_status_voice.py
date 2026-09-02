"""Tests for StatusPanel's voice-status feature (client/status_panel.py):
recording/posting a voice status, and picking an existing audio file from
the status media chooser.

Covers three bugs found while adapting this feature:

1. _send_media_status_bg() (image/video status) posted its base64 payload
   under a "base64" key. statusController.ts's real, unmodified upstream
   sendImageStorie()/sendVideoStorie() (`const { path } = req.body`) only
   ever reads a "path" field — a data:...;base64,... URI works there too
   (sender.layer.js's sendImageStatus()/sendVideoStatus() special-case a
   "data:" prefix) — but under the WRONG key it was simply never read,
   so every status image/video post from the media picker silently failed.

2. _send_all_media_statuses_bg() must route a picked AUDIO file (.mp3/
   .ogg/.wav/.m4a/.aac) to the real voice-status path (_send_status_voice_bg,
   which posts to /send-status-voice-base64) instead of the image/video
   endpoint, which has no audio branch at all and would just error.

3. _send_status_voice_bg() converts its input to OGG via
   main_window._convert_wav_to_ogg() and then deletes the *original* file
   — safe for a temp WAV this module recorded itself, but for a picked
   audio FILE that "original" is the user's own file on disk (e.g. an MP3
   from their music folder). Deleting it unconditionally would silently
   destroy a file the user never asked to have removed. is_temp_file=True
   is required explicitly by the recording call site; the media-picker
   call site must never pass it.

StatusPanel is a wx.Panel and cannot be instantiated without a running
wx.App, so methods are bound onto a small stub — same approach as the rest
of this test suite (test_status_panel.py).
"""

import os
import types

import status_panel as status_panel_module
from status_panel import StatusPanel


class _FakeI18n:
    def t(self, key):
        return key


class _FakeWidget:
    def __init__(self):
        self.shown = False
        self.enabled = True

    def Show(self, show=True):
        self.shown = bool(show)

    def Hide(self):
        self.shown = False

    def IsShown(self):
        return self.shown

    def Enable(self, enable=True):
        self.enabled = bool(enable)

    def Disable(self):
        self.enabled = False


class _FakeVideoPlayer:
    """Opening a post panel stops any status video that was playing."""

    def __init__(self):
        self.stop_calls = 0

    def stop(self):
        self.stop_calls += 1


class _FakeLabel(_FakeWidget):
    def __init__(self):
        super().__init__()
        self.label = ""

    def SetLabel(self, text):
        self.label = text


class _FakeButton(_FakeLabel):
    def __init__(self):
        super().__init__()
        self.enabled = True

    def Enable(self, enable=True):
        self.enabled = bool(enable)

    def Disable(self):
        self.enabled = False

    def SetFocus(self):
        pass


class _FakeTimer:
    def __init__(self):
        self.started = []
        self.stop_calls = 0

    def Start(self, milliseconds):
        self.started.append(milliseconds)

    def Stop(self):
        self.stop_calls += 1


class _FakeMainWindow:
    def __init__(self, convert_result="converted.ogg"):
        self.i18n = _FakeI18n()
        self.settings = {}
        self.app_name = "WinZapp"
        self.wpp_server = "http://127.0.0.1"
        self.wpp_port = 6300
        self.token = "test-token"
        self.output_calls = []
        self._convert_result = convert_result
        self.convert_calls = []

    def output(self, text, interrupt=False):
        self.output_calls.append(text)

    def _convert_wav_to_ogg(self, path):
        self.convert_calls.append(path)
        return self._convert_result


class _Stub:
    _on_choose_voice_status   = StatusPanel._on_choose_voice_status
    _on_ctrl_r_shortcut       = StatusPanel._on_ctrl_r_shortcut
    _hide_post_panels         = StatusPanel._hide_post_panels
    _is_status_composer_open  = StatusPanel._is_status_composer_open
    _enter_status_composer    = StatusPanel._enter_status_composer
    _leave_status_composer    = StatusPanel._leave_status_composer
    _start_voice_recording    = StatusPanel._start_voice_recording
    _send_media_status_bg     = StatusPanel._send_media_status_bg
    _send_all_media_statuses_bg = StatusPanel._send_all_media_statuses_bg
    _send_status_voice_bg     = StatusPanel._send_status_voice_bg
    _stop_recorded_audio_preview = StatusPanel._stop_recorded_audio_preview
    _cleanup_recorded_audio_temp_file = StatusPanel._cleanup_recorded_audio_temp_file
    _silence_send_voice_focus_if_enabled = StatusPanel._silence_send_voice_focus_if_enabled
    _voice_recording_silence_enabled = StatusPanel._voice_recording_silence_enabled
    _focus_recording_button_silently = StatusPanel._focus_recording_button_silently

    def __init__(self, convert_result="converted.ogg"):
        self.main_window = _FakeMainWindow(convert_result=convert_result)
        self._post_panel = _FakeWidget()
        self._media_post_panel = _FakeWidget()
        self._voice_post_panel = _FakeWidget()
        self._add_status_btn = _FakeWidget()
        self._refresh_status_btn = _FakeWidget()
        self._list_label = _FakeWidget()
        self._status_list = _FakeButton()
        for widget in (
            self._add_status_btn,
            self._refresh_status_btn,
            self._list_label,
            self._status_list,
        ):
            widget.Show()
        # Opening a post panel now also hides the status viewer and stops the
        # in-app video player alongside the other panels; without these the
        # method under test dies on an AttributeError before reaching anything
        # this file actually asserts.
        self._viewer_panel = _FakeWidget()
        self._video_player = _FakeVideoPlayer()
        # The panel is now prepared (buttons/label reset, any previous stream
        # torn down) BEFORE the PyAudio availability check, so the stub has to
        # survive that setup to reach the behaviour this class asserts.
        self._recording_paused = False
        self._is_recording = False
        self._voice_status_lbl = _FakeLabel()
        self._voice_start_btn = _FakeButton()
        self._voice_pause_btn = _FakeButton()
        self._voice_play_btn = _FakeButton()
        self._voice_send_btn = _FakeButton()
        self._voice_close_btn = _FakeButton()
        self._recording_pa = None
        self._recording_stream = None
        self._recording_frames = []
        self._recording_rate = 48000
        self._recording_channels = 1
        self._recorded_audio_sound = None
        self._recorded_audio_temp_path = None
        self._recorded_audio_timer = _FakeTimer()
        self.status_sent_calls = 0
        self.voice_calls = []
        self.media_calls = []

    def _stop_recording_stream(self):
        self._recording_stream = None
        self._recording_pa = None

    def Layout(self):
        """StatusPanel is a wx.Panel; the prepare step relayouts it."""

    def _on_status_sent(self):
        self.status_sent_calls += 1

    # Recorded so tests can assert routing without exercising the real
    # network/ffmpeg call inside each method.
    def _send_status_voice_bg_recorder(self, path, is_temp_file=False):
        self.voice_calls.append((path, is_temp_file))


def _touch(tmp_path, name):
    p = tmp_path / name
    p.write_bytes(b"fake-data")
    return str(p)


class TestSendMediaStatusUsesThePathField:
    def test_image_status_posts_under_path_not_base64(self, tmp_path, monkeypatch):
        stub = _Stub()
        img = _touch(tmp_path, "photo.jpg")
        posted = {}

        class _Resp:
            status_code = 200
            text = ""

        def fake_post(url, json=None, headers=None, timeout=None):
            posted["url"] = url
            posted["json"] = json
            return _Resp()

        monkeypatch.setattr(status_panel_module.requests, "post", fake_post)
        monkeypatch.setattr(status_panel_module.wx, "CallAfter", lambda fn, *a, **kw: fn(*a, **kw))

        stub._send_media_status_bg(img, "legenda")

        assert posted["url"].endswith("/send-image-storie")
        assert "path" in posted["json"]
        assert "base64" not in posted["json"]
        assert posted["json"]["path"].startswith("data:image/jpeg;base64,")
        assert posted["json"]["caption"] == "legenda"
        assert stub.status_sent_calls == 1

    def test_video_status_posts_under_path_not_base64(self, tmp_path, monkeypatch):
        stub = _Stub()
        vid = _touch(tmp_path, "clip.mp4")
        posted = {}

        class _Resp:
            status_code = 200
            text = ""

        def fake_post(url, json=None, headers=None, timeout=None):
            posted["url"] = url
            posted["json"] = json
            return _Resp()

        monkeypatch.setattr(status_panel_module.requests, "post", fake_post)
        monkeypatch.setattr(status_panel_module.wx, "CallAfter", lambda fn, *a, **kw: fn(*a, **kw))

        stub._send_media_status_bg(vid, "")

        assert posted["url"].endswith("/send-video-storie")
        assert "path" in posted["json"]
        assert "base64" not in posted["json"]

    def test_unsupported_extension_reports_the_error(self, tmp_path, monkeypatch):
        stub = _Stub()
        doc = _touch(tmp_path, "file.pdf")
        monkeypatch.setattr(
            status_panel_module.wx, "MessageBox",
            lambda *a, **kw: None,
        )
        monkeypatch.setattr(status_panel_module.wx, "CallAfter", lambda fn, *a, **kw: fn(*a, **kw))

        stub._send_media_status_bg(doc, "")

        assert stub.status_sent_calls == 0


class TestAudioFilesAreRoutedToTheVoiceStatusPath:
    def test_audio_extension_calls_send_status_voice_bg_not_media(self, tmp_path, monkeypatch):
        stub = _Stub()
        audio = _touch(tmp_path, "note.mp3")
        calls = []
        monkeypatch.setattr(stub, "_send_status_voice_bg", lambda path, **kw: calls.append(("voice", path)) or True)
        monkeypatch.setattr(stub, "_send_media_status_bg", lambda path, caption, **kw: calls.append(("media", path)) or True)

        stub._send_all_media_statuses_bg([audio], "ignored caption")

        assert calls == [("voice", audio)]

    def test_non_audio_extension_still_goes_through_send_media_status_bg(self, tmp_path, monkeypatch):
        stub = _Stub()
        img = _touch(tmp_path, "pic.png")
        calls = []
        monkeypatch.setattr(stub, "_send_status_voice_bg", lambda path, **kw: calls.append(("voice", path)) or True)
        monkeypatch.setattr(stub, "_send_media_status_bg", lambda path, caption, **kw: calls.append(("media", path)) or True)

        stub._send_all_media_statuses_bg([img], "legenda")

        assert calls == [("media", img)]

    def test_mixed_selection_routes_each_file_independently(self, tmp_path, monkeypatch):
        stub = _Stub()
        img   = _touch(tmp_path, "pic.png")
        audio = _touch(tmp_path, "note.ogg")
        calls = []
        monkeypatch.setattr(stub, "_send_status_voice_bg", lambda path, **kw: calls.append(("voice", path)) or True)
        monkeypatch.setattr(stub, "_send_media_status_bg", lambda path, caption, **kw: calls.append(("media", path)) or True)

        stub._send_all_media_statuses_bg([img, audio], "")

        assert calls == [("media", img), ("voice", audio)]

    def test_batch_failures_produce_a_single_aggregated_dialog(self, tmp_path, monkeypatch):
        """The exact bug: posting several files where more than one fails must
        show ONE summary MessageBox, not one blocking dialog per failure."""
        stub = _Stub()
        img1 = _touch(tmp_path, "pic1.png")
        img2 = _touch(tmp_path, "pic2.png")
        img3 = _touch(tmp_path, "pic3.png")
        boxes = []
        monkeypatch.setattr(status_panel_module.wx, "MessageBox",
                             lambda *a, **kw: boxes.append(a))
        monkeypatch.setattr(status_panel_module.wx, "CallAfter", lambda fn, *a, **kw: fn(*a, **kw))
        results = iter([True, False, False])
        monkeypatch.setattr(stub, "_send_media_status_bg", lambda path, caption, **kw: next(results))

        stub._send_all_media_statuses_bg([img1, img2, img3], "")

        assert len(boxes) == 1, "one popup per batch, not one per failed file"
        assert stub.status_sent_calls == 0  # not routed through the per-file success path in this test

    def test_batch_all_succeed_shows_no_dialog(self, tmp_path, monkeypatch):
        stub = _Stub()
        img1 = _touch(tmp_path, "pic1.png")
        img2 = _touch(tmp_path, "pic2.png")
        boxes = []
        monkeypatch.setattr(status_panel_module.wx, "MessageBox",
                             lambda *a, **kw: boxes.append(a))
        monkeypatch.setattr(status_panel_module.wx, "CallAfter", lambda fn, *a, **kw: fn(*a, **kw))
        monkeypatch.setattr(stub, "_send_media_status_bg", lambda path, caption, **kw: True)

        stub._send_all_media_statuses_bg([img1, img2], "")

        assert boxes == []


class TestSendStatusVoiceBgNeverDeletesAUserFileByAccident:
    def test_temp_file_is_deleted_after_conversion(self, tmp_path, monkeypatch):
        stub = _Stub()
        wav = _touch(tmp_path, "recorded.wav")
        ogg = _touch(tmp_path, "recorded.wav.ogg")
        stub.main_window._convert_result = ogg
        monkeypatch.setattr(status_panel_module.requests, "post",
                             lambda *a, **k: type("R", (), {"status_code": 200, "text": ""})())
        monkeypatch.setattr(status_panel_module.wx, "CallAfter", lambda fn, *a, **kw: fn(*a, **kw))

        stub._send_status_voice_bg(wav, is_temp_file=True)

        assert not os.path.exists(wav), "the temp WAV this module recorded must be cleaned up"

    def test_a_picked_user_file_is_never_deleted(self, tmp_path, monkeypatch):
        """The exact bug: is_temp_file defaults to False, so a picked audio
        file from the user's own disk survives the send."""
        stub = _Stub()
        picked = _touch(tmp_path, "my_song.mp3")
        ogg = _touch(tmp_path, "my_song.mp3.ogg")
        stub.main_window._convert_result = ogg
        monkeypatch.setattr(status_panel_module.requests, "post",
                             lambda *a, **k: type("R", (), {"status_code": 200, "text": ""})())
        monkeypatch.setattr(status_panel_module.wx, "CallAfter", lambda fn, *a, **kw: fn(*a, **kw))

        stub._send_status_voice_bg(picked)  # is_temp_file not passed -> False

        assert os.path.exists(picked), "picking a file from disk must never delete it"

    def test_posts_base64_ptt_to_the_status_voice_endpoint(self, tmp_path, monkeypatch):
        stub = _Stub()
        wav = _touch(tmp_path, "recorded.wav")
        ogg = _touch(tmp_path, "recorded.wav.ogg")
        stub.main_window._convert_result = ogg
        posted = {}

        class _Resp:
            status_code = 200
            text = ""

        def fake_post(url, json=None, headers=None, timeout=None):
            posted["url"] = url
            posted["json"] = json
            return _Resp()

        monkeypatch.setattr(status_panel_module.requests, "post", fake_post)
        monkeypatch.setattr(status_panel_module.wx, "CallAfter", lambda fn, *a, **kw: fn(*a, **kw))

        stub._send_status_voice_bg(wav, is_temp_file=True)

        assert posted["url"].endswith("/send-status-voice-base64")
        assert posted["json"]["base64Ptt"].startswith("data:audio/ogg;codecs=opus;base64,")
        assert stub.status_sent_calls == 1

    def test_conversion_failure_reports_the_error_and_does_not_post(self, tmp_path, monkeypatch):
        stub = _Stub(convert_result=None)  # ffmpeg missing/failed
        wav = _touch(tmp_path, "recorded.wav")
        posted = []
        monkeypatch.setattr(status_panel_module.requests, "post", lambda *a, **k: posted.append(1))
        monkeypatch.setattr(status_panel_module.wx, "MessageBox", lambda *a, **kw: None)
        monkeypatch.setattr(status_panel_module.wx, "CallAfter", lambda fn, *a, **kw: fn(*a, **kw))

        stub._send_status_voice_bg(wav, is_temp_file=True)

        assert posted == []
        assert stub.status_sent_calls == 0


class TestChooseVoiceStatusDegradesGracefullyWithoutPyaudio:
    """Same PyAudio-missing regression covered for ConversationsPanel in
    tests/test_voice_recording_unavailable.py, mirrored here since
    StatusPanel does its own independent PyAudio open."""

    def test_reports_unavailable_message_when_pyaudio_missing(self, monkeypatch):
        """The check moved with the behaviour: _on_choose_voice_status() now
        only *prepares* the panel ("NOT recording yet" — the user then presses
        Record or Ctrl+R), so the PyAudio availability test lives at the point
        recording actually starts. Asserting it on the open action instead
        tested a step that no longer decides anything."""
        monkeypatch.setattr(status_panel_module, "pyaudio", None)
        stub = _Stub()

        stub._start_voice_recording()

        assert stub.main_window.output_calls == ["voice_recording_unavailable"]
        assert stub._recording_stream is None

    def test_opening_the_panel_no_longer_reports_anything(self, monkeypatch):
        """Preparing the panel is silent even with no PyAudio — the user has
        not asked to record yet."""
        monkeypatch.setattr(status_panel_module, "pyaudio", None)
        stub = _Stub()

        stub._on_choose_voice_status(None)

        assert stub.main_window.output_calls == []
        assert stub._recording_stream is None

    def test_audio_composer_has_close_and_record_without_other_ui(self):
        stub = _Stub()

        stub._on_choose_voice_status(None)

        assert stub._voice_post_panel.shown is True
        assert stub._voice_post_panel.enabled is True
        assert stub._voice_close_btn.shown is True
        assert stub._voice_close_btn.label == "close"
        assert stub._voice_start_btn.shown is True
        assert stub._post_panel.enabled is False
        assert stub._media_post_panel.enabled is False

    def test_ctrl_r_does_not_open_audio_outside_the_audio_option(self):
        stub = _Stub()
        reached = []
        stub._start_voice_recording = lambda: reached.append(True)

        stub._on_ctrl_r_shortcut(None)

        assert reached == []
        assert stub._voice_post_panel.shown is False


class _FakePreviewSound:
    def __init__(self, path):
        self.path = path
        self.is_playing = False
        self.stop_calls = 0

    def play(self):
        self.is_playing = True

    def stop(self):
        self.stop_calls += 1
        self.is_playing = False


class _PreviewStub(_Stub):
    _toggle_pause_voice_recording = StatusPanel._toggle_pause_voice_recording
    _toggle_play_recorded_audio = StatusPanel._toggle_play_recorded_audio
    _on_recorded_audio_timer = StatusPanel._on_recorded_audio_timer
    _on_ctrl_p_shortcut = StatusPanel._on_ctrl_p_shortcut

    def __init__(self):
        super().__init__()
        self._voice_post_panel.Show()
        self._is_recording = True
        self._recording_frames = [b"\x00\x00" * 400]


class TestVoiceStatusPausedRecordingPreview:
    def test_pause_reveals_the_same_preview_control_as_conversations(self):
        stub = _PreviewStub()

        stub._toggle_pause_voice_recording(None)

        assert stub._recording_paused is True
        assert stub._voice_pause_btn.label == "resume_recording"
        assert stub._voice_play_btn.shown is True

    def test_play_and_stop_use_a_temporary_wav_and_clean_it_up(self, monkeypatch):
        opened = []

        def _file_stream(file):
            sound = _FakePreviewSound(file)
            opened.append(sound)
            return sound

        monkeypatch.setattr(status_panel_module.sl_stream, "FileStream", _file_stream)
        stub = _PreviewStub()
        stub._recording_paused = True

        stub._toggle_play_recorded_audio(None)

        temp_path = stub._recorded_audio_temp_path
        assert os.path.isfile(temp_path)
        assert opened[0].is_playing is True
        assert stub._voice_play_btn.label == "stop_recorded_audio_playback"
        assert stub._recorded_audio_timer.started == [300]

        stub._toggle_play_recorded_audio(None)

        assert opened[0].stop_calls == 1
        assert stub._recorded_audio_sound is None
        assert stub._recorded_audio_temp_path is None
        assert not os.path.exists(temp_path)
        assert stub._voice_play_btn.label == "play_recorded_audio"

    def test_resuming_stops_preview_and_hides_its_button(self, monkeypatch):
        sound = _FakePreviewSound("snapshot.wav")
        sound.play()
        stub = _PreviewStub()
        stub._recording_paused = True
        stub._recorded_audio_sound = sound

        stub._toggle_pause_voice_recording(None)

        assert sound.stop_calls == 1
        assert stub._recording_paused is False
        assert stub._voice_play_btn.shown is False

    def test_ctrl_p_uses_the_preview_only_while_paused(self):
        calls = []
        stub = _PreviewStub()
        stub._toggle_play_recorded_audio = lambda event: calls.append(event)

        stub._on_ctrl_p_shortcut("not-paused")
        stub._recording_paused = True
        stub._on_ctrl_p_shortcut("paused")

        assert calls == ["paused"]


class _FakeStream:
    def __init__(self):
        self.stopped = False
        self.closed = False

    def start_stream(self):
        pass

    def stop_stream(self):
        self.stopped = True

    def close(self):
        self.closed = True


class _FakeRecordingPyAudio:
    """Stands in for a live pyaudio.PyAudio instance: records every open()
    so a test can assert the driver was NOT touched on the wx thread."""

    def __init__(self, works=True):
        self.works = works
        self.opened_with = []

    def open(self, rate, channels, format, input, input_device_index,
             frames_per_buffer, stream_callback):
        self.opened_with.append((rate, channels))
        if not self.works:
            raise OSError(-9996, "Device unavailable")
        return _FakeStream()


class _RecordingStub(_Stub):
    """_Stub plus the attributes the background stream open touches."""

    _start_voice_recording = StatusPanel._start_voice_recording
    _on_close_voice_panel  = StatusPanel._on_close_voice_panel
    _on_record_voice_button = StatusPanel._on_record_voice_button

    def __init__(self, pa_works=True, device_name="Microfone USB"):
        super().__init__()
        self.main_window.effective_input_device_name = device_name
        self._recording_pa = _FakeRecordingPyAudio(works=pa_works)
        self._recording_starting = False
        self._recording_open_token = 0
        self._recording_rate = 48000
        self._recording_channels = 1
        self._status_list = _FakeButton()


class _CapturedThread:
    """Captures the thread target instead of running it, so the test controls
    exactly when the "background" work happens."""

    created = []

    def __init__(self, target=None, daemon=None, **_kw):
        self.target = target
        self.daemon = daemon
        self.started = False
        _CapturedThread.created.append(self)

    def start(self):
        self.started = True


class TestVoiceStatusRecordingOpensOffTheUiThread:
    """find_input_device_index() and pa.open() both negotiate with the audio
    driver and can block for seconds. In StatusPanel they ran directly on the
    wx thread, so starting a voice status froze the window — and the screen
    reader reading it — for however long the driver took. ConversationsPanel
    had the same bug and was fixed first (see
    tests/test_recording_open_failure.py); this is the same treatment for the
    status panel, including the finally that guarantees the flag is released.
    """

    def _patch(self, monkeypatch, call_after, message_boxes=None):
        _CapturedThread.created = []
        monkeypatch.setattr(status_panel_module.threading, "Thread", _CapturedThread)
        monkeypatch.setattr(status_panel_module.wx, "CallAfter",
                            lambda func, *a, **kw: call_after.append((func, a)))
        monkeypatch.setattr(status_panel_module.wx, "MessageBox",
                            lambda *a, **kw: (message_boxes if message_boxes is not None else []).append(a))
        monkeypatch.setattr(status_panel_module, "pyaudio",
                            types.SimpleNamespace(paInt16=8, paContinue=0))

    def test_the_driver_is_not_touched_before_the_thread_runs(self, monkeypatch):
        scheduled = []
        self._patch(monkeypatch, scheduled)
        stub = _RecordingStub()

        stub._start_voice_recording()

        # Returned immediately: the open is queued, not done.
        assert stub._recording_starting is True
        assert stub._is_recording is False
        assert stub._recording_pa.opened_with == [], "pa.open() ran on the wx thread"
        assert len(_CapturedThread.created) == 1
        assert _CapturedThread.created[0].daemon is True
        assert _CapturedThread.created[0].started is True

    def test_successful_open_arms_the_panel_from_the_callback(self, monkeypatch):
        scheduled = []
        self._patch(monkeypatch, scheduled)
        monkeypatch.setattr(status_panel_module, "find_input_device_index", lambda *a, **kw: 3)
        stub = _RecordingStub()

        stub._start_voice_recording()
        _CapturedThread.created[0].target()          # the "background" work

        assert stub._recording_pa.opened_with == [(48000, 1)]
        func, args = scheduled[0]
        func(*args)                                   # what wx would dispatch

        assert stub._is_recording is True
        assert stub._recording_starting is False
        assert stub._voice_status_lbl.label == "recording_in_progress"

    def test_open_failure_releases_the_flag_and_warns(self, monkeypatch):
        scheduled, boxes = [], []
        self._patch(monkeypatch, scheduled, boxes)
        monkeypatch.setattr(status_panel_module, "find_input_device_index", lambda *a, **kw: 3)
        stub = _RecordingStub(pa_works=False)

        stub._start_voice_recording()
        _CapturedThread.created[0].target()
        func, args = scheduled[0]
        func(*args)

        assert stub._recording_starting is False
        assert stub._is_recording is False
        assert len(boxes) == 1

    def test_an_exception_still_schedules_the_callback(self, monkeypatch):
        """The finally: an escaping exception would die unseen in a daemon
        thread, leaving _recording_starting stuck True and both entry points
        refusing to ever start recording again."""
        scheduled, boxes = [], []
        self._patch(monkeypatch, scheduled, boxes)

        def _boom(*_a, **_kw):
            raise OSError("no default host API")

        monkeypatch.setattr(status_panel_module, "find_input_device_index", _boom)
        stub = _RecordingStub()

        stub._start_voice_recording()
        _CapturedThread.created[0].target()

        assert len(scheduled) == 1, "exception swallowed the wx.CallAfter"
        func, args = scheduled[0]
        func(*args)
        assert stub._recording_starting is False

        # The user-visible symptom: pressing record again must get through.
        reached = []
        stub._start_voice_recording = lambda: reached.append(True)
        stub._on_record_voice_button(None)
        assert reached == [True], "record control stayed dead after a failed open"

    def test_a_stream_that_arrives_after_discard_is_thrown_away(self, monkeypatch):
        scheduled = []
        self._patch(monkeypatch, scheduled)
        monkeypatch.setattr(status_panel_module, "find_input_device_index", lambda *a, **kw: 3)
        stub = _RecordingStub()

        stub._start_voice_recording()
        _CapturedThread.created[0].target()

        # User closes the voice panel while the stream is still opening.
        stub._on_close_voice_panel(None)

        func, args = scheduled[0]
        func(*args)

        opened_stream = args[0]
        assert opened_stream.closed is True, "orphan stream left capturing"
        assert stub._is_recording is False
        assert stub._recording_stream is None


class _FakeSelectivePyAudio:
    """Like _FakeRecordingPyAudio, but only ONE device index opens — the shape
    of a machine whose default host API refuses the microphone that another
    host API accepts. Records every attempted index, in order."""

    def __init__(self, working_index=None):
        self.working_index = working_index
        self.opened_indices = []
        self.opened_with = []

    def open(self, rate, channels, format, input, input_device_index,
             frames_per_buffer, stream_callback):
        self.opened_indices.append(input_device_index)
        self.opened_with.append((rate, channels))
        if self.working_index is None or input_device_index != self.working_index:
            raise OSError(-9999, "Unanticipated host error")
        return _FakeStream()


class TestVoiceStatusHostApiFallback:
    """Same last resort as ConversationsPanel, and deliberately identical to
    it: posting a voice status and sending a voice message have no reason to
    disagree about which microphones exist. This panel already drifted behind
    the other one once — it kept opening the stream on the wx thread long
    after the conversations panel had stopped (see
    TestVoiceStatusRecordingOpensOffTheUiThread above).
    """

    def _patch(self, monkeypatch, call_after, message_boxes=None):
        _CapturedThread.created = []
        monkeypatch.setattr(status_panel_module.threading, "Thread", _CapturedThread)
        monkeypatch.setattr(status_panel_module.wx, "CallAfter",
                            lambda func, *a, **kw: call_after.append((func, a)))
        monkeypatch.setattr(status_panel_module.wx, "MessageBox",
                            lambda *a, **kw: (message_boxes if message_boxes is not None else []).append(a))
        monkeypatch.setattr(status_panel_module, "pyaudio",
                            types.SimpleNamespace(paInt16=8, paContinue=0))

    def test_a_failed_default_falls_back_to_an_enumerated_device(self, monkeypatch):
        scheduled, boxes = [], []
        self._patch(monkeypatch, scheduled, boxes)
        monkeypatch.setattr(status_panel_module, "find_input_device_index", lambda *a, **kw: None)
        monkeypatch.setattr(status_panel_module, "fallback_input_device_indices",
                            lambda pa, exclude=(): [12])

        stub = _RecordingStub(device_name="")
        stub._recording_pa = _FakeSelectivePyAudio(working_index=12)
        stub._start_voice_recording()
        _CapturedThread.created[0].target()
        func, args = scheduled[0]
        func(*args)

        assert stub._recording_pa.opened_indices[0] is None
        assert 12 in stub._recording_pa.opened_indices
        assert stub._is_recording is True
        assert stub._recording_starting is False
        assert boxes == [], "recording started — there was nothing to warn about"

    def test_the_pinned_device_is_not_tried_a_second_time(self, monkeypatch):
        scheduled, boxes = [], []
        self._patch(monkeypatch, scheduled, boxes)
        seen = {}

        def _candidates(pa, exclude=()):
            seen["exclude"] = exclude
            return []

        monkeypatch.setattr(status_panel_module, "find_input_device_index", lambda *a, **kw: 3)
        monkeypatch.setattr(status_panel_module, "fallback_input_device_indices", _candidates)

        stub = _RecordingStub(pa_works=False)
        stub._start_voice_recording()
        _CapturedThread.created[0].target()
        func, args = scheduled[0]
        func(*args)

        assert 3 in seen["exclude"]

    def test_every_candidate_failing_still_warns(self, monkeypatch):
        scheduled, boxes = [], []
        self._patch(monkeypatch, scheduled, boxes)
        monkeypatch.setattr(status_panel_module, "find_input_device_index", lambda *a, **kw: None)
        monkeypatch.setattr(status_panel_module, "fallback_input_device_indices",
                            lambda pa, exclude=(): [12, 18])

        stub = _RecordingStub(device_name="")
        stub._recording_pa = _FakeSelectivePyAudio(working_index=None)
        stub._start_voice_recording()
        _CapturedThread.created[0].target()
        func, args = scheduled[0]
        func(*args)

        assert 12 in stub._recording_pa.opened_indices
        assert 18 in stub._recording_pa.opened_indices
        assert stub._recording_starting is False
        assert stub._is_recording is False
        assert len(boxes) == 1


class TestABatchSurvivesAHelperRaising:
    """A per-file helper that RAISES (instead of returning False) used to tear
    out of _send_all_media_statuses_bg()'s loop: every remaining file was
    skipped and the aggregate failure dialog at the end of that loop was never
    reached, so the batch stopped in total silence on a background thread.

    The concrete path was _send_status_voice_bg()'s try/finally with no
    except around reading the converted OGG — fixed at the source — but the
    loop must not depend on each helper remembering to catch everything.
    """

    def test_a_raising_audio_helper_does_not_abort_the_batch(self, tmp_path, monkeypatch):
        stub = _Stub()
        img1  = _touch(tmp_path, "pic1.png")
        audio = _touch(tmp_path, "song.mp3")
        img2  = _touch(tmp_path, "pic2.png")
        boxes, attempted = [], []
        monkeypatch.setattr(status_panel_module.wx, "MessageBox",
                            lambda *a, **kw: boxes.append(a))
        monkeypatch.setattr(status_panel_module.wx, "CallAfter", lambda fn, *a, **kw: fn(*a, **kw))

        def _raising_voice(path, **kw):
            attempted.append(path)
            raise OSError("the converted OGG vanished")

        def _ok_media(path, caption, **kw):
            attempted.append(path)
            return True

        monkeypatch.setattr(stub, "_send_status_voice_bg", _raising_voice)
        monkeypatch.setattr(stub, "_send_media_status_bg", _ok_media)

        stub._send_all_media_statuses_bg([img1, audio, img2], "")

        # The file AFTER the raising one was still attempted.
        assert attempted == [img1, audio, img2]
        # And the user was told, exactly once, that one file failed.
        assert len(boxes) == 1
        assert "(1/3)" in boxes[0][0]

    def test_the_ogg_read_failing_is_reported_not_raised(self, tmp_path, monkeypatch):
        """_send_status_voice_bg() itself must return False on an unreadable
        OGG rather than letting the exception escape."""
        stub = _Stub(convert_result=str(tmp_path / "never-written.ogg"))
        audio = _touch(tmp_path, "song.mp3")
        monkeypatch.setattr(status_panel_module.wx, "CallAfter", lambda fn, *a, **kw: fn(*a, **kw))
        monkeypatch.setattr(status_panel_module.wx, "MessageBox", lambda *a, **kw: None)

        # _convert_wav_to_ogg reports success, but the file isn't really there
        # to be read — os.path.isfile() is what the earlier guard checks, so
        # make it pass and let open() be the thing that fails.
        monkeypatch.setattr(status_panel_module.os.path, "isfile", lambda p: True)

        ok = stub._send_status_voice_bg(audio, report_result=False)

        assert ok is False
        # The user's own picked file must still be there — only the temp OGG
        # is ever unlinked.
        assert os.path.isfile(audio)
