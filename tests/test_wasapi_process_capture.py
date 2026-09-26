"""Synthetic only: no real audio, Controller DLL, wx or native activation."""
import importlib
import queue
import struct
import sys
import threading
import time
import unittest


def module():
    try:
        return importlib.import_module('core.wasapi_process_capture')
    except ImportError:
        raise AssertionError('production process-loopback backend is missing')


class Target:
    pid = 42
    def __init__(self):
        self.refs = 1
        self.alive = True
    def acquire(self):
        self.refs += 1
    def release(self):
        self.refs -= 1
    def check(self):
        if not self.alive:
            raise RuntimeError('pinned NVDA process exited')


class Native:
    def __init__(self, log):
        self.log = log
        self.owner = threading.get_ident()
        self.packets = queue.Queue()
        self.tail = None
        self.call('initialize')
    def call(self, action):
        assert threading.get_ident() == self.owner
        self.log.append(action)
    def start(self):
        self.call('start')
    def stop(self):
        self.call('stop')
        if self.tail is not None:
            self.packets.put(self.tail)
            self.tail = None
    def read(self):
        self.call('read')
        try:
            return self.packets.get_nowait()
        except queue.Empty:
            return None
    def close(self):
        self.call('close')
    def diagnostic_snapshot(self):
        return {}


def packet(stamp=100000, frames=8, channels=2):
    from core.system_audio_capture import AudioPacket
    return AudioPacket(stamp, struct.pack('<f', .25) * frames * channels)


class WorkerTests(unittest.TestCase):
    def make_stream(self, **kwargs):
        m = module()
        self.log, self.target, self.natives = [], Target(), []
        def factory(target, mode, rate, channels, cancel):
            n = Native(self.log)
            self.natives.append(n)
            return n
        stream = m.ProcessLoopbackStream(self.target, 0, 48000, 2,
                                        native_factory=factory, **kwargs)
        self.addCleanup(stream.close)
        return stream

    def test_constructor_only_initializes_owner_and_stop_preserves_tail_reset_resumes(self):
        stream = self.make_stream()
        native = self.natives[0]
        self.assertNotEqual(native.owner, threading.get_ident())
        self.assertEqual(self.log, ['initialize'])
        self.assertEqual(stream.read_packets(), [])
        stream.start()
        native.tail = packet()
        stream.stop()
        self.assertEqual(stream.read_packets(), [packet()])
        native.packets.put(packet(200000))
        stream.reset()
        self.assertFalse(stream.running)
        self.assertEqual(stream.read_packets(), [])
        native.tail = packet(300000)
        stream.start()
        stream.stop()
        self.assertEqual(stream.read_packets(), [packet(300000)])
        stream.close()
        self.assertNotIn('reset', self.log)
        self.assertEqual(self.target.refs, 1)
        self.assertEqual(self.log[-1], 'close')

    def test_timeout_during_start_identity_check_prevents_later_native_start(self):
        stream = self.make_stream(timeout=.03)
        gate = threading.Event()
        original = self.target.check
        calls = []
        def check():
            calls.append(1)
            # First check may be the top of the idle owner loop; gate all
            # checks so cancellation races either queued or dispatched Start.
            gate.wait(1)
            original()
        self.target.check = check
        with self.assertRaisesRegex(RuntimeError, 'acknowledgement timeout'):
            stream.start()
        gate.set()
        stream._thread.join(1)
        self.assertNotIn('start', self.log)

    def test_close_timeout_and_target_release_failure_are_visible(self):
        stream = self.make_stream(timeout=.03)
        native = self.natives[0]
        gate, entered = threading.Event(), threading.Event()
        original = native.close
        def close():
            entered.set()
            gate.wait(1)
            original()
        native.close = close
        with self.assertRaisesRegex(RuntimeError, 'cleanup timeout'):
            stream.close()
        self.assertTrue(entered.is_set())
        gate.set()
        stream._thread.join(1)
        stream.close()

    def test_pin_release_failure_is_reported_without_unhandled_worker_exception(self):
        target = Target()
        def release():
            target.refs -= 1
            raise RuntimeError('pin cleanup failed')
        target.release = release
        stream = module().ProcessLoopbackStream(target, 0, 48000, 2,
                                               native_factory=lambda *args: Native([]))
        with self.assertRaisesRegex(RuntimeError, 'pin cleanup failed'):
            stream.close()
        self.assertTrue(stream.diagnostic_snapshot()['cleanup_failed'])

    def test_fatal_command_timeout_is_visible_to_reads_and_metadata(self):
        stream = self.make_stream(timeout=.03)
        gate = threading.Event()
        original = self.natives[0].read
        self.natives[0].read = lambda: (gate.wait(1), original())[1]
        with self.assertRaisesRegex(RuntimeError, 'acknowledgement timeout'):
            stream.stop()
        try:
            self.assertTrue(stream.diagnostic_snapshot()['failed'])
            with self.assertRaisesRegex(RuntimeError, 'timeout'):
                stream.read_packets()
        finally:
            gate.set()
            stream._thread.join(1)

    def test_metadata_only_snapshot_has_no_pcm_pid_or_absolute_stamp(self):
        stream = self.make_stream()
        self.natives[0].tail = packet()
        stream.start()
        stream.stop()
        snapshot = stream.diagnostic_snapshot()
        self.assertTrue(all(isinstance(v, (int, float)) for v in snapshot.values()))
        self.assertFalse({'pid', 'data', 'timestamp', 'device', 'identity'} & snapshot.keys())

    def test_reader_queues_while_consumer_is_stalled_then_drains_all_pcm(self):
        stream = self.make_stream()
        stream.start()
        packets = [packet(100000 + i * 100000, frames=480) for i in range(65)]
        for p in packets:
            self.natives[0].packets.put(p)
        # Consumer intentionally does not call read_packets. The native owner
        # must still release/copy every packet independently of that consumer.
        deadline = time.monotonic() + 1
        while stream.diagnostic_snapshot()['packets'] < 65 and time.monotonic() < deadline:
            time.sleep(.001)
        self.assertEqual(stream.diagnostic_snapshot()['queued_frames'], 65 * 480)
        self.assertEqual(stream.read_packets(), packets)
        self.assertEqual(stream.read_packets(), [])
        stream.stop()

    def test_start_failure_is_returned_synchronously_and_cleanup_is_owned(self):
        stream = self.make_stream()
        def fail():
            self.natives[0].call('start-failed')
            raise RuntimeError('native Start failed')
        self.natives[0].start = fail
        with self.assertRaisesRegex(RuntimeError, 'Start failed'):
            stream.start()
        stream._thread.join(1)
        self.assertEqual(self.log[-1], 'close')
        self.assertFalse(stream.running)

    def test_queue_is_bounded_and_failure_preserves_copied_prefix(self):
        stream = self.make_stream()
        native = self.natives[0]
        native.packets.put(packet(frames=48000))
        native.packets.put(packet(200000, frames=48000))
        native.packets.put(packet(300000))
        stream.start()
        stream._thread.join(1)
        self.assertFalse(stream._thread.is_alive())
        self.assertEqual(len(stream.read_packets()), 2)
        with self.assertRaisesRegex(RuntimeError, 'overflow'):
            stream.read_packets()
        self.assertEqual(stream.diagnostic_snapshot()['queue_peak_frames'], 96000)

    def test_target_exit_stops_reader_and_cannot_resume(self):
        stream = self.make_stream()
        stream.start()
        self.target.alive = False
        stream._thread.join(1)
        with self.assertRaisesRegex(RuntimeError, 'exited'):
            stream.read_packets()
        with self.assertRaisesRegex(RuntimeError, 'exited'):
            stream.start()
        self.assertEqual(self.log[-1], 'close')

    def test_initialization_timeout_never_delays_start_and_keeps_target_until_cleanup(self):
        gate, exited = threading.Event(), threading.Event()
        log, target = [], Target()
        def factory(*args):
            gate.wait(1)
            try:
                return Native(log)
            finally:
                exited.set()
        with self.assertRaisesRegex(RuntimeError, 'initialization timeout'):
            module().ProcessLoopbackStream(target, 0, 48000, 2,
                                           native_factory=factory, timeout=.02)
        self.assertEqual(target.refs, 2)
        gate.set()
        self.assertTrue(exited.wait(1))
        deadline = time.monotonic() + 1
        while target.refs != 1 and time.monotonic() < deadline:
            time.sleep(.001)
        self.assertEqual(target.refs, 1)
        self.assertNotIn('start', log)

    def test_cancel_queued_start_while_owner_busy_never_calls_native_start(self):
        stream = self.make_stream(timeout=.03)
        native = self.natives[0]
        entered, gate = threading.Event(), threading.Event()
        original = native.read
        def blocked_read():
            entered.set()
            gate.wait(1)
            return original()
        native.read = blocked_read
        # stop drains on the owner even when already stopped.
        with self.assertRaisesRegex(RuntimeError, 'acknowledgement timeout'):
            stream.stop()
        self.assertTrue(entered.is_set())
        with self.assertRaisesRegex(RuntimeError, 'closed|timeout'):
            stream.start()
        gate.set()
        stream._thread.join(1)
        self.assertNotIn('start', self.log)


class FakeProcessAPI:
    def __init__(self):
        self.rows = [(11, 'nvda.exe'), (22, 'NVDA.EXE'), (33, 'other.exe')]
        self.sessions = {11: 2, 22: 1, 33: 1}
        self.current = 1
        self.identities = {11: 111, 22: 222}
        self.closed = []
        self.exited = set()
    def processes(self):
        return self.rows
    def current_session(self):
        return self.current
    def session(self, pid):
        return self.sessions[pid]
    def open(self, pid):
        return pid
    def image_name(self, handle):
        return 'nvda.exe'
    def identity(self, handle):
        return self.identities[handle]
    def alive(self, handle):
        return handle not in self.exited
    def close(self, handle):
        self.closed.append(handle)


class DiscoveryTests(unittest.TestCase):
    def test_pid_reuse_between_snapshot_and_open_cannot_pin_another_image(self):
        api = FakeProcessAPI()
        api.image_name = lambda handle: 'other.exe'
        with self.assertRaisesRegex(RuntimeError, 'identity'):
            module().discover_nvda(api=api)
        self.assertEqual(api.closed, [22])

    def test_session_change_during_open_fails_closed(self):
        api = FakeProcessAPI()
        def changed(pid):
            api.sessions[pid] = 99
            return pid
        api.open = changed
        with self.assertRaisesRegex(RuntimeError, 'session'):
            module().discover_nvda(api=api)
        self.assertEqual(api.closed, [22])

    def test_current_session_only_pins_creation_identity_and_refcounted_handle(self):
        api = FakeProcessAPI()
        target = module().discover_nvda(api=api)
        self.assertEqual(target.pid, 22)
        target.acquire()
        target.release()
        self.assertEqual(api.closed, [])
        target.check()
        api.identities[22] = 999
        with self.assertRaisesRegex(RuntimeError, 'identity'):
            target.check()
        target.release()
        self.assertEqual(api.closed, [22])

    def test_absent_returns_none_but_ambiguous_or_inaccessible_fails_closed(self):
        api = FakeProcessAPI()
        api.rows = [(11, 'nvda.exe')]
        self.assertIsNone(module().discover_nvda(api=api))
        api.rows += [(22, 'nvda.exe'), (44, 'nvda.exe')]
        api.sessions[44], api.identities[44] = 1, 444
        with self.assertRaisesRegex(RuntimeError, 'ambiguous'):
            module().discover_nvda(api=api)
        self.assertEqual(sorted(api.closed), [22, 44])
        api = FakeProcessAPI()
        def denied(pid):
            raise RuntimeError('access denied')
        api.open = denied
        with self.assertRaisesRegex(RuntimeError, 'access denied'):
            module().discover_nvda(api=api)

    @unittest.skipUnless(sys.platform == 'win32', 'Windows Toolhelp ABI test')
    def test_native_toolhelp_bindings_enumerate_and_close_without_process_side_effects(self):
        import ctypes as C
        m = module()
        calls, index = [], [0]
        rows = [(11, 'nvda.exe'), (22, 'other.exe')]
        class Function:
            def __init__(self, fn):
                self.fn = fn
            def __call__(self, *args):
                return self.fn(*args)
        class Kernel:
            pass
        k = Kernel()
        def output(pointer):
            if index[0] == len(rows):
                C.set_last_error(18)
                return 0
            row = C.cast(pointer, C.POINTER(m.PROCESSENTRY32W)).contents
            self.assertEqual(row.dwSize, C.sizeof(m.PROCESSENTRY32W))
            row.th32ProcessID, row.szExeFile = rows[index[0]]
            index[0] += 1
            return 1
        functions = dict(CreateToolhelp32Snapshot=lambda *a: 100,
                         Process32FirstW=lambda h, p: output(p),
                         Process32NextW=lambda h, p: output(p),
                         CloseHandle=lambda h: (calls.append(h), 1)[1])
        for name in ('CreateToolhelp32Snapshot', 'Process32FirstW', 'Process32NextW',
                     'GetCurrentProcessId', 'ProcessIdToSessionId', 'OpenProcess',
                     'QueryFullProcessImageNameW', 'GetProcessTimes',
                     'WaitForSingleObject', 'CloseHandle'):
            setattr(k, name, Function(functions.get(name, lambda *a: 1)))
        api = m._ProcessAPI(kernel32=k)
        self.assertEqual(api.processes(), rows)
        self.assertEqual(calls, [100])
        self.assertEqual(C.sizeof(m.PROCESSENTRY32W), 568 if C.sizeof(m.P) == 8 else 556)
        self.assertIs(k.OpenProcess.restype, m.P)

    def test_exit_and_restart_does_not_retarget_new_pid(self):
        api = FakeProcessAPI()
        target = module().discover_nvda(api=api)
        api.exited.add(22)
        api.rows = [(55, 'nvda.exe')]
        with self.assertRaisesRegex(RuntimeError, 'exited'):
            target.check()
        self.assertEqual(target.pid, 22)
        target.release()


class FakePointer:
    def __init__(self, kind, log):
        self.kind, self.log = kind, log
        self.owner = threading.get_ident()
        self.buffer = None
        self.frames, self.flags, self.stamp = 2, 0, 100000
        self.empty = False
        self.null = False
        self.failure = None
        self.buffer_status = 0
    def call(self, index, types, *args):
        import ctypes as C
        m = module()
        assert threading.get_ident() == self.owner
        self.log.append((self.kind, index))
        if self.failure == index:
            raise RuntimeError('injected native failure')
        def assign(arg, ctype, value):
            C.cast(arg, C.POINTER(ctype))[0] = value
        if self.kind == 'client' and index == 14:
            assign(args[1], m.P, 2)
        if self.kind == 'capture':
            if index == 5:
                assign(args[0], m.U32, 0 if self.empty else 2)
            elif index == 3:
                self.buffer = C.create_string_buffer(struct.pack('<4f', .1, -.2, .3, -.4))
                assign(args[0], m.P, 0 if self.null else C.addressof(self.buffer))
                for arg, ctype, value in zip(args[1:], [m.U32, m.U32, m.U64, m.U64],
                                              [self.frames, self.flags, 0, self.stamp]):
                    assign(arg, ctype, value)
            elif index == 4:
                # Prove bytes were copied before native memory is reused.
                if self.buffer:
                    C.memset(C.addressof(self.buffer), 0, len(self.buffer))
        return self.buffer_status if self.kind == 'capture' and index == 3 else 0
    def release(self):
        assert threading.get_ident() == self.owner
        self.log.append((self.kind, 'release'))


class FakeAudioAPI:
    def __init__(self):
        self.log = []
        self.client, self.capture = FakePointer('client', self.log), FakePointer('capture', self.log)
    def initialize(self):
        self.log.append(('apartment', 'initialize'))
    def uninitialize(self):
        self.log.append(('apartment', 'uninitialize'))
    def activate(self, pid, mode, cancel):
        self.log.append(('activate', mode))
        return self.client
    def pointer(self, value):
        assert value.value == 2
        return self.capture
    def create_event(self):
        self.log.append(('event', 'create'))
        return 3
    def close_event(self, event):
        self.log.append(('event', 'close'))


class NativeTests(unittest.TestCase):
    def make_native(self):
        api = FakeAudioAPI()
        native = module()._NativeProcessReader(Target(), 0, 48000, 2,
                                              threading.Event(), api=api)
        self.addCleanup(native.close)
        return native, api

    def test_initialize_only_float_format_copy_before_release_and_lifecycle(self):
        native, api = self.make_native()
        self.assertNotIn(('client', 10), api.log)
        self.assertNotIn(('capture', 3), api.log)
        native.start()
        result = native.read()
        self.assertEqual(result.data, struct.pack('<4f', .1, -.2, .3, -.4))
        native.stop()
        native.start()
        native.close()
        self.assertNotIn(('client', 12), api.log)
        self.assertEqual(api.log[-1], ('apartment', 'uninitialize'))
        self.assertEqual(api.log.count(('capture', 'release')), 1)
        self.assertEqual(api.log.count(('client', 11)), 2)

    def test_packet_validation_silence_and_release_even_on_errors(self):
        native, api = self.make_native()
        for flags, frames, null, stamp, message in [
            (4, 2, False, 100000, 'timestamp'), (0, 0, False, 100000, 'size'),
            (0, 96001, False, 100000, 'size'), (0, 2, True, 100000, 'null'),
            (8, 2, False, 100000, 'flags'), (0, 2, False, 0, 'timestamp')]:
            with self.subTest(message=message, frames=frames):
                c = api.capture
                c.flags, c.frames, c.null, c.stamp = flags, frames, null, stamp
                before = api.log.count(('capture', 4))
                with self.assertRaisesRegex(RuntimeError, message):
                    native.read()
                self.assertEqual(api.log.count(('capture', 4)), before + 1)
        api.capture.flags, api.capture.frames, api.capture.null = 2, 2, True
        api.capture.stamp = 100000
        self.assertEqual(native.read().data, bytes(16))
        api.capture.empty = True
        before = api.log.count(('capture', 4))
        self.assertIsNone(native.read())
        self.assertEqual(api.log.count(('capture', 4)), before)

    def test_empty_success_and_getbuffer_failure_do_not_release_unowned_buffer(self):
        native, api = self.make_native()
        api.capture.buffer_status = 0x8890001
        self.assertIsNone(native.read())
        self.assertNotIn(('capture', 4), api.log)
        api.capture.failure = 3
        with self.assertRaisesRegex(RuntimeError, 'injected'):
            native.read()
        self.assertNotIn(('capture', 4), api.log)

    def test_timestamp_order_is_validated_and_discontinuity_keeps_timestamp(self):
        native, api = self.make_native()
        native.read()
        with self.assertRaisesRegex(RuntimeError, 'nonmonotonic'):
            native.read()
        api.capture.stamp, api.capture.flags = 900000, 1
        result = native.read()
        self.assertEqual(result.timestamp, 900000)
        self.assertEqual(native.diagnostic_snapshot()['discontinuities'], 1)

    def test_stop_failure_still_releases_every_native_resource_once(self):
        native, api = self.make_native()
        native.start()
        api.client.failure = 11
        with self.assertRaisesRegex(RuntimeError, 'injected'):
            native.close()
        self.assertEqual(api.log.count(('capture', 'release')), 1)
        self.assertEqual(api.log.count(('client', 'release')), 1)
        self.assertIn(('event', 'close'), api.log)
        self.assertEqual(api.log[-1], ('apartment', 'uninitialize'))
        native.close()

    def test_partial_initialize_failure_releases_client_and_apartment(self):
        api = FakeAudioAPI()
        api.client.failure = 14
        with self.assertRaisesRegex(RuntimeError, 'injected'):
            module()._NativeProcessReader(Target(), 1, 48000, 2, threading.Event(), api=api)
        self.assertIn(('client', 'release'), api.log)
        self.assertEqual(api.log[-1], ('apartment', 'uninitialize'))


class ActivationTests(unittest.TestCase):
    def surface(self, *, immediate=0, result=0, delayed=False):
        import ctypes as C
        m = module()
        state = dict(released=[], owners=[], target=None)
        owner = threading.get_ident()
        class Pointer:
            def __init__(self, value):
                self.value = value.value if hasattr(value, 'value') else value
            def call(self, index, types, *args):
                state['owners'].append(threading.get_ident())
                assert threading.get_ident() == owner
                if self.value == 1:  # GetActivateResult
                    C.cast(args[0], C.POINTER(m.I32))[0] = result
                    C.cast(args[1], C.POINTER(m.P))[0] = 2
                elif self.value == 2:  # QueryInterface
                    C.cast(args[1], C.POINTER(m.P))[0] = 3
                return 0
            def release(self):
                state['released'].append(self.value)
        def activate(path, iid, variant, handler, output):
            self.assertEqual(path, 'VAD\\Process_Loopback')
            v = C.cast(variant, C.POINTER(m.PROPVARIANT)).contents
            self.assertEqual(v.vt, 65)
            self.assertEqual(v.value.counted.count, 12)
            params = C.cast(v.value.counted.pointer, C.POINTER(m.ActivationParams)).contents
            state['target'] = (params.pid, params.mode)
            if immediate:
                return immediate
            address = C.cast(handler, m.P).value
            table = C.cast(address, C.POINTER(C.POINTER(m.P))).contents
            addref = m.CALL(m.U32, m.P)(table[1])
            release = m.CALL(m.U32, m.P)(table[2])
            complete = m.CALL(m.I32, m.P, m.P)(table[3])
            addref(address)
            C.cast(output, C.POINTER(m.P))[0] = 1
            def finish():
                complete(address, 1)
                release(address)
            state['finish'] = finish
            if not delayed:
                worker = threading.Thread(target=finish)
                worker.start()
                worker.join(1)
            return 0
        return activate, Pointer, state

    def test_callback_hands_no_native_interfaces_across_threads_and_releases_all(self):
        m = module()
        activate, pointer, state = self.surface()
        before = len(m._handlers)
        client = m._activate_client(42, 1, threading.Event(), activate=activate,
                                    pointer_factory=pointer, timeout=.05)
        self.assertEqual(client.value, 3)
        self.assertEqual(state['target'], (42, 1))
        self.assertEqual(sorted(state['released']), [1, 2])
        self.assertEqual(len(m._handlers), before)

    def test_activation_hresult_and_result_failure_release_references(self):
        m = module()
        for immediate, result in [(-2147467259, 0), (0, -2147467259)]:
            activate, pointer, state = self.surface(immediate=immediate, result=result)
            before = len(m._handlers)
            with self.assertRaisesRegex(RuntimeError, 'HRESULT'):
                m._activate_client(42, 0, threading.Event(), activate=activate,
                                   pointer_factory=pointer, timeout=.05)
            self.assertEqual(len(m._handlers), before)
            if not immediate:
                self.assertEqual(sorted(state['released']), [1, 2])

    def test_timeout_retains_handler_until_late_callback_without_start_or_result(self):
        import gc
        m = module()
        activate, pointer, state = self.surface(delayed=True)
        before = len(m._handlers)
        with self.assertRaisesRegex(RuntimeError, 'activation timeout'):
            m._activate_client(42, 0, threading.Event(), activate=activate,
                               pointer_factory=pointer, timeout=.01)
        self.assertEqual(len(m._handlers), before + 1)
        gc.collect()
        state['finish']()
        self.assertEqual(len(m._handlers), before)
        self.assertEqual(state['owners'], [])
        self.assertEqual(state['released'], [1])

    def test_cancel_before_activation_does_not_invoke_api(self):
        m = module()
        cancel = threading.Event()
        cancel.set()
        def forbidden(*args):
            self.fail('post-cancellation activation')
        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
            m._activate_client(42, 0, cancel, activate=forbidden)


class BackendTests(unittest.TestCase):
    def backend(self):
        from core.wasapi_capture import WasapiBackend
        b = object.__new__(WasapiBackend)
        b._streams, b._process_targets = [], []
        b._initialized, b.enumerator = False, None
        return b

    def test_pair_uses_same_pin_exclude_then_include_and_cleanup_tracks_both(self):
        from unittest.mock import patch
        m = module()
        target, owners, modes = Target(), [], []
        def factory(pin, mode, rate, channels):
            self.assertIs(pin, target)
            modes.append(mode)
            def native_factory(*args):
                n = Native([])
                owners.append(n.owner)
                return n
            return m.ProcessLoopbackStream(pin, mode, rate, channels,
                                           native_factory=native_factory)
        b = self.backend()
        with patch.object(m, 'discover_nvda', return_value=target), \
             patch.object(m, 'open_process_stream', side_effect=factory):
            pair = b.open_nvda_loopbacks(48000, 2)
        self.assertEqual(modes, [1, 0])
        self.assertEqual(len(set(owners)), 2)
        self.assertEqual(tuple(b._streams), pair)
        self.assertEqual(target.refs, 3)
        b.close()
        self.assertEqual(target.refs, 0)

    def test_absent_alone_returns_none_and_activation_failure_never_falls_back(self):
        from unittest.mock import patch
        m = module()
        b = self.backend()
        with patch.object(m, 'discover_nvda', return_value=None):
            self.assertIsNone(b.open_nvda_loopbacks(48000, 2))
        target = Target()
        with patch.object(m, 'discover_nvda', return_value=target), \
             patch.object(m, 'open_process_stream', side_effect=RuntimeError('unsupported')):
            with self.assertRaisesRegex(RuntimeError, 'unsupported'):
                b.open_nvda_loopbacks(48000, 2)
        b.close()
        self.assertEqual(target.refs, 0)

    def test_process_cleanup_failure_is_visible_but_remaining_streams_cleaned(self):
        b = self.backend()
        calls = []
        class Stream:
            process_loopback = True
            def close(self):
                calls.append('closed')
                raise RuntimeError('cleanup failed')
        b._streams = [Stream(), Stream()]
        with self.assertRaisesRegex(RuntimeError, 'cleanup failed'):
            b.close()
        self.assertEqual(calls, ['closed', 'closed'])
