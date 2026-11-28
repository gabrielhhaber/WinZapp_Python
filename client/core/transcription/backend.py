"""What a transcription backend is, and which one a transcription uses.

There are two backends — faster-whisper, and whisper.cpp since part 9b — and
the shape was drawn while there was only the first, so nothing had to be
migrated to add the second. It is deliberately the smallest thing that answers the three questions
the rest of the app has: what this backend is called, whether it can run on
*this* machine, and transcribe this file.

Two of those are load-bearing beyond the interface:

* **`is_available()` may not raise, and may not import the backend either.** It
  is what the settings UI asks before offering a backend at all, and the
  machine it is asked on is precisely the one where the package may be missing
  or half-installed. Importing to find out would also load a few hundred
  megabytes of CUDA DLLs to answer a yes/no question — on the UI thread, every
  time the dialog is drawn.

* **Choosing is data, not discovery.** `BACKEND_IDS` is a tuple in preference
  order; a configured id wins if it can run, otherwise the first one that can.
  No entry-point scanning and no registration decorators: a dynamic plugin list
  would make "why is transcription unavailable here?" unanswerable from a log.

The request carries the *decisions*, never the machinery that made them: the
device and the compute type are resolved by device.py immediately before the
run (see its docstring on why a probe is never reused) and passed in. A backend
that decided for itself would be a second answer to a question that already has
one, and the sm_120 int8 rule would then have to be true in two places.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from core.transcription import device as device_module, errors, precision

BACKEND_FASTER_WHISPER = "faster_whisper"
# whisper-cli.exe, downloaded on demand with its GGML models (part 9). Second:
# faster-whisper ships with WinZapp and stays what "automatic" means wherever
# it can run; whisper.cpp is what a user chooses — for the quantized files, or
# for a machine where faster-whisper cannot run.
BACKEND_WHISPER_CPP = "whisper_cpp"

# Every backend there is, in preference order. This order is the answer to
# "which one when the user has not chosen": the first that can actually run.
BACKEND_IDS = (BACKEND_FASTER_WHISPER, BACKEND_WHISPER_CPP)


@dataclass(frozen=True)
class TranscriptionSegment:
    """One timed chunk of speech, as the model divided it.

    The times cost nothing to keep — the backend receives them together with
    the text — and they are what a later "play the audio from here" needs.
    """

    start: float
    end: float
    text: str


@dataclass(frozen=True)
class TranscriptionRequest:
    """Everything a backend needs, with every decision already made.

    `audio_path` is the *prepared* file (PCM 16 kHz mono — see audio_prep), not
    the message's own media file: every backend wants the same thing, so
    converting once outside them keeps the conversion, and its four distinct
    failures, in a single place.
    """

    audio_path: str
    models_root: str
    model_id: str
    device: str
    compute_type: str
    # None means "let the model detect it", which is the default: WhatsApp
    # carries no language for a voice note, and a wrongly forced language
    # produces confident nonsense rather than an error.
    language: str | None = None
    # Known from the conversion, and the denominator of the progress fraction.
    duration_seconds: float | None = None
    # A voice note ends in silence far more often than a recording made to be
    # transcribed does, and Whisper answers silence with invented sentences. A
    # listener cannot tell an invented sentence from a real one, which is why
    # this defaults to on — see faster_whisper_backend for what happens when
    # the filter itself cannot load.
    vad_filter: bool = True
    # The external_models.ExternalReference records of the models the user
    # pointed WinZapp at in folders of their own. `model_id` may be a custom
    # choice ("external:<id>"), which only these can resolve to a folder, and a
    # catalogue id is loaded from one of them when WinZapp's own folder has no
    # complete copy. Empty for a user who never did.
    external_references: tuple = ()
    # The GPU's compute capability as the job's probe measured it, or None when
    # unknown. faster-whisper's precision rule already arrives decided in
    # `compute_type`; whisper.cpp needs the figure itself, because whether its
    # CUDA build can run at all depends on it (no sm_120 kernels — see
    # whisper_cpp_builds.cuda_build_supported()).
    compute_capability: tuple | None = None
    # The user's own terms as one short text (vocabulary.py), or "". Never
    # logged: it holds names.
    initial_prompt: str = ""


@dataclass(frozen=True)
class TranscriptionResult:
    """What came back, plus what produced it.

    `text` and `segments` are the only fields carrying what was said, and
    neither may ever reach `logging` — see the privacy note in job.py. Every
    other field here is exactly what the log is allowed to keep.
    """

    text: str
    language: str | None
    language_probability: float | None
    duration_seconds: float | None
    segments: tuple = ()
    backend: str = ""
    model_id: str = ""
    device: str = ""
    compute_type: str = ""
    # Whether the voice-activity filter actually ran. False means the backend
    # had to fall back (see faster_whisper_backend._run) and the tail of the
    # text may be invented — which a listener cannot detect, so the UI has to
    # say it rather than let a downgraded transcription look like a normal one.
    vad_used: bool = True

    @property
    def is_empty(self) -> bool:
        """Whether the model heard nothing worth transcribing.

        Not a failure, and deliberately not an error code: with the filter on,
        a note holding only noise legitimately produces no segments. But it
        does have to be *announced* — a blind user handed a window with nothing
        in it has no way to tell that from a crash.
        """
        return not (self.text or "").strip()


class TranscriptionBackend:
    """The interface, as a plain base class.

    Not an ABC and not a Protocol: this repository moves state through plain
    objects and functions, and an abstract registration mechanism for one
    implementation would be more machinery than the thing it abstracts.
    """

    #: Stable id, stored in settings and matched against BACKEND_IDS. Not a
    #: user-facing name — the UI layer translates it, like every other code
    #: this package produces.
    id = ""

    def is_available(self) -> bool:
        """Whether this backend could run here. Never raises."""
        return False

    def resolve_device(self, preference, probe):
        """(device, reason) for a run of this backend: device.py's decision.

        The rule stays in device.py — this only picks which of its rules
        applies and hands it what the backend alone can measure (whisper.cpp:
        whether its graphics-card build is installed). The default is the
        CTranslate2 answer, `device.resolve_device()`. Never raises.
        """
        return device_module.resolve_device(preference, probe)

    def resolve_compute_type(self, preference, device, probe):
        """The precision a run on `device` loads with: a
        precision.PrecisionChoice, for the stored `preference`.

        The default is CTranslate2's question, `precision.resolve_compute_type()`
        — the user's choice when the device can run it, its replacement when it
        cannot. A backend whose precision is the model file overrides this to
        ignore the preference. Never raises.
        """
        return precision.resolve_compute_type(preference, device, probe)

    def load_model(self, request, should_cancel=None) -> None:
        """Make `request`'s model ready, so the caller can announce the wait.

        Optional: a backend with nothing to load leaves this alone. It exists
        because loading large-v3 takes tens of seconds, and a phase has to be
        announced *before* its wait rather than after it.

        `should_cancel` is part of the signature from the start, while there is
        one implementation and nothing to migrate. faster-whisper cannot honour
        it mid-load (one blocking call into C++), but a backend that can — the
        whisper.cpp of part 9 loads in Python — should not have to change this
        interface to do so.
        """

    def transcribe(self, request, progress=None, should_cancel=None):
        """A TranscriptionResult, or a TranscriptionError."""
        raise NotImplementedError

    def trial_load(self, directory, device, compute_type, should_cancel=None) -> None:
        """Load the model in `directory` once, transcribe nothing, let it go.

        The one check a model the catalogue does not know can be put through
        (see external_models): there is no size or digest to compare it with,
        so "this backend can open it" is the whole of the evidence. Returns
        nothing on success and raises a TranscriptionError otherwise; it never
        touches whatever `load_model()` has cached.

        On the interface rather than in external_models because what a model
        folder has to hold is the backend's business — a CTranslate2 folder is
        not a whisper.cpp file — and part 9 answers it for its own format.
        """
        raise NotImplementedError

    def release(self) -> None:
        """Drop whatever is cached, freeing its memory. Optional."""


# Instances, not classes: the loaded model is cached on the instance, so handing
# out a fresh one per call would reload several gigabytes per transcription.
_instances: dict = {}
# Which makes construction a race worth closing: two transcriptions starting
# together would otherwise build two backends, load the model into memory
# twice, and leave a release() on one of them freeing half of it.
_instances_lock = threading.Lock()


def get_backend(backend_id):
    """The single instance of `backend_id`, or None for an unknown id.

    None rather than raising, for the same reason model_catalog.get_model()
    gives: the id comes from settings, where a backend that a later version
    renamed or dropped is a normal state and not a fault.
    """
    if not backend_id:
        return None
    with _instances_lock:
        if backend_id in _instances:
            return _instances[backend_id]
        instance = _construct(backend_id)
        if instance is not None:
            _instances[backend_id] = instance
        return instance


def _construct(backend_id):
    """One instance of a backend, imported at the moment it is asked for.

    The import is inside the function because the concrete backend imports this
    module for its base class and its dataclasses; at module level the two would
    be a cycle. With two entries an `if` each is smaller, and far easier to
    follow, than the registration hook that would avoid it.
    """
    if backend_id == BACKEND_FASTER_WHISPER:
        from core.transcription.faster_whisper_backend import FasterWhisperBackend

        return FasterWhisperBackend()
    if backend_id == BACKEND_WHISPER_CPP:
        from core.transcription.whisper_cpp_backend import WhisperCppBackend

        return WhisperCppBackend()
    return None


def available_backend_ids() -> tuple:
    """The ids that can actually run here, in preference order."""
    usable = []
    for backend_id in BACKEND_IDS:
        backend = get_backend(backend_id)
        if backend is not None and backend.is_available():
            usable.append(backend_id)
    return tuple(usable)


def resolve_backend(preferred=None):
    """The backend to transcribe with: the configured one, or the first usable.

    A configured backend that cannot run falls through to the next rather than
    failing, because a settings value outlives the installation it was written
    on: a user who removes the optional component would otherwise be told
    transcription is unavailable while another backend sits there ready.
    """
    chosen = get_backend(preferred)
    if chosen is not None and chosen.is_available():
        return chosen
    for backend_id in BACKEND_IDS:
        backend = get_backend(backend_id)
        if backend is not None and backend.is_available():
            return backend
    raise errors.TranscriptionError(
        errors.BACKEND_MISSING, f"no usable backend (preferred={preferred!r})"
    )
