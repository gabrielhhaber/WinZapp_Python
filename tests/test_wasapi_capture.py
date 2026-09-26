"""Native ABI/COM tests use synthetic vtables; never start real devices."""
import ctypes as C
import importlib
import unittest


class NativeTests(unittest.TestCase):
    def test_windows_abi_and_hresult_are_fixed_width_on_every_host(self):
        try:
            w = importlib.import_module('core.wasapi_capture')
        except ImportError:
            self.fail('native WASAPI backend is not implemented')
        self.assertEqual(C.sizeof(w.GUID), 16)
        self.assertEqual(C.sizeof(w.WAVEFORMATEX), 18)
        self.assertEqual(w.WAVEFORMATEX.cbSize.offset, 16)
        self.assertEqual(C.sizeof(w.PROPERTYKEY), 20)
        self.assertEqual(C.sizeof(w.PROPVARIANT), 24 if C.sizeof(C.c_void_p) == 8 else 16)
        w.check_hresult(0, 'ok')
        w.check_hresult(1, 'already initialized')
        with self.assertRaisesRegex(w.WasapiError, '88890004'):
            w.check_hresult(0x88890004, 'device invalidated')
        fmt = w.capture_format(44100, 2)
        self.assertEqual((fmt.wFormatTag, fmt.nSamplesPerSec, fmt.nChannels,
                          fmt.wBitsPerSample, fmt.nBlockAlign, fmt.nAvgBytesPerSec),
                         (3, 44100, 2, 32, 8, 352800))

    def test_stream_uses_shared_conversion_loopback_and_releases_silent_packet(self):
        from core import wasapi_capture as w
        calls = []
        float_data = (C.c_float * 2)(.25, -.25)
        class Pointer:
            def __init__(self, kind):
                self.kind, self.released = kind, 0
            def release(self):
                self.released += 1
            def call(self, index, types, *args):
                calls.append((self.kind, index, args))
                if self.kind == 'device' and index == 3:
                    C.cast(args[-1], C.POINTER(C.c_void_p))[0] = 1
                elif self.kind == 'device' and index == 6:
                    C.cast(args[0], C.POINTER(w.U32))[0] = 1
                elif self.kind == 'client' and index == 14:
                    C.cast(args[-1], C.POINTER(C.c_void_p))[0] = 2
                elif self.kind == 'capture' and index == 5:
                    C.cast(args[0], C.POINTER(w.U32))[0] = 1 if not any(k == 'capture' and i == 4 for k, i, a in calls) else 0
                elif self.kind == 'capture' and index == 3:
                    C.cast(args[0], C.POINTER(C.c_void_p))[0] = C.addressof(float_data)
                    C.cast(args[1], C.POINTER(w.U32))[0] = 1
                    C.cast(args[2], C.POINTER(w.U32))[0] = 2  # SILENT ignores dirty payload
                    C.cast(args[4], C.POINTER(w.U64))[0] = 123456
                return 0
        device, client, capture = (Pointer(k) for k in ('device', 'client', 'capture'))
        stream = w.WasapiStream(device, 48000, 2, loopback=True,
                                pointer_factory=lambda p: {1: client, 2: capture}[p.value])
        init = next(args for kind, index, args in calls if kind == 'client' and index == 3)
        self.assertEqual(init[:4], (0, 0x88020000, 10_000_000, 0))
        stream.start()
        packets = stream.read_packets()
        self.assertEqual(len(packets), 1)
        self.assertEqual((packets[0].timestamp, packets[0].data), (123456, bytes(8)))
        stream.stop()
        stream.reset()
        stream.close()
        stream.close()
        self.assertEqual((device.released, client.released, capture.released), (1, 1, 1))
        self.assertIn(('capture', 4, (1,)), calls)

    def test_endpoint_defaults_and_missing_named_microphone_fail_closed(self):
        from core import wasapi_capture as w
        class Ole:
            def __init__(self): self.initialized = self.closed = 0
            def CoInitializeEx(self, a, flags): self.initialized += 1; return 0
            def CoCreateInstance(self, *args):
                C.cast(args[-1], C.POINTER(C.c_void_p))[0] = 10
                return 0
            def CoUninitialize(self): self.closed += 1
        class Enumerator:
            def __init__(self): self.calls = []; self.released = 0
            def call(self, index, types, *args):
                self.calls.append((index, args[:2]))
                C.cast(args[-1], C.POINTER(C.c_void_p))[0] = 11
            def release(self): self.released += 1
        ole, enum = Ole(), Enumerator()
        endpoint = object()
        backend = w.WasapiBackend(ole32=ole, pointer_factory=lambda p: enum if p.value == 10 else endpoint)
        self.assertIs(backend._default_endpoint(0), endpoint)
        self.assertIs(backend._default_endpoint(1), endpoint)
        self.assertEqual(enum.calls, [(4, (0, 0)), (4, (1, 0))])
        backend._named_microphone = lambda name: (_ for _ in ()).throw(w.WasapiError('not found'))
        with self.assertRaisesRegex(w.WasapiError, 'not found'):
            backend.open_microphone('Configured USB', 48000, 1)
        self.assertEqual(len(enum.calls), 2)  # no silent default-mic fallback
        backend.close(); backend.close()
        self.assertEqual((ole.initialized, ole.closed, enum.released), (1, 1, 1))


class NativeHarness:
    """Synthetic COM interfaces; payload memory owned here, never OS capture."""
    def __init__(self, packets=(), *, init_error=False, state=1, empty=False):
        from core import wasapi_capture as w
        self.w = w
        self.packets = list(packets)
        self.calls, self.released, self.buffers = [], [], []
        self.init_error, self.state, self.empty = init_error, state, empty
        owner = self
        class Pointer:
            def __init__(self, kind): self.kind = kind
            def release(self): owner.released.append(self.kind)
            def call(self, index, types, *args):
                owner.calls.append((self.kind, index, args))
                def out(arg, kind, value): C.cast(arg, C.POINTER(kind))[0] = value
                if self.kind == 'device' and index == 3: out(args[-1], w.P, 1)
                if self.kind == 'device' and index == 6: out(args[0], w.U32, owner.state)
                if self.kind == 'client' and index == 3 and owner.init_error:
                    raise w.WasapiError('Initialize rejected conversion')
                if self.kind == 'client' and index == 14: out(args[-1], w.P, 2)
                if self.kind == 'capture' and index == 5: out(args[0], w.U32, bool(owner.packets or owner.empty))
                if self.kind == 'capture' and index == 3:
                    if owner.empty: return 0x08890001
                    stamp, flags, data = owner.packets.pop(0)
                    block = C.create_string_buffer(data)
                    owner.buffers.append(block)
                    out(args[0], w.P, C.addressof(block) if not flags & 2 else None)
                    out(args[1], w.U32, len(data) // 4)
                    out(args[2], w.U32, flags)
                    out(args[4], w.U64, stamp)
                return 0
        self.device, self.client, self.capture = (Pointer(k) for k in ('device', 'client', 'capture'))
    def stream(self, loopback=False):
        return self.w.WasapiStream(self.device, 48000, 1, loopback=loopback,
                                   pointer_factory=lambda p: {1: self.client, 2: self.capture}[p.value])


class NativeEdgeTests(unittest.TestCase):
    def test_empty_success_does_not_release_undefined_fields(self):
        h = NativeHarness(empty=True)
        stream = h.stream()
        self.assertEqual(stream.read_packets(), [])
        self.assertFalse(any(k == 'capture' and i == 4 for k, i, a in h.calls))
        stream.close()

    def test_float_payload_copied_before_release(self):
        import struct
        data = struct.pack('<2f', .1, -.2)
        h = NativeHarness([(1234, 0, data)])
        stream = h.stream()
        packet = stream.read_packets()[0]
        C.memset(C.addressof(h.buffers[0]), 0, len(data))
        self.assertEqual(packet.data, data)
        self.assertEqual(packet.timestamp, 1234)
        stream.close()

    def test_bad_timestamp_releases_packet_and_fails_closed(self):
        h = NativeHarness([(12, 4, bytes(4))])
        stream = h.stream()
        with self.assertRaisesRegex(h.w.WasapiError, 'timestamp'):
            stream.read_packets()
        self.assertTrue(any(k == 'capture' and i == 4 for k, i, a in h.calls))
        stream.close()

    def test_loopback_silent_gap_discontinuity_is_not_device_failure(self):
        h = NativeHarness([(12, 1, bytes(4)), (30_000_000, 1, bytes(4))])
        stream = h.stream(loopback=True)
        self.assertEqual([p.timestamp for p in stream.read_packets()], [12, 30_000_000])
        stream.close()

    def test_microphone_discontinuity_keeps_packets_and_counts_glitch(self):
        h = NativeHarness([(12, 1, bytes(4)), (300, 1, bytes(4))])
        stream = h.stream()
        try:
            self.assertEqual([p.timestamp for p in stream.read_packets()], [12, 300])
            self.assertEqual(stream.diagnostic_snapshot()['discontinuities'], 1)
        finally:
            stream.close()

    def test_endpoint_removal_is_fatal_even_with_no_loopback_packets(self):
        h = NativeHarness(state=8)
        stream = h.stream(loopback=True)
        with self.assertRaisesRegex(h.w.WasapiError, 'no longer active'):
            stream.read_packets()
        stream.close()

    def test_initialize_failure_releases_every_acquired_interface(self):
        h = NativeHarness(init_error=True)
        with self.assertRaisesRegex(h.w.WasapiError, 'Initialize'):
            h.stream()
        self.assertEqual(h.released, ['client', 'device'])

    def test_raw_vtable_dispatch_uses_hresult_and_this_pointer(self):
        from core import wasapi_capture as w
        seen = []
        @w.CALL(w.U32, w.P)
        def release(this): seen.append(('release', this)); return 0
        @w.CALL(w.I32, w.P, w.U32)
        def method(this, value): seen.append(('call', this, value)); return -2004287484
        table = (w.P * 4)(None, None, C.cast(release, w.P), C.cast(method, w.P))
        class Instance(C.Structure):
            _fields_ = [('vtbl', C.POINTER(w.P))]
        instance = Instance(C.cast(table, C.POINTER(w.P)))
        obj = C.pointer(instance)
        pointer = w.ComPointer(C.cast(obj, w.P))
        with self.assertRaisesRegex(w.WasapiError, '88890004'):
            pointer.call(3, [w.U32], 42)
        pointer.release(); pointer.release()
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0][1], C.cast(obj, w.P).value)
        self.assertEqual(seen[0][2], 42)


if __name__ == '__main__':
    unittest.main()
