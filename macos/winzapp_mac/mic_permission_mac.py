"""Say what macOS is doing about recording permissions before a recording.

macOS holds an app's first microphone access until the user answers its prompt,
and that prompt can queue behind another one (Focus status), so a record button
looked dead for minutes with nothing spoken. A refused permission is quieter
still: the recording runs and holds silence.

TCCAccessPreflight is private but it is the only way to read System Audio
Recording, and it asks macOS itself, so unlike AVCaptureDevice it does not go
stale in a running app. Measured: 0 granted, 2 never asked. 1 is denied.
"""

import ctypes as C
import logging

_TCC = "/System/Library/PrivateFrameworks/TCC.framework/Versions/A/TCC"
_COREFOUNDATION = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
_UTF8 = 0x08000100
_GRANTED, _DENIED = 0, 1

MICROPHONE = ("kTCCServiceMicrophone", "mac_mic_permission_waiting", "mac_mic_permission_denied")
COMPUTER_AUDIO = ("kTCCServiceAudioCapture", "mac_system_audio_permission_waiting",
                  "mac_system_audio_permission_denied")

_preflight = None
_cf_string = None


def status(service):
    """TCCAccessPreflight's answer for *service*, or None if it cannot be read."""
    global _preflight, _cf_string
    try:
        if _preflight is None:
            _cf_string = C.CDLL(_COREFOUNDATION).CFStringCreateWithCString
            _cf_string.restype = C.c_void_p
            _cf_string.argtypes = [C.c_void_p, C.c_char_p, C.c_uint32]
            _preflight = C.CDLL(_TCC).TCCAccessPreflight
            _preflight.restype = C.c_long
            _preflight.argtypes = [C.c_void_p, C.c_void_p]
        return int(_preflight(_cf_string(None, service.encode(), _UTF8), None))
    except Exception:
        logging.debug("[mic_permission_mac] status unavailable", exc_info=True)
        return None


def notice_key(answer, permission):
    """The string to speak for *answer*; None when there is nothing to say."""
    _service, waiting, denied = permission
    if answer is None or answer == _GRANTED:
        return None
    return denied if answer == _DENIED else waiting


def notices(permissions):
    """One sentence at most, for the first permission that needs saying: a
    second would be cut off by the first prompt anyway."""
    for permission in permissions:
        key = notice_key(status(permission[0]), permission)
        if key:
            return key
    return None


def _announced(start, permissions):
    """Whatever is said comes before the open, because the open is what raises
    the prompt: speaking later would talk over it, and no delay suits someone
    still reading it. A prompt that appears takes the screen reader away from
    this sentence; one queued behind another prompt lets it finish. Nothing
    here stops a recording, since a wrong reading must not cost one."""
    def wrapped(self, *a, **k):
        key = notices(permissions)
        if key:
            self.main_window.output(self.main_window.i18n.t(key))
        return start(self, *a, **k)
    return wrapped


def install():
    from status_tab.status_voice import StatusVoiceMixin
    from ui.conversation_panel.system_audio_recording import SystemAudioRecordingMixin
    from ui.conversation_panel.voice_recording import VoiceRecordingMixin
    for cls in (VoiceRecordingMixin, StatusVoiceMixin):
        cls._start_voice_recording = _announced(cls._start_voice_recording, (MICROPHONE,))
    SystemAudioRecordingMixin._start_system_audio_recording = _announced(
        SystemAudioRecordingMixin._start_system_audio_recording, (MICROPHONE, COMPUTER_AUDIO))
