"""Serialize account presence writes, keeping only the latest pending state."""
import logging
import threading
from collections.abc import Callable


class OnlinePresenceSender:
    def __init__(self, send: Callable[[str], None]):
        self._send = send
        self._lock = threading.Lock()
        self._pending: str | None = None
        self._running = False

    def submit(self, presence: str | None) -> None:
        """None drops queued work on disconnect/shutdown; no persistent worker."""
        with self._lock:
            self._pending = presence
            if self._running or presence is None:
                return
            self._running = True
            try:
                threading.Thread(target=self._drain, daemon=True,
                                 name="online-presence").start()
            except Exception:
                self._running = False
                logging.exception("[presence] could not start sender")

    def _drain(self) -> None:
        while True:
            with self._lock:
                presence = self._pending
                self._pending = None
                if presence is None:
                    self._running = False
                    return
            try:
                self._send(presence)
            except Exception:
                # A failed request must not strand a newer offline update.
                logging.exception("[presence] sender failed")
