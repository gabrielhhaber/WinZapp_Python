"""A configured audio device that is missing at startup is announced, never
put in a modal box.

The box was queued with wx.CallAfter from MainWindow.__init__ and so ran on
the UI thread ahead of the Node spawn, which is queued the same way. A user
whose "External Headphones" were unplugged got the box at every launch; an
unattended launch left it waiting, Node never started, and the startup dialog
ended in "WinZapp API failed to start in time" and closed the app.
"""

import copy

import core.sound_system as sound_system_module
from core.sound_system import SoundSystem
from main_window import settings as settings_module


class _Speech:
    def __init__(self):
        self.spoken = []

    def output(self, text, **_options):
        self.spoken.append(text)


class _I18n:
    def t(self, key):
        return key + ":{device}"


class _FakeWx:
    """Stands in for the wx module inside core.sound_system: CallAfter runs
    the call at once (nothing is queued behind it), and any MessageBox is
    recorded so the test can assert there was none."""

    OK = ICON_WARNING = 0

    def __init__(self):
        self.boxes = []

    def CallAfter(self, fn, *args, **kwargs):
        fn(*args, **kwargs)

    def MessageBox(self, *args, **kwargs):
        self.boxes.append(args)
        raise AssertionError("a modal message box would block the UI thread")


class _Output:
    def __init__(self):
        self._device = -1

    def set_device(self, device):
        raise Exception("simulated BASS device error")

    def free(self):
        pass

    def init_device(self, device=None):
        self._device = device


class _MainWindow:
    def __init__(self, settings):
        self.background_mode = False
        self.app_name = "WinZapp"
        self.i18n = _I18n()
        self.speak_output = _Speech()
        self.settings = settings
        self.load_sounds_calls = 0
        self.sound_system = SoundSystem(self, sound_dir="nonexistent")
        self.sound_system.output = _Output()

    def load_sounds(self):
        self.load_sounds_calls += 1


def _missing_devices(monkeypatch):
    fake_wx = _FakeWx()
    monkeypatch.setattr(sound_system_module, "wx", fake_wx)
    monkeypatch.setattr(settings_module, "wx", fake_wx)
    monkeypatch.setattr(sound_system_module, "find_output_device_index", lambda name: None)
    monkeypatch.setattr(sound_system_module, "find_default_output_device_index", lambda: None)
    monkeypatch.setattr(settings_module, "find_input_device_index", lambda name: None)
    settings = {"audio_devices": {
        "output_device_name": "External Headphones",
        "effects_output_device_name": "External Headphones",
        "input_device_name": "USB Mic",
    }}
    return fake_wx, _MainWindow(settings)


def _apply(mw):
    settings_module.SettingsMixin._apply_configured_audio_devices(mw)


def test_a_missing_device_at_startup_is_spoken_not_boxed(monkeypatch):
    fake_wx, mw = _missing_devices(monkeypatch)
    _apply(mw)
    assert fake_wx.boxes == []
    assert mw.speak_output.spoken == [
        "audio_device_failed_output:External Headphones",
        "audio_device_failed_input:USB Mic",
    ]
    # Playback really fell back to the system default.
    assert mw.sound_system.output._device == -1


def test_the_stored_device_choice_is_left_untouched(monkeypatch):
    _fake_wx, mw = _missing_devices(monkeypatch)
    before = copy.deepcopy(mw.settings)
    _apply(mw)
    assert mw.settings == before
    # Still the configured device, so it is used again once it is back.
    assert mw.sound_system._configured_output_device == "External Headphones"
    assert mw.effective_input_device_name == ""


def test_the_notice_is_given_once_per_session(monkeypatch):
    """Output and effects on the same missing device, a settings import that
    re-applies the devices, and a later playback failure on it: one notice."""
    fake_wx, mw = _missing_devices(monkeypatch)
    _apply(mw)
    _apply(mw)
    mw.sound_system._last_recovery_at = None
    mw.sound_system.handle_playback_failure()
    assert fake_wx.boxes == []
    assert mw.speak_output.spoken.count("audio_device_failed_output:External Headphones") == 1
    assert mw.speak_output.spoken.count("audio_device_failed_input:USB Mic") == 1


def test_background_autostart_stays_silent(monkeypatch):
    fake_wx, mw = _missing_devices(monkeypatch)
    mw.background_mode = True
    _apply(mw)
    assert fake_wx.boxes == []
    assert mw.speak_output.spoken == []
