"""Synthetic diagnostics tests: no devices, GUI, or real account data."""
import importlib
import json
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class WriterTests(unittest.TestCase):
    def diagnostics_class(self):
        try:
            module = importlib.import_module('core.system_audio_diagnostics')
        except ImportError:
            self.fail('persistent capture diagnostics are not implemented')
        return module.CaptureDiagnostics

    def test_all_static_capture_failures_have_allowlisted_reasons(self):
        import ast
        from core import system_audio_capture, wasapi_capture
        cls = self.diagnostics_class()
        messages = set()
        for module in (system_audio_capture, wasapi_capture):
            tree = ast.parse(Path(module.__file__).read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
                    args = node.exc.args
                    if args and isinstance(args[0], ast.Constant) and isinstance(args[0].value, str):
                        messages.add(args[0].value)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'capture.log'
            log = cls(rate=48000, channels=2, path=path)
            for message in sorted(messages):
                log.event('error', error=RuntimeError(message))
            log.close()
            records = [json.loads(s) for s in path.read_text(encoding='utf-8').splitlines()]
            self.assertEqual(len(records), len(messages))
            missing = [message for message, record in zip(sorted(messages), records)
                       if record['reason'] == 'unknown']
            self.assertEqual(missing, [])

    def test_rotation_bounded_observations_and_unsafe_fields(self):
        cls = self.diagnostics_class()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'capture.log'
            log = cls(rate=8000, channels=1, path=path)
            log.event('start')
            self.assertEqual((log._handler.maxBytes, log._handler.backupCount), (1048576, 2))
            initial_size = path.stat().st_size
            for n in range(1000):
                log.update(phase='SECRET', source='device name', chat_id='123456789@lid',
                           callback_ms=float('nan'), delivered_frames='PRIVATE',
                           native={'flags': b'PCM'})
                log.packet('microphone', packet_frames=80, deadline_frames=n,
                           audio=b'PCM', name='SECRET')
            self.assertEqual(path.stat().st_size, initial_size)  # no per-packet disk writes
            log.event('error', error=ValueError('SECRET'))
            record = json.loads(path.read_text(encoding='utf-8').splitlines()[-1])
            self.assertEqual(len(record['recent']), 8)
            self.assertEqual(record['microphone_packets'], 1000)
            self.assertEqual(record['microphone_frames'], 80000)
            self.assertEqual(record['delivered_frames'], 0)
            log._handler.maxBytes = 4096
            for _ in range(20):
                log.event('stop')
            log.close()
            files = list(Path(folder).iterdir())
            self.assertEqual({f.name for f in files}, {'capture.log', 'capture.log.1', 'capture.log.2'})
            for file in files:
                self.assertLessEqual(file.stat().st_size, 4096)
                text = file.read_text(encoding='utf-8')
                for secret in ('SECRET', 'PRIVATE', 'PCM', 'device name', '123456789'):
                    self.assertNotIn(secret, text)
                for line in text.splitlines():
                    json.loads(line)

    def test_unknown_exception_messages_and_custom_type_names_never_leak(self):
        cls = self.diagnostics_class()
        class SecretError(Exception):
            def __str__(self):
                raise AssertionError('must not stringify exceptions')
        SecretError.__name__ = 'Alice_Private_Device'
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'capture.log'
            log = cls(rate=8000, channels=1, path=path)
            log.event('error', error=TypeError('Alice Private Device'))
            log.event('error', error=SecretError('Alice Private Device'))
            log.close()
            text = path.read_text(encoding='utf-8')
            records = [json.loads(s) for s in text.splitlines()]
            self.assertEqual([r['exception_type'] for r in records], ['TypeError', 'Exception'])
            self.assertEqual([r['reason'] for r in records], ['unknown', 'unknown'])
            self.assertNotIn('Alice', text)
            self.assertNotIn('Private', text)

    def test_concurrent_errors_have_one_primary_and_close_never_reopens(self):
        import threading
        cls = self.diagnostics_class()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'capture.log'
            log = cls(rate=8000, channels=1, path=path)
            barrier = threading.Barrier(8)
            def emit():
                barrier.wait(2)
                log.event('error', error=TimeoutError('Timed out changing capture pause state'))
            workers = [threading.Thread(target=emit) for _ in range(8)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(3)
                self.assertFalse(worker.is_alive())
            log.close()
            before = path.read_bytes()
            log.event('error', error=RuntimeError('late'))
            self.assertEqual(path.read_bytes(), before)
            records = [json.loads(s) for s in before.decode('utf-8').splitlines()]
            self.assertEqual(len(records), 8)
            self.assertEqual(sum(r['primary'] for r in records), 1)

    def test_bad_path_and_handler_failures_are_silent(self):
        import io
        cls = self.diagnostics_class()
        with tempfile.TemporaryDirectory() as folder:
            blocked = Path(folder) / 'not_a_directory'
            blocked.write_text('occupied', encoding='utf-8')
            log = cls(rate=8000, channels=1, path=blocked / 'capture.log')
            with patch('sys.stderr', new_callable=io.StringIO) as stderr:
                log.event('start')
                log.close()
                log = cls(rate=8000, channels=1, path=Path(folder) / 'capture.log')
                log.event('start')
                with patch.object(log._handler, 'shouldRollover', side_effect=OSError('PRIVATE')):
                    log.event('error', error=RuntimeError('PRIVATE'))
                log.close()
                self.assertEqual(stderr.getvalue(), '')

    def test_lazy_account_scoped_append_is_private_and_isolated(self):
        cls = self.diagnostics_class()
        root = logging.getLogger()
        handlers, level = list(root.handlers), root.level
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'account' / 'system_audio_capture.log'
            with patch('app_paths.log_path', return_value=str(path)) as resolve:
                importlib.reload(importlib.import_module('core.system_audio_diagnostics'))
                diagnostic = cls(rate=48000, channels=2)
                resolve.assert_not_called()
                diagnostic.event('start')
                diagnostic.event('error', error=RuntimeError('Alice /private/device 123456789@lid'))
                diagnostic.close()
                resolve.assert_called_once_with('system_audio_capture.log')
            diagnostic = cls(rate=48000, channels=2, path=path)
            diagnostic.event('stop')
            diagnostic.close()
            text = path.read_text(encoding='utf-8')
            records = [json.loads(line) for line in text.splitlines()]
            self.assertEqual([r['event'] for r in records], ['start', 'error', 'stop'])
            self.assertEqual(records[1]['reason'], 'unknown')
            self.assertEqual(records[1]['exception_type'], 'RuntimeError')
            self.assertEqual(records[0]['rate'], 48000)
            self.assertGreater(records[0].get('time_unix_ms', 0), 0)
            for secret in ('Alice', '/private', '123456789', 'device'):
                self.assertNotIn(secret, text)
        self.assertEqual(root.handlers, handlers)
        self.assertEqual(root.level, level)


class RecorderDiagnosticsTests(unittest.TestCase):
    def test_microphone_stall_has_source_and_idle_time(self):
        import itertools
        import threading
        from core.system_audio_capture import SystemAudioRecorder
        from core.system_audio_diagnostics import CaptureDiagnostics
        from tests.test_system_audio_capture import FakeBackend
        backend = FakeBackend()
        backend.clock = lambda: next(ticks)
        ticks = itertools.count(0, 30_000_000)
        backend.mic.read_packets = lambda: []
        backend.loop.read_packets = lambda: []
        done = threading.Event()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'capture.log'
            recorder = SystemAudioRecorder(lambda pcm: None, lambda exc: done.set(), rate=8000,
                backend_factory=lambda: backend,
                diagnostics=CaptureDiagnostics(rate=8000, channels=1, path=path))
            try:
                recorder.start()
                self.assertTrue(done.wait(1))
            finally:
                recorder.stop()
            records = [json.loads(s) for s in path.read_text(encoding='utf-8').splitlines()]
            primary = next(r for r in records if r.get('primary'))
            self.assertEqual(primary['reason'], 'microphone_stalled')
            self.assertEqual((primary['phase'], primary['source']), ('read', 'microphone'))
            self.assertGreater(primary['mic_idle_ms'], 2000)

    def test_pause_reset_failure_identifies_source_and_phase(self):
        import threading
        from core.system_audio_capture import SystemAudioRecorder
        from core.system_audio_diagnostics import CaptureDiagnostics
        from tests.test_system_audio_capture import FakeBackend
        backend = FakeBackend()
        done = threading.Event()
        original = RuntimeError('SECRET reset failure')
        def reset():
            raise original
        backend.loop.reset = reset
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'capture.log'
            recorder = SystemAudioRecorder(lambda pcm: None, lambda exc: done.set(), rate=8000,
                backend_factory=lambda: backend,
                diagnostics=CaptureDiagnostics(rate=8000, channels=1, path=path))
            try:
                recorder.start()
                with self.assertRaises(RuntimeError):
                    recorder.set_paused(True)
                self.assertTrue(done.wait(1))
            finally:
                recorder.stop()
            records = [json.loads(s) for s in path.read_text(encoding='utf-8').splitlines()]
            primary = next(r for r in records if r.get('primary'))
            self.assertEqual((primary['phase'], primary['source']), ('reset', 'loopback'))
            self.assertIs(recorder.failure, original)

    def test_callback_error_and_broken_sink_never_change_capture_failure(self):
        import threading
        from core.system_audio_capture import SystemAudioRecorder
        from core.system_audio_diagnostics import CaptureDiagnostics
        from tests.test_system_audio_capture import FakeBackend
        class BrokenSink:
            def __getattr__(self, name):
                raise OSError('SECRET unavailable sink')
        for broken in (False, True):
            with self.subTest(broken=broken), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'capture.log'
                done = threading.Event()
                calls, errors = [], []
                original = RuntimeError('SECRET callback /private/name')
                def frames(pcm):
                    calls.append(pcm)
                    raise original
                def failed(exc):
                    errors.append(exc)
                    done.set()
                sink = BrokenSink() if broken else CaptureDiagnostics(rate=8000, channels=1, path=path)
                recorder = SystemAudioRecorder(frames, failed, rate=8000,
                    backend_factory=FakeBackend, diagnostics=sink)
                try:
                    recorder.start()
                    self.assertTrue(done.wait(2))
                finally:
                    recorder.stop()
                self.assertEqual(len(calls), 1)
                self.assertEqual(errors, [original])
                self.assertIs(recorder.failure, original)
                if not broken:
                    text = path.read_text(encoding='utf-8')
                    records = [json.loads(s) for s in text.splitlines()]
                    primary = next(r for r in records if r.get('primary'))
                    self.assertEqual((primary['phase'], primary['source']), ('frames', 'callback'))
                    self.assertEqual(primary['reason'], 'unknown')
                    self.assertEqual(primary['delivered_frames'], 0)
                    self.assertGreater(primary.get('callback_ms', -1), 0)
                    self.assertNotIn('SECRET', text)

    def test_native_packet_metadata_is_persisted_on_read_failure(self):
        import threading
        from core.system_audio_capture import SystemAudioRecorder
        from core.system_audio_diagnostics import CaptureDiagnostics
        from tests.test_system_audio_capture import FakeBackend
        from tests.test_wasapi_capture import NativeHarness
        backend = FakeBackend()
        native = NativeHarness([(12, 4, bytes(8))])
        backend.mic = native.stream()
        done = threading.Event()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'capture.log'
            recorder = SystemAudioRecorder(lambda pcm: None, lambda exc: done.set(), rate=8000,
                backend_factory=lambda: backend,
                diagnostics=CaptureDiagnostics(rate=8000, channels=1, path=path))
            try:
                recorder.start()
                self.assertTrue(done.wait(1))
            finally:
                recorder.stop()
            errors = [json.loads(s) for s in path.read_text(encoding='utf-8').splitlines()
                      if json.loads(s)['event'] == 'error']
            self.assertEqual(errors[0]['reason'], 'unreliable_timestamp')
            self.assertEqual(errors[0].get('native', {}).get('microphone', {}).get('flags'), 4)
            self.assertEqual(errors[0]['native']['microphone']['packet_frames'], 2)
            self.assertEqual(errors[0]['native']['microphone']['state'], 1)

    def test_late_packet_reason_deadline_and_prefix_are_preserved(self):
        import threading
        from core.system_audio_capture import AudioPacket, SystemAudioRecorder
        from core.system_audio_diagnostics import CaptureDiagnostics
        from tests.test_system_audio_capture import FakeBackend
        backend = FakeBackend()
        frames, errors = [], []
        delivered, failed = threading.Event(), threading.Event()
        def receive(data):
            frames.append(data)
            delivered.set()
        def failure(exc):
            errors.append(exc)
            failed.set()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'capture.log'
            recorder = SystemAudioRecorder(receive, failure, rate=8000,
                backend_factory=lambda: backend,
                diagnostics=CaptureDiagnostics(rate=8000, channels=1, path=path))
            try:
                recorder.start()
                self.assertTrue(delivered.wait(2))
                prefix = b''.join(frames)
                backend.loop.read_packets = lambda: [AudioPacket(0, bytes(4))]
                self.assertTrue(failed.wait(2))
            finally:
                recorder.stop()
            records = [json.loads(s) for s in path.read_text(encoding='utf-8').splitlines()]
            primary = next(r for r in records if r.get('primary'))
            self.assertEqual(primary['reason'], 'late_packet')
            self.assertEqual((primary['phase'], primary['source']), ('mix', 'loopback'))
            self.assertLess(primary['recent'][-1]['deadline_frames'], 0)
            self.assertTrue(b''.join(frames).startswith(prefix))
            self.assertEqual(len(errors), 1)

    def test_lifecycle_metrics_and_original_error_survive_cleanup(self):
        import inspect
        import threading
        from core.system_audio_capture import SystemAudioRecorder
        from core.system_audio_diagnostics import CaptureDiagnostics
        from core.wasapi_capture import WasapiError
        from tests.test_system_audio_capture import FakeBackend
        self.assertIn('diagnostics', inspect.signature(SystemAudioRecorder).parameters)
        backend = FakeBackend()
        frames, errors = [], []
        delivered, failed = threading.Event(), threading.Event()
        def receive(data):
            frames.append(data)
            delivered.set()
        def failure(exc):
            errors.append(exc)
            failed.set()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'capture.log'
            diagnostics = CaptureDiagnostics(rate=8000, channels=2, path=path)
            recorder = SystemAudioRecorder(receive, failure, rate=8000, channels=2,
                                           backend_factory=lambda: backend, diagnostics=diagnostics)
            try:
                recorder.start()
                self.assertTrue(delivered.wait(2))
                recorder.set_paused(True)
                recorder.set_paused(False)
                original = WasapiError('WASAPI capture data discontinuity')
                backend.loop.error = original
                def broken_close():
                    raise OSError('SECRET device cleanup path')
                backend.loop.close = broken_close
                self.assertTrue(failed.wait(2))
            finally:
                recorder.stop()
            text = path.read_text(encoding='utf-8')
            records = [json.loads(line) for line in text.splitlines()]
            events = [r['event'] for r in records]
            for event in ('start', 'opened', 'pause', 'resume', 'error', 'stop'):
                self.assertIn(event, events)
            primary = next(r for r in records if r.get('primary'))
            self.assertEqual(primary['reason'], 'capture_discontinuity')
            self.assertEqual((primary['phase'], primary['source']), ('read', 'loopback'))
            self.assertGreater(primary['delivered_frames'], 0)
            self.assertGreater(primary['microphone_packets'], 0)
            self.assertGreater(primary['loopback_packets'], 0)
            self.assertGreater(primary.get('microphone_read_ms', -1), 0)
            self.assertGreater(primary.get('loopback_read_ms', -1), 0)
            self.assertTrue(primary['recent'])
            self.assertLessEqual(len(primary['recent']), 8)
            self.assertEqual(sum(r.get('primary', False) for r in records), 1)
            self.assertEqual(records[-1]['delivered_frames'], sum(len(p) // 4 for p in frames))
            self.assertIs(recorder.failure, original)
            self.assertEqual(errors, [original])
            self.assertNotIn('SECRET', text)
            self.assertTrue(diagnostics._closed)


class TimeoutDiagnosticsTests(unittest.TestCase):
    def test_all_bounded_wait_failures_are_persisted_before_worker_exits(self):
        import queue
        import threading
        from core.system_audio_capture import SystemAudioRecorder
        from core.system_audio_diagnostics import CaptureDiagnostics
        from tests.test_system_audio_capture import FakeBackend
        for mode, reason in [('startup', 'startup_timeout'), ('pause', 'pause_timeout'),
                             ('stop', 'stop_timeout'), ('queue', 'pause_queue_timeout')]:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'capture.log'
                gate, entered = threading.Event(), threading.Event()
                backend = FakeBackend()
                def factory():
                    if mode == 'startup':
                        entered.set()
                        gate.wait(2)
                    return backend
                original_read = backend.mic.read_packets
                def read():
                    entered.set()
                    gate.wait(2)
                    return original_read()
                if mode != 'startup':
                    backend.mic.read_packets = read
                diagnostic = CaptureDiagnostics(rate=8000, channels=1, path=path)
                recorder = SystemAudioRecorder(lambda pcm: None, lambda exc: None,
                    rate=8000, backend_factory=factory, diagnostics=diagnostic,
                    startup_timeout=.03, shutdown_timeout=.03)
                try:
                    if mode == 'startup':
                        with self.assertRaises(TimeoutError):
                            recorder.start()
                    else:
                        recorder.start()
                        self.assertTrue(entered.wait(1))
                        if mode == 'queue':
                            for _ in range(16):
                                recorder._commands.put_nowait((True, backend.clock(), threading.Event()))
                            with self.assertRaises(queue.Full):
                                recorder.set_paused(True)
                        elif mode == 'pause':
                            with self.assertRaises(TimeoutError):
                                recorder.set_paused(True)
                        else:
                            with self.assertRaises(TimeoutError):
                                recorder.stop()
                    records = [json.loads(s) for s in path.read_text(encoding='utf-8').splitlines()]
                    errors = [r for r in records if r['event'] == 'error']
                    self.assertTrue(errors, 'timeout was not persisted while worker was blocked')
                    self.assertEqual(errors[0]['reason'], reason)
                    self.assertTrue(errors[0]['primary'])
                    self.assertFalse(diagnostic._closed)
                finally:
                    gate.set()
                    recorder.stop()
                self.assertTrue(diagnostic._closed)


class NativeDiagnosticsTests(unittest.TestCase):
    def test_release_failure_does_not_hide_original_packet_error(self):
        import threading
        from core import wasapi_capture as w
        from core.system_audio_capture import SystemAudioRecorder
        from core.system_audio_diagnostics import CaptureDiagnostics
        from tests.test_system_audio_capture import FakeBackend
        from tests.test_wasapi_capture import NativeHarness
        backend = FakeBackend()
        native = NativeHarness([(12, 4, bytes(8))])
        backend.mic = native.stream()
        original_call = native.capture.call
        def call(index, types, *args):
            if index == 4:
                w.check_hresult(0x88890004, 'COM method 4')
            return original_call(index, types, *args)
        native.capture.call = call
        done = threading.Event()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'capture.log'
            recorder = SystemAudioRecorder(lambda pcm: None, lambda exc: done.set(), rate=8000,
                backend_factory=lambda: backend,
                diagnostics=CaptureDiagnostics(rate=8000, channels=1, path=path))
            try:
                recorder.start()
                self.assertTrue(done.wait(1))
            finally:
                recorder.stop()
            records = [json.loads(s) for s in path.read_text(encoding='utf-8').splitlines()]
            errors = [r for r in records if r['event'] == 'error']
            self.assertEqual(errors[0]['reason'], 'unreliable_timestamp')
            self.assertTrue(errors[0]['primary'])
            self.assertEqual(errors[1]['reason'], 'hresult_failure')
            self.assertFalse(errors[1]['primary'])
            # Diagnostics only: do not change the exception/callback contract.
            self.assertEqual(recorder.failure.hresult, 0x88890004)

    def test_packet_failure_retains_safe_metadata_and_structured_hresult(self):
        from core import wasapi_capture as w
        from tests.test_wasapi_capture import NativeHarness
        h = NativeHarness([(12, 1, bytes(4)), (300, 4, bytes(8))])
        stream = h.stream()
        try:
            with self.assertRaisesRegex(w.WasapiError, 'timestamp'):
                stream.read_packets()
            self.assertTrue(hasattr(stream, 'diagnostic_snapshot'))
            snapshot = stream.diagnostic_snapshot()
            self.assertEqual(snapshot['flags'], 4)
            self.assertEqual(snapshot['packet_frames'], 2)
            self.assertEqual(snapshot['state'], 1)
            self.assertEqual(snapshot['seen_packet'], 1)
            self.assertEqual(snapshot['total_frames'], 3)
            self.assertEqual(snapshot['stamp_delta_100ns'], 288)
            self.assertTrue(all(type(v) is int for v in snapshot.values()))
            snapshot['flags'] = 99
            self.assertEqual(stream.diagnostic_snapshot()['flags'], 4)
        finally:
            stream.close()
        with tempfile.TemporaryDirectory() as folder:
            from core.system_audio_diagnostics import CaptureDiagnostics
            path = Path(folder) / 'capture.log'
            log = CaptureDiagnostics(rate=48000, channels=1, path=path)
            try:
                w.check_hresult(0x88890004, 'COM method 3')
            except w.WasapiError as exc:
                self.assertEqual(getattr(exc, 'hresult', None), 0x88890004)
                log.event('error', error=exc)
            log.close()
            record = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(record['hresult'], '0x88890004')
            self.assertEqual(record['operation'], 'COM method 3')
            self.assertEqual(record['reason'], 'hresult_failure')


if __name__ == '__main__':
    unittest.main()
