"""Process-tree loopback across ALL render endpoints (not an endpoint filter).

Each stream owns its native interfaces on a dedicated MTA reader. Public reads
only exchange copied packets. No Controller DLL, routing or volume changes.
Opening initializes only; explicit, acknowledged start is required.
"""
from collections import deque
import ctypes as C
import queue
import threading
import time

from .wasapi_capture import (CALL, GUID, I32, P, PROPVARIANT, U32, U64,
                             WAVEFORMATEX, ComPointer, WasapiError,
                             IID_AUDIO_CLIENT, IID_CAPTURE_CLIENT,
                             capture_format, check_hresult)


class PROCESSENTRY32W(C.Structure):
    _fields_ = [('dwSize', U32), ('cntUsage', U32), ('th32ProcessID', U32),
                ('th32DefaultHeapID', C.c_size_t), ('th32ModuleID', U32),
                ('cntThreads', U32), ('th32ParentProcessID', U32),
                ('pcPriClassBase', I32), ('dwFlags', U32),
                ('szExeFile', C.c_wchar * 260)]


class _ProcessAPI:
    def __init__(self, kernel32=None):
        self.k = kernel32 or C.WinDLL('kernel32', use_last_error=True)
        for name, args, result in [
            ('CreateToolhelp32Snapshot', [U32, U32], P),
            ('Process32FirstW', [P, C.POINTER(PROCESSENTRY32W)], I32),
            ('Process32NextW', [P, C.POINTER(PROCESSENTRY32W)], I32),
            ('GetCurrentProcessId', [], U32),
            ('ProcessIdToSessionId', [U32, C.POINTER(U32)], I32),
            ('OpenProcess', [U32, I32, U32], P),
            ('QueryFullProcessImageNameW', [P, U32, C.c_wchar_p, C.POINTER(U32)], I32),
            ('GetProcessTimes', [P, C.POINTER(U64), C.POINTER(U64),
                                 C.POINTER(U64), C.POINTER(U64)], I32),
            ('WaitForSingleObject', [P, U32], U32),
            ('CloseHandle', [P], I32),
        ]:
            fn = getattr(self.k, name)
            fn.argtypes, fn.restype = args, result

    def processes(self):
        snapshot = self.k.CreateToolhelp32Snapshot(2, 0)
        if snapshot == P(-1).value:
            raise WasapiError('NVDA process snapshot failed')
        try:
            row = PROCESSENTRY32W()
            row.dwSize = C.sizeof(row)
            result = self.k.Process32FirstW(snapshot, C.byref(row))
            rows = []
            while result:
                rows.append((row.th32ProcessID, row.szExeFile))
                result = self.k.Process32NextW(snapshot, C.byref(row))
            if C.get_last_error() != 18:  # ERROR_NO_MORE_FILES
                raise WasapiError('NVDA process enumeration failed')
            return rows
        finally:
            self.close(snapshot)

    def current_session(self):
        return self.session(self.k.GetCurrentProcessId())

    def session(self, pid):
        session = U32()
        if not self.k.ProcessIdToSessionId(pid, C.byref(session)):
            raise WasapiError('NVDA process session lookup failed')
        return session.value

    def open(self, pid):
        handle = self.k.OpenProcess(0x1000 | 0x100000, False, pid)
        if not handle:
            raise WasapiError('Cannot pin NVDA process')
        return handle

    def image_name(self, handle):
        buffer = C.create_unicode_buffer(32768)
        size = U32(len(buffer))
        if not self.k.QueryFullProcessImageNameW(handle, 0, buffer, C.byref(size)):
            raise WasapiError('NVDA process image identity lookup failed')
        return buffer.value.rsplit('\\', 1)[-1]

    def identity(self, handle):
        creation, exit_time, kernel, user = U64(), U64(), U64(), U64()
        if not self.k.GetProcessTimes(handle, C.byref(creation), C.byref(exit_time),
                                      C.byref(kernel), C.byref(user)):
            raise WasapiError('NVDA process identity lookup failed')
        return creation.value

    def alive(self, handle):
        result = self.k.WaitForSingleObject(handle, 0)
        if result not in (0, 258):
            raise WasapiError('NVDA process lifetime check failed')
        return result == 258

    def close(self, handle):
        if not self.k.CloseHandle(handle):
            raise WasapiError('NVDA process handle cleanup failed')


class _PinnedProcess:
    """Backend plus each live owner hold a lease; stalled owners keep it pinned."""
    def __init__(self, api, pid, handle, identity):
        self._api, self.pid, self._handle = api, pid, handle
        self._identity = identity
        self._refs = 1
        self._lock = threading.Lock()

    def acquire(self):
        with self._lock:
            if not self._refs:
                raise WasapiError('NVDA process identity already released')
            self._refs += 1

    def release(self):
        with self._lock:
            if self._refs:
                self._refs -= 1
                if not self._refs:
                    self._api.close(self._handle)
                    self._handle = None

    def check(self):
        with self._lock:
            if not self._refs or not self._api.alive(self._handle):
                raise WasapiError('The pinned NVDA process exited')
            if self._api.identity(self._handle) != self._identity:
                raise WasapiError('The pinned NVDA process identity changed')


def discover_nvda(*, api=None):
    """None means genuinely absent, never inaccessible/ambiguous/lookup failure."""
    api = api or _ProcessAPI()
    current = api.current_session()
    targets = []
    try:
        for pid, name in api.processes():
            if name.casefold() != 'nvda.exe' or api.session(pid) != current:
                continue
            handle = api.open(pid)
            try:
                if api.image_name(handle).casefold() != 'nvda.exe':
                    raise WasapiError('NVDA process image identity changed during discovery')
                if api.session(pid) != current:
                    raise WasapiError('NVDA process session changed during discovery')
                target = _PinnedProcess(api, pid, handle, api.identity(handle))
            except BaseException:
                api.close(handle)
                raise
            targets.append(target)
            target.check()
        if len(targets) > 1:
            raise WasapiError('NVDA process identity is ambiguous')
        return targets.pop() if targets else None
    finally:
        for target in targets:
            target.release()


class ActivationParams(C.Structure):
    # AUDIOCLIENT_ACTIVATION_PARAMS: type + PROCESS_LOOPBACK_PARAMS union.
    _fields_ = [('activation_type', I32), ('pid', U32), ('mode', I32)]


class _Handler(C.Structure):
    _fields_ = [('vtable', C.POINTER(P))]


_handlers = {}
_handlers_lock = threading.Lock()
_HANDLER_IIDS = {bytes(GUID.parse(value)) for value in (
    '00000000-0000-0000-c000-000000000046',  # IUnknown
    '41d949ab-9862-444a-80f6-c261334da5eb',  # completion handler
    '94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90',  # IAgileObject
)}


@CALL(I32, P, C.POINTER(GUID), C.POINTER(P))
def _handler_query(this, iid, out):
    if not out:
        return -2147467261  # E_POINTER
    out[0] = None
    if not iid or bytes(iid.contents) not in _HANDLER_IIDS:
        return -2147467262  # E_NOINTERFACE
    with _handlers_lock:
        state = _handlers.get(this)
        if state is None:
            return -2147467259
        state.refs += 1
        out[0] = this
    return 0


@CALL(U32, P)
def _handler_addref(this):
    with _handlers_lock:
        state = _handlers[this]
        state.refs += 1
        return state.refs


@CALL(U32, P)
def _handler_release(this):
    with _handlers_lock:
        state = _handlers[this]
        state.refs -= 1
        if not state.refs:
            del _handlers[this]
        return state.refs


@CALL(I32, P, P)
def _handler_completed(this, operation):
    # No client is obtained here: the owner retrieves it from its own async
    # operation after the signal. In particular, no callback-thread COM client
    # pointer is passed to another apartment and no audio is started here.
    with _handlers_lock:
        state = _handlers.get(this)
    if state is not None:
        state.done.set()
    return 0


# Permanent thunks/table allow final Release to free a handler safely without
# freeing the currently executing ctypes callback itself. Native refcounts keep
# pending state+activation blob alive after our bounded timeout.
_HANDLER_TABLE = (P * 4)(*[C.cast(fn, P).value for fn in (
    _handler_query, _handler_addref, _handler_release, _handler_completed)])


class _ActivationState:
    def __init__(self, pid, mode):
        self.handler = _Handler(_HANDLER_TABLE)
        self.params = ActivationParams(1, pid, mode)
        self.variant = PROPVARIANT()
        self.variant.vt = 65  # VT_BLOB
        self.variant.value.counted.count = C.sizeof(self.params)
        self.variant.value.counted.pointer = C.addressof(self.params)
        self.done = threading.Event()
        self.refs = 1
        self.address = C.addressof(self.handler)
        with _handlers_lock:
            # A permanently broken activation must not cause unbounded retention
            # through repeated attempts. Never free still-owned native callbacks.
            if len(_handlers) >= 16:
                raise WasapiError('Too many pending process loopback activations')
            _handlers[self.address] = self


def _activate_client(pid, mode, cancel, *, activate=None,
                     pointer_factory=ComPointer, timeout=3.0):
    if cancel.is_set():
        raise WasapiError('Process loopback activation cancelled')
    if activate is None:
        activate = C.WinDLL('Mmdevapi').ActivateAudioInterfaceAsync
        activate.argtypes = [C.c_wchar_p, C.POINTER(GUID), C.POINTER(PROPVARIANT),
                             P, C.POINTER(P)]
        activate.restype = I32
    state = _ActivationState(pid, mode)
    op, result_pointer = P(), P()
    operation = unknown = client = None
    try:
        status = activate('VAD\\Process_Loopback', C.byref(IID_AUDIO_CLIENT),
                          C.byref(state.variant), C.byref(state.handler), C.byref(op))
        if op.value:
            operation = pointer_factory(op)
        check_hresult(status, 'ActivateAudioInterfaceAsync(process loopback)')
        if operation is None:
            raise WasapiError('Process loopback activation returned null operation')
        deadline = time.monotonic() + timeout
        while not state.done.wait(.01):
            if cancel.is_set():
                raise WasapiError('Process loopback activation cancelled')
            if time.monotonic() >= deadline:
                raise WasapiError('Process loopback activation timeout')
        if cancel.is_set():
            raise WasapiError('Process loopback activation cancelled')
        hr = I32()
        operation.call(3, [C.POINTER(I32), C.POINTER(P)],
                       C.byref(hr), C.byref(result_pointer))
        if result_pointer.value:
            unknown = pointer_factory(result_pointer)
        check_hresult(hr.value, 'Process loopback activation result')
        if unknown is None:
            raise WasapiError('Process loopback activation returned null client')
        pointer = P()
        unknown.call(0, [C.POINTER(GUID), C.POINTER(P)],
                     C.byref(IID_AUDIO_CLIENT), C.byref(pointer))
        client = pointer_factory(pointer)
        if cancel.is_set():
            raise WasapiError('Process loopback activation cancelled')
        result, client = client, None
        return result
    finally:
        for obj in (client, unknown, operation):
            if obj is not None:
                obj.release()
        _handler_release(state.address)


class _AudioAPI:
    pointer = staticmethod(ComPointer)
    activate = staticmethod(_activate_client)

    def __init__(self):
        self.ole = C.WinDLL('ole32')
        self.ole.CoInitializeEx.argtypes, self.ole.CoInitializeEx.restype = [P, U32], I32
        self.ole.CoUninitialize.argtypes, self.ole.CoUninitialize.restype = [], None
        self.kernel = C.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateEventW.argtypes = [P, I32, I32, C.c_wchar_p]
        self.kernel.CreateEventW.restype = P
        self.kernel.CloseHandle.argtypes, self.kernel.CloseHandle.restype = [P], I32

    def initialize(self):
        check_hresult(self.ole.CoInitializeEx(None, 0), 'CoInitializeEx(process MTA)')

    def uninitialize(self):
        self.ole.CoUninitialize()

    def create_event(self):
        event = self.kernel.CreateEventW(None, False, False, None)
        if not event:
            raise WasapiError('Process loopback event creation failed')
        return event

    def close_event(self, event):
        if not self.kernel.CloseHandle(event):
            raise WasapiError('Process loopback event cleanup failed')


class _NativeProcessReader:
    """Native owner, deliberately has no Reset method (process API constraint)."""
    def __init__(self, target, mode, rate, channels, cancel, *, api=None):
        self._owner = threading.get_ident()
        self.rate, self.channels = rate, channels
        self._api = api or _AudioAPI()
        self.client = self.capture = self.event = None
        self._initialized = self.running = False
        self._stamp = None
        self._diagnostic = dict(discontinuities=0)
        try:
            self._api.initialize()
            self._initialized = True
            target.check()
            if cancel.is_set():
                raise WasapiError('Process loopback activation cancelled')
            self.client = self._api.activate(target.pid, mode, cancel)
            target.check()
            if cancel.is_set():
                raise WasapiError('Process loopback activation cancelled')
            fmt = capture_format(rate, channels)
            flags = 0x20000 | 0x40000 | 0x80000000 | 0x08000000
            self.client.call(3, [I32, U32, C.c_int64, C.c_int64,
                                 C.POINTER(WAVEFORMATEX), P],
                             0, flags, 0, 0, C.byref(fmt), None)
            pointer = P()
            self.client.call(14, [C.POINTER(GUID), C.POINTER(P)],
                             C.byref(IID_CAPTURE_CLIENT), C.byref(pointer))
            self.capture = self._api.pointer(pointer)
            self.event = self._api.create_event()
            self.client.call(13, [P], self.event)
        except BaseException:
            self.close()
            raise

    def _check_owner(self):
        if self._owner != threading.get_ident():
            raise WasapiError('Process loopback native ownership violation')

    def start(self):
        self._check_owner()
        self.client.call(10, [])
        self.running = True
        self._stamp = None

    def stop(self):
        self._check_owner()
        if self.running:
            self.client.call(11, [])
            self.running = False

    def read(self):
        from .system_audio_capture import AudioPacket
        self._check_owner()
        available = U32()
        self.capture.call(5, [C.POINTER(U32)], C.byref(available))
        self._diagnostic['available'] = available.value
        if not available.value:
            return None
        data, frames, flags, position, stamp = P(), U32(), U32(), U64(), U64()
        result = self.capture.call(3, [C.POINTER(P), C.POINTER(U32), C.POINTER(U32),
                                      C.POINTER(U64), C.POINTER(U64)],
                                   C.byref(data), C.byref(frames), C.byref(flags),
                                   C.byref(position), C.byref(stamp))
        if result == 0x8890001:  # AUDCLNT_S_BUFFER_EMPTY: no owned buffer
            return None
        try:
            self._diagnostic.update(flags=flags.value, packet_frames=frames.value)
            if not 0 < frames.value <= self.rate * 2:
                raise WasapiError('Invalid or excessive process WASAPI packet size')
            if flags.value & 4 or not stamp.value:
                raise WasapiError('Process WASAPI packet timestamp is unreliable')
            if flags.value & ~7:
                raise WasapiError('Unknown process WASAPI packet flags')
            if self._stamp is not None:
                delta = stamp.value - self._stamp
                self._diagnostic['stamp_delta_100ns'] = delta
                if delta <= 0:
                    raise WasapiError('Process WASAPI packet timestamp is nonmonotonic')
                if flags.value & 1:
                    self._diagnostic['discontinuities'] += 1
            size = frames.value * self.channels * 4
            if flags.value & 2:
                payload = bytes(size)
            elif data.value:
                payload = C.string_at(data, size)
            else:
                raise WasapiError('Process WASAPI returned a null audio buffer')
            self._stamp = stamp.value
            return AudioPacket(stamp.value, payload)
        finally:
            self.capture.call(4, [U32], frames.value)

    def diagnostic_snapshot(self):
        return dict(self._diagnostic)

    def close(self):
        self._check_owner()
        error = None
        try:
            self.stop()
        except BaseException as exc:
            error = exc
        for name in ('capture', 'client'):
            pointer = getattr(self, name)
            if pointer is not None:
                try:
                    pointer.release()
                except BaseException as exc:
                    error = error or exc
                setattr(self, name, None)
        self.running = False  # interfaces released, including a failed Stop
        try:
            if self.event is not None:
                self._api.close_event(self.event)
                self.event = None
        except BaseException as exc:
            error = error or exc
        finally:
            if self._initialized:
                self._api.uninitialize()
                self._initialized = False
        if error is not None:
            raise error


class ProcessLoopbackStream:
    """A bounded producer; native calls never run on the consumer thread.

    Stop copies the remaining tail before acknowledging. Reset discards queued
    and native residue while stopped, WITHOUT IAudioClient.Reset. Read delivers
    an already copied prefix before raising a terminal producer error next time.
    """
    process_loopback = True

    def __init__(self, target, mode, rate, channels, *, native_factory=None,
                 timeout=5.0):
        capture_format(rate, channels)
        self.rate, self.channels = rate, channels
        self._target, self._mode = target, mode
        self._factory = native_factory or _NativeProcessReader
        self._timeout = timeout
        self._lock = threading.Lock()
        self._commands = queue.Queue(maxsize=1)
        self._packets = deque()
        self._queued_frames = 0
        self._cancel = threading.Event()
        self._ready = threading.Event()
        self._failure = self._cleanup_error = None
        self._running = False
        self._diagnostic = dict(queue_peak_frames=0, packets=0, frames=0)
        target.acquire()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name='wasapi-process-reader')
        try:
            self._thread.start()
        except BaseException:
            target.release()
            raise
        if not self._ready.wait(timeout):
            self._cancel.set()
            with self._lock:
                self._failure = self._failure or WasapiError('Process loopback initialization timeout')
            raise self._failure
        if self._failure is not None:
            self.close()
            raise self._failure

    @property
    def running(self):
        with self._lock:
            return self._running

    def _put(self, packet):
        frames = len(packet.data) // (self.channels * 4)
        with self._lock:
            if self._queued_frames + frames > self.rate * 2:
                raise WasapiError('Process loopback queue overflow')
            self._packets.append(packet)
            self._queued_frames += frames
            d = self._diagnostic
            d['queue_peak_frames'] = max(d['queue_peak_frames'], self._queued_frames)
            d['packets'] += 1
            d['frames'] += frames

    def _drain(self, native, *, discard=False):
        deadline = time.monotonic() + 1.0
        for _ in range(256):
            if time.monotonic() >= deadline:
                raise WasapiError('Process loopback drain timeout')
            packet = native.read()
            if packet is None:
                return
            if not discard:
                self._put(packet)
        raise WasapiError('Process loopback bounded drain exceeded')

    def _run(self):
        native = None
        command = None
        try:
            self._target.check()
            native = self._factory(self._target, self._mode, self.rate,
                                   self.channels, self._cancel)
            if self._cancel.is_set():
                return
            self._ready.set()
            while not self._cancel.is_set():
                self._target.check()
                try:
                    command = self._commands.get(timeout=.002)
                except queue.Empty:
                    command = None
                if command is not None:
                    action, done, reply, deadline, start_cancel = command
                    if (self._cancel.is_set() or time.monotonic() >= deadline
                            or (start_cancel is not None and start_cancel.is_set())):
                        raise WasapiError('Process loopback command cancelled')
                    if action == 'start':
                        if not self.running:
                            self._target.check()
                            if (self._cancel.is_set() or time.monotonic() >= deadline
                                    or (start_cancel is not None and start_cancel.is_set())):
                                raise WasapiError('Process loopback command cancelled')
                            native.start()
                            with self._lock:
                                self._running = True
                    elif action == 'stop':
                        if self.running:
                            native.stop()
                            with self._lock:
                                self._running = False
                        self._drain(native)
                    elif action == 'reset':
                        if self.running:
                            raise WasapiError('Process loopback reset requires stopped capture')
                        self._drain(native, discard=True)
                        with self._lock:
                            self._packets.clear()
                            self._queued_frames = 0
                    done.set()
                    command = None
                if self.running and not self._cancel.is_set():
                    self._drain(native)
                    with self._lock:
                        self._diagnostic.update(native.diagnostic_snapshot())
        except BaseException as exc:
            with self._lock:
                self._failure = self._failure or exc
            self._cancel.set()
            if command is not None:
                command[2]['error'] = exc
                command[1].set()
        finally:
            try:
                if native is not None:
                    native.close()
            except BaseException as exc:
                self._cleanup_error = exc
            finally:
                with self._lock:
                    self._running = False
                try:
                    self._target.release()
                except BaseException as exc:
                    self._cleanup_error = self._cleanup_error or exc
                self._ready.set()
                # Wake a command queued just as the worker was failing.
                try:
                    pending = self._commands.get_nowait()
                except queue.Empty:
                    pass
                else:
                    pending[2]['error'] = self._failure or WasapiError('Process loopback closed')
                    pending[1].set()

    def _command(self, action, *, start_cancel=None):
        if self._failure is not None:
            raise self._failure
        if self._cancel.is_set():
            raise WasapiError('Process loopback closed')
        if start_cancel is not None and start_cancel.is_set():
            raise WasapiError('Process loopback command cancelled')
        done, reply = threading.Event(), {}
        try:
            self._commands.put_nowait((action, done, reply, time.monotonic() + self._timeout,
                                      start_cancel))
        except queue.Full:
            raise WasapiError('Process loopback command already pending') from None
        if not done.wait(self._timeout):
            self._cancel.set()
            with self._lock:
                self._failure = self._failure or WasapiError(
                    'Process loopback command acknowledgement timeout')
            raise self._failure
        if 'error' in reply:
            raise reply['error']

    def start(self, *, cancel_event=None):
        # Only cancel pending Start, not the running reader: normal Stop must
        # still drain the captured tail before closing the recording.
        self._command('start', start_cancel=cancel_event)

    def stop(self):
        self._command('stop')

    def reset(self):
        self._command('reset')

    def read_packets(self):
        with self._lock:
            packets = list(self._packets)
            self._packets.clear()
            self._queued_frames = 0
            failure = self._failure
        if not packets and failure is not None:
            raise failure
        return packets

    def diagnostic_snapshot(self):
        with self._lock:
            return dict(self._diagnostic, running=int(self._running), loopback=1,
                        process_loopback=1, failed=int(self._failure is not None),
                        queued_frames=self._queued_frames,
                        cleanup_failed=int(self._cleanup_error is not None),
                        reader_alive=int(self._thread.is_alive()))

    def close(self):
        self._cancel.set()
        self._thread.join(self._timeout)
        if self._thread.is_alive():
            raise WasapiError('Process loopback reader cleanup timeout')
        if self._cleanup_error is not None:
            raise self._cleanup_error


def open_process_stream(target, mode, rate, channels):
    return ProcessLoopbackStream(target, mode, rate, channels)
