"""Cancel an in-flight resume through the real worker, without devices or GUI."""
import threading
import unittest

from core.system_audio_capture import SystemAudioRecorder
from tests.test_system_audio_nvda_capture import NvdaBackend
from tests.test_system_audio_volume import QuietDiagnostics


class CancelResumeTests(unittest.TestCase):
    def test_cancel_during_microphone_resume_never_starts_later_process_streams(self):
        backend, pcm, failures = NvdaBackend(), [], []
        recorder = SystemAudioRecorder(pcm.append, failures.append, rate=8000, channels=2,
            separate_nvda=True, backend_factory=lambda: backend,
            diagnostics=QuietDiagnostics())
        entered, release = threading.Event(), threading.Event()
        resume_errors, stop_errors = [], []
        recorder.start()
        backend.feed()
        recorder.set_paused(True)
        prefix = b''.join(pcm)
        self.assertTrue(prefix)
        before = len(backend.log)
        original_start = backend.mic.start
        def delayed_start():
            entered.set()
            if not release.wait(3):
                raise AssertionError('test did not release microphone start')
            original_start()
        backend.mic.start = delayed_start
        def resume():
            try:
                recorder.set_paused(False)
            except Exception as exc:
                resume_errors.append(exc)
        def stop():
            try:
                recorder.stop()
            except Exception as exc:
                stop_errors.append(exc)
        resumer = threading.Thread(target=resume)
        stopper = threading.Thread(target=stop)
        try:
            resumer.start()
            self.assertTrue(entered.wait(2))
            stopper.start()
            self.assertTrue(recorder._stop.wait(2))
        finally:
            release.set()
            resumer.join(4)
            if stopper.ident is not None:
                stopper.join(4)
            recorder.close()
        self.assertFalse(resumer.is_alive())
        self.assertFalse(stopper.is_alive())
        self.assertEqual(stop_errors, [])
        self.assertEqual(failures, [])
        self.assertEqual(b''.join(pcm), prefix)
        starts = [kind for kind, action in backend.log[before:] if action == 'start']
        self.assertEqual(starts, ['mic'], 'Cancel must prevent later process Start calls')


if __name__ == '__main__':
    unittest.main()
