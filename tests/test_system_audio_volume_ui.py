"""Focused slider UI contracts using real extracted methods, no wx windows."""
import ast
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from tests.test_system_audio_ui import CaptureHarness, Control, CONVERSATIONS, bind


class VolumeControlsTests(CaptureHarness, unittest.TestCase):
    def test_ui_slider_reaches_real_capture_mixer(self):
        import struct
        from core.system_audio_capture import SystemAudioRecorder
        from tests.test_system_audio_volume import ControlledBackend, QuietDiagnostics
        backend = ControlledBackend()
        # Explicit no-NVDA resolver: the UI requests separation, but this test
        # exercises the endpoint fallback without inspecting live processes.
        backend.open_nvda_loopbacks = lambda rate, channels: None
        def factory(*args, **kwargs):
            recorder = SystemAudioRecorder(*args, **kwargs, backend_factory=lambda: backend,
                                           diagnostics=QuietDiagnostics())
            self.addCleanup(recorder.close)
            return recorder
        self.namespace['SystemAudioRecorder'] = factory
        p = self.p
        p._start_system_audio_recording()
        self.threads.drain()
        self.drain_ui()
        self.assertTrue(p._is_recording)
        for volume in (100, 50, 0):
            p._system_audio_volume_slider.value = volume
            p._on_system_audio_volume(None)
            backend.feed(frames=480)
        p._system_audio_session['recorder'].close()
        self.assertIsNone(p._system_audio_session['error'])
        expected = b''.join(struct.pack('<2h', round((.25 + .5 * volume / 100) * 16384),
                                       round((.25 + .25 * volume / 100) * 16384)) * 480
                            for volume in (100, 50, 0))
        self.assertEqual(b''.join(p._recording_frames), expected)

    def test_open_volume_change_pause_and_discard(self):
        p = self.p
        p._system_audio_volume_slider.value = 65
        p._start_system_audio_recording()
        self.assertTrue(p._system_audio_volume_slider.shown)
        self.assertFalse(p._system_audio_volume_slider.enabled)
        self.threads.drain()
        self.drain_ui()
        rec = self.recorders[-1]
        self.assertEqual(rec.system_audio_volume, 65)
        self.assertTrue(p._system_audio_volume_slider.enabled)
        p._system_audio_volume_slider.value = 30
        p._on_system_audio_volume(None)
        rec.set_system_audio_volume.assert_called_once_with(30)
        # Exercise real pause/resume acknowledgements, not just a paused flag.
        bind(p, ['_toggle_system_audio_pause', '_on_system_audio_paused'], self.namespace)
        p._toggle_system_audio_pause()
        self.assertFalse(p._system_audio_volume_slider.enabled)
        self.threads.drain()
        self.drain_ui()
        self.assertTrue(p._recording_paused)
        self.assertTrue(p._system_audio_volume_slider.enabled)
        p._system_audio_volume_slider.value = 0
        p._on_system_audio_volume(None)
        rec.set_system_audio_volume.assert_called_with(0)
        p._toggle_system_audio_pause()
        self.threads.drain()
        self.drain_ui()
        self.assertFalse(p._recording_paused)
        self.assertTrue(p._system_audio_volume_slider.enabled)
        rec.set_paused.assert_called_with(False)
        bind(p, ['_discard_voice_message'], self.namespace)
        p._discard_voice_message(None)
        self.assertFalse(p._system_audio_volume_slider.shown)
        self.assertFalse(p._system_audio_volume_label.shown)
        rec.set_system_audio_volume.reset_mock()
        p._on_system_audio_volume(None)
        rec.set_system_audio_volume.assert_not_called()
        self.threads.drain()

    def test_slider_disabled_on_failure_and_send_transition(self):
        rec = self.open()
        p = self.p
        bind(p, ['_finish_system_audio_for_send'], self.namespace)
        p._on_system_audio_ready_to_send = Mock()
        p._finish_system_audio_for_send(None)
        self.assertFalse(p._system_audio_volume_slider.enabled)
        p._on_system_audio_volume(None)
        rec.set_system_audio_volume.assert_not_called()
        self.threads.drain()
        self.drain_ui()
        p._system_audio_session['transition'] = False
        p._on_system_audio_error(p._system_audio_session)
        self.assertFalse(p._system_audio_volume_slider.enabled)

    def test_ordinary_mode_hidden_and_initial_failure_hides_slider(self):
        p = self.p
        p._update_system_audio_volume_controls()
        self.assertFalse(p._system_audio_volume_slider.shown)
        self.start_hook = Mock(side_effect=OSError('synthetic open failure'))
        self.open()
        self.threads.drain()
        self.assertFalse(p._system_audio_volume_slider.shown)
        self.assertFalse(p._system_audio_volume_label.shown)


class VolumeCreationTests(unittest.TestCase):
    def test_native_named_slider_follows_send_and_is_initially_hidden(self):
        label, slider = Mock(), Mock()
        wx = SimpleNamespace(StaticText=Mock(return_value=label), Slider=Mock(return_value=slider),
                             SL_HORIZONTAL=1, SL_LABELS=2, EXPAND=4, LEFT=8, RIGHT=16,
                             BOTTOM=32, EVT_SLIDER='slider-event', EVT_KEY_DOWN='key-event')
        p = SimpleNamespace(_voice_panel=object(), _send_voice_btn=object(),
                            main_window=SimpleNamespace(settings={}, i18n=SimpleNamespace(t=lambda key:key)),
                            _on_system_audio_volume=Mock(), _on_system_audio_volume_key=Mock())
        bind(p, ['_create_system_audio_volume_controls'],
             {'wx':wx, 'AccessibleRecordingVolumeSlider':Mock()})
        sizer = Mock()
        p._create_system_audio_volume_controls(sizer)
        self.assertEqual(wx.Slider.call_args.kwargs['value'], 100)
        self.assertEqual(wx.Slider.call_args.kwargs['minValue'], 0)
        self.assertEqual(wx.Slider.call_args.kwargs['maxValue'], 100)
        slider.SetName.assert_called_once_with('system_audio_recording_volume')
        slider.MoveAfterInTabOrder.assert_called_once_with(p._send_voice_btn)
        self.assertEqual(slider.Bind.call_count, 2)
        slider.Bind.assert_any_call('slider-event', p._on_system_audio_volume)
        slider.Bind.assert_any_call('key-event', p._on_system_audio_volume_key)
        slider.Hide.assert_called_once()
        label.Hide.assert_called_once()
        # Inspect actual composer wiring, not just the helper in isolation.
        text = CONVERSATIONS.read_text(encoding='utf-8')
        send = text.index('voice_sizer.Add(self._send_voice_btn')
        create = text.index('self._create_system_audio_volume_controls(voice_sizer)')
        finish = text.index('self._voice_panel.SetSizer(voice_sizer)')
        self.assertLess(send, create)
        self.assertLess(create, finish)
        tree = ast.parse(text)
        relabel = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                       and '_send_voice_btn.SetLabel' in (ast.get_source_segment(text, n) or '')
                       and n.name != '__init__')
        self.assertIn('self._relabel_system_audio_volume_controls()', ast.get_source_segment(text, relabel) or '')
        helper = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                      and n.name == '_relabel_system_audio_volume_controls')
        self.assertIn('_system_audio_volume_slider.SetName', ast.unparse(helper))


if __name__ == '__main__':
    unittest.main()
