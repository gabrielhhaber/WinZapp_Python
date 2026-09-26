"""NVDA gain and renewed consent: real AST methods, no wx app or devices."""
import ast
import copy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from tests.test_system_audio_ui import WARNING, CONVERSATIONS, ROOT, CaptureHarness, bind, functions


def warning_namespace():
    tree = ast.parse(WARNING.read_text(encoding='utf-8'))
    ns = {'confirm_with_checkbox': Mock(return_value=(False, False))}
    exec(compile(ast.Module(body=[n for n in tree.body if not isinstance(
        n, (ast.Import, ast.ImportFrom))], type_ignores=[]), str(WARNING), 'exec'), ns)
    return ns


class RenewedConsentTests(unittest.TestCase):
    def panel(self, settings, answer):
        ns = warning_namespace()
        ns['ask_system_audio'] = Mock(return_value=answer)
        p = SimpleNamespace(main_window=SimpleNamespace(settings=settings,
                            i18n=SimpleNamespace(t=lambda key: key), save_settings=Mock()),
                            _is_recording=False, _recording_starting=False,
                            conversation={'remoteJid': 'chat'},
                            _record_voice_system_btn=SimpleNamespace(IsEnabled=lambda: True),
                            _start_system_audio_recording=Mock())
        bind(p, ['_on_record_system_audio'], ns)
        return p, ns

    def test_old_optout_requires_renewal_and_no_changes_nothing(self):
        for revision in (None, 0, 1, 'invalid', '2', True):
            with self.subTest(revision=revision):
                settings = {'general': {'unrelated': 42}, 'user_interface': {
                    'warn_system_audio_recording': False, 'unrelated': 'keep'}}
                if revision is not None:
                    settings['user_interface']['system_audio_consent_revision'] = revision
                original = copy.deepcopy(settings)
                p, ns = self.panel(settings, (False, True))
                p._on_record_system_audio(None)
                ns['ask_system_audio'].assert_called_once()
                p._start_system_audio_recording.assert_not_called()
                p.main_window.save_settings.assert_not_called()
                self.assertEqual(settings, original)

    def test_yes_records_scope_only_after_confirmation_and_preserves_other_settings(self):
        for dont_ask in (False, True):
            with self.subTest(dont_ask=dont_ask):
                settings = {'general': {'unrelated': 42}, 'user_interface': {
                    'warn_system_audio_recording': False, 'unrelated': 'keep'}}
                p, ns = self.panel(settings, (True, dont_ask))
                p._on_record_system_audio(None)
                self.assertEqual(settings, {'general': {'unrelated': 42}, 'user_interface': {
                    'warn_system_audio_recording': not dont_ask,
                    'system_audio_consent_revision': 2, 'unrelated': 'keep'}})
                p.main_window.save_settings.assert_called_once_with()
                p._start_system_audio_recording.assert_called_once_with()
                self.assertEqual(ns['system_audio_warning_enabled'](settings), not dont_ask)
                # The existing Interface checkbox still restores the warning.
                settings['user_interface']['warn_system_audio_recording'] = True
                self.assertTrue(ns['system_audio_warning_enabled'](settings))

    def test_default_backfill_never_grants_scope_or_erases_optout(self):
        import json
        defaults = json.loads((ROOT / 'client/data/settings_default.json').read_text(encoding='utf-8'))
        ns = functions(ROOT / 'client/core/utils.py', ['backfill_missing_defaults'], {'copy': copy})
        settings = {'general': {'unrelated': 42}, 'user_interface': {
            'warn_system_audio_recording': False, 'unrelated': 'keep'}}
        self.assertTrue(ns['backfill_missing_defaults'](settings, defaults))
        self.assertEqual(settings['user_interface']['system_audio_consent_revision'], 0)
        self.assertIs(settings['user_interface']['warn_system_audio_recording'], False)
        self.assertEqual(settings['user_interface']['unrelated'], 'keep')
        self.assertTrue(warning_namespace()['system_audio_warning_enabled'](settings))
        p, _ = self.panel(settings, (True, True))
        p._on_record_system_audio(None)
        self.assertFalse(ns['backfill_missing_defaults'](settings, defaults))
        self.assertFalse(warning_namespace()['system_audio_warning_enabled'](settings))
        self.assertEqual(settings['general']['unrelated'], 42)

    def test_current_optout_skips_warning_and_does_not_rewrite_settings(self):
        settings = {'user_interface': {'warn_system_audio_recording': False,
                                      'system_audio_consent_revision': 2}}
        p, ns = self.panel(settings, (False, False))
        p._on_record_system_audio(None)
        ns['ask_system_audio'].assert_not_called()
        p.main_window.save_settings.assert_not_called()
        p._start_system_audio_recording.assert_called_once_with()


class NvdaVolumeTests(CaptureHarness, unittest.TestCase):
    def test_both_values_snapshotted_before_async_start_and_availability(self):
        p = self.p
        p._system_audio_volume_slider.value = 35
        p._nvda_volume_slider.value = 70
        p._start_system_audio_recording()
        self.assertTrue(p._nvda_volume_slider.shown)
        self.assertFalse(p._nvda_volume_slider.enabled)
        # A worker must never query wx. Changes after dispatch cannot change its snapshot.
        p._system_audio_volume_slider.GetValue = Mock(side_effect=AssertionError('wx off thread'))
        p._nvda_volume_slider.GetValue = Mock(side_effect=AssertionError('wx off thread'))
        self.threads.drain()
        self.drain_ui()
        rec = self.recorders[-1]
        self.assertTrue(rec.separate_nvda)
        self.assertEqual((rec.system_audio_volume, rec.nvda_volume), (35, 70))
        self.assertTrue(p._nvda_volume_slider.enabled)
        self.assertEqual(p._system_audio_volume_label.label, 'system_audio_recording_other_volume')
        self.assertEqual(p._system_audio_volume_slider.name, 'system_audio_recording_other_volume')
        self.assertEqual(p._nvda_volume_label.label, 'system_audio_recording_nvda_volume')
        self.assertEqual(p._nvda_volume_slider.name, 'system_audio_recording_nvda_volume')

    def test_pause_live_gain_resume_send_failure_and_stale_callbacks(self):
        p = self.p
        rec = self.open()
        bind(p, ['_toggle_system_audio_pause', '_on_system_audio_paused',
                 '_finish_system_audio_for_send', '_on_system_audio_ready_to_send'], self.namespace)
        p._send_voice_message = Mock()
        p._nvda_volume_slider.value = 50
        p._on_nvda_volume(None)
        rec.set_nvda_volume.assert_called_once_with(50)
        rec.set_system_audio_volume.assert_not_called()
        p._toggle_system_audio_pause()
        self.assertFalse(p._nvda_volume_slider.enabled)
        p._on_nvda_volume(None)
        rec.set_nvda_volume.assert_called_once_with(50)
        self.threads.drain()
        self.drain_ui()
        self.assertTrue(p._recording_paused)
        self.assertTrue(p._nvda_volume_slider.enabled)
        p._nvda_volume_slider.value = 0
        p._on_nvda_volume(None)
        rec.set_nvda_volume.assert_called_with(0)
        p._toggle_system_audio_pause()
        self.threads.drain()
        self.drain_ui()
        self.assertTrue(p._nvda_volume_slider.enabled)
        # A retranslation updates both names, including the active split label.
        p.main_window.i18n.t = lambda key: 'translated:' + key
        p._relabel_system_audio_volume_controls()
        self.assertEqual(p._nvda_volume_slider.name, 'translated:system_audio_recording_nvda_volume')
        self.assertEqual(p._system_audio_volume_slider.name, 'translated:system_audio_recording_other_volume')
        rec.set_nvda_volume.reset_mock()
        p._finish_system_audio_for_send(None)
        self.assertFalse(p._nvda_volume_slider.enabled)
        p._on_nvda_volume(None)
        rec.set_nvda_volume.assert_not_called()
        rec.on_error(OSError('synthetic failure during send'))
        self.threads.drain()
        self.drain_ui()
        self.assertFalse(p._nvda_volume_slider.enabled)
        p._send_voice_message.assert_not_called()
        old_session = p._system_audio_session
        p._stop_system_audio_recording()
        p._is_recording = False
        new = self.open()
        p._on_system_audio_paused(old_session, True, None)
        p._on_system_audio_opened(old_session, OSError('stale'))
        rec.on_error(OSError('stale'))
        self.drain_ui()
        self.assertTrue(p._nvda_volume_slider.enabled)
        p._on_nvda_volume(None)
        new.set_nvda_volume.assert_called_once_with(0)
        rec.set_nvda_volume.assert_not_called()

    def test_ordinary_cancelled_and_failed_start_hide_both_new_controls(self):
        p = self.p
        p._update_system_audio_volume_controls()
        self.assertFalse(p._nvda_volume_slider.shown)
        self.assertFalse(p._nvda_volume_label.shown)
        self.assertFalse(p._nvda_volume_slider.enabled)
        def fail(rec):
            raise OSError('synthetic unsupported process capture')
        self.start_hook = fail
        self.open()
        self.assertFalse(p._nvda_volume_slider.shown)
        self.assertFalse(p._nvda_volume_label.shown)
        self.assertFalse(p._nvda_volume_slider.enabled)
        p._on_nvda_volume(None)
        self.recorders[-1].set_nvda_volume.assert_not_called()
        self.start_hook = lambda rec: None
        rec = self.open()
        bind(p, ['_discard_voice_message'], self.namespace)
        p._discard_voice_message(None)
        self.assertFalse(p._nvda_volume_slider.shown)
        self.assertFalse(p._nvda_volume_label.shown)
        self.assertFalse(p._nvda_volume_slider.enabled)
        p._on_nvda_volume(None)
        rec.set_nvda_volume.assert_not_called()
        self.threads.drain()

    def test_actual_ui_handlers_through_three_source_worker_to_exact_pcm(self):
        import struct
        from core.system_audio_capture import SystemAudioRecorder
        from tests.test_system_audio_nvda_capture import NvdaBackend
        from tests.test_system_audio_volume import QuietDiagnostics
        backend = NvdaBackend()
        recorders = []
        def factory(*args, **kwargs):
            recorder = SystemAudioRecorder(*args, **kwargs, backend_factory=lambda: backend,
                                           diagnostics=QuietDiagnostics())
            self.addCleanup(recorder.close)
            recorders.append(recorder)
            return recorder
        self.namespace['SystemAudioRecorder'] = factory
        p = self.p
        p._system_audio_volume_slider.value = 50
        p._nvda_volume_slider.value = 100
        p._start_system_audio_recording()
        self.threads.drain()
        self.drain_ui()
        self.assertTrue(p._nvda_volume_slider.enabled)
        bind(p, ['_toggle_system_audio_pause', '_on_system_audio_paused'], self.namespace)
        expected = bytearray()
        # Distinct mono mic=.25, other=(0,.5), alternating NVDA=(v,-v).
        def feed(other, nvda):
            backend.feed(frames=480)
            for frame in range(480):
                voice = (.125 if frame % 2 else -.125) * nvda / 100
                expected.extend(struct.pack('<2h', round((.25 + voice) * 16384),
                                            round((.25 + .5 * other / 100 - voice) * 16384)))
        feed(50, 100)  # constructor gains
        for volume in (50, 0, 100):
            p._nvda_volume_slider.value = volume
            p._on_nvda_volume(None)
            feed(50, volume)
        p._system_audio_volume_slider.value = 0
        p._on_system_audio_volume(None)
        feed(0, 100)  # first slider still excludes NVDA
        p._toggle_system_audio_pause()
        self.threads.drain()
        self.drain_ui()
        self.assertTrue(p._recording_paused)
        self.assertTrue(p._nvda_volume_slider.enabled)
        p._nvda_volume_slider.value = 0
        p._on_nvda_volume(None)
        with backend.condition:
            backend.now += 10_000_000
        p._toggle_system_audio_pause()
        self.threads.drain()
        self.drain_ui()
        feed(0, 0)  # microphone remains at its original level, paused gap absent
        recorders[0].close()
        self.assertIsNone(p._system_audio_session['error'])
        self.assertEqual(b''.join(p._recording_frames), bytes(expected))

    def test_no_nvda_keeps_legacy_label_and_disables_only_nvda_control(self):
        self.start_hook = lambda rec: setattr(rec, 'nvda_volume_available', False)
        rec = self.open()
        self.assertTrue(self.p._nvda_volume_slider.shown)
        self.assertFalse(self.p._nvda_volume_slider.enabled)
        self.assertTrue(self.p._system_audio_volume_slider.enabled)
        self.assertEqual(self.p._system_audio_volume_label.label, 'system_audio_recording_volume')
        bind(self.p, ['_on_nvda_volume'], self.namespace)
        self.p._on_nvda_volume(None)
        rec.set_nvda_volume.assert_not_called()


class NvdaCreationTests(unittest.TestCase):
    def test_native_control_and_visual_tab_order_after_first_slider(self):
        label, slider, sizer = Mock(), Mock(), Mock()
        wx = SimpleNamespace(StaticText=Mock(return_value=label), Slider=Mock(return_value=slider),
                             SL_HORIZONTAL=1, SL_LABELS=2, EXPAND=4, LEFT=8, RIGHT=16,
                             BOTTOM=32, EVT_SLIDER='slider-event', EVT_KEY_DOWN='key-event')
        p = SimpleNamespace(_voice_panel=object(), _system_audio_volume_slider=object(),
                            main_window=SimpleNamespace(settings={}, i18n=SimpleNamespace(t=lambda key: key)),
                            _on_nvda_volume=Mock(), _on_nvda_volume_key=Mock())
        bind(p, ['_create_nvda_volume_controls'],
             {'wx': wx, 'AccessibleRecordingVolumeSlider': Mock()})
        p._create_nvda_volume_controls(sizer)
        self.assertEqual(wx.Slider.call_args.kwargs, dict(value=100, minValue=0, maxValue=100,
                                                         style=3, size=(260, -1)))
        wx.StaticText.assert_called_once_with(p._voice_panel, label='system_audio_recording_nvda_volume')
        slider.SetName.assert_called_once_with('system_audio_recording_nvda_volume')
        slider.MoveAfterInTabOrder.assert_called_once_with(p._system_audio_volume_slider)
        slider.SetLineSize.assert_called_once_with(1)
        slider.SetPageSize.assert_called_once_with(10)
        slider.Bind.assert_any_call('slider-event', p._on_nvda_volume)
        slider.Bind.assert_any_call('key-event', p._on_nvda_volume_key)
        self.assertEqual(slider.Bind.call_count, 2)
        self.assertEqual([c.args[0] for c in sizer.Add.call_args_list], [label, slider])
        slider.Hide.assert_called_once_with()
        label.Hide.assert_called_once_with()
        text = CONVERSATIONS.read_text(encoding='utf-8')
        first = text.index('self._create_system_audio_volume_controls(voice_sizer)')
        second = text.index('self._create_nvda_volume_controls(voice_sizer)')
        self.assertLess(first, second)
        self.assertLess(second, text.index('self._voice_panel.SetSizer(voice_sizer)'))
        tree = ast.parse(text)
        retranslate = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                           and n.name == 'refresh_labels')
        self.assertIn('self._relabel_system_audio_volume_controls()', ast.unparse(retranslate))


import tests.test_system_audio_volume_keys as volume_keys


class NvdaKeyTests(volume_keys.VolumeKeyTests):
    """Run the same direction, notification, limits and native-key contract."""
    def setUp(self):
        super().setUp()
        self.p._nvda_volume_slider = self.slider
        self.p._on_nvda_volume = self.p._on_system_audio_volume
        bind(self.p, ['_on_nvda_volume_key'], {'wx': self.wx})
        self.p._on_system_audio_volume_key = self.p._on_nvda_volume_key


if __name__ == '__main__':
    unittest.main()
