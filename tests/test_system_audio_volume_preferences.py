"""Recording gain preferences: headless UI methods, no real devices/windows."""
import unittest
from unittest.mock import Mock

from tests.test_system_audio_ui import CaptureHarness


def create_volume_controls(panel):
    from types import SimpleNamespace
    from tests.test_system_audio_ui import bind
    def slider(parent, *, value, **kwargs):
        control = Mock()
        control.GetValue.return_value = value
        return control
    wx = SimpleNamespace(Slider=slider, StaticText=Mock(), SL_HORIZONTAL=0, SL_LABELS=0,
                         LEFT=0, RIGHT=0, BOTTOM=0, EXPAND=0, EVT_SLIDER=0, EVT_KEY_DOWN=1)
    namespace = {'wx': wx, 'AccessibleRecordingVolumeSlider': Mock()}
    panel._on_system_audio_volume_key = Mock()
    panel._on_nvda_volume_key = Mock()
    bind(panel, ['_create_system_audio_volume_controls', '_create_nvda_volume_controls'], namespace)
    panel._create_system_audio_volume_controls(Mock())
    panel._create_nvda_volume_controls(Mock())


class RecordingVolumePreferencesTests(CaptureHarness, unittest.TestCase):
    def test_new_panel_restores_saved_levels_without_saving_or_moving_focus(self):
        self.p.main_window.settings['general'].update(
            system_audio_recording_volume=0, system_audio_recording_nvda_volume=35)
        self.p.main_window._schedule_save_settings = Mock()
        create_volume_controls(self.p)
        self.assertEqual(self.p._system_audio_volume_slider.GetValue(), 0)
        self.assertEqual(self.p._nvda_volume_slider.GetValue(), 35)
        self.p.main_window._schedule_save_settings.assert_not_called()
        self.p._system_audio_volume_slider.SetFocus.assert_not_called()
        self.p._nvda_volume_slider.SetFocus.assert_not_called()

    def test_defaults_and_invalid_saved_levels_use_100_but_zero_is_preserved(self):
        for saved, expected in ((None, 100), (-1, 100), (101, 100), ('50', 100),
                                (False, 100), (float('nan'), 100), ([], 100), (0, 0), (57, 57)):
            with self.subTest(saved=saved):
                self.p.main_window.settings['general'].update(
                    system_audio_recording_volume=saved, system_audio_recording_nvda_volume=saved)
                create_volume_controls(self.p)
                self.assertEqual(self.p._system_audio_volume_slider.GetValue(), expected)
                self.assertEqual(self.p._nvda_volume_slider.GetValue(), expected)
        self.p.main_window.settings['general'] = {}
        create_volume_controls(self.p)
        self.assertEqual(self.p._system_audio_volume_slider.GetValue(), 100)
        self.assertEqual(self.p._nvda_volume_slider.GetValue(), 100)

    def test_unavailable_nvda_does_not_overwrite_remembered_level(self):
        mw = self.p.main_window
        mw.settings['general']['system_audio_recording_nvda_volume'] = 35
        recorder = self.open()
        recorder.nvda_volume_available = False
        self.p._nvda_volume_slider.SetValue(0)
        self.p._on_nvda_volume(None)
        self.assertEqual(mw.settings['general']['system_audio_recording_nvda_volume'], 35)
        mw._schedule_save_settings.assert_not_called()
        recorder.set_nvda_volume.assert_not_called()

    def test_discard_then_restart_preserves_both_levels_in_account_file(self):
        import json
        from pathlib import Path
        import tempfile
        from tests.test_system_audio_ui import bind
        clock = SettingsClock()
        with tempfile.TemporaryDirectory() as folder:
            real = settings_window(folder, clock, self.p.main_window.settings)
            self.p.main_window._schedule_save_settings = real._schedule_save_settings
            self.open()
            self.p._system_audio_volume_slider.SetValue(0)
            self.p._on_system_audio_volume(None)
            clock.advance(.2)
            self.p._nvda_volume_slider.SetValue(35)
            self.p._on_nvda_volume(None)
            bind(self.p, ['_discard_voice_message'], self.namespace)
            self.p._discard_voice_message(None)
            clock.advance(.49)
            target = Path(folder) / 'settings.json'
            self.assertFalse(target.exists())
            clock.advance(.01)
            saved = json.loads(target.read_text())
            self.assertEqual(saved['general']['system_audio_recording_volume'], 0)
            self.assertEqual(saved['general']['system_audio_recording_nvda_volume'], 35)
            self.p.main_window.settings = saved
            create_volume_controls(self.p)  # reconstruct controls like a new launch
            recorder = self.open()
            self.assertEqual((recorder.system_audio_volume, recorder.nvda_volume), (0, 35))

    def test_live_computer_gain_is_remembered_before_recording_finishes(self):
        mw = self.p.main_window
        mw._schedule_save_settings = Mock()
        recorder = self.open()
        self.p._system_audio_volume_slider.SetValue(0)
        self.p._on_system_audio_volume(None)
        recorder.set_system_audio_volume.assert_called_once_with(0)
        self.assertEqual(mw.settings['general'].get('system_audio_recording_volume'), 0)
        mw._schedule_save_settings.assert_called_once_with(delay=0.5)
        self.assertTrue(self.p._is_recording)


    def test_nvda_gain_is_remembered_independently_including_zero(self):
        mw = self.p.main_window
        mw.settings['general']['system_audio_recording_volume'] = 35
        mw._schedule_save_settings = Mock()
        recorder = self.open()
        self.p._nvda_volume_slider.SetValue(0)
        self.p._on_nvda_volume(None)
        recorder.set_nvda_volume.assert_called_once_with(0)
        self.assertEqual(mw.settings['general'].get('system_audio_recording_nvda_volume'), 0)
        self.assertEqual(mw.settings['general']['system_audio_recording_volume'], 35)
        mw._schedule_save_settings.assert_called_once_with(delay=0.5)


class RecordingDefaultsTests(unittest.TestCase):
    def test_gain_defaults_are_100_in_both_sources_and_stay_per_account(self):
        import ast
        import json
        from tests.test_system_audio_ui import ROOT
        defaults = json.loads((ROOT / 'client/data/settings_default.json').read_text(encoding='utf-8'))
        tree = ast.parse((ROOT / 'client/core/utils.py').read_text(encoding='utf-8'))
        assignment = next(n for n in tree.body if isinstance(n, ast.Assign)
                          and any(isinstance(t, ast.Name) and t.id == 'DEFAULT_SETTINGS' for t in n.targets))
        general = next(value for key, value in zip(assignment.value.keys, assignment.value.values)
                       if isinstance(key, ast.Constant) and key.value == 'general')
        python_defaults = ast.literal_eval(general)
        global_tree = ast.parse((ROOT / 'client/app_settings.py').read_text(encoding='utf-8'))
        global_keys = ast.literal_eval(next(n.value for n in global_tree.body
            if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == '_GENERAL_GLOBAL' for t in n.targets)))
        for key in ('system_audio_recording_volume', 'system_audio_recording_nvda_volume'):
            self.assertEqual(defaults['general'].get(key), 100)
            self.assertEqual(python_defaults.get(key), 100)
            self.assertNotIn(key, global_keys)


class SettingsClock:
    """Deterministic threading.Timer replacement, never a desktop event loop."""
    def __init__(self):
        self.now = 0
        self.timers = []

    def Timer(self, interval, function):
        from types import SimpleNamespace
        timer = SimpleNamespace(interval=interval, function=function,
                                due=self.now + interval, cancelled=False, started=False,
                                fired=False, daemon=False)
        timer.cancel = lambda: setattr(timer, 'cancelled', True)
        timer.start = lambda: setattr(timer, 'started', True)
        self.timers.append(timer)
        return timer

    def advance(self, seconds):
        target = round(self.now + seconds, 6)
        for timer in sorted(self.timers, key=lambda item: item.due):
            if timer.started and not timer.cancelled and not timer.fired and timer.due <= target:
                self.now = timer.due
                timer.fired = True
                timer.function()
        self.now = target


def settings_window(folder, clock, settings=None):
    """Real scheduler/atomic JSON writer, sandbox account and no GUI imports."""
    import json
    import logging
    import os
    from pathlib import Path
    import threading
    from types import SimpleNamespace, MethodType
    import uuid
    from tests.test_system_audio_ui import ROOT, functions
    namespace = {
        'threading': SimpleNamespace(Timer=clock.Timer), 'json': json, 'os': os,
        'uuid': uuid, 'logging': logging, 'data_path': lambda name: str(Path(folder) / name),
    }
    methods = functions(ROOT / 'client/main.py',
        ['_schedule_save_settings', '_flush_pending_debounced_saves',
         'save_settings', '_save_settings_locked'], namespace)
    window = SimpleNamespace(settings=settings if settings is not None else {'general': {}},
        _save_timer_lock=threading.Lock(), _save_lock=threading.Lock(),
        _save_timer=None, _settings_save_timer=None,
        _persist_global_settings=Mock(), _do_save=Mock())
    for name in ('_schedule_save_settings', '_flush_pending_debounced_saves',
                 'save_settings', '_save_settings_locked'):
        setattr(window, name, MethodType(methods[name], window))
    return window


class RecordingSettingsDebounceTests(unittest.TestCase):
    def test_shutdown_flush_saves_before_deadline_and_ignores_old_timer(self):
        import tempfile
        import json
        from pathlib import Path
        clock = SettingsClock()
        with tempfile.TemporaryDirectory() as folder:
            mw = settings_window(folder, clock, {'general': {'system_audio_recording_nvda_volume': 0}})
            mw._schedule_save_settings(delay=0.5)
            timer = mw._settings_save_timer
            clock.advance(.1)
            mw._flush_pending_debounced_saves()
            self.assertEqual(json.loads((Path(folder) / 'settings.json').read_text())
                             ['general']['system_audio_recording_nvda_volume'], 0)
            self.assertIsNone(mw._settings_save_timer)
            self.assertTrue(timer.cancelled)
            timer.function()
            mw._flush_pending_debounced_saves()
            self.assertEqual(mw._persist_global_settings.call_count, 1)

    def test_normal_settings_keep_two_second_debounce(self):
        import tempfile
        from pathlib import Path
        clock = SettingsClock()
        with tempfile.TemporaryDirectory() as folder:
            mw = settings_window(folder, clock)
            mw._schedule_save_settings()
            clock.advance(.5)
            self.assertFalse((Path(folder) / 'settings.json').exists())
            clock.advance(1.5)
            self.assertTrue((Path(folder) / 'settings.json').exists())

    def test_account_writes_are_independent(self):
        import json
        import tempfile
        from pathlib import Path
        clock = SettingsClock()
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            first = settings_window(a, clock, {'general': {'system_audio_recording_volume': 0}})
            second = settings_window(b, clock, {'general': {'system_audio_recording_volume': 80}})
            first._schedule_save_settings(delay=.5)
            second._schedule_save_settings(delay=.5)
            clock.advance(.5)
            self.assertEqual(json.loads((Path(a) / 'settings.json').read_text())
                             ['general']['system_audio_recording_volume'], 0)
            self.assertEqual(json.loads((Path(b) / 'settings.json').read_text())
                             ['general']['system_audio_recording_volume'], 80)

    def test_cancelled_timer_callback_cannot_save_before_new_deadline(self):
        import tempfile
        from pathlib import Path
        clock = SettingsClock()
        with tempfile.TemporaryDirectory() as folder:
            mw = settings_window(folder, clock)
            mw._schedule_save_settings(delay=0.5)
            old = mw._settings_save_timer
            clock.advance(.3)
            mw._schedule_save_settings(delay=0.5)
            old.function()  # cancelled callback already entered on its thread
            self.assertFalse((Path(folder) / 'settings.json').exists())
            clock.advance(.5)
            self.assertEqual(mw._persist_global_settings.call_count, 1)

    def test_background_settings_do_not_postpone_pending_slider_save(self):
        import tempfile
        from pathlib import Path
        clock = SettingsClock()
        with tempfile.TemporaryDirectory() as folder:
            mw = settings_window(folder, clock)
            mw._schedule_save_settings(delay=0.5)
            pending = mw._settings_save_timer
            clock.advance(.2)
            mw._schedule_save_settings()  # unrelated presence/settings update
            self.assertIs(mw._settings_save_timer, pending)
            clock.advance(.3)
            self.assertTrue((Path(folder) / 'settings.json').exists())

    def test_save_occurs_half_second_after_last_movement(self):
        import inspect
        import json
        from pathlib import Path
        import tempfile
        clock = SettingsClock()
        with tempfile.TemporaryDirectory() as folder:
            mw = settings_window(folder, clock)
            self.assertIn('delay', inspect.signature(mw._schedule_save_settings).parameters,
                          'Existing account settings scheduler needs a configurable debounce')
            target = Path(folder) / 'settings.json'
            mw.settings['general']['system_audio_recording_volume'] = 70
            mw._schedule_save_settings(delay=0.5)
            clock.advance(.3)
            mw.settings['general']['system_audio_recording_volume'] = 0
            mw._schedule_save_settings(delay=0.5)
            clock.advance(.49)
            self.assertFalse(target.exists(), 'No intermediate disk write while adjusting')
            clock.advance(.01)
            self.assertEqual(json.loads(target.read_text())['general']['system_audio_recording_volume'], 0)
            self.assertEqual(mw._persist_global_settings.call_count, 1)
            self.assertIsNone(mw._settings_save_timer)


if __name__ == '__main__':
    unittest.main()
