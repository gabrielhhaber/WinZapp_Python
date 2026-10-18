"""faster-whisper (CTranslate2) as a WinZapp transcription backend.

Three things here are worth more than the code around them:

* **Nothing may leave the machine.** That is the whole promise of the feature —
  the audio of a private conversation is transcribed on the user's own
  computer — and the one line enforcing it is `local_files_only=True` at the
  model load. Without it, faster-whisper treats its first argument as a Hugging
  Face repository id the moment the directory does not look like a model, and
  quietly reaches for the network. The weights are on disk because part 2 put
  them there and checked them; the backend loads them from that directory and
  from nowhere else.

* **Loading is expensive, so the loaded model is cached.** large-v3 is 3 GB
  read off disk and pushed into VRAM; a user transcribing three voice notes in
  a row must pay that once. The cache is keyed on everything that changes what
  was loaded (directory, device, compute type), and `release()` exists because
  the UI has to be able to hand the VRAM back.

* **The error text is not an API.** CTranslate2's failures arrive as strings
  from its C++ layer and change shape between versions, so they are matched
  case-insensitively, by fragment. When a message stops matching, the answer
  degrades to BACKEND_ERROR — a generic "internal error" that still logs the
  real text — and never to a different diagnosis: telling a user with plenty of
  VRAM to pick a smaller model, or a user whose driver is broken to install
  more memory, sends them off to fix something that is not broken.

* **"cuBLAS could not be loaded" is the ordinary answer on most machines, not
  a broken install.** The ctranslate2 wheel ships its own DLL and a cuDNN
  *stub loader* only, and resolves cublas64_12.dll dynamically at run time out
  of the CUDA runtime — which WinZapp does not bundle (part 4 downloads it on
  demand, the way the models are downloaded). A machine carrying nothing but
  an NVIDIA display driver therefore reports a CUDA device and then fails at
  the model load, which is exactly why that string maps to CUDA_UNAVAILABLE.
  The corollary belongs to part 4: `get_cuda_device_count() > 0` does **not**
  prove CUDA is usable here, so whatever decides that the acceleration is
  available has to look for the libraries as well.

The device and the compute type are decided by device.py and arrive in the
request; nothing here re-decides them (that is what keeps the sm_120 int8 rule
in one place).
"""

from __future__ import annotations

import importlib.util
import logging
import os
import threading
import time

from core.transcription import audio_prep, device, errors, external_models
from core.transcription._fileops import check_cancel as _check_cancel
from core.transcription.backend import (
    BACKEND_FASTER_WHISPER,
    TranscriptionBackend,
    TranscriptionResult,
    TranscriptionSegment,
)

# The packages that have to be importable for this backend to run at all.
_REQUIRED_MODULES = ("faster_whisper", "ctranslate2")

# The one file whose absence makes faster-whisper go to the network *despite*
# local_files_only: its loader answers a missing tokenizer.json by calling
# Tokenizer.from_pretrained("openai/whisper-<size>"), which downloads and does
# not consult the flag at all. The model store already checks every catalogue
# file by name and size, so this is belt and braces — but it is the belt, and
# the offline promise is too important to rest on another module's invariant.
_TOKENIZER_FILE = "tokenizer.json"

# CTranslate2 ran out of memory *on the card*. Checked before everything else
# because these strings also contain the words the "CUDA is missing" table
# matches on: CUBLAS_STATUS_ALLOC_FAILED and CUDNN_STATUS_ALLOC_FAILED are a
# full card, not a missing cuBLAS or cuDNN, and matching the shared
# "_ALLOC_FAILED" suffix covers both without a line per library.
_CUDA_MEMORY_MARKERS = (
    "cuda failed with error out of memory",
    "cuda_error_out_of_memory",
    "_alloc_failed",
    "cuda out of memory",
    "out of memory on device",
)

# The host ran out of memory. `bad_alloc` is what CTranslate2's own allocations
# raise through pybind11; MemoryError is Python's.
_HOST_MEMORY_MARKERS = (
    "bad_alloc",
    "memoryerror",
    "cannot allocate memory",
    "not enough memory",
    "insufficient memory",
)

# An allocation failure that does not say which memory it means. Which one it
# is follows from where the model was being loaded, which is the only thing
# here that knows.
_GENERIC_MEMORY_MARKERS = ("out of memory",)

# CUDA, cuDNN or cuBLAS is not there at run time. This is a different problem
# from "this machine has no GPU" (device.py already answers that one before a
# run starts): it is a machine that reported a usable card and then could not
# load the libraries to drive it, which is a broken or partial driver install.
_CUDA_MISSING_MARKERS = (
    "no cuda-capable device",
    "cuda driver version is insufficient",
    "cuda_error_no_device",
    "cuda_error_insufficient_driver",
    "cudaerrorinsufficientdriver",
    "cuda runtime",
    "cudnn",
    "cublas",
    "libcuda",
    "cuda is not available",
    # A card whose architecture this CTranslate2 build has no kernels for —
    # too new, or too old for the compiled -gencode list. It is a GPU fault
    # the CPU does not have, and without these two it landed in BACKEND_ERROR,
    # which carries no offer to re-run on the processor.
    "no kernel image",
    "invalid device function",
)

# The device refused the compute type (part 11): asked explicitly for one it
# cannot run, CTranslate2 raises "Requested <type> compute type, but the
# target device or backend do not support efficient <type> computation"
# rather than falling back. Its own code so the user is sent to the setting
# that fixes it — and, on the card, offered the processor. Not "do not support
# efficient" alone: CTranslate2's warning about a conversion it did make ("The
# compute type inferred from the saved model is float16, but the target device
# or backend do not support efficient float16 computation. The model weights
# have been automatically converted ...") says that too.
_PRECISION_MARKERS = (
    "compute type, but the target device",
)

# The voice-activity filter is the one part of the run that needs onnxruntime,
# which is a separate binary in the frozen build. If it will not load, the
# transcription is still worth having.
# "vad" alone is three letters matched against the whole exception text, which
# is wide enough to catch an unrelated message; the two spellings the filter
# itself produces are as effective and much narrower.
_VAD_FAILURE_MARKERS = ("onnx", "silero", "vad_filter", "vad filter")


class FasterWhisperBackend(TranscriptionBackend):
    """faster-whisper, loaded lazily and kept warm between transcriptions."""

    id = BACKEND_FASTER_WHISPER

    def __init__(self, model_factory=None):
        # `model_factory` is the tests' way in: a real load is several
        # gigabytes and, for the interesting half of the error table, a GPU.
        # Production passes nothing and gets faster_whisper.WhisperModel.
        self._model_factory = model_factory
        self._lock = threading.RLock()
        self._model = None
        self._key = None
        # Bumped by release(). A load runs outside the lock (see _model_for),
        # so this is what tells a load that finishes late that the model it is
        # holding is no longer wanted.
        self._generation = 0

    # ── Availability ─────────────────────────────────────────────────────────

    def is_available(self) -> bool:
        """Whether faster-whisper could be imported, without importing it.

        find_spec() answers from the import machinery's metadata, so this stays
        cheap enough for a settings dialog to call while it draws. It cannot
        tell a broken CTranslate2 DLL from a working one — that failure surfaces
        at the first load, as BACKEND_MISSING.
        """
        try:
            return all(
                importlib.util.find_spec(name) is not None
                for name in _REQUIRED_MODULES
            )
        except Exception:
            # find_spec raises rather than answering for a package whose parent
            # is missing or whose metadata is damaged. "Not available" is the
            # only answer this function may ever give.
            return False

    # ── Model cache ──────────────────────────────────────────────────────────

    def load_model(self, request, should_cancel=None) -> None:
        """Load `request`'s model if it is not already the one in hand.

        `should_cancel` is accepted and, for this backend, can only be honoured
        on either side of the load: WhisperModel(...) is one blocking call into
        C++ with no callback to hang a cancellation on. See the note in job.py
        about what that means for the phase the user is waiting through.
        """
        _check_cancel(should_cancel)
        self._model_for(request)
        _check_cancel(should_cancel)

    def release(self) -> None:
        """Drop the cached model.

        CTranslate2 frees the VRAM when the last reference to the translator
        goes, so dropping the reference is the whole mechanism — there is no
        explicit unload call to make.
        """
        with self._lock:
            had_model = self._model is not None
            self._model = None
            self._key = None
            # Inside the lock, and read by any load already in flight. Without
            # it, a job thread that started loading before this call would
            # write its model into the cache *after* this emptied it: the user
            # has been told the memory came back — so they will not ask a
            # second time — and several gigabytes of VRAM would stay held for
            # the rest of the session. That is precisely the case part 6 uses,
            # giving up on a large-v3 that takes tens of seconds to come up.
            self._generation += 1
        if had_model:
            logging.info("[transcription] released the loaded model")

    def _model_for(self, request):
        """The cached model for this request, loading it if need be.

        model_directory() runs on every call, not only on a miss: it is the
        cheap names-and-sizes check by design (ensure_ready()'s, plus the same
        for a folder the user pointed WinZapp at), and a model deleted from
        another window — or an external disk unplugged — between two
        transcriptions has to be noticed here rather than inside CTranslate2.
        """
        directory = external_models.model_directory(
            request.models_root, request.model_id, request.external_references
        )
        if not os.path.isfile(os.path.join(directory, _TOKENIZER_FILE)):
            # Refused here rather than handed to faster-whisper, which answers
            # this one missing file by downloading a tokenizer from Hugging
            # Face — ignoring local_files_only while it does. MODEL_CORRUPTED
            # because that is what it is, and because its sentence ("download
            # the model again") is the fix.
            raise errors.TranscriptionError(
                errors.MODEL_CORRUPTED,
                f"{request.model_id}: {_TOKENIZER_FILE} is missing",
            )

        key = (directory, request.device, request.compute_type)
        with self._lock:
            if self._model is not None and self._key == key:
                return self._model
            generation = self._generation

        # Deliberately outside the lock: loading large-v3 takes tens of seconds
        # and release() is called from the UI thread, which must not be made to
        # wait for it. The cost of two loads racing is a wasted load, and the
        # cost of holding the lock is a frozen window.
        _prepare_cuda(request.device)
        model_class = self._model_factory or _whisper_model_class()
        started = time.monotonic()
        try:
            model = model_class(
                directory,
                device=request.device,
                compute_type=request.compute_type,
                # The offline promise of the whole feature, in one argument:
                # without it faster-whisper falls back to treating the first
                # argument as a Hugging Face repo id and downloads. The weights
                # were fetched and verified by the model store; nothing about a
                # transcription may reach the network.
                local_files_only=True,
            )
        except Exception as exc:
            raise classify_backend_error(exc, request.device) from exc
        logging.info(
            "[transcription] loaded model=%s device=%s compute=%s in %.1fs",
            request.model_id, request.device, request.compute_type,
            time.monotonic() - started,
        )
        with self._lock:
            if generation == self._generation:
                self._model = model
                self._key = key
            else:
                # release() ran while this load was in flight. The model is
                # still returned, so the transcription that asked for it goes
                # ahead and its memory goes when the run ends — but it is never
                # cached, which is what makes "released" true.
                logging.info(
                    "[transcription] the model was released while it loaded — "
                    "using it for this run only"
                )
        return model

    def trial_load(self, directory, device, compute_type, should_cancel=None) -> None:
        """Open the model in `directory` and drop it again, uncached.

        What external_models calls before a model the catalogue does not know
        may be chosen. The two guards of `_model_for()` are repeated here and
        not skipped, because this is precisely the folder nobody vouched for:
        tokenizer.json is checked by hand, since without it faster-whisper goes
        to Hugging Face whatever `local_files_only` says, and the flag itself
        keeps a folder that does not look like a model from being read as a
        repository id and downloaded.

        Never through `_model_for()`: that caches, and a trial must not evict
        the model the user is actually transcribing with, nor stay in memory
        once it has answered. The reference is dropped before returning, which
        for CTranslate2 is the whole of freeing it (see `release()`). A model
        that is already cached is not released first — if the card has no room
        for both, INSUFFICIENT_VRAM is the true answer, and the caller decides
        whether to release and ask again.
        """
        _check_cancel(should_cancel)
        if not os.path.isfile(os.path.join(directory, _TOKENIZER_FILE)):
            raise errors.TranscriptionError(
                errors.MODEL_CORRUPTED, f"{directory}: {_TOKENIZER_FILE} is missing"
            )
        _prepare_cuda(device)
        model_class = self._model_factory or _whisper_model_class()
        started = time.monotonic()
        try:
            model = model_class(
                directory,
                device=device,
                compute_type=compute_type,
                local_files_only=True,
            )
        except Exception as exc:
            raise classify_backend_error(exc, device) from exc
        del model
        logging.info(
            "[transcription] trial load of %s on %s/%s succeeded in %.1fs",
            directory, device, compute_type, time.monotonic() - started,
        )

    # ── Transcription ────────────────────────────────────────────────────────

    def transcribe(self, request, progress=None, should_cancel=None):
        """Transcribe the prepared audio, reporting progress per segment.

        An empty result is a legitimate outcome and never an error: with the
        voice-activity filter on, a note holding only noise yields no segments
        at all. It comes back as a result whose `text` is "" (`is_empty`), and
        saying so is the UI's job — a failure code would have a screen reader
        announce an error about something the user cannot act on.
        """
        _check_cancel(should_cancel)
        model = self._model_for(request)
        _check_cancel(should_cancel)

        segments, info, vad_used = self._run(model, request)
        duration = _duration_of(request, info)

        collected = []
        reported = 0.0
        try:
            # faster-whisper returns a generator: the decoding happens while
            # this loop pulls from it, which is what makes per-segment progress
            # free and makes *between segments* the only point where the C++
            # call is not on the stack — so it is where cancelling can happen.
            for segment in segments:
                _check_cancel(should_cancel)
                collected.append(
                    TranscriptionSegment(
                        start=float(getattr(segment, "start", 0.0) or 0.0),
                        end=float(getattr(segment, "end", 0.0) or 0.0),
                        text=str(getattr(segment, "text", "") or "").strip(),
                    )
                )
                if progress is not None and duration:
                    # Clamped and monotonic: a segment can end past the
                    # duration we measured (the model pads the tail), and a
                    # progress bar that goes backwards is worse than a coarse
                    # one — a screen reader reads every number it is given.
                    reported = max(reported, min(1.0, collected[-1].end / duration))
                    progress(reported)
        except errors.TranscriptionError:
            raise
        except Exception as exc:
            raise classify_backend_error(exc, request.device) from exc

        if progress is not None:
            progress(1.0)

        return TranscriptionResult(
            text=" ".join(segment.text for segment in collected if segment.text),
            language=getattr(info, "language", None),
            language_probability=getattr(info, "language_probability", None),
            duration_seconds=duration or None,
            segments=tuple(collected),
            vad_used=vad_used,
            backend=self.id,
            model_id=request.model_id,
            device=request.device,
            compute_type=request.compute_type,
        )

    def _run(self, model, request):
        """(segments, info, vad_used), with the VAD filter as a preference.

        faster-whisper runs the voice-activity filter inside `transcribe()`
        itself, before it hands back the segment generator, so a filter that
        cannot load fails here and nowhere else. Transcribing without it is a
        worse transcription; refusing to transcribe is none at all, and the
        user cannot install onnxruntime into a frozen build to fix it.

        Which is why the third element exists rather than only a warning in the
        log. Without the filter, a note ending in silence comes back with an
        invented sentence at the end, and a listener has no way to tell one from
        a real one — so the result has to be able to say that its own tail is
        less trustworthy, and part 6 has to be able to say it out loud.
        """
        audio = audio_prep.read_waveform(request.audio_path)
        if audio is None:
            audio = request.audio_path
        try:
            segments, info = model.transcribe(
                audio,
                language=request.language,
                vad_filter=request.vad_filter,
            )
            return segments, info, bool(request.vad_filter)
        except errors.TranscriptionError:
            raise
        except Exception as exc:
            if not request.vad_filter or not _looks_like_vad_failure(exc):
                raise classify_backend_error(exc, request.device) from exc
            logging.warning(
                "[transcription] the voice-activity filter is unavailable "
                "(%s: %s) — transcribing without it",
                type(exc).__name__, errors.scrub_media_names(str(exc)),
            )
        try:
            segments, info = model.transcribe(
                audio, language=request.language, vad_filter=False
            )
            return segments, info, False
        except Exception as exc:
            raise classify_backend_error(exc, request.device) from exc


def _prepare_cuda(device_name):
    if device_name == device.DEVICE_CUDA:
        ok, _missing, detail = device.prepare_cuda_libraries()
        if ok is False:
            raise errors.TranscriptionError(errors.CUDA_UNAVAILABLE, detail)


def classify_backend_error(exc, device_name):
    """A CTranslate2/faster-whisper exception as one of our error codes.

    Matched case-insensitively and by fragment, over the exception's type name
    and text together, because these strings come out of a C++ layer and are
    reworded between versions. The order matters: an allocation failure names
    cuBLAS or CUDA too, so memory is decided before "the driver is missing".

    Anything unrecognised becomes BACKEND_ERROR with the real text in the
    detail — the log keeps the evidence, the user gets a sentence, and nobody
    is sent to fix the wrong thing.
    """
    detail = f"{type(exc).__name__}: {exc}"
    message = detail.lower()

    if _matches(message, _CUDA_MEMORY_MARKERS):
        return errors.TranscriptionError(errors.INSUFFICIENT_VRAM, detail)
    if _matches(message, _HOST_MEMORY_MARKERS):
        return errors.TranscriptionError(errors.INSUFFICIENT_RAM, detail)
    if isinstance(exc, MemoryError) or _matches(message, _GENERIC_MEMORY_MARKERS):
        # Which memory ran out is not in the message, but it is in where the
        # model was being loaded.
        code = (
            errors.INSUFFICIENT_VRAM
            if device_name == "cuda"
            else errors.INSUFFICIENT_RAM
        )
        return errors.TranscriptionError(code, detail)
    if _matches(message, _PRECISION_MARKERS):
        return errors.TranscriptionError(errors.PRECISION_UNSUPPORTED, detail)
    if _matches(message, _CUDA_MISSING_MARKERS):
        return errors.TranscriptionError(errors.CUDA_UNAVAILABLE, detail)
    return errors.TranscriptionError(errors.BACKEND_ERROR, detail)


def _matches(message, markers) -> bool:
    return any(marker in message for marker in markers)


def _looks_like_vad_failure(exc) -> bool:
    """Whether this failure is the VAD filter's rather than the model's."""
    message = f"{type(exc).__name__}: {exc}".lower()
    return isinstance(exc, ImportError) or _matches(message, _VAD_FAILURE_MARKERS)


def _duration_of(request, info) -> float:
    """Seconds of audio, preferring what the conversion measured.

    The prepared file's own length is exact and known before the model is even
    loaded; `info.duration` is what faster-whisper made of the same file, and
    with the VAD filter on it can be the *filtered* length, which would make
    the progress fraction reach 1.0 early.
    """
    for candidate in (request.duration_seconds, getattr(info, "duration", None)):
        try:
            value = float(candidate)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0.0


def _whisper_model_class():
    """faster_whisper.WhisperModel, imported at the moment of use.

    Any failure is BACKEND_MISSING, including an ImportError from a ctranslate2
    whose DLLs will not load: "installed but unusable" and "not installed" ask
    the same thing of the user — install the component again — and the
    difference between them is in the log, where it belongs.
    """
    try:
        from faster_whisper import WhisperModel
    except Exception as exc:
        raise errors.TranscriptionError(
            errors.BACKEND_MISSING, f"{type(exc).__name__}: {exc}"
        ) from exc
    return WhisperModel
