from io import BytesIO
import json
from pathlib import Path
from threading import Event

from core.call_logic import active_call_label_key, incoming_call_can_answer
from core.call_video import camera_names, jpeg_frames
from main import MainWindow


def test_camera_names_only_reads_video_devices():
    listing = '''[dshow @ 000] DirectShow video devices (some may be both video and audio devices)
[dshow @ 000] "Integrated Camera"
[dshow @ 000]   Alternative name "@device_pnp_abc"
[dshow @ 000] "USB Camera"
[dshow @ 000] DirectShow audio devices
[dshow @ 000] "Microphone"
'''
    assert camera_names(listing) == ["Integrated Camera", "USB Camera"]


def test_camera_names_accepts_current_ffmpeg_directshow_suffix_and_spacing():
    listing = '''[dshow @ 000] DirectShow video devices (some may be both video and audio devices)
[dshow @ 000]  "Integrated Camera" (video)
[dshow @ 000]     Alternative name "@device_pnp_abc"
[dshow @ 000]    "USB Camera"    (VIDEO)
[dshow @ 000] DirectShow audio devices
[dshow @ 000]  "Microphone" (audio)
'''
    assert camera_names(listing) == ["Integrated Camera", "USB Camera"]


def test_jpeg_pipe_discards_noise_and_yields_complete_frames():
    frame_a = b'\xff\xd8first\xff\xd9'
    frame_b = b'\xff\xd8second\xff\xd9'
    assert list(jpeg_frames(BytesIO(b'junk' + frame_a + frame_b), Event())) == [
        frame_a, frame_b,
    ]


def test_individual_video_is_answerable_and_has_video_label():
    assert incoming_call_can_answer({"is_video": True})
    assert not incoming_call_can_answer({"is_group": True})
    assert active_call_label_key({"is_video": True}) == "video_call_active_label"


def test_video_button_and_labels_exist_in_all_languages():
    languages = Path(__file__).parents[1] / 'client' / 'languages'
    for path in languages.glob('*.json'):
        if path.name == 'language_map.json':
            continue
        entries = json.loads(path.read_text(encoding='utf-8'))
        assert entries['video_call_button']
        assert entries['video_call_window_title']
        assert entries['voice_call_video_off_button']
        assert entries['voice_call_video_on_button']
        assert '{name}' in entries['incoming_video_call_announcement']


def test_video_button_is_restricted_to_individual_chats():
    source = (Path(__file__).parents[1] / 'client' / 'ui' / 'conversations.py').read_text(encoding='utf-8')
    assert 'unavailable = jid.endswith(("@g.us", "@newsletter", "@broadcast"))' in source
    assert 'self._video_call_btn.Show(bool(jid) and not unavailable)' in source
    assert 'self.main_window.start_video_call(jid, name)' in source


class _NoCameraWs:
    def send_call_camera_frame(self, _frame):
        pass


class _NoCameraMainWindow:
    _start_call_camera = MainWindow._start_call_camera

    def __init__(self):
        self.ws = _NoCameraWs()
        self._active_voice_call = {"identity": "call-1", "is_video": True}
        self._call_camera_capture = None

    def _find_api_ffmpeg(self):
        return "ffmpeg.exe"


def test_missing_camera_does_not_end_or_clear_video_call(monkeypatch):
    import core.call_video as call_video

    class MissingCameraCapture:
        def __init__(self, _ffmpeg, _send_frame):
            self.stopped = False

        def start(self):
            raise RuntimeError("No camera found")

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(call_video, "CameraCapture", MissingCameraCapture)
    stub = _NoCameraMainWindow()
    active_before = stub._active_voice_call

    assert stub._start_call_camera() is False
    assert stub._active_voice_call is active_before
    assert stub._active_voice_call["is_video"] is True
    assert stub._call_camera_capture is None
    assert stub._call_camera_available is False
    assert stub._call_camera_enabled is False


def test_call_window_video_toggle_is_hidden_without_camera():
    source = (Path(__file__).parents[1] / 'client' / 'main.py').read_text(encoding='utf-8')
    assert 'self.voice_call_window_video_button.Hide()' in source
    assert 'video_button.Show(is_video and local_camera_available)' in source
    assert 'self.voice_call_window_video_button.Bind(wx.EVT_BUTTON, self.toggle_call_video)' in source
    assert 'def _stop_call_camera(self, *, reset_availability: bool = False):' in source
