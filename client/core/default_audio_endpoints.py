"""Follow Windows' default audio devices while a call is in progress.

A call opens PortAudio on a concrete device index (core/call_audio.py), so
when Windows moves its default output elsewhere the call keeps playing where it
started. Plugging a wired headset into many laptops does exactly that: the
driver activates a "Headphones" endpoint and makes it the default, every other
program follows, and the "Speakers" endpoint stays valid, so the call's stream
never fails and nothing tells WinZapp to move (report of 2026-11-18: the call
stayed on the speakers when the headset was plugged in, and on the headset
when it was taken out). The same happens to the microphone of a headset.

Nothing here opens audio. ``default_endpoint_ids()`` reads the ids of the
default endpoints through MMDevice (no stream, no session);
``DefaultEndpointWatcher`` polls them and reports a change once it has held
for two readings (a plug can make the default flicker); and
``refresh_portaudio_devices()`` makes PortAudio enumerate again, since it
reads the device list and the defaults only once, when it initialises -- the
copy sounddevice ships has no ``PaWasapi_UpdateDeviceList`` (it answers
paInternalError, measured 2026-11-18). That refresh closes every PortAudio
stream of the process, so it runs only while the call's streams are closed.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
import uuid
from typing import Callable, Optional

#: How often the defaults are read, in seconds. Two equal readings in a row
#: are needed, so a change is acted on within about a second.
POLL_SECONDS = 0.5

_RENDER, _CAPTURE, _CONSOLE = 0, 1, 0
_CLSCTX_ALL = 23
_RPC_E_CHANGED_MODE = -2147417850   # 0x80010106: COM already set up as STA


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]


def _guid(text):
    return _GUID.from_buffer_copy(uuid.UUID(text).bytes_le)


def _method(pointer, index, *argtypes):
    """The COM method at vtable *index* of *pointer*, as a callable."""
    vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    prototype = ctypes.WINFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, *argtypes)
    return lambda *args: prototype(vtable[index])(pointer, *args)


def _release(pointer):
    if pointer:
        _method(pointer, 2)()


def _default_id(enumerator, flow, ole32) -> Optional[str]:
    """The id of the default *flow* endpoint, None when there is none."""
    device = ctypes.c_void_p()
    # IMMDeviceEnumerator::GetDefaultAudioEndpoint(flow, eConsole, &device)
    hr = _method(enumerator, 4, ctypes.c_int32, ctypes.c_int32,
                 ctypes.POINTER(ctypes.c_void_p))(flow, _CONSOLE, ctypes.byref(device))
    if hr < 0 or not device:
        return None   # E_NOTFOUND: no endpoint of this kind at the moment
    try:
        text = ctypes.c_wchar_p()
        # IMMDevice::GetId(&text)
        if _method(device.value, 5, ctypes.POINTER(ctypes.c_wchar_p))(ctypes.byref(text)) < 0:
            return None
        try:
            return text.value
        finally:
            ole32.CoTaskMemFree(text)
    finally:
        _release(device.value)


def default_endpoint_ids() -> tuple[Optional[str], Optional[str]]:
    """(output id, input id) of Windows' default endpoints; (None, None) when
    they cannot be read (not Windows, no audio service)."""
    if sys.platform != "win32":
        return None, None
    ole32 = ctypes.windll.ole32
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    hr = ole32.CoInitializeEx(None, 0)   # MTA; S_FALSE when already set up
    initialized = hr >= 0
    if hr < 0 and hr != _RPC_E_CHANGED_MODE:
        return None, None
    enumerator = ctypes.c_void_p()
    try:
        clsid = _guid("BCDE0395-E52F-467C-8E3D-C4579291692E")   # MMDeviceEnumerator
        iid = _guid("A95664D2-9614-4F35-A746-DE8DB63617E6")     # IMMDeviceEnumerator
        if ole32.CoCreateInstance(ctypes.byref(clsid), None, _CLSCTX_ALL,
                                  ctypes.byref(iid), ctypes.byref(enumerator)) < 0:
            return None, None
        return (_default_id(enumerator.value, _RENDER, ole32),
                _default_id(enumerator.value, _CAPTURE, ole32))
    except OSError:
        return None, None
    finally:
        _release(enumerator.value)
        if initialized:
            ole32.CoUninitialize()


def followed_change(previous, current, *, follow_output: bool, follow_input: bool) -> bool:
    """Whether going from the *previous* to the *current* (output, input)
    default ids should move the call.

    Only a direction the call takes from the system default counts (a device
    the user chose by name is kept wherever the default goes), and only a move
    TO an endpoint: with no default at all there is nothing to reopen on, and
    the reopen would fail and end the call's audio.
    """
    for follows, before, after in ((follow_output, previous[0], current[0]),
                                   (follow_input, previous[1], current[1])):
        if follows and after is not None and after != before:
            return True
    return False


class DefaultEndpointWatcher:
    """Calls *on_change()* when a followed default endpoint changes.

    *follows()* answers (follow_output, follow_input) at every reading, so a
    device chosen in the middle of the call is honoured at once. *on_change*
    returns False when it could not act yet (a device switch already running):
    the change is then reported again at the next reading.
    """

    def __init__(self, on_change: Callable[[], bool], follows: Callable[[], tuple],
                 *, reader=default_endpoint_ids, interval: float = POLL_SECONDS,
                 name: str = "WinZappCallDefaultDevices", tag: str = "call_audio"):
        self._on_change = on_change
        self._follows = follows
        self._reader = reader
        self._interval = interval
        self._name = name
        self._tag = tag
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._acted_on = None
        self._last = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._acted_on = self._last = self._reader()
        self._thread = threading.Thread(target=self._run, name=self._name,
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return self._thread is not None and not self._stop.is_set()

    def poll(self) -> bool:
        """One reading; True when *on_change* was called and accepted it."""
        follow_output, follow_input = self._follows()
        if not (follow_output or follow_input):
            # Both devices chosen by name: nothing to follow, so no COM reading
            # either. The baseline is taken again when following resumes.
            self._acted_on = self._last = None
            return False
        current = self._reader()
        if self._acted_on is None:
            self._acted_on = self._last = current
            return False
        stable = current == self._last
        self._last = current
        if not stable or current == self._acted_on:
            return False
        if not followed_change(self._acted_on, current, follow_output=follow_output,
                               follow_input=follow_input):
            # Not ours to follow (a chosen device, or a default that vanished):
            # remembered, so it is not reconsidered at every reading.
            self._acted_on = current
            return False
        if self._on_change() is False:
            return False
        self._acted_on = current
        return True

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.poll()
            except Exception:
                logging.exception("[%s] reading the default audio devices failed", self._tag)


def refresh_portaudio_devices(sd) -> bool:
    """Make PortAudio list the devices and the defaults again. Closes every
    PortAudio stream of the process: call it only with the call's streams
    closed. Returns whether it worked."""
    if sd is None:
        return False
    try:
        sd._terminate()
    except Exception:
        logging.exception("[call_audio] PortAudio could not be closed to list the devices again")
        return False
    try:
        sd._initialize()
        return True
    except Exception:
        # Left uninitialised, every later stream would fail: one more try,
        # after which the reopen reports the failure as any device error.
        logging.exception("[call_audio] PortAudio could not list the devices again")
        try:
            sd._initialize()
        except Exception:
            pass
        return False
