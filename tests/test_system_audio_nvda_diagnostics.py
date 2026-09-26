"""Allowlisted three-source diagnostics; payload/identity must never reach disk."""
import json
from pathlib import Path
import tempfile
import unittest

from core.system_audio_diagnostics import CaptureDiagnostics


class NvdaDiagnosticsTests(unittest.TestCase):
    def test_real_worker_reports_nvda_failure_and_all_three_packet_counts(self):
        from core.system_audio_capture import SystemAudioRecorder
        from tests.test_system_audio_nvda_capture import NvdaBackend
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'capture.log'
            backend, frames, errors = NvdaBackend(), [], []
            recorder = SystemAudioRecorder(frames.append, errors.append, rate=8000, channels=2,
                backend_factory=lambda: backend, separate_nvda=True,
                diagnostics=CaptureDiagnostics(rate=8000, channels=2, path=path))
            try:
                recorder.start()
                backend.feed()
                backend.nvda.error = RuntimeError('The pinned NVDA process exited')
                self.assertTrue(recorder._done.wait(2))
            finally:
                recorder.stop()
            records = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
            primary = next(row for row in records if row.get('primary'))
            self.assertEqual((primary['source'], primary['phase'], primary['reason']),
                             ('nvda', 'read', 'nvda_exited'))
            for source in ('microphone', 'other', 'nvda'):
                self.assertEqual(primary[source + '_packets'], 1)
                self.assertEqual(primary[source + '_frames'], 80)
            self.assertEqual(records[-1]['delivered_frames'], 80)
            self.assertEqual(len(errors), 1)
            self.assertTrue(frames)

    def test_process_queue_metadata_is_allowlisted_without_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'capture.log'
            log = CaptureDiagnostics(rate=8000, channels=2, path=path)
            allowed = dict(queue_peak_frames=480, packets=4, frames=480,
                           process_loopback=1, failed=0, queued_frames=80,
                           cleanup_failed=0, reader_alive=1)
            log.update(source='nvda', native=dict(allowed, pid=987654,
                       process_name='SECRET', payload=b'PRIVATE'))
            log.event('stop')
            log.close()
            text = path.read_text(encoding='utf-8')
            self.assertEqual(json.loads(text)['native']['nvda'], allowed)
            for private in ('987654', 'SECRET', 'PRIVATE', 'process_name', 'payload', 'pid'):
                self.assertNotIn(private, text)

    def test_static_native_failures_have_reasons_without_exception_text(self):
        import ast
        from core import wasapi_process_capture
        from core.system_audio_diagnostics import _REASONS
        tree = ast.parse(Path(wasapi_process_capture.__file__).read_text(encoding='utf-8'))
        messages = {node.exc.args[0].value for node in ast.walk(tree)
                    if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
                    and node.exc.args and isinstance(node.exc.args[0], ast.Constant)
                    and isinstance(node.exc.args[0].value, str)}
        self.assertEqual(sorted(messages - _REASONS.keys()), [])

    def test_process_sources_have_private_numeric_counters(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'capture.log'
            log = CaptureDiagnostics(rate=8000, channels=2, path=path)
            for source in ('other', 'nvda'):
                for _ in range(10):
                    log.packet(source, packet_frames=80, payload=b'SECRETPCM', pid=932345)
                log.update(source=source, phase='read', native={
                    'running': 1, 'total_frames': 800, 'pid': 932345, 'name': 'SECRETNAME',
                    'data': b'SECRETPCM'}, **{source + '_late_packets': 2,
                    source + '_late_frames': 40, source + '_read_ms': 12.5})
            log.event('stop')
            log.close()
            text = path.read_text(encoding='utf-8')
            record = json.loads(text)
            self.assertEqual(record['source'], 'nvda')
            for source in ('other', 'nvda'):
                self.assertEqual(record[source + '_packets'], 10)
                self.assertEqual(record[source + '_frames'], 800)
                self.assertEqual(record[source + '_late_packets'], 2)
                self.assertEqual(record[source + '_late_frames'], 40)
                self.assertEqual(record[source + '_read_ms'], 12.5)
                self.assertEqual(record['native'][source], {'running': 1, 'total_frames': 800})
            self.assertEqual(len(record['recent']), 8)
            for secret in ('932345', 'pid', 'SECRET', 'payload', 'name'):
                self.assertNotIn(secret, text)


if __name__ == '__main__':
    unittest.main()
