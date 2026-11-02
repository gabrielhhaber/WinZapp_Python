"""Typing preferences and one bounded, cancellable sound cadence per chat.

No network, threads, GUI or audio device access. The UI supplies scheduling,
playback and a context guard. Ordinary presence updates renew activity, never
postpone the next sound. Speech is deliberately outside the repeat loop.
"""
import time

MODES = ("off", "speech", "sound", "both")
DEFAULT_INTERVAL = 3
ACTIVITY_SECONDS = 10
SILENCE_CAPTURE_KEY = "typing_sound_silence_capture"


def mode(settings):
    speech = settings.get("speech_content", {})
    spoken = bool(speech.get("announce_typing", True))
    audible = speech.get("typing_sound", False) is True
    return "both" if spoken and audible else "speech" if spoken else "sound" if audible else "off"


def interval(settings):
    value = settings.get("speech_content", {}).get("typing_sound_interval", DEFAULT_INTERVAL)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DEFAULT_INTERVAL
    return int(value) if 1 <= value <= 30 else DEFAULT_INTERVAL


def silence_during_capture(settings):
    return settings.get("speech_content", {}).get(SILENCE_CAPTURE_KEY, True) is not False


def save_preferences(settings, selected, seconds, *, silence_capture=None):
    selected = selected if selected in MODES else "speech"
    speech = settings.setdefault("speech_content", {})
    speech["announce_typing"] = selected in ("speech", "both")
    speech["typing_sound"] = selected in ("sound", "both")
    speech["typing_sound_interval"] = interval({"speech_content": {"typing_sound_interval": seconds}})
    if silence_capture is not None:
        speech[SILENCE_CAPTURE_KEY] = bool(silence_capture)


class TypingCadence:
    """One timer and one owned sound, fenced even after Stop queued a callback."""
    def __init__(self, schedule, current_context, play, stop, seconds, clock=None):
        self.schedule = schedule
        self.current_context = current_context
        self.play = play
        self.stop_sound = stop
        self.seconds = seconds
        self.clock = clock or time.monotonic
        self.context = None
        self.people = {}
        self.timer = None
        self.generation = object()

    def stop(self):
        self.finish_stop(*self.retire())

    def retire(self):
        """Fence work without touching any GUI/audio object (also safe for teardown)."""
        self.generation = generation = object()
        timer, self.timer = self.timer, None
        self.context = None
        # Replace the map: a UI snapshot can still be iterating its old value.
        self.people = {}
        return timer, generation

    def finish_stop(self, timer, generation):
        """Retire the exact old native timer on the UI thread, preserving successors."""
        if timer is not None:
            try:
                timer.Stop()
            except RuntimeError:
                pass
        if generation is self.generation:
            self.stop_sound()

    def observe(self, context, person, composing):
        self.observe_many(context, {person: composing})

    def observe_many(self, context, changes):
        """Apply one accepted participant batch without an artificial silence gap."""
        if context is None or context != self.current_context():
            self.stop()
            return
        if self.context != context:
            self.stop()
            self.context = context
        now = self.clock()
        self.people = {person: until for person, until in self.people.items() if until > now}
        for person, composing in changes.items():
            if composing:
                self.people[person] = now + ACTIVITY_SECONDS
            else:
                self.people.pop(person, None)
        if not self.people:
            self.stop()
        elif self.timer is None:
            self._tick(self.generation)

    def dismiss(self, person=None):
        if person is None:
            self.people.clear()
        else:
            self.people.pop(person, None)
        if not self.people:
            self.stop()

    def _tick(self, generation):
        if generation != self.generation:
            return
        self.generation = generation = object()
        self.timer = None
        if self.context is None or self.context != self.current_context():
            self.stop()
            return
        now = self.clock()
        self.people = {person: until for person, until in self.people.items() if until > now}
        if not self.people:
            self.stop()
            return
        try:
            self.play()
            # Playback may synchronously invalidate the UI/account/settings.
            if generation == self.generation and self.context == self.current_context():
                due = min(self.seconds(), min(self.people.values()) - now)
                self.timer = self.schedule(max(1, int(due * 1000)), lambda: self._tick(generation))
            elif generation == self.generation:
                self.stop()
        except Exception:
            # A missing device/file must never escape into the presence pipeline.
            if generation == self.generation:
                self.stop()
