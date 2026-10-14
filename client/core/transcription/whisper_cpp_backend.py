"""whisper.cpp as a WinZapp transcription backend: whisper-cli, run per note.

What differs from faster_whisper_backend, and why it is shaped this way:

* **A separate program, not a library.** whisper-cli.exe (whisper_cpp_runtime
  downloads it) runs once per transcription as a child process, the way
  audio_prep runs ffmpeg: no console window, stdin closed, stderr into a file
  rather than a pipe nobody drains, polled in short slices so a cancellation
  kills it rather than waiting. It also runs at below-normal priority on
  Windows: it is meant to take every core it is given, and a screen reader
  starved of CPU stops speaking — for a blind user the whole machine has then
  stopped answering.

* **Nothing stays loaded, unless the user asked.** The program loads the
  model, transcribes and exits, so the price is the load, paid inside every
  run. With "Keep the model loaded in memory" on (residency.py) the same
  release's whisper-server.exe is asked instead (whisper_cpp_server): it
  loads the model once and answers each recording. Anything that goes wrong
  with it puts the run back on whisper-cli, so the server can make a
  transcription faster and never fail one; `release()` stops it. `load_model()`
  only checks that the model file and the program are there, so a missing one
  is reported before the "transcribing" phase rather than half way into it.

* **Nothing leaves the machine.** The program is handed a local model file
  and a local WAV — paths, never a URL or a model name it could go and fetch;
  the files it reads were downloaded and verified by model_store and
  whisper_cpp_runtime.

* **The words never pass through anything that logs.** The text is read from
  the JSON file `-oj` writes into a private temporary folder (deleted however
  the run ends); stdout, where the program prints the same words, goes to the
  null device; stderr — progress, the detected language, failures — is what
  an error detail may quote, and whisper_cpp_cli.log_tail() drops anything
  shaped like a segment from it besides.

* **The voice-activity filter is a preference, as in faster-whisper.** It
  needs its own model file (whisper_cpp_catalog.VAD_MODEL). Missing, or failing
  to load, it costs the filter and never the transcription — and the result
  says so through `vad_used=False`, because without it a note ending in silence
  can come back with an invented last sentence the listener cannot detect.

The device arrives decided (device.py), and picks the build: "cuda" runs the
CUDA build, and anything else runs the CPU build with `-ng`. "cuda" is not
trusted on its own, though: the CUDA build of this release has no Blackwell
kernels, so it is used only when the request's `compute_capability` — the one
the job's probe measured, carried in the request like every other decision —
passes whisper_cpp_builds.cuda_build_supported(). Unknown, or sm_120 and
newer, is CUDA_UNAVAILABLE, as is a CUDA build that is not installed: the code
the job already answers with an offer to run on the processor. The compute
type does not apply here: the precision is the GGML file's quantization, which
the result reports instead.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
import wave

from core.transcription import (
    _fileops,
    device as device_module,
    errors,
    external_ggml,
    precision,
    whisper_cpp_builds,
    whisper_cpp_catalog,
    whisper_cpp_cli,
    whisper_cpp_runtime,
    whisper_cpp_server,
    whisper_cpp_store,
)
from core.transcription import residency as residency_module
from core.transcription.backend import (
    BACKEND_WHISPER_CPP,
    TranscriptionBackend,
    TranscriptionResult,
)
from core.transcription._fileops import (
    check_cancel as _check_cancel,
    private_temp_dir,
    unlink as _unlink,
)
from core.transcription.device import DEVICE_CUDA

# How often the running program is checked on: what a cancellation waits, at
# most, before the process is killed.
_POLL_SECONDS = 0.1

# The trial load's input: one second of silence, enough for the program to
# load the model and finish, with nothing to transcribe.
_TRIAL_SECONDS = 1
_SAMPLE_RATE = 16000


class _VadFailure(errors.TranscriptionError):
    """A run with the filter on that failed in the filter, and only there.

    Its own type so transcribe() can retry without the filter while every
    other failure, already classified, goes straight to the caller.
    """


class WhisperCppBackend(TranscriptionBackend):
    """whisper-cli.exe, one process per transcription."""

    id = BACKEND_WHISPER_CPP

    def __init__(self, runtime_root=None, cpu_count=None):
        # Both for the tests: production passes nothing and gets the global
        # runtime folder and this machine's core count.
        self._runtime_root = runtime_root
        self._cpu_count = cpu_count

    def is_available(self) -> bool:
        """Whether the processor build of the program is installed. Never raises.

        The processor build, not "any build": it is the one every answer falls
        back to — a card its graphics build cannot run on, the processor asked
        for, a trial load — so a machine with only the graphics build would
        offer a backend that fails on exactly those. The tab installs it first
        for the same reason. A manifest read and a stat per file — cheap enough
        for a settings dialog to ask while it draws.
        """
        return self._installed(whisper_cpp_builds.BUILD_CPU)

    def resolve_device(self, preference, probe):
        """device.py's whisper.cpp rule, with the one fact only this backend
        can measure: whether the graphics-card build is installed."""
        return device_module.resolve_whisper_cpp_device(
            preference, probe, self._installed(whisper_cpp_builds.BUILD_CUDA)
        )

    def resolve_compute_type(self, preference, device, probe):
        """Always "automatic": a GGML file's quantization is the file itself
        (one catalogue entry per variant), and the program has no load-time
        precision to choose. The faster-whisper choice is not said here,
        since nothing would honour it."""
        return precision.resolve_compute_type(precision.AUTO, device, probe)

    def load_model(self, request, should_cancel=None) -> None:
        """Check the model file and the program are there; load nothing."""
        _check_cancel(should_cancel)
        external_ggml.model_file(
            request.models_root, request.model_id, request.external_references
        )
        self._executable_for(request.device, request.compute_capability)

    def release(self) -> None:
        """Stop the server that keeps the model loaded, when one is running.
        whisper-cli holds nothing between runs."""
        whisper_cpp_server.stop()

    def transcribe(self, request, progress=None, should_cancel=None):
        """Transcribe the prepared WAV; progress comes from the program's `-pp`.

        An empty result is a result, not an error, exactly as with
        faster-whisper: a note holding only noise yields no segments.
        """
        _check_cancel(should_cancel)
        # WinZapp's own copy, a verified file of the user's elsewhere, or a
        # custom file they chose (external_ggml) — decided again here, at load
        # time, since a disk can be unplugged between the decision and the run.
        model_file = external_ggml.model_file(
            request.models_root, request.model_id, request.external_references
        )
        executable = self._executable_for(request.device, request.compute_capability)

        vad_model = None
        if request.vad_filter:
            vad_model = whisper_cpp_store.vad_model_path(request.models_root)
            if vad_model is None:
                logging.warning(
                    "[transcription] the whisper.cpp voice-activity model is not "
                    "installed — transcribing without the filter"
                )

        # One bar across both runs: when the filter fails and the note is run
        # again without it, the user hears the numbers carry on rather than
        # fall back to 0 — a screen reader reads every number it is given.
        reached = [0.0]

        def forward(fraction):
            if progress is not None and fraction > reached[0]:
                reached[0] = fraction
                progress(fraction)

        started = time.monotonic()
        try:
            try:
                segments, language, probability = self._run_on_server(
                    model_file, request, vad_model, forward, should_cancel
                )
            except whisper_cpp_server.ServerUnavailable as exc:
                if self._keeps_loaded():
                    # Not an error: the note is transcribed the usual way. The
                    # reason holds no text and no path (log_tail, basenames),
                    # and a media file's name is scrubbed all the same.
                    logging.info("[transcription] whisper.cpp server not used: %s",
                                 errors.scrub_media_names(str(exc)))
                segments, language, probability = self._run(
                    executable, model_file, request, vad_model, forward, should_cancel
                )
            vad_used = vad_model is not None
        except _VadFailure as exc:
            logging.warning(
                "[transcription] the whisper.cpp voice-activity filter failed (%s) — "
                "transcribing without it", exc.log_line,
            )
            segments, language, probability = self._run(
                executable, model_file, request, None, forward, should_cancel
            )
            vad_used = False

        forward(1.0)
        logging.info(
            "[transcription] whisper.cpp model=%s device=%s language=%s vad=%s in %.1fs",
            request.model_id, request.device, language, vad_used,
            time.monotonic() - started,
        )
        entry = whisper_cpp_catalog.get_model(request.model_id)
        return TranscriptionResult(
            text=" ".join(segment.text for segment in segments if segment.text),
            language=language,
            language_probability=probability,
            duration_seconds=request.duration_seconds or None,
            segments=segments,
            backend=self.id,
            model_id=request.model_id,
            device=request.device,
            compute_type=(entry.quantization if entry else "") or request.compute_type,
            vad_used=vad_used,
        )

    def trial_load(self, directory, device, compute_type, should_cancel=None) -> None:
        """Have the program open the GGML file at `directory`, on silence.

        For this backend a model is one file, so `directory` is that file's
        path. The program has no "load and exit" mode, so it transcribes one
        second of silence, with the language given (no detection) and no
        filter: success is the program exiting cleanly having loaded it.

        Always on the CPU build, whatever `device` says: the question is
        whether this file is a model the program can read, which does not
        depend on where it runs, and the CPU build is the one always offered
        — while the interface carries no compute capability to vouch for the
        CUDA build with.
        """
        _check_cancel(should_cancel)
        if not os.path.isfile(directory):
            raise errors.TranscriptionError(
                errors.MODEL_CORRUPTED, f"{directory}: not a file"
            )
        device = "cpu"
        executable = self._executable_for(device, None)
        started = time.monotonic()
        with private_temp_dir("winzapp-whisper-") as workdir:
            silence = os.path.join(workdir, "trial.wav")
            with wave.open(silence, "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(_SAMPLE_RATE)
                handle.writeframes(b"\x00\x00" * _SAMPLE_RATE * _TRIAL_SECONDS)
            self._execute(executable, directory, silence, workdir, "en", device,
                          None, None, should_cancel)
        model_name = os.path.basename(directory)
        logging.info(
            "[transcription] whisper.cpp trial load of %s on %s succeeded in %.1fs",
            model_name, device, time.monotonic() - started,
        )

    # ── Internals ────────────────────────────────────────────────────────────

    def _root_for(self, build):
        """Where `build` is launched from: the root this backend was built with
        (the tests'), else wherever it is installed — WinZapp's own folder or
        the one the user asked to reuse (whisper_cpp_runtime.usable_root())."""
        if self._runtime_root:
            return self._runtime_root
        return whisper_cpp_runtime.usable_root(build)

    def _installed(self, build) -> bool:
        # None (installed nowhere we know of) reads WinZapp's own folder, as it
        # always did, and answers "not installed" from there.
        try:
            return (
                whisper_cpp_runtime.installation_state(build, self._root_for(build)).state
                == whisper_cpp_runtime.STATE_INSTALLED
            )
        except Exception:
            return False

    def _executable_for(self, device, compute_capability):
        """The installed build for `device`, or the code that says why not."""
        build = self._build_for(device)
        if build.uses_cuda and not whisper_cpp_builds.cuda_build_supported(
            compute_capability
        ):
            # Even if the build is on disk: on sm_120 it would load the model
            # and then fail with no kernels for the card.
            raise errors.TranscriptionError(
                errors.CUDA_UNAVAILABLE,
                f"the CUDA build of whisper.cpp does not run on compute capability "
                f"{compute_capability}",
            )
        root = self._root_for(build)
        executable = whisper_cpp_runtime.executable_path(build, root)
        if executable is not None:
            # Before every launch, not only at install: see verify_executable.
            whisper_cpp_runtime.verify_executable(build, root)
            return executable
        if build.uses_cuda:
            # The code that carries the offer to run on the processor instead.
            raise errors.TranscriptionError(
                errors.CUDA_UNAVAILABLE, "the CUDA build of whisper.cpp is not installed"
            )
        raise errors.TranscriptionError(
            errors.WHISPER_CPP_NOT_INSTALLED, "the CPU build of whisper.cpp is not installed"
        )

    def _keeps_loaded(self) -> bool:
        return residency_module.residency.keep_loaded

    def _build_for(self, device):
        return (whisper_cpp_builds.BUILD_CUDA if device == DEVICE_CUDA
                else whisper_cpp_builds.BUILD_CPU)

    def _run_on_server(self, model_file, request, vad_model, progress, should_cancel):
        """(segments, language, probability) from the server that keeps the
        model loaded. ServerUnavailable when the setting is off, the program
        is not there, or anything about it goes wrong: the caller runs
        whisper-cli. Called after `_executable_for()`, which verified the
        build's files."""
        if not self._keeps_loaded():
            raise whisper_cpp_server.ServerUnavailable("not asked to keep the model loaded")
        build = self._build_for(request.device)
        executable = whisper_cpp_runtime.server_path(build, self._root_for(build))
        if executable is None:
            raise whisper_cpp_server.ServerUnavailable("the server program is not installed")
        cpu_count = self._cpu_count if self._cpu_count is not None else os.cpu_count()
        config = whisper_cpp_server.ServerConfig(
            executable, model_file, request.device == DEVICE_CUDA, vad_model,
            whisper_cpp_cli.default_thread_count(cpu_count),
        )
        raw, stderr_text = whisper_cpp_server.transcribe(
            config, request.audio_path, request.language, progress, should_cancel
        )
        try:
            segments = whisper_cpp_server.parse_response(raw)
        except ValueError as exc:
            raise whisper_cpp_server.ServerUnavailable(
                f"unreadable answer ({type(exc).__name__})") from exc
        asked = whisper_cpp_cli.language_argument(request.language)
        detected, probability = whisper_cpp_cli.parse_detected_language(stderr_text)
        if asked != whisper_cpp_cli.LANGUAGE_AUTO:
            return segments, asked, None
        return segments, detected, probability

    def _run(self, executable, model_file, request, vad_model, progress, should_cancel):
        """(segments, language, probability) for one run of the program."""
        with private_temp_dir("winzapp-whisper-") as workdir:
            stderr_text = self._execute(
                executable, model_file, request.audio_path, workdir, request.language,
                request.device, vad_model, progress, should_cancel,
            )
            try:
                with open(os.path.join(workdir, "out.json"), "rb") as handle:
                    raw = handle.read()
                # The transcript, in the clear: gone the moment it is read
                # rather than whenever the folder is.
                _unlink(os.path.join(workdir, "out.json"))
                segments, language = whisper_cpp_cli.parse_output_json(raw)
            except (OSError, ValueError) as exc:
                raise errors.TranscriptionError(
                    errors.BACKEND_ERROR,
                    f"unreadable output ({type(exc).__name__}); "
                    f"{whisper_cpp_cli.log_tail(stderr_text)}",
                ) from exc
        detected, probability = whisper_cpp_cli.parse_detected_language(stderr_text)
        return segments, language or detected, probability

    def _execute(self, executable, model_file, audio_file, workdir, language,
                 device, vad_model, progress, should_cancel):
        """Run the program to completion. Returns its stderr; raises on failure."""
        cpu_count = self._cpu_count if self._cpu_count is not None else os.cpu_count()
        files = [model_file, audio_file, os.path.join(workdir, "out")]
        if vad_model:
            files.append(vad_model)
        # Run from the program's own folder: see whisper_cpp_cli.cli_paths().
        cwd = os.path.dirname(executable)
        relative = whisper_cpp_cli.cli_paths(cwd, files)
        command = whisper_cpp_cli.build_command(
            executable, relative[0], relative[1], relative[2], language,
            whisper_cpp_cli.default_thread_count(cpu_count),
            use_gpu=device == DEVICE_CUDA,
            vad_model_path=relative[3] if vad_model else None,
        )
        returncode, stderr_text = _run_process(
            command, cwd, os.path.join(workdir, "stderr.log"), progress, should_cancel,
        )
        if vad_model and whisper_cpp_cli.vad_unsupported(stderr_text):
            # Whatever the exit status: see vad_unsupported().
            raise _VadFailure(
                errors.BACKEND_ERROR, f"--vad is not supported: "
                f"{whisper_cpp_cli.log_tail(stderr_text)}"
            )
        if returncode != 0:
            failure = whisper_cpp_cli.classify_failure(returncode, stderr_text, device)
            if vad_model and whisper_cpp_cli.looks_like_vad_failure(stderr_text):
                raise _VadFailure(failure.code, failure.detail)
            raise failure
        return stderr_text


def _run_process(command, cwd, stderr_path, progress, should_cancel):
    """(returncode, stderr) — progress fed as it is printed, killed on cancel."""
    creationflags = 0
    if sys.platform == "win32":
        creationflags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
        )
    received = []
    carry = ""
    reported = 0.0
    # Two handles on one file, so reading never moves the program's own write
    # position — a handle it inherits shares its offset with ours.
    with open(stderr_path, "wb") as writer, open(stderr_path, "rb") as reader:
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=writer,
                cwd=cwd,
                creationflags=creationflags,
            )
        except OSError as exc:
            # Found by the manifest and then not startable: quarantined by an
            # antivirus, or not an executable any more. Installing it again is
            # the fix either way.
            raise errors.TranscriptionError(
                errors.WHISPER_CPP_CORRUPTED, f"{type(exc).__name__}: {exc}"
            ) from exc
        try:
            while True:
                try:
                    returncode = process.wait(timeout=_POLL_SECONDS)
                    finished = True
                except subprocess.TimeoutExpired:
                    finished = False
                chunk = reader.read()
                if chunk:
                    received.append(chunk)
                    percent, carry = whisper_cpp_cli.scan_progress(
                        carry, chunk.decode("utf-8", errors="replace")
                    )
                    # Monotonic: with the filter on, or a model restarting a
                    # window, a number can repeat or go back, and a screen
                    # reader reads every number it is given.
                    if progress is not None and percent is not None:
                        fraction = min(1.0, percent / 100.0)
                        if fraction > reported:
                            reported = fraction
                            progress(fraction)
                if finished:
                    break
                if should_cancel is not None and should_cancel():
                    raise errors.TranscriptionError(
                        errors.CANCELLED, "cancelled while whisper.cpp was running"
                    )
        except BaseException:
            # Killed, not abandoned: an orphaned whisper-cli keeps every core
            # busy, and keeps the temporary folder's files open so they cannot
            # be deleted.
            _fileops.kill_process(process, "whisper.cpp")
            raise
    return returncode, b"".join(received).decode("utf-8", errors="replace")
