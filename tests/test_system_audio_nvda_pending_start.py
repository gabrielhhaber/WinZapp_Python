"""Recorder cancellation reaches an actual queued process-reader command."""
import queue
import threading
import unittest

from core.system_audio_capture import SystemAudioRecorder
from core.wasapi_process_capture import ProcessLoopbackStream
from tests.test_system_audio_capture import FakeBackend
from tests.test_system_audio_volume import QuietDiagnostics
from tests.test_wasapi_process_capture import Native, Target


class PendingStartTests(unittest.TestCase):
    def test_stop_cancels_process_start_already_dispatched_to_its_owner(self):
        entered, release = threading.Event(), threading.Event()
        logs = [[], []]
        target = Target()
        class HeldCommandQueue(queue.Queue):
            def get(self, *args, **kwargs):
                command = super().get(*args, **kwargs)
                if command[0] == 'start':
                    entered.set()
                    if not release.wait(3):
                        raise AssertionError('test did not release queued start')
                return command
        class Backend(FakeBackend):
            def open_nvda_loopbacks(self, rate, channels):
                self.pair = []
                for index, mode in enumerate((1, 0)):
                    stream = ProcessLoopbackStream(target, mode, rate, channels,
                        native_factory=lambda *args, index=index: Native(logs[index]))
                    self.pair.append(stream)
                self.pair[0]._commands = HeldCommandQueue(maxsize=1)
                return tuple(self.pair)
        backend = Backend()
        frames, runtime_errors, startup_errors, stop_errors = [], [], [], []
        recorder = SystemAudioRecorder(frames.append, runtime_errors.append, rate=8000,
            channels=2, separate_nvda=True, backend_factory=lambda: backend,
            diagnostics=QuietDiagnostics())
        def start():
            try:
                recorder.start()
            except Exception as exc:
                startup_errors.append(exc)
        def stop():
            try:
                recorder.stop()
            except Exception as exc:
                stop_errors.append(exc)
        starter, stopper = threading.Thread(target=start), threading.Thread(target=stop)
        try:
            starter.start()
            self.assertTrue(entered.wait(2), 'process Start was not queued')
            stopper.start()
            self.assertTrue(recorder._stop.wait(2))
        finally:
            release.set()
            starter.join(4)
            if stopper.ident is not None:
                stopper.join(4)
            recorder.close()
        self.assertFalse(starter.is_alive())
        self.assertFalse(stopper.is_alive())
        self.assertEqual(stop_errors, [])
        self.assertTrue(startup_errors)
        self.assertEqual(frames, [])
        self.assertEqual(runtime_errors, [])
        for log in logs:
            self.assertIn('initialize', log)
            self.assertNotIn('start', log, 'queued process Start ignored recorder cancellation')
            self.assertIn('close', log)
        self.assertEqual(target.refs, 1)


if __name__ == '__main__':
    unittest.main()
