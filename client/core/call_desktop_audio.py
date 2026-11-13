"""Capture selected desktop audio into the existing call sender's PCM clock."""
import logging
import os
import queue
import sys
import threading

import numpy as np

from .desktop_audio_sources import active_render_pids, pin_process, shareable_processes
from .system_audio_capture import TimelineMixer
from .wasapi_capture import WasapiBackend, WasapiError
from .wasapi_process_capture import _ProcessAPI, discover_nvda, open_process_stream

RATE = 48_000
FRAME_TICKS = 200_000
CAPTURE_DELAY = 600_000
MAX_SOURCES = 16
PROCESS_ACCESS_DENIED = 0x80070005


def desktop_audio_supported():
    return sys.platform == "win32" and sys.getwindowsversion().build >= 20348


def mix_desktop_pcm(microphone, desktop, volume=100):
    if not desktop:
        return microphone
    near = np.frombuffer(microphone, dtype="<i2").astype(np.float32)
    shared = np.frombuffer(desktop, dtype="<i2")[:len(near)].astype(np.float32)
    shared *= volume / 100
    # Keep the microphone intact and reserve its headroom before adding music.
    section = near[:len(shared)]
    headroom = np.where(shared >= 0, 32767 - section, 32768 + section)
    peak = np.max(np.abs(shared) - headroom, initial=0)
    if peak > 0:
        ratios = np.ones_like(shared)
        np.divide(headroom, np.abs(shared), out=ratios, where=shared != 0)
        shared *= np.min(ratios, initial=1)
    section += shared
    return np.clip(np.rint(near), -32768, 32767).astype("<i2").tobytes()


class CallDesktopAudio:
    """A bounded, disposable worker. The call sender remains the only emitter."""
    def __init__(self, system, nvda, on_state, *, app=None, volume=200):
        if not (system or nvda or app) or (system and app):
            raise ValueError("Select at least one desktop audio source")
        self.system, self.nvda, self.on_state = system, nvda, on_state
        self.app, self.volume = app, volume
        self._stop = threading.Event()
        self._frames = queue.Queue(maxsize=4)
        self._remainder = b""

    def start(self):
        threading.Thread(target=self._run, name="call-desktop-audio", daemon=True).start()

    def stop(self):
        self._stop.set()

    def take(self, size):
        while not self._stop.is_set() and len(self._remainder) < size:
            try:
                self._remainder += self._frames.get_nowait()
            except queue.Empty:
                break
        result, self._remainder = self._remainder[:size], self._remainder[size:]
        return b"" if self._stop.is_set() else result

    def _offer(self, pcm):
        try:
            self._frames.put_nowait(pcm)
        except queue.Full:
            try:
                self._frames.get_nowait()
            except queue.Empty:
                pass
            self._frames.put_nowait(pcm)

    def _open(self, target):
        try:
            stream = open_process_stream(target, 0, RATE, 1)
            try:
                stream.start(cancel_event=self._stop)
                return stream
            except BaseException:
                stream.close()
                raise
        finally:
            target.release()

    def _run(self):
        backend = None
        streams = {}
        failed = False
        try:
            backend, api = WasapiBackend(), _ProcessAPI()
            if self.nvda:
                target = discover_nvda(api=api)
                if target is None:
                    raise WasapiError("NVDA is not running")
                streams[None] = (0, self._open(target))
            if self.app:
                streams[self.app.pid] = (1, self._open_selected_app(api))
            start = backend.clock()
            mixer = TimelineMixer(RATE, 1, start, source_count=MAX_SOURCES + 1)
            next_discovery = start
            announced = False
            while not self._stop.is_set():
                now = backend.clock()
                if self.system and now >= next_discovery:
                    self._refresh_system(backend, api, streams, mixer)
                    next_discovery = backend.clock() + 2_500_000
                if not announced and not self._stop.is_set():
                    self.on_state(self, True)
                    announced = True
                for slot, stream in streams.values():
                    for packet in stream.read_packets():
                        mixer.add(slot, packet)
                end = backend.clock() - CAPTURE_DELAY
                if end - start > 2_500_000:
                    start = end
                    mixer = TimelineMixer(RATE, 1, start, source_count=MAX_SOURCES + 1)
                while not self._stop.is_set() and start + FRAME_TICKS <= end:
                    start += FRAME_TICKS
                    self._offer(mixer.render_until(start))
                self._stop.wait(.008)
        except Exception:
            if not self._stop.is_set():
                logging.exception("[call_audio] desktop capture stopped")
                failed = True
        finally:
            self.stop()
            if failed:
                self.on_state(self, False)
            for _, stream in streams.values():
                try:
                    stream.close()
                except (OSError, RuntimeError):
                    logging.exception("[call_audio] desktop capture cleanup failed")
            if backend is not None:
                try:
                    backend.close()
                except (OSError, RuntimeError):
                    logging.exception("[call_audio] desktop capture backend cleanup failed")

    def _open_selected_app(self, api):
        app = self.app
        target = pin_process(api, app.pid)
        try:
            rows = api.process_tree()
            if (target.creation_time != app.creation_time or api.session(app.pid) != api.current_session()
                    or app.pid not in shareable_processes(rows, {app.pid}, os.getpid())):
                raise WasapiError("Selected audio application changed")
            target.acquire()
            return self._open(target)
        finally:
            target.release()

    def _refresh_system(self, backend, api, streams, mixer):
        render_pids = active_render_pids(backend)
        pids = shareable_processes(api.process_tree(), render_pids, os.getpid())
        if len(pids) > MAX_SOURCES:
            raise WasapiError("Too many active desktop audio sources")
        for pid in (streams.keys() - {None}) - pids:
            slot, stream = streams[pid]
            stream.close()
            del streams[pid]
            mixer.reset_source(slot)
        free_slots = sorted(set(range(1, MAX_SOURCES + 1)) - {slot for slot, _ in streams.values()})
        for pid, slot in zip(sorted(pids - streams.keys()), free_slots):
            if self._stop.is_set():
                return
            try:
                target = pin_process(api, pid)
            except WasapiError as error:
                if error.hresult == PROCESS_ACCESS_DENIED:
                    # System services can own a render session, but Windows
                    # denies an ordinary user the handle needed to pin them.
                    continue
                if any(row[0] == pid for row in api.process_tree()):
                    raise
                continue
            try:
                # Recheck after pinning to reject a reused PID.
                if pid in shareable_processes(api.process_tree(), render_pids, os.getpid()):
                    target.acquire()  # _open takes ownership of this reference.
                    streams[pid] = (slot, self._open(target))
            finally:
                target.release()
