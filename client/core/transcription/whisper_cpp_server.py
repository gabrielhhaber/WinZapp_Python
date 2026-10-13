"""whisper-server.exe kept running between transcriptions: the model stays loaded.

Why this exists. whisper-cli (whisper_cpp_backend) loads the model, transcribes
and exits, so every voice note pays the load again. Reported from users'
logs: with "Keep the model loaded in memory" on and a graphics card, each
transcription still reloaded the model. That setting only ever reached
faster-whisper. The release zip WinZapp already downloads and verifies ships a
second program, whisper-server.exe, which loads the model once and then
answers one HTTP request per recording. With the setting on, the backend asks
it instead of launching whisper-cli.

The rules it keeps, which are whisper_cpp_backend's own:

* **Any doubt goes back to whisper-cli.** Everything here that is not a
  cancellation raises `ServerUnavailable`, and the backend then runs the note
  the way it always did, where failures are classified and reported. The
  server can only ever make a transcription faster, never fail one.

* **Nothing leaves the machine.** The server listens on 127.0.0.1, on a port
  the system picked, and is handed local files only. The audio reaches it as
  one request over loopback and the text comes back in the answer, in memory.

* **The words never pass through anything that logs.** stdout goes to the null
  device. stderr goes to a file in a private temporary folder (progress, the
  detected language, the voice filter's figures), read for those and removed
  with the folder.

* **It never outlives WinZapp.** The process is put in a Windows job object
  whose last handle WinZapp holds: when WinZapp ends, however it ends, Windows
  kills the server and its memory (the card's too) is given back. A server
  that cannot be put in the job is killed at once and not used.

* **One at a time.** A second transcription while one is running does not
  wait for the server: it is told the server is busy and runs on whisper-cli,
  as two concurrent transcriptions always could.

Run from the program's own folder with relative paths, at below-normal
priority and with no console window, for the reasons whisper_cpp_cli.cli_paths()
and whisper_cpp_backend give.

Pure pieces (`build_command`, `encode_request`, `parse_response`) are tested
directly; the process handling is exercised through fakes.
"""

from __future__ import annotations

import atexit
import http.client
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass

from core.transcription import _fileops, errors, whisper_cpp_cli
from core.transcription.backend import TranscriptionSegment

SERVER_NAME = "whisper-server.exe"

_HOST = "127.0.0.1"
#: A large model onto a card can take a while; past this it is not coming.
_START_TIMEOUT_SECONDS = 180.0
_POLL_SECONDS = 0.1
#: The recording is sent in one request, held in memory: past this size it is
#: whisper-cli's, which reads the file itself.
_MAX_UPLOAD_BYTES = 256 * 1024 * 1024
#: The server's stderr file only grows; restart it rather than fill the disk.
_MAX_STDERR_BYTES = 8 * 1024 * 1024
#: whisper-cli's own decoding defaults, which the server does not share.
_BEAM_SIZE = 5
_BEST_OF = 5
#: The temporary folder's prefix: one temp_sweep.PREFIXES already sweeps.
_TEMP_PREFIX = "winzapp-whisper-"


class ServerUnavailable(Exception):
    """The server is not there to ask: run whisper-cli instead."""


@dataclass(frozen=True)
class ServerConfig:
    """What a running server was started with; a different one is a restart."""

    executable: str
    model_file: str
    use_gpu: bool
    vad_model: str | None
    threads: int


# ── Pure ─────────────────────────────────────────────────────────────────────

def build_command(executable, model_path, port, threads, use_gpu, vad_model_path=None) -> list:
    """The whole argument list. Paths as whisper_cpp_cli.cli_paths() gives them."""
    command = [
        executable,
        "-m", model_path,
        "--host", _HOST,
        "--port", str(int(port)),
        "-t", str(int(threads)),
        "-bs", str(_BEAM_SIZE),
        "-bo", str(_BEST_OF),
        # Always given, as whisper-cli's -l is: the default is English.
        "-l", whisper_cpp_cli.LANGUAGE_AUTO,
        "-pp",
        # The answer's per-language table is never read.
        "-nlp",
    ]
    if not use_gpu:
        command.append("-ng")
    if vad_model_path:
        command += ["--vad", "-vm", vad_model_path]
    return command


def encode_request(audio_bytes, language, boundary):
    """(body, content type) of the one multipart request a transcription is."""
    def field(name, value):
        return (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                f"{value}\r\n").encode("ascii")

    body = (
        field("response_format", "verbose_json")
        + field("language", whisper_cpp_cli.language_argument(language))
        + (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"audio.wav\"\r\n"
           f"Content-Type: audio/wav\r\n\r\n").encode("ascii")
        + audio_bytes
        + f"\r\n--{boundary}--\r\n".encode("ascii")
    )
    return body, f"multipart/form-data; boundary={boundary}"


def parse_response(raw):
    """Segments from the server's `verbose_json` answer. Offsets are seconds.

    Raises ValueError for anything that is not the expected object; its text
    never holds the content. `strict=False` for the reason
    whisper_cpp_cli.parse_output_json() gives.
    """
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    data = json.loads(text, strict=False)
    if not isinstance(data, dict) or not isinstance(data.get("segments"), list):
        raise ValueError("the server's answer is not a transcription")
    segments = []
    for item in data["segments"]:
        if not isinstance(item, dict):
            continue
        segments.append(TranscriptionSegment(
            start=_number(item.get("start")),
            end=_number(item.get("end")),
            text=str(item.get("text") or "").strip(),
        ))
    return tuple(segments)


def _number(value) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


# ── The process ──────────────────────────────────────────────────────────────

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((_HOST, 0))
        return probe.getsockname()[1]


def _tie_to_this_process(process):
    """Put `process` in a job object that dies with WinZapp; its handle, which
    must be kept. Raises OSError when Windows refuses any step."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BasicLimits),
                    ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    kill_on_job_close = 0x2000
    extended_limit_information = 9
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW")
    limits = _ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = kill_on_job_close
    if (not kernel32.SetInformationJobObject(job, extended_limit_information,
                                             ctypes.byref(limits), ctypes.sizeof(limits))
            or not kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(int(process._handle)))):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(job)
        raise OSError(error, "job object")
    return job


def _close_job(job) -> None:
    try:
        import ctypes
        ctypes.WinDLL("kernel32").CloseHandle(ctypes.c_void_p(job))
    except Exception:
        pass


class _Server:
    """One running whisper-server.exe."""

    def __init__(self, config, popen=subprocess.Popen, tie=_tie_to_this_process):
        self.config = config
        self._process = None
        self._job = None
        self._workdir = tempfile.mkdtemp(prefix=_TEMP_PREFIX)
        self._stderr_path = os.path.join(self._workdir, "stderr.log")
        self._writer = None
        self._reader = None
        self._port = _free_port()
        cwd = os.path.dirname(config.executable)
        files = [config.model_file] + ([config.vad_model] if config.vad_model else [])
        relative = whisper_cpp_cli.cli_paths(cwd, files)
        command = build_command(
            config.executable, relative[0], self._port, config.threads, config.use_gpu,
            vad_model_path=relative[1] if config.vad_model else None,
        )
        creationflags = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                         | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0))
        try:
            # Two handles on one file, as in whisper_cpp_backend._run_process.
            self._writer = open(self._stderr_path, "wb")
            self._reader = open(self._stderr_path, "rb")
            self._process = popen(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=self._writer, cwd=cwd, creationflags=creationflags,
            )
            self._job = tie(self._process)
        except Exception as exc:
            self.stop()
            raise ServerUnavailable(f"could not start ({type(exc).__name__})") from exc

    def alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def wait_until_ready(self, should_cancel=None, timeout=_START_TIMEOUT_SECONDS) -> None:
        """Until the model is loaded and the server answers; it only starts
        listening once the model is in."""
        deadline = time.monotonic() + timeout
        while True:
            if not self.alive():
                tail = whisper_cpp_cli.log_tail(self.read_stderr())
                self.stop()
                raise ServerUnavailable(f"exited while loading: {tail}")
            if should_cancel is not None and should_cancel():
                self.stop()
                raise errors.TranscriptionError(
                    errors.CANCELLED, "cancelled while whisper.cpp was loading the model")
            try:
                connection = http.client.HTTPConnection(_HOST, self._port, timeout=1.0)
                try:
                    connection.request("GET", "/health")
                    if connection.getresponse().status == 200:
                        return
                finally:
                    connection.close()
            except OSError:
                pass
            if time.monotonic() >= deadline:
                self.stop()
                raise ServerUnavailable("did not answer in time")
            time.sleep(_POLL_SECONDS)

    def read_stderr(self) -> str:
        """What the server wrote since the last read."""
        try:
            return self._reader.read().decode("utf-8", errors="replace") if self._reader else ""
        except (OSError, ValueError):
            return ""

    def transcribe(self, audio_path, language, progress=None, should_cancel=None):
        """(the answer's bytes, the stderr text of this request)."""
        try:
            if os.path.getsize(audio_path) > _MAX_UPLOAD_BYTES:
                raise ServerUnavailable("the recording is too long for one request")
            with open(audio_path, "rb") as handle:
                audio = handle.read()
        except OSError as exc:
            raise ServerUnavailable(f"could not read the audio ({type(exc).__name__})") from exc
        body, content_type = encode_request(audio, language, uuid.uuid4().hex)
        del audio
        self.read_stderr()  # what came before this request is not about it
        outcome = {}

        def post():
            try:
                connection = http.client.HTTPConnection(_HOST, self._port)
                try:
                    connection.request("POST", "/inference", body, {"Content-Type": content_type})
                    response = connection.getresponse()
                    outcome["status"] = response.status
                    outcome["data"] = response.read()
                finally:
                    connection.close()
            except Exception as exc:  # reported below, on the caller's thread
                outcome["error"] = type(exc).__name__

        worker = threading.Thread(target=post, name="whisper-server-request", daemon=True)
        worker.start()
        received, carry, reported = [], "", 0.0
        while True:
            worker.join(_POLL_SECONDS)
            chunk = self.read_stderr()
            if chunk:
                received.append(chunk)
                percent, carry = whisper_cpp_cli.scan_progress(carry, chunk)
                if progress is not None and percent is not None:
                    fraction = min(1.0, percent / 100.0)
                    if fraction > reported:
                        reported = fraction
                        progress(fraction)
            if not worker.is_alive():
                break
            if should_cancel is not None and should_cancel():
                # The one way to stop a transcription the server has begun.
                self.stop()
                raise errors.TranscriptionError(
                    errors.CANCELLED, "cancelled while whisper.cpp was running")
            if not self.alive():
                break
        stderr_text = "".join(received)
        if outcome.get("status") != 200 or "data" not in outcome:
            reason = outcome.get("error") or f"status {outcome.get('status')}"
            tail = whisper_cpp_cli.log_tail(stderr_text)
            self.stop()
            raise ServerUnavailable(f"{reason}; {tail}")
        try:
            if os.path.getsize(self._stderr_path) > _MAX_STDERR_BYTES:
                self.stop()
        except OSError:
            pass
        return outcome["data"], stderr_text

    def stop(self) -> None:
        """Kill the server and remove its folder. Never raises."""
        process, self._process = self._process, None
        if process is not None:
            _fileops.kill_process(process, "whisper.cpp server")
        job, self._job = self._job, None
        if job:
            _close_job(job)
        for handle in (self._writer, self._reader):
            try:
                if handle is not None:
                    handle.close()
            except OSError:
                pass
        self._writer = self._reader = None
        workdir, self._workdir = self._workdir, None
        if workdir:
            shutil.rmtree(workdir, ignore_errors=True)


# ── The one server ───────────────────────────────────────────────────────────

_lock = threading.Lock()
_current = None


def transcribe(config, audio_path, language, progress=None, should_cancel=None, server_factory=_Server):
    """Ask the running server, starting or restarting it as `config` needs.

    Returns (answer bytes, stderr text of the request). Raises ServerUnavailable
    for everything but a cancellation.
    """
    global _current
    if sys.platform != "win32":
        raise ServerUnavailable("only on Windows")
    if not _lock.acquire(blocking=False):
        raise ServerUnavailable("busy with another transcription")
    try:
        server = _current
        if server is not None and (server.config != config or not server.alive()):
            server.stop()
            server = _current = None
        if server is None:
            started = time.monotonic()
            server = server_factory(config)
            _current = server
            try:
                server.wait_until_ready(should_cancel)
            except BaseException:
                _current = None
                raise
            logging.info("[transcription] whisper.cpp server ready in %.1fs (gpu=%s vad=%s)",
                         time.monotonic() - started, config.use_gpu, bool(config.vad_model))
        try:
            return server.transcribe(audio_path, language, progress, should_cancel)
        finally:
            if not server.alive():
                server.stop()
                if _current is server:
                    _current = None
    finally:
        _lock.release()


def stop(wait_seconds=5.0) -> bool:
    """Stop the server if one is running; True when one was. Waits for a
    transcription in flight for at most `wait_seconds` before giving up."""
    global _current
    if not _lock.acquire(timeout=wait_seconds):
        return False
    try:
        server, _current = _current, None
        if server is None:
            return False
        server.stop()
        logging.info("[transcription] whisper.cpp server stopped")
        return True
    finally:
        _lock.release()


def running() -> bool:
    server = _current
    return server is not None and server.alive()


atexit.register(lambda: stop(wait_seconds=1.0))
