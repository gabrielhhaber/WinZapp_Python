"""Headless UI contract tests: AST-extracted real methods, no wx windows/devices.

Run independently of the Windows-only conftest with:
    python3 -m unittest discover -s tests -p test_system_audio_ui.py -v
"""
import ast
from pathlib import Path
from types import SimpleNamespace, MethodType
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
CONVERSATIONS = ROOT / 'client/ui/conversations.py'
WARNING = ROOT / 'client/ui/dialogs/system_audio_warning.py'


def functions(path, names, namespace=None):
    tree = ast.parse(path.read_text(encoding='utf-8'))
    selected = [n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name in names]
    missing = set(names) - {n.name for n in selected}
    assert not missing, f'Missing implementation: {sorted(missing)}'
    ns = {} if namespace is None else namespace
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), 'exec'), ns)
    return ns


def bind(panel, names, namespace):
    ns = functions(CONVERSATIONS, names, namespace)
    for name in names:
        setattr(panel, name, MethodType(ns[name], panel))
    return panel


def warning_functions(confirm=None):
    tree = ast.parse(WARNING.read_text(encoding='utf-8'))
    ns = {'confirm_with_checkbox': confirm}
    exec(compile(ast.Module(body=[n for n in tree.body if not isinstance(
        n, (ast.Import, ast.ImportFrom))], type_ignores=[]), str(WARNING), 'exec'), ns)
    return ns


class TestPrivacyWarning(unittest.TestCase):
    def test_warning_uses_standard_confirmation_with_safe_defaults(self):
        self.assertTrue(WARNING.exists(), 'System audio warning dialog is missing')
        confirm = Mock(return_value=(False, False))
        ns = warning_functions(confirm)
        self.assertTrue(ns['system_audio_warning_enabled']({}))
        self.assertFalse(ns['system_audio_warning_enabled'](
            {'user_interface': {'warn_system_audio_recording': False,
                                'system_audio_consent_revision': 2}}))
        ns['ask_system_audio']('parent', SimpleNamespace(t=lambda key: key))
        confirm.assert_called_once_with(
            'parent', 'system_audio_recording_warning', 'system_audio_recording_title',
            'mark_all_read_dont_show_again', yes_label='yes_button', no_label='no_button',
            checked=False, default_yes=False)

    def panel(self, answer=(True, False)):
        mw = SimpleNamespace(settings={}, i18n=SimpleNamespace(t=lambda key: key),
                             save_settings=Mock())
        panel = SimpleNamespace(main_window=mw, _is_recording=False,
                                _recording_starting=False, conversation={'remoteJid': 'chat'},
                                _record_voice_system_btn=SimpleNamespace(IsEnabled=lambda: True),
                                _start_system_audio_recording=Mock())
        ask = Mock(return_value=answer)
        ns = warning_functions()
        ns['ask_system_audio'] = ask
        bind(panel, ['_on_record_system_audio'], ns)
        return panel, ask

    def test_no_never_records_or_remembers_checkbox(self):
        p, _ = self.panel((False, True))
        p._on_record_system_audio(None)
        p._start_system_audio_recording.assert_not_called()
        p.main_window.save_settings.assert_not_called()
        self.assertEqual(p.main_window.settings, {})

    def test_yes_remembers_checkbox_and_starts(self):
        p, _ = self.panel((True, True))
        p._on_record_system_audio(None)
        p._start_system_audio_recording.assert_called_once_with()
        self.assertFalse(p.main_window.settings['user_interface']['warn_system_audio_recording'])
        p.main_window.save_settings.assert_called_once_with()

    def test_opted_out_skips_warning(self):
        p, ask = self.panel()
        p.main_window.settings = {'user_interface': {'warn_system_audio_recording': False,
                                                     'system_audio_consent_revision': 2}}
        p._on_record_system_audio(None)
        ask.assert_not_called()
        p._start_system_audio_recording.assert_called_once_with()

    def test_recording_opening_and_posting_disabled_are_guarded(self):
        for state in ['recording', 'opening', 'disabled', 'no_conversation']:
            with self.subTest(state=state):
                p, ask = self.panel()
                if state == 'recording': p._is_recording = True
                if state == 'opening': p._recording_starting = True
                if state == 'disabled': p._record_voice_system_btn.IsEnabled = lambda: False
                if state == 'no_conversation': p.conversation = None
                p._on_record_system_audio(None)
                ask.assert_not_called()
                p._start_system_audio_recording.assert_not_called()


class QueuedThreads:
    """Deterministic workers: start() queues, never opens a real device."""
    def __init__(self):
        import threading
        self.Event, self.Lock = threading.Event, threading.Lock
        self.jobs = []

    def Thread(self, target, daemon=True):
        return SimpleNamespace(start=lambda: self.jobs.append(target))

    def drain(self):
        while self.jobs:
            self.jobs.pop(0)()


class Control:
    def __init__(self):
        self.shown, self.enabled, self.label, self.value = True, True, '', ''
    def Show(self, show=True): self.shown = show
    def Hide(self): self.shown = False
    def Enable(self, enable=True): self.enabled = enable
    def Disable(self): self.enabled = False
    def IsEnabled(self): return self.enabled
    def SetLabel(self, label): self.label = label
    def SetName(self, name): self.name = name
    def SetValue(self, value): self.value = value
    def SetFocus(self): pass
    def GetValue(self): return self.value
    def Layout(self): pass


class CaptureHarness:
    def setUp(self):
        self.threads, self.ui = QueuedThreads(), []
        self.recorders = []
        self.start_hook = lambda recorder: None
        owner = self
        class Recorder:
            def __init__(self, on_frames, on_error, microphone_name='', channels=1, rate=48000,
                         system_audio_volume=100, separate_nvda=False, nvda_volume=100):
                self.on_frames, self.on_error = on_frames, on_error
                self.microphone_name, self.channels, self.rate = microphone_name, channels, rate
                self.close = Mock()
                self.set_paused = Mock()
                self.system_audio_volume = system_audio_volume
                self.set_system_audio_volume = Mock()
                self.separate_nvda = separate_nvda
                self.nvda_volume = nvda_volume
                self.nvda_volume_available = True
                self.set_nvda_volume = Mock()
                owner.recorders.append(self)
            def start(self):
                owner.start_hook(self)
        self.recorder_type = Recorder
        self.namespace = {
            'threading': self.threads,
            'wx': SimpleNamespace(CallAfter=lambda fn, *args: self.ui.append((fn, args)),
                                  CallLater=lambda ms, fn, *args: self.ui.append((fn, args))),
            'SystemAudioRecorder': Recorder,
        }
        self.p = SimpleNamespace(
            conversation={'remoteJid': 'chat@s.whatsapp.net'},
            _is_recording=False, _recording_starting=False, _recording_open_token=0,
            _recording_paused=False, _recording_frames=[], _recording_stream=None,
            _recording_pa=None, _system_audio_session=None, _recording_system_audio=False,
            _system_audio_interrupted=False,
            main_window=SimpleNamespace(
                settings={'general': {}, 'speech_content': {'silence_while_recording': True}},
                i18n=SimpleNamespace(t=lambda key: key), output=Mock(),
                _schedule_save_settings=Mock(),
                effective_input_device_name='Chosen mic', send_recording_status=Mock(),
                voicemsg_startrecording_sound=SimpleNamespace(play=Mock()),
                voicemsg_discard_sound=SimpleNamespace(play=Mock()),
                voicemsg_pauserecording_sound=SimpleNamespace(play=Mock())),
            _stop_recorded_audio_preview=Mock(),
        )
        for name in ['_record_voice_system_btn', '_record_voice_alt_btn', 'record_voice_message_btn',
                     '_voice_panel', '_pause_resume_btn', '_send_voice_btn', '_discard_voice_btn',
                     '_play_recorded_btn', '_add_attachment_btn', '_emoji_btn', 'send_message_btn',
                     'message_field', 'conversation_panel']:
            setattr(self.p, name, Control())
        self.p._system_audio_volume_slider = Control()
        self.p._system_audio_volume_slider.value = 100
        self.p._system_audio_volume_label = Control()
        self.p._nvda_volume_slider = Control()
        self.p._nvda_volume_slider.value = 100
        self.p._nvda_volume_label = Control()
        names = ['_update_system_audio_volume_controls', '_on_system_audio_volume',
                 '_relabel_system_audio_volume_controls', '_on_nvda_volume',
                 '_start_system_audio_recording', '_on_system_audio_opened',
                 '_on_system_audio_error', '_stop_system_audio_recording',
                 '_show_system_audio_recording_controls', '_default_recording_stereo',
                 '_voice_recording_focus_suppression_enabled', '_focus_recording_button_silently',
                 '_hide_voice_panel']
        bind(self.p, names, self.namespace)

    def drain_ui(self):
        while self.ui:
            fn, args = self.ui.pop(0)
            fn(*args)

    def open(self):
        self.p._start_system_audio_recording()
        self.threads.drain()
        self.drain_ui()
        return self.recorders[-1]


class TestAsyncCapture(CaptureHarness, unittest.TestCase):
    def test_open_is_off_ui_thread_and_both_sources_reach_existing_buffer(self):
        self.p._start_system_audio_recording()
        self.assertTrue(self.p._recording_starting)
        self.assertFalse(self.p._is_recording)
        self.assertEqual(self.recorders, [])
        self.assertFalse(self.p._send_voice_btn.enabled)
        self.assertFalse(self.p._pause_resume_btn.enabled)
        self.threads.drain()
        self.assertFalse(self.p._is_recording)
        self.drain_ui()
        recorder = self.recorders[0]
        self.assertTrue(self.p._is_recording)
        self.assertFalse(self.p._recording_starting)
        self.assertEqual(recorder.microphone_name, 'Chosen mic')
        self.assertEqual((recorder.channels, recorder.rate), (2, 48000))
        recorder.on_frames(b'mixed pcm')
        self.assertEqual(self.p._recording_frames, [b'mixed pcm'])
        self.assertTrue(self.p._send_voice_btn.enabled)
        self.assertTrue(self.p._pause_resume_btn.enabled)

    def test_default_stereo_is_respected_without_an_extra_mode(self):
        self.p.main_window.settings['general']['voice_message_stereo'] = True
        recorder = self.open()
        self.assertEqual(recorder.channels, 2)
        self.assertTrue(self.p._recording_stereo)

    def test_open_failure_reports_and_closes_without_mic_fallback(self):
        def fail(recorder): raise OSError('loopback unavailable')
        self.start_hook = fail
        self.open()
        self.threads.drain()
        self.assertFalse(self.p._recording_starting)
        self.assertFalse(self.p._is_recording)
        self.assertEqual(self.p._recording_frames, [])
        self.assertFalse(self.p._voice_panel.shown)
        self.recorders[0].close.assert_called()
        self.p.main_window.output.assert_called_once_with('system_audio_recording_failed')

    def test_cancel_during_blocking_open_releases_late_stream(self):
        self.start_hook = lambda recorder: self.p._stop_system_audio_recording()
        self.p._start_system_audio_recording()
        self.threads.drain()
        self.drain_ui()
        self.threads.drain()
        self.assertFalse(self.p._is_recording)
        self.assertFalse(self.p._recording_starting)
        self.recorders[0].close.assert_called()

    def test_old_callbacks_cannot_pollute_new_recording(self):
        first = self.open()
        first.on_frames(b'first')
        self.p._stop_system_audio_recording()
        self.p._is_recording = False
        second = self.open()
        first.on_frames(b'stale')
        first.on_error(OSError('stale failure'))
        second.on_frames(b'second')
        self.drain_ui()
        self.assertEqual(self.p._recording_frames, [b'second'])
        self.assertTrue(self.p._is_recording)
        self.assertFalse(self.p._recording_paused)
        self.p.main_window.output.assert_not_called()

    def test_loss_preserves_partial_recording_but_never_resumes(self):
        recorder = self.open()
        recorder.on_frames(b'partial')
        recorder.on_error(OSError('device lost'))
        self.drain_ui()
        self.assertTrue(self.p._is_recording)
        self.assertTrue(self.p._recording_paused)
        self.assertTrue(self.p._system_audio_interrupted)
        self.assertEqual(self.p._recording_frames, [b'partial'])
        self.assertTrue(self.p._play_recorded_btn.shown)
        self.assertTrue(self.p._send_voice_btn.enabled)
        self.assertFalse(self.p._pause_resume_btn.enabled)
        self.p.main_window.output.assert_called_once_with('system_audio_recording_interrupted')

    def test_failed_open_reports_even_if_cleanup_times_out(self):
        def fail(recorder):
            recorder.close.side_effect = TimeoutError('closing unavailable device')
            raise OSError('opening unavailable device')
        self.start_hook = fail
        self.p._start_system_audio_recording()
        try:
            self.threads.drain()
        except TimeoutError:
            self.fail('Cleanup failure must not strand the UI in opening state')
        self.drain_ui()
        self.assertFalse(self.p._recording_starting)
        self.p.main_window.output.assert_called_once_with('system_audio_recording_failed')
        self.assertTrue(self.p._send_voice_btn.enabled)
        self.assertTrue(self.p._pause_resume_btn.enabled)
        try:
            self.threads.drain()
        except TimeoutError:
            self.fail('Discarded cleanup must not escape its worker')

    def test_error_before_open_completion_is_not_lost(self):
        def fail_early(recorder):
            recorder.on_frames(b'partial')
            recorder.on_error(OSError('device lost'))
        self.start_hook = fail_early
        self.open()
        self.assertTrue(self.p._system_audio_interrupted)
        self.assertEqual(self.p._recording_frames, [b'partial'])
        self.assertTrue(self.p._recording_paused)


class TestRecordingControls(CaptureHarness, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.p.main_window.speak_output = SimpleNamespace(
            silence_screen_reader_focus=Mock(), silence=Mock())
        bind(self.p, ['_toggle_pause_recording', '_discard_voice_message',
                      '_cancel_active_recording', '_stop_recording_stream', '_on_destroy',
                      '_silence_send_voice_focus_if_enabled', '_voice_recording_silence_enabled',
                      '_toggle_system_audio_pause', '_on_system_audio_paused'],
             self.namespace)

    def test_mixed_pause_and_resume_confirm_with_normal_labels_and_sound(self):
        recorder = self.open()
        sound = self.p.main_window.voicemsg_pauserecording_sound.play
        for paused, label in ((True, 'resume_recording'), (False, 'pause_recording')):
            with self.subTest(paused=paused):
                sound.reset_mock()
                self.p._toggle_pause_recording(None)
                sound.assert_not_called()
                self.threads.drain()
                recorder.set_paused.assert_called_with(paused)
                sound.assert_not_called()  # UI has not acknowledged the change yet.
                self.drain_ui()
                self.assertEqual(self.p._recording_paused, paused)
                self.assertEqual(self.p._pause_resume_btn.label, label)
                self.assertTrue(self.p._pause_resume_btn.enabled)
                self.assertEqual(self.p._play_recorded_btn.shown, paused)
                sound.assert_called_once_with()

    def test_pending_pause_keeps_button_focusable_and_ignores_repeat_activation(self):
        recorder = self.open()
        button = self.p._pause_resume_btn
        button.Disable = Mock(wraps=button.Disable)
        button.SetFocus = Mock()
        for paused in (True, False):
            with self.subTest(paused=paused):
                recorder.set_paused.reset_mock()
                sound = self.p.main_window.voicemsg_pauserecording_sound.play
                sound.reset_mock()
                self.p._toggle_pause_recording(None)
                self.assertTrue(self.p._system_audio_session['transition'])
                self.assertTrue(button.enabled, 'Disabling the focused button can move focus away')
                button.Disable.assert_not_called()
                self.assertFalse(self.p._send_voice_btn.enabled)
                self.p._toggle_pause_recording(None)
                self.threads.drain()
                recorder.set_paused.assert_called_once_with(paused)
                self.p._toggle_pause_recording(None)  # Ack is still pending on the UI queue.
                self.assertEqual(self.threads.jobs, [])
                self.drain_ui()
                self.assertFalse(self.p._system_audio_session['transition'])
                self.assertTrue(self.p._send_voice_btn.enabled)
                sound.assert_called_once_with()
                button.SetFocus.assert_not_called()  # Do not steal focus after a shortcut.

    def test_ordinary_mono_and_stereo_keep_the_same_pause_feedback(self):
        self.p._silence_send_voice_focus_if_enabled = Mock()
        for stereo in (False, True):
            with self.subTest(stereo=stereo):
                self.p._is_recording = True
                self.p._recording_system_audio = False
                self.p._recording_stereo = stereo
                self.p._recording_paused = False
                for paused, label in ((True, 'resume_recording'), (False, 'pause_recording')):
                    sound = self.p.main_window.voicemsg_pauserecording_sound.play
                    sound.reset_mock()
                    self.p._toggle_pause_recording(None)
                    self.assertEqual(self.p._recording_paused, paused)
                    self.assertEqual(self.p._pause_resume_btn.label, label)
                    self.assertEqual(self.p._play_recorded_btn.shown, paused)
                    sound.assert_called_once_with()

    def test_failed_pause_does_not_play_success_sound(self):
        recorder = self.open()
        recorder.on_frames(b'valid prefix')
        recorder.set_paused.side_effect = TimeoutError('pause timeout')
        self.p._toggle_pause_recording(None)
        self.threads.drain()
        self.drain_ui()
        self.p.main_window.voicemsg_pauserecording_sound.play.assert_not_called()
        self.assertTrue(self.p._system_audio_interrupted)
        self.assertFalse(self.p._pause_resume_btn.enabled)
        self.assertEqual(self.p._recording_frames, [b'valid prefix'])
        self.p.main_window.output.assert_called_once_with('system_audio_recording_interrupted')

    def test_pause_is_forwarded_to_both_sources_and_resume_reuses_recorder(self):
        recorder = self.open()
        self.p._toggle_pause_recording(None)
        self.threads.drain()
        self.drain_ui()
        recorder.set_paused.assert_called_once_with(True)
        self.assertTrue(self.p._play_recorded_btn.shown)
        recorder.on_frames(b'must not append while paused')
        self.assertEqual(self.p._recording_frames, [])
        self.p._toggle_pause_recording(None)
        self.threads.drain()
        self.drain_ui()
        self.assertEqual(recorder.set_paused.call_args.args, (False,))
        self.assertFalse(self.p._play_recorded_btn.shown)
        recorder.on_frames(b'resumed')
        self.assertEqual(self.p._recording_frames, [b'resumed'])
        self.assertEqual(len(self.recorders), 1)

    def test_pause_shortcut_cannot_restart_interrupted_recording(self):
        recorder = self.open()
        recorder.on_error(OSError('lost'))
        self.drain_ui()
        self.p._toggle_pause_recording(None)
        recorder.set_paused.assert_not_called()
        self.assertTrue(self.p._recording_paused)

    def test_mixed_mode_never_calls_external_screen_reader_silence(self):
        self.open()
        self.p._silence_send_voice_focus_if_enabled()
        self.p.main_window.speak_output.silence_screen_reader_focus.assert_not_called()
        self.p.main_window.speak_output.silence.assert_not_called()
        self.assertFalse(self.p._voice_recording_silence_enabled())

    def test_delayed_mic_only_silence_bursts_do_not_cut_mixed_capture(self):
        self.p._silence_send_voice_focus_if_enabled()
        speech = self.p.main_window.speak_output
        speech.silence_screen_reader_focus.assert_called_once_with()
        speech.silence_screen_reader_focus.reset_mock()
        speech.silence.reset_mock()
        self.p._recording_system_audio = True
        self.drain_ui()
        speech.silence_screen_reader_focus.assert_not_called()
        speech.silence.assert_not_called()

    def test_discard_while_opening_cancels_and_restores_composer(self):
        self.p._start_system_audio_recording()
        self.p._discard_voice_message(None)
        self.assertFalse(self.p._recording_starting)
        self.assertFalse(self.p._voice_panel.shown)
        self.assertTrue(self.p._record_voice_system_btn.shown)
        self.threads.drain()
        self.drain_ui()
        self.assertFalse(self.p._is_recording)

    def test_discard_stops_exact_old_recorder_before_next_start(self):
        old = self.open()
        self.p._discard_voice_message(None)
        self.assertFalse(self.p._recording_system_audio)
        self.assertIsNone(self.p._system_audio_session)
        self.p._start_system_audio_recording()
        self.threads.drain()
        self.drain_ui()
        new = self.recorders[-1]
        old.on_frames(b'old')
        new.on_frames(b'new')
        self.assertEqual(self.p._recording_frames, [b'new'])
        old.close.assert_called()
        new.close.assert_not_called()

    def test_leaving_chat_cancels_pending_capture_and_late_completion(self):
        self.p._start_system_audio_recording()
        self.p._cancel_active_recording()
        self.p.conversation = None
        self.threads.drain()
        self.drain_ui()
        self.assertFalse(self.p._is_recording)
        self.assertFalse(self.p._recording_starting)
        self.assertFalse(self.p._recording_system_audio)
        self.assertEqual(self.p._recording_frames, [])

    def test_destroy_invalidates_opening_capture(self):
        self.p._start_system_audio_recording()
        event = SimpleNamespace(Skip=Mock(), GetEventObject=lambda: self.p)
        self.p._on_destroy(event)
        self.threads.drain()
        self.drain_ui()
        self.assertFalse(self.p._recording_starting)
        self.assertFalse(self.p._is_recording)
        self.recorders[0].close.assert_called()
        event.Skip.assert_called_once_with()


class TestFlushAndBarriers(CaptureHarness, unittest.TestCase):
    def setUp(self):
        super().setUp()
        bind(self.p, ['_finish_system_audio_for_send', '_on_system_audio_ready_to_send',
                      '_toggle_system_audio_pause', '_on_system_audio_paused'], self.namespace)
        self.sent = []
        self.p._send_voice_message = lambda event: self.sent.append(list(self.p._recording_frames))

    def test_send_waits_for_backend_flush_and_keeps_tail(self):
        recorder = self.open()
        recorder.on_frames(b'prefix')
        recorder.close.side_effect = lambda: recorder.on_frames(b'tail')
        self.p._finish_system_audio_for_send(None)
        self.assertEqual(self.sent, [])
        self.assertFalse(self.p._send_voice_btn.enabled)
        self.threads.drain()
        self.assertEqual(self.sent, [])
        self.drain_ui()
        self.assertEqual(self.sent, [[b'prefix', b'tail']])

    def test_cancel_during_stop_never_sends_into_new_conversation(self):
        self.open()
        self.p._finish_system_audio_for_send(None)
        self.p._stop_system_audio_recording()
        self.p.conversation = {'remoteJid': 'different'}
        self.threads.drain()
        self.drain_ui()
        self.assertEqual(self.sent, [])

    def test_failure_during_flush_retains_audio_without_sending(self):
        recorder = self.open()
        recorder.on_frames(b'prefix')
        recorder.close.side_effect = lambda: recorder.on_error(OSError('device lost'))
        self.p._finish_system_audio_for_send(None)
        self.threads.drain()
        self.drain_ui()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.p._recording_frames, [b'prefix'])
        self.assertTrue(self.p._recording_paused)
        self.assertTrue(self.p._send_voice_btn.enabled)

    def test_preview_shortcut_cannot_play_during_resume_or_stop(self):
        self.open()
        bind(self.p, ['_toggle_play_recorded_audio'], self.namespace)
        self.p._recording_paused = True
        self.p._recorded_audio_sound = object()
        self.p._system_audio_session['transition'] = True
        self.p._toggle_play_recorded_audio(None)
        self.p._stop_recorded_audio_preview.assert_not_called()

    def test_real_send_handler_waits_for_flush_instead_of_encoding_early(self):
        self.open()
        bind(self.p, ['_send_voice_message'], self.namespace)
        self.p._finish_system_audio_for_send = Mock()
        self.p._send_voice_message(None)
        self.p._finish_system_audio_for_send.assert_called_once_with(None)
        self.assertTrue(self.p._is_recording)

    def test_stale_pause_ack_does_not_modify_new_session(self):
        old = self.open()
        self.p._toggle_system_audio_pause()
        self.p._stop_system_audio_recording()
        self.p._is_recording = False
        self.open()
        self.assertFalse(self.p._recording_paused)
        self.assertTrue(self.p._pause_resume_btn.enabled)
        self.assertIsNot(self.p._system_audio_session['recorder'], old)
        self.p.main_window.voicemsg_pauserecording_sound.play.assert_not_called()

    def test_preview_waits_for_pause_ack_and_resume_stops_preview_first(self):
        recorder = self.open()
        self.p._toggle_system_audio_pause()
        self.assertFalse(self.p._play_recorded_btn.shown)
        recorder.set_paused.assert_not_called()
        self.threads.drain()
        self.drain_ui()
        recorder.set_paused.assert_called_once_with(True)
        self.assertTrue(self.p._play_recorded_btn.shown)
        recorder.on_frames(b'late paused callback')
        self.assertEqual(self.p._recording_frames, [])
        self.p._toggle_system_audio_pause()
        self.p._stop_recorded_audio_preview.assert_called_once_with()
        self.assertEqual(recorder.set_paused.call_count, 1)
        self.threads.drain()
        self.drain_ui()
        self.assertEqual(recorder.set_paused.call_args.args, (False,))
        self.assertFalse(self.p._recording_paused)


class TestUiWiring(unittest.TestCase):
    def test_button_is_created_and_added_immediately_after_alternate(self):
        src = CONVERSATIONS.read_text(encoding='utf-8')
        self.assertIn('self._record_voice_system_btn = wx.Button(', src)
        start = src.index('self._record_voice_alt_btn = wx.Button(')
        end = src.index('# ── Attachment staging panel', start)
        block = src[start:end]
        self.assertIn('label=i18n.t("record_voice_message_system_audio")', block)
        self.assertLess(block.index('conv_sizer.Add(self._record_voice_alt_btn'),
                        block.index('conv_sizer.Add(self._record_voice_system_btn'))
        self.assertIn('AccessibleRecordVoiceMessage("Ctrl+Shift+H")', block)
        self.assertIn('self._on_record_system_audio)', block)
        help_src = (ROOT / 'client/ui/dialogs/shortcuts_dialog.py').read_text(encoding='utf-8')
        self.assertIn('shortcut_ctrl_shift_h_label', help_src)
        self.assertIn('ord("H"),          self.ID_CTRL_SHIFT_H)', src)
        self.assertIn('self._on_record_system_audio,       id=self.ID_CTRL_SHIFT_H)', src)

    def test_button_mirrors_all_alternate_visibility_and_enablement(self):
        src = CONVERSATIONS.read_text(encoding='utf-8')
        for action in ['Show', 'Hide', 'Enable', 'Disable']:
            self.assertEqual(src.count(f'self._record_voice_alt_btn.{action}()'),
                             src.count(f'self._record_voice_system_btn.{action}()'), action)
        self.assertIn('self._record_voice_system_btn.SetLabel(i18n.t("record_voice_message_system_audio"))', src)

    def test_settings_interface_build_load_save_retranslate(self):
        path = ROOT / 'client/ui/dialogs/settings_dialog.py'
        tree = ast.parse(path.read_text(encoding='utf-8'))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'SettingsDialog')
        # Each corresponding stereo lifecycle operation must have the new checkbox next to it.
        sources = [ast.unparse(n) for n in cls.body if isinstance(n, ast.FunctionDef)]
        for marker in ['self._warn_stereo_voice_cb = wx.CheckBox',
                       'self._warn_stereo_voice_cb.SetValue',
                       'self._warn_stereo_voice_cb.GetValue',
                       'self._warn_stereo_voice_cb.SetLabel']:
            matches = [s for s in sources if marker in s and '_confirm_stereo_voice_if_newly_enabled(self)' not in s]
            self.assertTrue(matches, marker)
            for source in matches:
                self.assertIn('_warn_system_audio_cb', source)
                self.assertIn('system_audio', source)

    def test_late_mic_only_callback_cannot_write_into_new_mixed_buffer(self):
        tree = ast.parse(CONVERSATIONS.read_text(encoding='utf-8'))
        start = next(n for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef) and n.name == '_start_voice_recording')
        callback = next(n for n in ast.walk(start)
                        if isinstance(n, ast.FunctionDef) and n.name == '_callback')
        old_buffer, new_buffer = [], []
        panel = SimpleNamespace(_recording_paused=False, _recording_frames=new_buffer)
        ns = {'self': panel, 'pyaudio': None, 'recording_frames': old_buffer}
        exec(compile(ast.Module(body=[callback], type_ignores=[]), str(CONVERSATIONS), 'exec'), ns)
        ns['_callback'](b'late microphone', 1, None, 0)
        self.assertEqual(new_buffer, [])
        self.assertEqual(old_buffer, [b'late microphone'])

    def test_send_freezes_mixed_buffer_before_transferring_to_encoder(self):
        ns = functions(CONVERSATIONS, ['_send_voice_message'])
        src = ast.unparse(next(n for n in ast.walk(ast.parse(CONVERSATIONS.read_text(encoding='utf-8')))
                              if isinstance(n, ast.FunctionDef) and n.name == '_send_voice_message'))
        self.assertIn('self._stop_system_audio_recording()', src)
        self.assertLess(src.index('self._stop_system_audio_recording()'),
                        src.index('frames = self._recording_frames'))


if __name__ == '__main__':
    unittest.main()
