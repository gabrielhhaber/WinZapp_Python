"""DefaultAudioDevicesMixin -- part of MainWindow (see main_window/__init__.py).

What plays or records on "system default" follows Windows' default device
when it changes: plugging a headset in (or taking it out) moves WinZapp's sound
the way it moves every other program's. Two followers, each with its own
DefaultEndpointWatcher (the why is in core/default_audio_endpoints.py):

* program sounds (BASS), for the life of the window: SoundSystem.
  follow_default_output() moves the output and the effects that are on the
  default, on the UI thread;
* a call in progress (PortAudio), output and microphone: the call is reopened
  through the in-call device switch the call settings already use
  (``_restart_active_voice_call_audio``), with PortAudio's device list read
  again first.

A device chosen by name, in the settings or with the quick switch
(Ctrl+Alt+Shift+H/G), is never moved.
"""

import logging

import wx

from core.default_audio_endpoints import DefaultEndpointWatcher

#: Program sounds are checked less often than a call: a second's delay on a
#: notification sound is not noticed, and this watcher runs all the time.
_PROGRAM_SOUNDS_POLL_SECONDS = 1.0


class DefaultAudioDevicesMixin:
    # ── program sounds ──────────────────────────────────────────────────

    def _start_default_output_follow(self) -> None:
        """Follow Windows' default output for the program sounds (once, at
        startup, after the configured devices were applied)."""
        if getattr(self, "_default_output_watcher", None) is not None:
            return
        watcher = DefaultEndpointWatcher(
            self._on_default_output_changed,
            lambda: (self.sound_system.follows_default_output(), False),
            interval=_PROGRAM_SOUNDS_POLL_SECONDS,
        )
        self._default_output_watcher = watcher
        try:
            watcher.start()
        except Exception:
            logging.exception("[sound_system] could not follow the default output device")
            self._default_output_watcher = None

    def _on_default_output_changed(self) -> bool:
        """The watcher's callback (its own thread): BASS is moved on the UI
        thread, where every sound is played."""
        if getattr(self, "_shutting_down", False):
            return True
        wx.CallAfter(self._follow_default_output)
        return True

    def _follow_default_output(self) -> None:
        if getattr(self, "_shutting_down", False):
            return
        try:
            self.sound_system.follow_default_output()
        except Exception:
            logging.exception("[sound_system] moving the sounds to the new default output failed")

    # ── a call in progress ──────────────────────────────────────────────

    def _call_devices_following_default(self) -> tuple:
        """(output follows, input follows): a call device with no name is the
        system default."""
        return (not self.call_audio_device("output"),
                not self.call_audio_device("input"))

    def _start_call_default_device_follow(self) -> None:
        """Start following the defaults for the call in progress (idempotent)."""
        watcher = getattr(self, "_call_default_device_watcher", None)
        if watcher is not None and watcher.running:
            return
        watcher = DefaultEndpointWatcher(self._on_call_default_device_changed,
                                         self._call_devices_following_default)
        self._call_default_device_watcher = watcher
        try:
            watcher.start()
        except Exception:
            logging.exception("[call_audio] could not follow the default audio devices")
            self._call_default_device_watcher = None

    def _stop_call_default_device_follow(self) -> None:
        watcher = getattr(self, "_call_default_device_watcher", None)
        self._call_default_device_watcher = None
        if watcher is not None:
            watcher.stop()

    def _on_call_default_device_changed(self) -> bool:
        """The watcher's callback (its own thread). False while a device
        switch is already running, so the change is offered again."""
        if not getattr(self, "_active_voice_call", None):
            return True     # the call is over; the watcher is being stopped
        logging.info("[call_audio] Windows default audio device changed; moving the call")
        return self._restart_active_voice_call_audio(refresh_device_list=True)
