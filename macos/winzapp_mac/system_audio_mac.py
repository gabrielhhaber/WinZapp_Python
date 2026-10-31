"""Microphone + computer audio on macOS: the capture behind Command+Shift+H.

core.system_audio_capture.SystemAudioRecorder only needs a backend with a
clock and two kinds of stream, so this replaces core.wasapi_capture's.

Computer audio comes from a Core Audio process tap (macOS 14.4 and later)
placed inside a private aggregate device, read directly with a Core Audio
IOProc: PortAudio lists devices only when its first instance starts, and
WinZapp's microphone has already started it, so it can never see this one.
It needs no virtual driver and leaves playback untouched. macOS asks for
System Audio Recording permission the first time it runs.
"""

import array
import collections
import ctypes as C
import logging
import platform
import subprocess
import time
import uuid

import wx

_COREAUDIO = "/System/Library/Frameworks/CoreAudio.framework"
_DEVICE_NAME = "WinZapp computer audio"
_FRAMES_PER_BUFFER = 480
# Bounded like WASAPI's 1 s reserve, with room for a slow mixing thread.
_MAX_QUEUED_PACKETS = 1000
# Past this the stream's own count is abandoned and the clock followed.
_RESYNC_100NS = 200_000
_MINIMUM_MACOS = (14, 4)
_AUDIO_ROUTERS = ("CrosspointAgent", "arkaudiod", "Audio Hijack", "Loopback")
_paFloat32 = 1
_paContinue = 0


class MacCaptureError(RuntimeError):
    pass


def clock():
    """100 ns units, as the recorder's timeline expects."""
    return time.monotonic_ns() // 100


def _macos_version():
    parts = (platform.mac_ver()[0] or "0").split(".")
    try:
        return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        return 0, 0


def available():
    return _macos_version() >= _MINIMUM_MACOS


class PacketClock:
    """Stamps for buffers that arrive with jitter but hold exact sample counts:
    each continues the last unless the stream really fell away from the clock."""

    def __init__(self, rate):
        self._rate = rate
        self._expected = None

    def stamp(self, frames, now):
        duration = frames * 10_000_000 // self._rate
        start = now - duration
        if self._expected is None or abs(start - self._expected) > _RESYNC_100NS:
            stamp = start
        else:
            stamp = self._expected
        self._expected = stamp + duration
        return stamp

    def reset(self):
        self._expected = None


def _four_cc(text):
    return int.from_bytes(text.encode("ascii"), "big")


class _Address(C.Structure):
    _fields_ = [("selector", C.c_uint32), ("scope", C.c_uint32), ("element", C.c_uint32)]


class _AudioBuffer(C.Structure):
    _fields_ = [("channels", C.c_uint32), ("size", C.c_uint32), ("data", C.c_void_p)]


class _AudioBufferList(C.Structure):
    _fields_ = [("count", C.c_uint32), ("buffers", _AudioBuffer * 8)]


_IOProc = C.CFUNCTYPE(C.c_int32, C.c_uint32, C.c_void_p, C.POINTER(_AudioBufferList),
                      C.c_void_p, C.c_void_p, C.c_void_p, C.c_void_p)
_NOMINAL_RATE = _Address(_four_cc("nsrt"), _four_cc("glob"), 0)
_PROCESS_FROM_PID = _Address(_four_cc("id2p"), _four_cc("glob"), 0)
_SYSTEM_OBJECT = 1


def interleaved(buffers, channels):
    """*channels* of interleaved float32 from Core Audio's (channel count, bytes)
    buffers, which may already be interleaved or hold one channel each."""
    if len(buffers) == 1 and buffers[0][0] == channels:
        return buffers[0][1]
    arrays = [array.array("f", data) for _count, data in buffers[:channels]]
    if len(arrays) != channels:
        raise MacCaptureError(f"Expected {channels} audio channels, got {len(arrays)}")
    out = array.array("f", bytes(4 * channels * len(arrays[0])))
    for channel, samples in enumerate(arrays):
        out[channel::channels] = samples
    return out.tobytes()


class _PacketStream:
    """What SystemAudioRecorder reads. Opening does not start a stream."""

    def __init__(self, rate, channels, loopback):
        self.rate, self.channels = rate, channels
        self.running = False
        self._loopback = loopback
        self._packets = collections.deque()
        self._clock = PacketClock(rate)
        self._dropped = 0
        self._seen_packet = False

    def _queue(self, data, frames):
        from core.system_audio_capture import AudioPacket
        if len(self._packets) >= _MAX_QUEUED_PACKETS:
            self._dropped += 1
        else:
            self._packets.append(AudioPacket(self._clock.stamp(frames, clock()), data))

    def reset(self):
        self._packets.clear()
        self._clock.reset()
        self._seen_packet = False

    def read_packets(self):
        packets = []
        while self._packets:
            packets.append(self._packets.popleft())
        self._seen_packet = self._seen_packet or bool(packets)
        return packets

    def diagnostic_snapshot(self):
        return {"running": int(self.running), "loopback": int(self._loopback),
                "seen_packet": int(self._seen_packet), "discontinuities": self._dropped}


class PortAudioCapture(_PacketStream):
    """The microphone."""

    def __init__(self, pa, device_index, rate, channels):
        super().__init__(rate, channels, False)
        self._stream = pa.open(
            rate=rate, channels=channels, format=_paFloat32, input=True,
            input_device_index=device_index, frames_per_buffer=_FRAMES_PER_BUFFER,
            stream_callback=self._on_audio, start=False)

    def _on_audio(self, data, frames, _time_info, _status):
        self._queue(bytes(data), frames)
        return None, _paContinue

    def start(self):
        self._stream.start_stream()
        self.running = True

    def stop(self):
        if self.running:
            self._stream.stop_stream()
            self.running = False

    def close(self):
        stream, self._stream = self._stream, None
        if stream is not None:
            self.running = False
            stream.close()


class TapCapture(_PacketStream):
    """Computer audio, from ComputerAudioTap's device."""

    def __init__(self, tap, rate, channels):
        super().__init__(rate, channels, True)
        self._tap = tap
        tap.set_rate(rate)
        self._proc = _IOProc(self._on_audio)  # kept alive for as long as the proc id
        self._proc_id = C.c_void_p()
        status = tap.api.AudioDeviceCreateIOProcID(
            tap.device, self._proc, None, C.byref(self._proc_id))
        if status:
            raise MacCaptureError(f"Could not read the capture device ({status})")

    def _on_audio(self, _device, _now, inputs, _in_time, _out, _out_time, _client):
        try:
            found = inputs.contents
            buffers = [(b.channels, C.string_at(b.data, b.size))
                       for b in found.buffers[:found.count] if b.data]
            if buffers:
                data = interleaved(buffers, self.channels)
                self._queue(data, len(data) // (4 * self.channels))
        except Exception:
            self._dropped += 1
        return 0

    def start(self):
        status = self._tap.api.AudioDeviceStart(self._tap.device, self._proc_id)
        if status:
            raise MacCaptureError(f"Could not start computer audio ({status})")
        self.running = True

    def stop(self):
        if self.running:
            self._tap.api.AudioDeviceStop(self._tap.device, self._proc_id)
            self.running = False

    def close(self):
        self.stop()
        proc_id, self._proc_id = self._proc_id, None
        if proc_id:
            self._tap.api.AudioDeviceDestroyIOProcID(self._tap.device, proc_id)


def _process_objects(name):
    """Core Audio process objects of the running processes called *name*."""
    found = subprocess.run(["pgrep", "-x", name], capture_output=True, text=True)
    api = C.CDLL(f"{_COREAUDIO}/CoreAudio")
    objects = []
    for pid in found.stdout.split():
        pid = C.c_int32(int(pid))
        process, size = C.c_uint32(0), C.c_uint32(4)
        status = api.AudioObjectGetPropertyData(
            _SYSTEM_OBJECT, C.byref(_PROCESS_FROM_PID), C.sizeof(pid), C.byref(pid),
            C.byref(size), C.byref(process))
        if status == 0 and process.value:
            objects.append(process.value)
    return objects


def voiceover_process():
    """VoiceOver's Core Audio process object, or None if it is not running."""
    objects = _process_objects("VoiceOver")
    return objects[0] if objects else None


def audio_routers():
    """Processes that replay other apps' audio. A tap already has that audio
    from the app that made it, so including them records it twice, a few
    milliseconds apart: measured at 16.7 ms through Crosspoint, which sounds
    phasey and also defeats VoiceOver's own slider."""
    return [process for name in _AUDIO_ROUTERS for process in _process_objects(name)]


class ComputerAudioTap:
    """A tap inside a private aggregate device: everything playing except
    *excluded*, or only the processes in *only*."""

    def __init__(self, *, excluded=(), only=None):
        import objc
        from Foundation import NSDictionary
        self.api = C.CDLL(f"{_COREAUDIO}/CoreAudio")
        self.api.AudioHardwareCreateProcessTap.argtypes = [C.c_void_p, C.POINTER(C.c_uint32)]
        self.api.AudioHardwareCreateAggregateDevice.argtypes = [C.c_void_p, C.POINTER(C.c_uint32)]
        self.api.AudioDeviceCreateIOProcID.argtypes = [
            C.c_uint32, _IOProc, C.c_void_p, C.POINTER(C.c_void_p)]
        self.api.AudioDeviceStart.argtypes = [C.c_uint32, C.c_void_p]
        self.api.AudioDeviceStop.argtypes = [C.c_uint32, C.c_void_p]
        self.api.AudioDeviceDestroyIOProcID.argtypes = [C.c_uint32, C.c_void_p]
        self._tap = C.c_uint32(0)
        self.device_id = C.c_uint32(0)
        objc.loadBundle("CoreAudio", {}, bundle_path=_COREAUDIO)
        description = objc.lookUpClass("CATapDescription").alloc()
        description = (description.initStereoGlobalTapButExcludeProcesses_(list(excluded))
                       if only is None else description.initStereoMixdownOfProcesses_(list(only)))
        description.setName_(_DEVICE_NAME)
        description.setPrivate_(True)
        status = self.api.AudioHardwareCreateProcessTap(
            objc.pyobjc_id(description), C.byref(self._tap))
        if status:
            raise MacCaptureError(f"Could not create the audio tap ({status})")
        settings = NSDictionary.dictionaryWithDictionary_({
            "uid": f"com.winzapp.computer-audio.{uuid.uuid4().hex}",
            "name": _DEVICE_NAME, "private": 1, "stacked": 0, "tapautostart": 1,
            "taps": [{"uid": description.UUID().UUIDString(), "drift": 1}],
        })
        status = self.api.AudioHardwareCreateAggregateDevice(
            objc.pyobjc_id(settings), C.byref(self.device_id))
        if status:
            self.close()
            raise MacCaptureError(f"Could not create the capture device ({status})")

    @property
    def device(self):
        return self.device_id.value

    def set_rate(self, rate):
        """The recorder's timeline assumes *rate*, so anything else is a failure."""
        wanted = C.c_double(rate)
        self.api.AudioObjectSetPropertyData(
            self.device, C.byref(_NOMINAL_RATE), 0, None, C.sizeof(wanted), C.byref(wanted))
        actual, size = C.c_double(0), C.c_uint32(C.sizeof(C.c_double))
        status = self.api.AudioObjectGetPropertyData(
            self.device, C.byref(_NOMINAL_RATE), 0, None, C.byref(size), C.byref(actual))
        if status or int(actual.value) != rate:
            raise MacCaptureError(f"The capture device runs at {actual.value} Hz, not {rate}")

    def close(self):
        if self.device_id.value:
            self.api.AudioHardwareDestroyAggregateDevice(self.device_id)
            self.device_id = C.c_uint32(0)
        if self._tap.value:
            self.api.AudioHardwareDestroyProcessTap(self._tap)
            self._tap = C.c_uint32(0)


class MacBackend:
    def __init__(self):
        import pyaudio
        self._pyaudio = pyaudio.PyAudio
        self._owned = []
        self._taps = []

    @staticmethod
    def clock():
        return clock()

    def open_microphone(self, name, rate, channels):
        from core.audio_devices import find_input_device_index
        pa = self._pyaudio()
        self._owned.append(pa)
        return PortAudioCapture(pa, find_input_device_index(name, pa), rate, channels)

    def open_loopback(self, rate, channels):
        return self._open_tap(rate, channels, excluded=audio_routers())

    def _open_tap(self, rate, channels, **which):
        tap = ComputerAudioTap(**which)
        self._taps.append(tap)
        return TapCapture(tap, rate, channels)

    def open_nvda_loopbacks(self, rate, channels):
        """Everything except VoiceOver, then VoiceOver alone, for its own
        slider. None when it is not running: one stream, as the recorder expects."""
        process = voiceover_process()
        if process is None:
            return None
        return (self._open_tap(rate, channels, excluded=[process, *audio_routers()]),
                self._open_tap(rate, channels, only=[process]))

    def close(self):
        for pa in self._owned:
            try:
                pa.terminate()
            except Exception:
                logging.debug("[system_audio_mac] PortAudio close failed", exc_info=True)
        self._owned.clear()
        for tap in self._taps:
            tap.close()
        self._taps.clear()


def without_value_labels(style):
    """wxOSX draws SL_LABELS as loose static text beside the slider, which
    VoiceOver reads as stray numbers; the slider already speaks its value."""
    return style & ~wx.SL_LABELS


class _Slider(wx.Slider):
    def __init__(self, *args, **kwargs):
        if "style" in kwargs:
            kwargs["style"] = without_value_labels(kwargs["style"])
        super().__init__(*args, **kwargs)


def install():
    if not available():
        return
    from core import wasapi_capture
    wasapi_capture.WasapiBackend = MacBackend
    wx.Slider = _Slider
