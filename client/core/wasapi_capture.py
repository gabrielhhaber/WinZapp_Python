"""Minimal native shared-mode WASAPI capture, with no import-time Windows calls.

Endpoint COM objects belong to the thread constructing WasapiBackend. Call
close on that same thread; optional process loopbacks have dedicated MTA owners.
No BASS/PortAudio state is touched, no session is muted, and endpoint
loopback captures ALL sessions on the default eRender/eConsole endpoint pinned
at open. Native high-quality conversion handles rate/channel differences.

References: Microsoft Learn IAudioClient::Initialize, IAudioCaptureClient::
GetBuffer, Loopback Recording, AUDCLNT_STREAMFLAGS_XXX.
"""
import ctypes as C
import sys
import uuid

U32 = C.c_uint32
I32 = C.c_int32
U64 = C.c_uint64
P = C.c_void_p
CALL = getattr(C, 'WINFUNCTYPE', C.CFUNCTYPE)


class GUID(C.Structure):
    _fields_ = [('Data1', U32), ('Data2', C.c_uint16), ('Data3', C.c_uint16),
                ('Data4', C.c_ubyte * 8)]

    @classmethod
    def parse(cls, value):
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


class WAVEFORMATEX(C.Structure):
    _layout_ = 'ms'
    _pack_ = 1
    _fields_ = [('wFormatTag', C.c_uint16), ('nChannels', C.c_uint16),
                ('nSamplesPerSec', U32), ('nAvgBytesPerSec', U32),
                ('nBlockAlign', C.c_uint16), ('wBitsPerSample', C.c_uint16),
                ('cbSize', C.c_uint16)]


class PROPERTYKEY(C.Structure):
    _fields_ = [('fmtid', GUID), ('pid', U32)]


class _VariantData(C.Union):
    # The largest PROPVARIANT member is a counted array (ULONG + pointer).
    class Counted(C.Structure):
        _fields_ = [('count', U32), ('pointer', P)]
    _fields_ = [('pointer', P), ('counted', Counted), ('integer', U64)]


class PROPVARIANT(C.Structure):
    _fields_ = [('vt', C.c_uint16), ('reserved', C.c_uint16 * 3),
                ('value', _VariantData)]


class WasapiError(RuntimeError):
    def __init__(self, message, *, hresult=None, operation=None):
        super().__init__(message)
        self.hresult, self.operation = hresult, operation


def check_hresult(value, operation):
    if I32(value).value < 0:
        raise WasapiError(f'{operation}: HRESULT 0x{value & 0xffffffff:08x}',
                          hresult=value & 0xffffffff, operation=operation)
    return value


def capture_format(rate, channels):
    if channels not in (1, 2) or not 8000 <= rate <= 192000:
        raise ValueError('Capture requires mono/stereo and a rate from 8000 to 192000 Hz')
    return WAVEFORMATEX(3, channels, rate, rate * channels * 4, channels * 4, 32, 0)


IID_AUDIO_CLIENT = GUID.parse('1cb9ad4c-dbfa-4c32-b178-c2f568a703b2')
IID_CAPTURE_CLIENT = GUID.parse('c8adbd64-e71e-48a0-a4de-185c395cd317')


class ComPointer:
    """One owned IUnknown reference. Never crosses the capture worker thread."""
    def __init__(self, pointer):
        self.pointer = pointer if isinstance(pointer, P) else P(pointer)
        if not self.pointer.value:
            raise WasapiError('COM returned a null interface')

    def call(self, index, argtypes, *args):
        table = C.cast(self.pointer, C.POINTER(C.POINTER(P))).contents
        method = CALL(I32, P, *argtypes)(table[index])
        result = method(self.pointer, *args)
        return check_hresult(result, f'COM method {index}')

    def release(self):
        if self.pointer.value:
            table = C.cast(self.pointer, C.POINTER(C.POINTER(P))).contents
            CALL(U32, P)(table[2])(self.pointer)
            self.pointer = P()


class WasapiStream:
    """Owns an endpoint, audio client and capture client; opening does NOT Start."""
    def __init__(self, device, rate, channels, *, loopback=False,
                 pointer_factory=ComPointer):
        self.device, self.client, self.capture = device, None, None
        self.rate, self.channels = rate, channels
        self.running = False
        self._loopback = loopback
        self._seen_packet = False
        self._diagnostic = {}
        self._last_packet_stamp = None
        self._discontinuities = 0
        try:
            fmt = capture_format(rate, channels)
            pointer = P()
            device.call(3, [C.POINTER(GUID), U32, P, C.POINTER(P)],
                        C.byref(IID_AUDIO_CLIENT), 23, None, C.byref(pointer))
            self.client = pointer_factory(pointer)
            # Shared mode, 1 s capture reserve, zero periodicity. Windows
            # supplies both the channel matrix and high-quality sample converter.
            flags = 0x80000000 | 0x08000000 | (0x20000 if loopback else 0)
            self.client.call(3, [I32, U32, C.c_int64, C.c_int64,
                                 C.POINTER(WAVEFORMATEX), P],
                             0, flags, 10_000_000, 0, C.byref(fmt), None)
            pointer = P()
            self.client.call(14, [C.POINTER(GUID), C.POINTER(P)],
                             C.byref(IID_CAPTURE_CLIENT), C.byref(pointer))
            self.capture = pointer_factory(pointer)
        except BaseException:
            self.close()
            raise

    def start(self):
        self.client.call(10, [])
        self.running = True

    def stop(self):
        if self.running:
            self.client.call(11, [])
            self.running = False

    def reset(self):
        self.client.call(12, [])
        self._seen_packet = False
        self._last_packet_stamp = None
        self._diagnostic.clear()

    def diagnostic_snapshot(self):
        """Numeric metadata only; no buffer, endpoint name/id or absolute QPC."""
        return dict(self._diagnostic, running=int(self.running),
                    loopback=int(self._loopback), seen_packet=int(self._seen_packet),
                    discontinuities=self._discontinuities)

    def read_packets(self):
        from .system_audio_capture import AudioPacket
        state = U32()
        self._diagnostic = {}
        self.device.call(6, [C.POINTER(U32)], C.byref(state))
        self._diagnostic['state'] = state.value
        if state.value != 1:
            raise WasapiError('The pinned audio endpoint is no longer active')
        packets = []
        total_frames = 0
        # Never let a faulty driver or perpetually refilling device starve the
        # other source/control commands. Failure preserves already emitted PCM.
        for _ in range(256):
            available = U32()
            self.capture.call(5, [C.POINTER(U32)], C.byref(available))
            self._diagnostic['available'] = available.value
            if not available.value:
                return packets
            data, frames, flags, position, stamp = P(), U32(), U32(), U64(), U64()
            result = self.capture.call(3, [C.POINTER(P), C.POINTER(U32),
                                          C.POINTER(U32), C.POINTER(U64), C.POINTER(U64)],
                                       C.byref(data), C.byref(frames), C.byref(flags),
                                       C.byref(position), C.byref(stamp))
            if result == 0x8890001:  # AUDCLNT_S_BUFFER_EMPTY; no ReleaseBuffer
                return packets
            try:
                total_frames += frames.value
                self._diagnostic.update(flags=flags.value, packet_frames=frames.value,
                                        total_frames=total_frames)
                if self._last_packet_stamp is not None:
                    self._diagnostic['stamp_delta_100ns'] = stamp.value - self._last_packet_stamp
                self._last_packet_stamp = stamp.value
                if not frames.value or total_frames > self.rate * 2:
                    raise WasapiError('Invalid or excessive WASAPI packet size')
                if flags.value & 4:
                    raise WasapiError('WASAPI packet timestamp is unreliable')
                # DATA_DISCONTINUITY is a recoverable glitch, not device loss.
                # Keep the valid packet at its original QPC time. The mixer
                # leaves missing time silent instead of shifting either source.
                # Invalid timestamps and inactive endpoints still fail closed.
                if flags.value & 1 and self._seen_packet:
                    self._discontinuities += 1
                size = frames.value * self.channels * 4
                if flags.value & 2:
                    payload = bytes(size)
                elif data.value:
                    payload = C.string_at(data, size)
                else:
                    raise WasapiError('WASAPI returned a null audio buffer')
                packets.append(AudioPacket(stamp.value, payload))
                self._seen_packet = True
            finally:
                self.capture.call(4, [U32], frames.value)
        raise WasapiError('WASAPI capture queue exceeded its bounded drain')

    def close(self):
        try:
            if self.client is not None:
                self.stop()
        finally:
            for name in ('capture', 'client', 'device'):
                pointer = getattr(self, name)
                if pointer is not None:
                    pointer.release()
                    setattr(self, name, None)


CLSID_ENUMERATOR = GUID.parse('bcde0395-e52f-467c-8e3d-c4579291692e')
IID_ENUMERATOR = GUID.parse('a95664d2-9614-4f35-a746-de8db63617e6')
FRIENDLY_NAME = PROPERTYKEY(GUID.parse('a45c254e-df1c-4efd-8020-67d146a850e0'), 14)


def _repair_name(name):
    # Same UTF-8-as-Latin1 repair as audio_devices.repair_device_name, without
    # importing PortAudio or any application/UI module on the capture thread.
    name = str(name or '').strip()
    try:
        return name.encode('latin-1').decode('utf-8')
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


class WasapiBackend:
    """Thread-affine native COM owner. Construction/enumeration never capture."""
    def __init__(self, *, ole32=None, pointer_factory=ComPointer):
        self.enumerator = None
        self._initialized = False
        self._pointer_factory = pointer_factory
        self._streams = []
        self._process_targets = []
        self._kernel32 = None
        if ole32 is None:
            if sys.platform != 'win32':
                raise OSError('System-audio recording requires Windows WASAPI')
            ole32 = C.WinDLL('ole32')
            for name, args, result in [
                ('CoInitializeEx', [P, U32], I32),
                ('CoCreateInstance', [C.POINTER(GUID), P, U32, C.POINTER(GUID), C.POINTER(P)], I32),
                ('CoUninitialize', [], None),
                ('CoTaskMemFree', [P], None),
                ('PropVariantClear', [C.POINTER(PROPVARIANT)], I32),
            ]:
                method = getattr(ole32, name)
                method.argtypes, method.restype = args, result
            self._kernel32 = C.WinDLL('kernel32')
            for name in ('QueryPerformanceCounter', 'QueryPerformanceFrequency'):
                method = getattr(self._kernel32, name)
                method.argtypes, method.restype = [C.POINTER(C.c_int64)], I32
            frequency = C.c_int64()
            if not self._kernel32.QueryPerformanceFrequency(C.byref(frequency)):
                raise WasapiError('QueryPerformanceFrequency failed')
            self._frequency = frequency.value
        self._ole32 = ole32
        check_hresult(ole32.CoInitializeEx(None, 0), 'CoInitializeEx(MTA)')
        self._initialized = True
        try:
            pointer = P()
            check_hresult(ole32.CoCreateInstance(C.byref(CLSID_ENUMERATOR), None, 23,
                          C.byref(IID_ENUMERATOR), C.byref(pointer)), 'MMDeviceEnumerator')
            self.enumerator = pointer_factory(pointer)
        except BaseException:
            self.close()
            raise

    def clock(self):
        value = C.c_int64()
        if not self._kernel32.QueryPerformanceCounter(C.byref(value)):
            raise WasapiError('QueryPerformanceCounter failed')
        return value.value * 10_000_000 // self._frequency

    def _default_endpoint(self, flow):
        pointer = P()
        # eRender=0 or eCapture=1, eConsole=0, resolved once and kept alive.
        self.enumerator.call(4, [I32, I32, C.POINTER(P)], flow, 0, C.byref(pointer))
        return self._pointer_factory(pointer)

    def _friendly_name(self, device):
        pointer = P()
        device.call(4, [U32, C.POINTER(P)], 0, C.byref(pointer))  # STGM_READ
        store = self._pointer_factory(pointer)
        value = PROPVARIANT()
        try:
            store.call(5, [C.POINTER(PROPERTYKEY), C.POINTER(PROPVARIANT)],
                       C.byref(FRIENDLY_NAME), C.byref(value))
            if value.vt != 31 or not value.value.pointer:  # VT_LPWSTR
                raise WasapiError('Audio endpoint has no friendly name')
            return C.wstring_at(value.value.pointer)
        finally:
            self._ole32.PropVariantClear(C.byref(value))
            store.release()

    def _named_microphone(self, name):
        pointer = P()
        self.enumerator.call(3, [I32, U32, C.POINTER(P)], 1, 1, C.byref(pointer))
        collection = self._pointer_factory(pointer)
        selected = None
        try:
            count = U32()
            collection.call(3, [C.POINTER(U32)], C.byref(count))
            for i in range(count.value):
                pointer = P()
                collection.call(4, [U32, C.POINTER(P)], i, C.byref(pointer))
                device = self._pointer_factory(pointer)
                try:
                    if _repair_name(self._friendly_name(device)) == _repair_name(name):
                        if selected is not None:
                            raise WasapiError('Configured microphone name is ambiguous')
                        selected, device = device, None
                finally:
                    if device is not None:
                        device.release()
            if selected is None:
                raise WasapiError('Configured microphone was not found')
            result, selected = selected, None
            return result
        finally:
            if selected is not None:
                selected.release()
            collection.release()

    def open_microphone(self, name, rate, channels):
        device = self._named_microphone(name) if name else self._default_endpoint(1)
        stream = WasapiStream(device, rate, channels, pointer_factory=self._pointer_factory)
        self._streams.append(stream)
        return stream

    def open_loopback(self, rate, channels):
        stream = WasapiStream(self._default_endpoint(0), rate, channels,
                              loopback=True, pointer_factory=self._pointer_factory)
        self._streams.append(stream)
        return stream

    def open_nvda_loopbacks(self, rate, channels):
        """All-endpoint EXCLUDE/INCLUDE pair, or None only when NVDA is absent.

        A detected but inaccessible/unsupported target is an error, never an
        endpoint fallback masquerading as independent NVDA gain.
        """
        from .wasapi_process_capture import discover_nvda, open_process_stream
        capture_format(rate, channels)
        target = discover_nvda()
        if target is None:
            return None
        self._process_targets.append(target)
        streams = []
        try:
            for mode in (1, 0):  # EXCLUDE target tree, INCLUDE same target tree
                stream = open_process_stream(target, mode, rate, channels)
                streams.append(stream)
                self._streams.append(stream)
            target.check()
            return tuple(streams)
        except BaseException:
            # Retain tracking until backend.close, including cleanup failures.
            for stream in streams:
                stream.close()
            raise

    def close(self):
        process_error = None
        try:
            for stream in reversed(self._streams):
                try:
                    stream.close()
                except Exception as exc:
                    if getattr(stream, 'process_loopback', False):
                        process_error = process_error or exc
                    # Preserve existing endpoint unplug cleanup behavior.
            self._streams.clear()
            for target in self._process_targets:
                try:
                    target.release()
                except Exception as exc:
                    process_error = process_error or exc
            self._process_targets.clear()
            if self.enumerator is not None:
                self.enumerator.release()
                self.enumerator = None
        finally:
            if self._initialized:
                self._ole32.CoUninitialize()
                self._initialized = False
        if process_error is not None:
            raise process_error
