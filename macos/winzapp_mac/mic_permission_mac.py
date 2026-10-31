"""Say what macOS is doing about the microphone before a recording starts.

macOS holds an app's first microphone access until the user answers its prompt,
and that prompt can queue behind another one (Focus status), so a record button
looked dead for minutes with nothing spoken. A refused permission is quieter
still: the stream opens and records silence.
"""

import logging

_NOT_DETERMINED = 0
_RESTRICTED = 1
_DENIED = 2
_AV_MEDIA_TYPE_AUDIO = "soun"

_device_class = None
_told_about_prompt = False


def authorization_status():
    """AVCaptureDevice's microphone status, or None if it cannot be read."""
    global _device_class
    try:
        if _device_class is None:
            import objc
            objc.loadBundle("AVFoundation", globals(),
                            bundle_path="/System/Library/Frameworks/AVFoundation.framework")
            _device_class = objc.lookUpClass("AVCaptureDevice")
        return int(_device_class.authorizationStatusForMediaType_(_AV_MEDIA_TYPE_AUDIO))
    except Exception:
        logging.debug("[mic_permission_mac] status unavailable", exc_info=True)
        return None


def is_refused(status):
    return status in (_RESTRICTED, _DENIED)


def _announced(start):
    """Wrap _start_voice_recording. Whatever is said comes before the open,
    because the open is what raises the prompt: speaking later would talk over
    it, and no delay suits someone still reading it. A prompt that appears
    takes the screen reader away from this sentence; one queued behind another
    prompt lets it finish. AVCaptureDevice's status is not refreshed in a
    running app when the prompt comes from Core Audio, so "not determined" is
    said once per launch, and nothing here ever stops a recording."""
    def start_voice_recording(self, *a, **k):
        global _told_about_prompt
        status = authorization_status()
        if is_refused(status):
            self.main_window.output(self.main_window.i18n.t("mac_mic_permission_denied"))
        elif status == _NOT_DETERMINED and not _told_about_prompt:
            _told_about_prompt = True
            self.main_window.output(self.main_window.i18n.t("mac_mic_permission_waiting"))
        return start(self, *a, **k)
    return start_voice_recording


def install():
    from status_tab.status_voice import StatusVoiceMixin
    from ui.conversation_panel.voice_recording import VoiceRecordingMixin
    for cls in (VoiceRecordingMixin, StatusVoiceMixin):
        cls._start_voice_recording = _announced(cls._start_voice_recording)
