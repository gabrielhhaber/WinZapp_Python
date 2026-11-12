"""WinZapp's side of the noise reducer's own process (noise_worker.py).

The process is started on the first recording that needs it, and again when
Settings > Audio's noise reducer changes to another graphics card build:
the old one ends, the new one starts and loads the model, and the user hears
both ("restarting", then "ready" or that the processor will be used). One
lock covers every use of the process: a voice message finished while it
restarts waits for it, after saying so, and is cleaned by the new one.

A failure to start (or a process that dies) is never a lost message: the
caller (neural_noise_reduction.reduce_voice_noise) falls back to the model
on the processor in WinZapp's own process. A device that failed to start is
not tried again on every recording, only after the settings choose it anew.
"""

import atexit
import logging
import os
import subprocess
import sys
import threading
import time

from core.cuda_components import noise_worker, runtime
from core.cuda_components.model import DEVICE_CPU

#: How long the new process may take to connect back.
CONNECT_SECONDS = 60
#: Loading the model on a graphics card (the CUDA libraries come in then).
LOAD_SECONDS = 180
#: Cleaning one recording: generous, a long note on a slow card.
DENOISE_SECONDS = 600
QUIT_SECONDS = 5


def worker_command(executable=None, frozen=None, client_dir=None) -> list:
    """WinZapp's own program with the worker flag (main.py hands it over)."""
    executable = executable or sys.executable
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if frozen:
        return [executable, noise_worker.WORKER_FLAG]
    client_dir = client_dir or os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    return [executable, os.path.join(client_dir, "main.py"), noise_worker.WORKER_FLAG]


class WorkerError(RuntimeError):
    pass


class WorkerProcess:
    """One running noise_worker; `popen` and `listen` are replaced in tests."""

    def __init__(self, device, popen=subprocess.Popen, listen=None, log_file=""):
        self.device = device
        self.provider = ""
        self._popen = popen
        self._listen = listen
        self._log_file = log_file
        self._proc = None
        self._conn = None

    def _listener(self, key):
        if self._listen is not None:
            return self._listen(key)
        from multiprocessing.connection import Listener
        return Listener(("127.0.0.1", 0), authkey=key)

    def start(self, model_path):
        """Start the process and load the model; WorkerError when it cannot."""
        key = os.urandom(32)
        listener = self._listener(key)
        try:
            port = listener.address[1]
            env = dict(os.environ)
            env[noise_worker.ENV_ADDRESS] = f"{port}:{key.hex()}:{self.device}"
            if self._log_file:
                env[noise_worker.ENV_LOG] = self._log_file
            self._proc = self._popen(
                worker_command(), env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self._conn = self._accept(listener)
        finally:
            listener.close()
        answer = self._ask(("load", model_path), LOAD_SECONDS)
        self.provider = answer[1] if len(answer) > 1 else ""

    def _accept(self, listener):
        got = {}

        def accept():
            try:
                got["conn"] = listener.accept()
            except Exception as exc:
                got["error"] = exc
        thread = threading.Thread(target=accept, name="noise-worker-accept", daemon=True)
        thread.start()
        deadline = time.monotonic() + CONNECT_SECONDS
        while thread.is_alive():
            thread.join(0.2)
            if self._proc is not None and self._proc.poll() is not None and thread.is_alive():
                break
            if time.monotonic() > deadline:
                break
        if "conn" in got:
            return got["conn"]
        self.stop()
        raise WorkerError("the noise reducer's process did not connect")

    def _ask(self, request, timeout):
        conn = self._conn
        if conn is None:
            raise WorkerError("not running")
        try:
            conn.send(request)
            if not conn.poll(timeout):
                raise WorkerError(f"no answer to {request[0]} in {timeout}s")
            answer = conn.recv()
        except WorkerError:
            self.stop()
            raise
        except (EOFError, OSError) as exc:
            self.stop()
            raise WorkerError(f"the noise reducer's process went away ({type(exc).__name__})")
        if not answer or answer[0] != "ok":
            raise WorkerError(answer[1] if len(answer) > 1 else "refused")
        return answer

    def denoise(self, audio, rate, channels) -> bytes:
        return self._ask(("denoise", audio, rate, channels), DENOISE_SECONDS)[1]

    def alive(self) -> bool:
        return self._conn is not None and (self._proc is None or self._proc.poll() is None)

    def stop(self):
        conn, proc = self._conn, self._proc
        self._conn = None
        if conn is not None:
            try:
                conn.send(("quit",))
                conn.poll(QUIT_SECONDS) and conn.recv()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass
        if proc is not None:
            try:
                proc.wait(QUIT_SECONDS)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._proc = None


class NoiseWorkerHost:
    """The one noise reducer process of this WinZapp, and its lock."""

    def __init__(self, make_process=None, model_path=None):
        self._lock = threading.Lock()
        self._make = make_process or self._default_process
        self._model_path = model_path
        self.device = DEVICE_CPU
        self.worker = None
        self.failed = set()
        self.restarting = False
        self.say = None            # say(i18n key) from any thread

    @staticmethod
    def _default_process(device):
        try:
            from app_paths import log_path
            log_file = log_path("noise_reducer_worker.log")
        except Exception:
            log_file = ""
        return WorkerProcess(device, log_file=log_file)

    def _path(self):
        if self._model_path:
            return self._model_path()
        from core import noise_model
        return noise_model.installed_path()

    def _say(self, key):
        if self.say is not None:
            try:
                self.say(key)
            except Exception:
                logging.debug("[noise_worker] announcement failed", exc_info=True)

    def configure(self, device, say=None):
        """At startup: the chosen device, without starting anything."""
        self.device = device
        if say is not None:
            self.say = say
        runtime.set_in_process(device)

    def _start_locked(self, device):
        worker = self._make(device)
        try:
            worker.start(self._path())
        except Exception as exc:
            logging.warning("[noise_reduction] the noise reducer's process for %s did not "
                            "start: %s", device, exc)
            self.failed.add(device)
            return None
        logging.info("[noise_reduction] noise reducer's process for %s on %s",
                     device, worker.provider or "?")
        self.worker = worker
        return worker

    def _stop_locked(self):
        worker, self.worker = self.worker, None
        if worker is not None:
            worker.stop()

    def stop(self):
        """End the process (components about to be removed, WinZapp closing)."""
        with self._lock:
            self._stop_locked()

    def apply(self, device, start=True, background=True):
        """Settings saved with *device*: from now on it is the one used. A
        process of another build ends; a GPU build's starts at once (when
        *start*: the reducer is on and its model there), announced. Returns
        the thread doing it, or None."""
        if device == self.device and (self.worker is None or self.worker.device == device):
            return None
        self.device = device
        self.failed.discard(device)
        runtime.set_in_process(device)
        from core.neural_noise_reduction import forget_denoiser
        forget_denoiser()
        if not runtime.uses_worker(device) or not start:
            if self.worker is not None:
                work = self.stop
            else:
                return None
        else:
            work = lambda: self._restart(device)        # noqa: E731
        if not background:
            work()
            return None
        thread = threading.Thread(target=work, name="noise-worker-restart", daemon=True)
        thread.start()
        return thread

    def _restart(self, device):
        self._say("noise_reducer_restarting")
        with self._lock:
            self.restarting = True
            try:
                self._stop_locked()
                if self.device != device:
                    return              # chosen again meanwhile: that one wins
                worker = self._start_locked(device)
            finally:
                self.restarting = False
        if worker is not None and "CUDA" in (worker.provider or ""):
            self._say("noise_reducer_ready")
        else:
            self._say("noise_reducer_restart_failed")

    def denoise(self, audio, rate, channels) -> bytes:
        """The recording cleaned by the process (started when needed);
        WorkerError when it cannot be, so the caller uses the processor."""
        if not self._lock.acquire(blocking=False):
            if self.restarting:
                self._say("noise_reducer_waiting_restart")
            self._lock.acquire()
        try:
            device = self.device
            if not runtime.uses_worker(device):
                raise WorkerError("the processor was chosen meanwhile")
            if self.worker is not None and (self.worker.device != device or not self.worker.alive()):
                self._stop_locked()
            if self.worker is None:
                if device in self.failed:
                    raise WorkerError(f"{device} did not start before")
                if self._start_locked(device) is None:
                    raise WorkerError(f"{device} did not start")
            return self.worker.denoise(audio, rate, channels)
        finally:
            self._lock.release()


def configure_for_window(main_window):
    """At startup: the device chosen in the settings, and its announcements
    through the main window's speech (from any thread)."""
    from core.cuda_components.model import device_of

    def say(key):
        import wx
        wx.CallAfter(lambda: main_window.output(main_window.i18n.t(key), interrupt=False))
    host.configure(device_of(main_window.settings), say=say)


host = NoiseWorkerHost()
atexit.register(lambda: host.worker is not None and host.worker.stop())
