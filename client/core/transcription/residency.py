"""How long a loaded model stays in memory.

faster-whisper caches the model it loaded (`FasterWhisperBackend._model_for`):
large-v3 is gigabytes read off disk and pushed into VRAM, and three voice notes
in a row must pay that once. What the cache did not have was an end: the model
stayed for the rest of the session, however long ago the last transcription
was, holding the RAM or VRAM other programs (a game, a browser) could use.

This module is the other half of the setting "Keep the model loaded in memory"
(`transcription.keep_model_loaded`, off by default):

* **Off** — the model is released `IDLE_RELEASE_SECONDS` after the last
  transcription *ended*, and only when none is running. Notes sent in a burst
  still pay the load once.
* **On** — it is never released by this module, and anyone transcribing all day
  does not wait for a load again after a pause.

Only a backend that holds something between runs benefits. whisper.cpp runs
one whisper-cli process per transcription and holds nothing — unless this
setting is on, when it asks the release's whisper-server.exe instead
(whisper_cpp_server), which keeps the model loaded and is what its
`release()` stops. For whisper.cpp the setting is therefore also the switch:
off, nothing of it stays in memory at all.

Nothing here is wx, and nothing here logs a message, a chat or a model id.
"""

import logging
import threading

#: Idle time after which an unwanted model is released.
IDLE_RELEASE_SECONDS = 300.0

#: The key inside the settings.json section (core.transcription.preferences).
SETTING_KEEP_MODEL_LOADED = "keep_model_loaded"


def keep_loaded_from_settings(settings) -> bool:
    """The stored choice; anything that is not exactly True reads as off, so a
    damaged value never keeps gigabytes pinned."""
    section = settings.get("transcription") if isinstance(settings, dict) else None
    if not isinstance(section, dict):
        return False
    return section.get(SETTING_KEEP_MODEL_LOADED) is True


class ModelResidency:
    """Decides when a backend's cached model is let go."""

    def __init__(self, idle_seconds=IDLE_RELEASE_SECONDS, timer_factory=threading.Timer):
        self._idle_seconds = float(idle_seconds)
        self._timer_factory = timer_factory
        self._lock = threading.Lock()
        self._keep = False
        #: id(backend) -> backend, for every backend that has been used.
        self._backends = {}
        #: id(backend) -> how many transcriptions are using it right now.
        self._busy = {}
        #: id(backend) -> the pending release timer.
        self._timers = {}

    # ── The setting ──────────────────────────────────────────────────────────

    @property
    def keep_loaded(self) -> bool:
        with self._lock:
            return self._keep

    def configure(self, keep_loaded) -> None:
        """Apply the setting. Turning it on cancels every pending release;
        turning it off starts the idle clock for every idle backend in use."""
        keep_loaded = bool(keep_loaded)
        with self._lock:
            if keep_loaded == self._keep:
                return
            self._keep = keep_loaded
            if keep_loaded:
                self._cancel_all_locked()
            else:
                for key, backend in self._backends.items():
                    if not self._busy.get(key):
                        self._arm_locked(key, backend)

    # ── A transcription using a backend ──────────────────────────────────────

    def begin_use(self, backend) -> None:
        key = id(backend)
        with self._lock:
            self._backends[key] = backend
            self._busy[key] = self._busy.get(key, 0) + 1
            self._cancel_locked(key)

    def end_use(self, backend) -> None:
        key = id(backend)
        with self._lock:
            count = self._busy.get(key, 0) - 1
            if count > 0:
                self._busy[key] = count
                return
            self._busy.pop(key, None)
            if not self._keep:
                self._arm_locked(key, backend)

    def using(self, backend):
        """`with residency.using(backend): ...` around a load and a run."""
        return _Use(self, backend)

    # ── Timers (lock held) ───────────────────────────────────────────────────

    def _arm_locked(self, key, backend) -> None:
        self._cancel_locked(key)
        holder = []
        timer = self._timer_factory(
            self._idle_seconds, lambda: self._expire(key, holder[0] if holder else None))
        holder.append(timer)
        timer.daemon = True
        self._timers[key] = timer
        timer.start()

    def _cancel_locked(self, key) -> None:
        timer = self._timers.pop(key, None)
        if timer is not None:
            timer.cancel()

    def _cancel_all_locked(self) -> None:
        for key in list(self._timers):
            self._cancel_locked(key)

    def _expire(self, key, timer=None) -> None:
        """The idle time ran out: release, unless a run or the setting says no.

        `timer` is the one firing: a timer already firing when begin_use()
        cancelled it must not act for — and take away — the next one.
        """
        with self._lock:
            if timer is not None and self._timers.get(key) is not timer:
                return
            self._timers.pop(key, None)
            if self._keep or self._busy.get(key):
                return
            backend = self._backends.get(key)
            if backend is None:
                return
            # Under the lock, so that a run beginning now cannot slip between
            # the check and the release; release() only drops a reference.
            try:
                backend.release()
            except Exception as exc:
                logging.warning("[transcription] releasing an idle model failed: %s",
                                type(exc).__name__)
                return
        logging.info("[transcription] released the idle model")


class _Use:
    def __init__(self, residency, backend):
        self._residency = residency
        self._backend = backend

    def __enter__(self):
        self._residency.begin_use(self._backend)
        return self

    def __exit__(self, *exc_info):
        self._residency.end_use(self._backend)
        return False


#: The process-wide instance: the backends are process-wide singletons.
residency = ModelResidency()
