"""Headless checks of real slider key handler; no windows or capture."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from tests.test_system_audio_ui import Control, bind


class VolumeKeyTests(unittest.TestCase):
    def setUp(self):
        self.slider = Control()
        self.slider.value = 50
        self.slider.GetMin = lambda: 0
        self.slider.GetMax = lambda: 100
        self.slider.GetLineSize = lambda: 1
        self.slider.GetPageSize = lambda: 10
        self.wx = SimpleNamespace(WXK_UP=1, WXK_DOWN=2, WXK_PAGEUP=3, WXK_PAGEDOWN=4,
                                  MOD_NONE=0, ACC_EVENT_OBJECT_VALUECHANGE=32782,
                                  OBJID_CLIENT=-4, ACC_SELF=0,
                                  Accessible=SimpleNamespace(NotifyEvent=Mock()))
        self.p = SimpleNamespace(_system_audio_volume_slider=self.slider,
                                 _on_system_audio_volume=Mock())
        bind(self.p, ['_on_system_audio_volume_key', '_on_recording_volume_key'], {'wx': self.wx})

    def key(self, code, modifiers=0):
        event = SimpleNamespace(GetKeyCode=lambda: code, GetModifiers=lambda: modifiers, Skip=Mock())
        self.p._on_system_audio_volume_key(event)
        return event

    def test_direction_single_update_and_accessibility_notification(self):
        for key, expected in ((1, 51), (2, 49), (3, 60), (4, 40)):
            with self.subTest(key=key):
                self.slider.value = 50
                self.p._on_system_audio_volume.reset_mock()
                self.wx.Accessible.NotifyEvent.reset_mock()
                event = self.key(key)
                self.assertEqual(self.slider.value, expected)
                event.Skip.assert_not_called()  # no second native movement
                self.p._on_system_audio_volume.assert_called_once_with(None)
                self.wx.Accessible.NotifyEvent.assert_called_once_with(32782, self.slider, -4, 0)

    def test_limits_do_not_emit_duplicate_updates(self):
        for initial, key in ((100, 1), (0, 2), (100, 3), (0, 4)):
            self.slider.value = initial
            self.key(key)
            self.assertEqual(self.slider.value, initial)
        self.p._on_system_audio_volume.assert_not_called()
        self.wx.Accessible.NotifyEvent.assert_not_called()
        self.slider.value = 95
        self.key(3)
        self.assertEqual(self.slider.value, 100)
        self.slider.value = 5
        self.key(4)
        self.assertEqual(self.slider.value, 0)

    def test_native_keys_shortcuts_and_disabled_control_are_untouched(self):
        self.key(999).Skip.assert_called_once()  # Tab/right/left/Home/End go native
        self.key(1, modifiers=1).Skip.assert_called_once()
        self.slider.enabled = False
        self.key(1).Skip.assert_called_once()
        self.assertEqual(self.slider.value, 50)
        self.p._on_system_audio_volume.assert_not_called()
        self.wx.Accessible.NotifyEvent.assert_not_called()


if __name__ == '__main__':
    unittest.main()
