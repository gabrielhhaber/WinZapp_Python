"""Where a transcription runs, and in which precision.

Probing and deciding are two different things and are kept apart on purpose.
Probing means loading the NVIDIA driver's own DLL and asking CTranslate2 how
many CUDA devices it can see — machine-specific, allowed to fail in a dozen
ways, and impossible to reproduce in a test. Deciding is a handful of
comparisons. So `probe_hardware()` is the only function here that touches the
machine, it returns its findings as a plain frozen dataclass, and every
decision function takes that dataclass as an argument. That is what lets the
"RTX 50xx picks the wrong precision" family of bugs be pinned by a test on a
machine with no GPU at all.

Nothing in this module raises for a hardware reason. A user who asked for CUDA
on a machine that has none gets the CPU and a reason code saying so — being
told is the point, failing is not, and for a blind user an error dialog in
place of a transcription is strictly worse than a slower transcription.

**Probe immediately before deciding, and never reuse a probe.**
available_memory_mb() plans against memory that is *free*, so when the probe
ran is part of the answer. A probe taken at startup is taken before Chromium
has loaded WhatsApp Web, before the media cache has filled and possibly before
the screen reader is even up: it reports the high number this module already
says is misleading, and auto_select_model() would then pick a model against
memory that no longer exists by the time it loads. probe_hardware() costs one
DLL load and three driver calls, which is nothing next to a transcription, so
call it at the point of decision every time.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
from dataclasses import dataclass

from core.transcription import errors, model_catalog, whisper_cpp_builds

DEVICE_CPU = "cpu"
DEVICE_CUDA = "cuda"

# What the user picked in the settings. "auto" is the default and the only one
# that is allowed to change its mind between launches.
PREFERENCE_AUTO = "auto"
PREFERENCE_CUDA = "cuda"
PREFERENCE_CPU = "cpu"

COMPUTE_INT8 = "int8"
COMPUTE_FLOAT16 = "float16"
COMPUTE_FLOAT32 = "float32"
# The rest of CTranslate2's compute types, which only a user's choice reaches
# (precision.py): select_compute_type() never answers with them.
COMPUTE_INT8_FLOAT32 = "int8_float32"
COMPUTE_INT8_FLOAT16 = "int8_float16"
COMPUTE_INT8_BFLOAT16 = "int8_bfloat16"
COMPUTE_INT16 = "int16"
COMPUTE_BFLOAT16 = "bfloat16"

# Why the device below was chosen. Symbolic, never a sentence: the UI maps them
# through DEVICE_REASON_I18N_KEYS so the announcement is in the user's language.
REASON_CUDA_SELECTED = "cuda_selected"
REASON_CPU_REQUESTED = "cpu_requested"
# The user asked for CUDA and there is none — this one has to be announced.
REASON_CUDA_UNAVAILABLE = "cuda_unavailable"
# Same situation under "auto", where the user asked for nothing in particular
# and does not need to hear it as a thwarted request.
REASON_NO_CUDA_FOUND = "no_cuda_found"
# CUDA devices were counted and then could not be questioned — a driver fault
# on a machine that really does have a card, which the user can act on.
# probe_hardware() never produces this reason on its own, and re-probing cannot
# either: a fresh probe would simply count the device again and land on
# CUDA_SELECTED. It takes a caller that demotes the card deliberately after a
# run has failed on it — dataclasses.replace(probe, cuda_available=False,
# cuda_device_count=0, cuda_probe_error=<what the run reported>) — which is the
# only state where "your driver is broken" is the right thing to say.
REASON_CUDA_DRIVER_ERROR = "cuda_driver_error"
# The card is there and CTranslate2 can count it, but the CUDA libraries it
# loads by name are nowhere on this machine. Its own reason because every other
# sentence available here is false — there *is* a graphics card, the driver is
# fine, and telling the user either of those sends them looking in the wrong
# place for something part 4b can simply download.
REASON_CUDA_LIBRARIES_MISSING = "cuda_libraries_missing"
# whisper.cpp only (resolve_whisper_cpp_device()). The card is there, and the
# graphics-card build of whisper.cpp WinZapp offers cannot run on it: a
# Blackwell card (sm_120), for which the pinned release has no kernels, or a
# card NVML could not describe, where offering a build that may not run costs
# more than the processor does. Said even under "automatic": the user has a
# card and deserves to know why it is not used.
REASON_CUDA_BUILD_UNSUPPORTED = "cuda_build_unsupported"
# whisper.cpp only. The card could run the graphics-card build and it is not
# installed: one download away, which the settings tab offers.
REASON_CUDA_BUILD_MISSING = "cuda_build_missing"

DEVICE_REASON_I18N_KEYS = {
    REASON_CUDA_SELECTED: "transcription_device_cuda_selected",
    REASON_CPU_REQUESTED: "transcription_device_cpu_requested",
    REASON_CUDA_UNAVAILABLE: "transcription_device_cuda_unavailable",
    REASON_NO_CUDA_FOUND: "transcription_device_no_cuda_found",
    REASON_CUDA_DRIVER_ERROR: "transcription_device_cuda_driver_error",
    REASON_CUDA_LIBRARIES_MISSING: "transcription_device_cuda_libraries_missing",
    REASON_CUDA_BUILD_UNSUPPORTED: "transcription_device_cuda_build_unsupported",
    REASON_CUDA_BUILD_MISSING: "transcription_device_cuda_build_missing",
}

# The failures a second run on the CPU could actually cure, and the sentence
# each one is offered with. An allowlist rather than a denylist: offering to
# redo a 40-minute recording that will then fail in exactly the same way costs
# the user the whole wait twice and teaches them to dismiss the offer. Every
# other code reaches the same end on either processor — a cancelled run, a
# model that is not installed or is corrupt, media WhatsApp has not finished
# downloading, RAM that was already too small for the model.
#
# BACKEND_ERROR is deliberately absent even though a GPU-only backend fault
# would be cured by the CPU: the one such fault seen on real hardware (int8 on
# sm_120) is already prevented by select_compute_type(), so what would be left
# under it is the genuinely unknown, and an offer that usually fails again is
# worse than no offer at all. PRECISION_UNSUPPORTED is the opposite case: the
# card refused the precision the user chose, and on the processor the same
# choice is resolved again (precision.resolve_compute_type()) to one that runs.
CPU_RETRY_I18N_KEYS = {
    errors.INSUFFICIENT_VRAM: "transcription_retry_on_cpu_vram",
    errors.CUDA_UNAVAILABLE: "transcription_retry_on_cpu_cuda",
    errors.PRECISION_UNSUPPORTED: "transcription_retry_on_cpu_precision",
}

# The CUDA libraries CTranslate2 opens by name at run time, and which therefore
# have to be loadable before "cuda" can be a truthful answer. Read off the
# shipped wheel (ctranslate2 4.8.2, the CUDA 12 Windows build) rather than off
# the documentation, because the two disagree:
#
# * ctranslate2.dll's import table names no CUDA library at all — the CUDA
#   runtime is linked statically — so importing the module proves nothing, and
#   `get_cuda_device_count()` needs only the driver's own nvcuda.dll. Its
#   *strings* carry "cublas64_12.dll" next to cublasCreate_v2/cublasGemmEx:
#   cuBLAS is opened with LoadLibrary the first time a model goes on the GPU,
#   and no package in requirements.txt ships it. That is the whole bug — on
#   every machine with a driver but no CUDA Toolkit the device is counted, cuda
#   is chosen, and the model load dies with "Could not load library
#   cublas64_12.dll".
# * cuBLAS's own import table names cublasLt64_12.dll, so loading cuBLAS by
#   name covers both: the Windows loader resolves the chain, and a missing
#   cublasLt fails this check exactly as it would fail the model load.
# * cuDNN is **not** in this list, and that is a finding rather than an
#   omission. The wheel ships a 266 KB cudnn64_9.dll, which is only the cuDNN 9
#   loader shim (it forwards to cudnn_graph/ops/cnn/adv64_9.dll, none of which
#   are shipped) and which ctranslate2/__init__.py loads with a blanket
#   CDLL(*.dll) at import. But this build of ctranslate2.dll references cuDNN
#   nowhere — not an import, not a string, in neither the DLL nor the _ext
#   extension module. Listing it here would refuse the GPU to every machine
#   over a library this build never calls.
_CUDA_RUNTIME_LIBRARIES = ("cublas64_12.dll",)

# Directories the CUDA runtime libraries may also be loaded from:
# {normcased path: (path, os.add_dll_directory cookie)}. Keyed that way so
# registering the same directory twice is a no-op, and the cookie is kept
# because it is the only handle on the registration. Empty here on purpose —
# this module never goes looking for an install, it is told about one. The
# download, its layout under app_paths.global_dir() and the call that registers
# it are part 4b's.
_extra_cuda_library_dirs = {}

# Both of the below are read on the job's worker thread and written on the UI
# thread — part 4b registers its directory when a download finishes, which can
# land in the middle of a decision. Iterating a dict while another thread adds
# to it raises RuntimeError, which would degrade a *successful* download into
# an "unknown" for that run.
_cuda_library_lock = threading.Lock()

# The memoized answer of probe_cuda_libraries(), or None for "not asked yet".
# This module's rule against reusing a probe is about *free memory*, which
# moves while the app runs; whether a library is loadable does not, and
# re-measuring it maps and unmaps cuBLAS (and, through it, cuBLASLt) before
# every transcription only for CTranslate2 to map them again a moment later.
# Invalidated by register_cuda_library_directory(), which is the one event that
# can change the answer — and therefore also part 4b's place to re-ask.
_cuda_library_answer = None

# Bumped by every invalidation, so a measurement that started before one cannot
# store its now-stale result afterwards. The measurement deliberately runs
# outside the lock (it maps ~600 MB), which is exactly the window part 4b's
# download lands in.
_cuda_library_generation = 0

# Prepared immediately before entering the native engine, not by the Settings
# probe. Retain the successful mappings: AddDllDirectory alone does not make
# CTranslate2's legacy LoadLibrary see our runtime, whereas an already-loaded
# module is found by its bare name. Never unload code the engine may be using.
_prepared_cuda_libraries = {}
_cuda_preparation_lock = threading.Lock()

# float16 needs Volta or newer to be worth anything; on Pascal and older the
# half-precision path is emulated and slower than float32, when it works.
_FLOAT16_MIN_CAPABILITY = (7, 0)

# Blackwell (sm_120) and anything after it.
_INT8_UNSUPPORTED_FROM_CAPABILITY = (12, 0)

# How much room auto_select_model() insists on beyond a model's recommended
# minimum. The minimums in model_catalog already cover weights, activations and
# the CUDA context; the extra 25% is for what else is on the machine while
# WinZapp transcribes — Chromium running WhatsApp Web (which also takes VRAM
# for compositing), the screen reader, and whatever the user was doing. Getting
# this wrong is not a graceful degradation: CTranslate2 aborts the run with an
# allocation error, after the user already waited for the model to load.
_MEMORY_HEADROOM = 1.25

# Bytes per weight for each compute type, which is what moves a model's
# memory when its precision changes. The catalogue's minimums were set for one
# precision per device (model_catalog: float16 on the card, int8 on the
# processor) and are roughly the weights plus as much again for activations,
# the beam's cache and the CUDA context. Only the weights are rescaled: the
# published faster-whisper figures (large-v2 on a card, 4.5 GB in float16 and
# 2.9 GB in int8_float16; small on the processor, int8 against float32) move
# by about what the weights do and no more. So 8 bits on the card frees half
# the float16 weights — about three quarters of the figure for large-v3 — and
# float32 adds them again, half as much again as the figure; on the processor
# float32 quadruples the int8 weights.
_COMPUTE_TYPE_WEIGHT_BYTES = {
    COMPUTE_INT8: 1,
    COMPUTE_INT8_FLOAT32: 1,
    COMPUTE_INT8_FLOAT16: 1,
    COMPUTE_INT8_BFLOAT16: 1,
    COMPUTE_INT16: 2,
    COMPUTE_FLOAT16: 2,
    COMPUTE_BFLOAT16: 2,
    COMPUTE_FLOAT32: 4,
}

# Where nvml.dll lives. The bare name first, then the two absolute paths pynvml
# itself falls back to: the DLL is installed by the display driver, and on a
# machine whose search order does not reach System32 (or that still has the
# legacy NVSMI layout) the bare name fails while the card is perfectly usable.
# That miss is not free — it drops the machine into the "VRAM unknown" branch,
# which is exactly where auto_select_model() has the least to work with.
_NVML_LIBRARY_PATHS = (
    "nvml.dll",
    os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", "nvml.dll"),
    os.path.join(
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        "NVIDIA Corporation", "NVSMI", "nvml.dll",
    ),
)


@dataclass(frozen=True)
class HardwareProbe:
    """What `probe_hardware()` managed to find out. Every field may be absent.

    A field being None means "unknown", never "zero": a decision that treats an
    unknown VRAM figure as 0 would silently hide every GPU whose driver DLL
    failed to load.

    The two error fields are not interchangeable, and conflating them is what
    made a machine with no NVIDIA hardware at all report a graphics driver
    fault. `driver_error` is an **aggregate** of everything that failed during
    the probe — a missing ctranslate2, NVML, the Windows memory API — and is
    log-facing only, like TranscriptionError's detail. `cuda_probe_error` is the
    narrow one: a GPU was counted and then could not be described. Only that one
    may drive a spoken reason, because the aggregate is non-empty on every
    machine that has not installed the transcription backend yet — which is all
    of them until the user asks for it.
    """

    cuda_available: bool = False
    cuda_device_count: int = 0
    compute_capability: tuple[int, int] | None = None
    total_vram_mb: int | None = None
    free_vram_mb: int | None = None
    total_ram_mb: int | None = None
    available_ram_mb: int | None = None
    cuda_probe_error: str | None = None
    driver_error: str | None = None
    #: Whether the CUDA libraries CTranslate2 loads at run time are loadable.
    #: None means the question could not be asked (no device was counted, or
    #: this is not Windows), and unknown must not block the GPU — only a
    #: measured False does. Counting a device never proved a run was possible;
    #: this is the field that does.
    cuda_libraries_ok: bool | None = None
    #: Which of them did not load, for the log and for part 4b's offer.
    missing_cuda_libraries: tuple[str, ...] = ()
    #: What `ctranslate2.get_supported_compute_types()` answered for the
    #: processor and for the card, sorted; None when it could not be asked
    #: (ctranslate2 missing, or no card counted for the second). The precision
    #: picker offers these and nothing else (precision.py).
    cpu_compute_types: tuple[str, ...] | None = None
    cuda_compute_types: tuple[str, ...] | None = None


def resolve_device(preference, probe) -> tuple[str, str]:
    """(device, reason) for a stated preference and a probe result.

    An unknown preference is treated as "auto" rather than rejected — it is a
    settings value, and a settings file written by an older or newer WinZapp
    must not be able to make transcription unavailable.
    """
    if preference == PREFERENCE_CPU:
        return DEVICE_CPU, REASON_CPU_REQUESTED

    if cuda_usable(probe):
        return DEVICE_CUDA, REASON_CUDA_SELECTED

    if probe.cuda_libraries_ok is False and probe.cuda_device_count > 0:
        # Announced under "auto" as well, unlike the two reasons below, and
        # that asymmetry is deliberate: this user has a usable card and one
        # download away from using it, so the fallback is worth saying even
        # though they asked for nothing in particular. Only a *measured* False
        # gets here — an unchecked machine (None) falls through, because
        # inventing an obstacle we did not observe would cost the GPU to
        # everyone the check could not run on.
        return DEVICE_CPU, REASON_CUDA_LIBRARIES_MISSING

    if preference == PREFERENCE_CUDA:
        # Asked for explicitly, so the fallback is worth saying out loud, and a
        # card that is present but unreachable is worth distinguishing from "no
        # GPU here": the first is usually a driver the user can reinstall.
        # Keyed on cuda_probe_error, never on the driver_error aggregate — that
        # one carries "ctranslate2 is not installed", i.e. the state of a copy
        # whose backend is missing or will not load, and sending those people
        # to reinstall a graphics driver they may not even have is worse than
        # saying nothing.
        if probe.cuda_probe_error:
            return DEVICE_CPU, REASON_CUDA_DRIVER_ERROR
        return DEVICE_CPU, REASON_CUDA_UNAVAILABLE

    return DEVICE_CPU, REASON_NO_CUDA_FOUND


def resolve_whisper_cpp_device(preference, probe, cuda_build_installed) -> tuple[str, str]:
    """(device, reason) for whisper.cpp, which answers a different question.

    `resolve_device()` asks whether CTranslate2 can run on the card; whisper.cpp
    runs a program of its own, built per card generation, so what decides is
    whether *that* build can run here and is installed. Two consequences, and
    both are the reason this is a function of its own rather than a flag:

    * **The card's compute capability decides up front.** The CUDA build of the
      pinned release has no Blackwell (sm_120) kernels, and an unknown
      capability is not vouched for either (whisper_cpp_builds.
      cuda_build_supported()). Resolving those to "cuda" would fail with
      CUDA_UNAVAILABLE on every single run after the model load; resolving them
      to the processor here, with a reason that says why, costs nothing.
    * **CTranslate2's cuBLAS check does not apply.** `cuda_libraries_ok` is
      about the libraries CTranslate2 opens by name; the whisper.cpp CUDA build
      is a separate download carrying its own. A machine that measured False
      there is not thereby a machine whisper.cpp cannot use the card on.

    `cuda_build_installed` is measured by the caller (the backend reads the
    install's manifest) — this module still touches nothing. The rest — no
    card, the processor asked for, a card that could not be questioned — reads
    exactly as `resolve_device()` says it.
    """
    if preference == PREFERENCE_CPU:
        return DEVICE_CPU, REASON_CPU_REQUESTED
    counted = bool(probe.cuda_available) and probe.cuda_device_count > 0
    if not counted:
        return resolve_device(preference, probe)
    if not whisper_cpp_builds.cuda_build_supported(probe.compute_capability):
        return DEVICE_CPU, REASON_CUDA_BUILD_UNSUPPORTED
    if not cuda_build_installed:
        return DEVICE_CPU, REASON_CUDA_BUILD_MISSING
    return DEVICE_CUDA, REASON_CUDA_SELECTED


def cuda_usable(probe) -> bool:
    """Whether CUDA can actually be used, not merely whether a DLL loaded.

    Counting devices is not the same question as being able to run on one:
    `get_cuda_device_count()` answers with the driver alone, while the run also
    needs the CUDA libraries CTranslate2 opens by name (see
    `_CUDA_RUNTIME_LIBRARIES`). A measured False therefore vetoes the GPU; an
    unknown does not.
    """
    return (
        bool(probe.cuda_available)
        and probe.cuda_device_count > 0
        and probe.cuda_libraries_ok is not False
    )


def should_retry_on_cpu(error, device_used=DEVICE_CUDA) -> bool:
    """Whether re-running this failed transcription on the CPU could help.

    Pure, and deliberately only an opinion: nothing re-runs on its own. Waiting
    through a long recording a second time is the user's call to make, so part 6
    asks and this only says whether asking makes sense.
    """
    if device_used != DEVICE_CUDA:
        # There is nothing to fall back to. A run that was already on the
        # processor and failed for a CUDA reason is not a state that occurs,
        # but the guard is what keeps the offer from appearing after one.
        return False
    return getattr(error, "code", None) in CPU_RETRY_I18N_KEYS


def cpu_retry_i18n_key(error, device_used=DEVICE_CUDA):
    """The i18n key of the offer to re-run on the CPU, or None for no offer.

    Which key comes back *is* the reason: the two failures worth re-running
    need different sentences — "there was not enough video memory" and "the
    card could not be used" send the user to different settings if they decline.
    """
    if not should_retry_on_cpu(error, device_used):
        return None
    return CPU_RETRY_I18N_KEYS[error.code]


def gpu_supports_int8(compute_capability) -> bool:
    """Whether int8 kernels exist for this GPU at all.

    This is the reason this module exists. The CTranslate2 builds that support
    sm_120 (>= 4.6.3, CUDA 12.8) are compiled with INT8 *disabled* for it, so on
    a Blackwell card every int8 variant — "int8", "int8_float16",
    "int8_bfloat16" — fails at model load with an unsupported-compute-type
    error, and the user hears nothing but "internal error".

    An unknown capability answers False: refusing int8 costs speed, offering it
    to a card that cannot run it costs the transcription.
    """
    if compute_capability is None:
        return False
    return tuple(compute_capability) < _INT8_UNSUPPORTED_FROM_CAPABILITY


def _int8_safe(compute, compute_capability) -> str:
    """`compute`, unless it is an int8 flavour this GPU has no kernels for.

    The sm_120 rule on the execution path rather than in a comment.
    select_compute_type() asks for float16 today, so this changes nothing
    today — it is what keeps the rule true the day a card short of VRAM makes
    int8_float16 look attractive, since that failure surfaces only at model
    load, on hardware no test runner has, after the user has already waited
    through a multi-gigabyte download.
    """
    if str(compute).startswith("int8") and not gpu_supports_int8(compute_capability):
        return COMPUTE_FLOAT16
    return compute


def select_compute_type(device, probe) -> str:
    """The CTranslate2 compute type to load the model with."""
    if device != DEVICE_CUDA:
        # int8 on the CPU is the whole reason CPU transcription is usable at
        # all — roughly three times faster than float32, with no audible
        # difference on speech.
        return COMPUTE_INT8

    capability = probe.compute_capability
    if capability is not None and tuple(capability) < _FLOAT16_MIN_CAPABILITY:
        return COMPUTE_FLOAT32

    # float16 everywhere else, including sm_120. An unknown capability lands
    # here too — a card CTranslate2 can see but NVML could not describe is far
    # more likely to be new than pre-Volta.
    return _int8_safe(COMPUTE_FLOAT16, capability)


def auto_select_model(probe, device, installed_ids, catalog=None, language=None,
                      compute_type=None):
    """The model id to use when the user has not chosen one, or None.

    Two rules, in this order:

    1. If a model that is already on disk fits, use the largest of those. A
       silent 3 GB download nobody asked for is worse than transcribing with
       what is already there — and on a metered connection it is a lot worse.
    2. Otherwise the largest model that fits, which the caller will have to
       download.

    "Fits" means the device's memory covers the model's recommended minimum
    with `_MEMORY_HEADROOM` to spare. When the memory could not be measured at
    all, only rule 1 applies and only for the smallest installed model: guessing
    upwards on an unknown machine is how a run dies half way through.

    A single-language model is never a candidate unless it is `language`'s —
    the language the user transcribes in, None while it is to be detected: an
    English one installed and picked for a user whose notes are in Portuguese
    would turn every one of them into confident English (see
    model_catalog.WhisperModel.language).

    A third-party model is never one rule 2 downloads: nobody asked for a
    fine-tune whose quality and licence are its publisher's, and several of
    them weigh exactly what an official model does (ivrit's large-v3 and
    large-v3 itself), so the size alone would pick one by accident. One the
    user already has on disk is theirs, and rule 1 may still pick it. Ties are
    broken the same way everywhere: the official entry, then the id.

    A 32-bit GGML file (whisper_cpp_catalog.GgmlFile.full_precision) is never
    a candidate under either rule, installed or not: it plans more memory than
    its own f16 file for the same model, and ranked by that figure it would
    win over a better model — distil-small.en at 32 bits over small.en. Only
    the user's own choice runs one.

    `compute_type` is the precision the user chose, when they chose one: a
    model that fits in float16 may not in float32, and int8 makes room for a
    larger one (`model_fits()`). None measures every model as the catalogue
    does, which is "automatic".
    """
    models = tuple(catalog) if catalog is not None else model_catalog.list_models()
    models = tuple(
        m for m in models
        if getattr(m, "language", None) in (None, language)
        and not getattr(m, "full_precision", False)
    )
    installed = set(installed_ids or ())
    budget_mb = available_memory_mb(probe, device)

    def _rank(model):
        # What it costs *on this device* rather than the catalogue's own
        # order, so "the largest that fits" still means the most demanding one
        # when a caller passes its own list (the tests do); then an official
        # entry over a third-party one of the same weight, then the id, so the
        # answer never depends on which of two equals came first. Always the
        # catalogue's own figure, whatever the precision: models share figures
        # there and differ in weights, so a rescaled figure would reorder them
        # (int8 ranking medium over large-v3-turbo). The precision decides
        # only what fits (model_fits()).
        return (_requirement_mb(model, device), model.download_bytes,
                not getattr(model, "third_party", False), model.id)

    if budget_mb is None:
        # Only the smallest installed one, for the reason above — the official
        # one of two equals, as everywhere else.
        installed_models = [m for m in models if m.id in installed]
        if not installed_models:
            return None
        smallest = min(_rank(m)[:2] for m in installed_models)
        return max((m for m in installed_models if _rank(m)[:2] == smallest), key=_rank).id

    fitting = [m for m in models if model_fits(m, budget_mb, device, compute_type)]
    already_here = [m for m in fitting if m.id in installed]
    if already_here:
        return max(already_here, key=_rank).id
    downloadable = [m for m in fitting if not getattr(m, "third_party", False)]
    return max(downloadable, key=_rank).id if downloadable else None


def available_memory_mb(probe, device):
    """Memory the transcription may plan around, in MB, or None if unknown.

    Free memory wins over total on both devices, and for the same reason: the
    machine is not idle while WinZapp transcribes. Chromium is running WhatsApp
    Web (holding RAM, and VRAM for compositing), a screen reader is running, and
    the user has their own work open. A 16 GB machine with 11 GB in use is a
    5 GB machine for this purpose, and planning against the 16 buys a 3 GB
    download followed by an allocation error.
    """
    if device == DEVICE_CUDA:
        free, total = probe.free_vram_mb, probe.total_vram_mb
    else:
        free, total = probe.available_ram_mb, probe.total_ram_mb
    return free if free is not None else total


def model_fits(model, budget_mb, device, compute_type=None) -> bool:
    """Whether `model` fits in `budget_mb` on `device`, headroom included.

    `compute_type` is the precision the model will load with when the user
    chose one (precision.memory_compute_type()); None keeps the catalogue's
    figure, which is what "automatic" has always been measured against.
    """
    if budget_mb is None:
        return False
    return budget_mb >= _requirement_mb(model, device, compute_type) * _MEMORY_HEADROOM


def _requirement_mb(model, device, compute_type=None) -> int:
    """The model's recommended minimum for the device it will run on.

    In `compute_type` when one is given: the figure with the weights moved
    from the precision it is for to this one (see _COMPUTE_TYPE_WEIGHT_BYTES).
    A model whose weights are not a float16 model.bin — a whisper.cpp file,
    whose figures are its quantization's own — keeps its figure: its precision
    is the file.
    """
    figure = model.min_vram_mb if device == DEVICE_CUDA else model.min_ram_mb
    # A card below 7.0 loads float32 under "automatic" (select_compute_type())
    # but is still measured against the float16 figure, as it was before
    # part 11: automatic passes no compute_type, so nothing moves here.
    reference = COMPUTE_FLOAT16 if device == DEVICE_CUDA else COMPUTE_INT8
    stored_bytes = getattr(model, "model_bin_bytes", None)
    if compute_type not in _COMPUTE_TYPE_WEIGHT_BYTES or not stored_bytes:
        return figure
    # model.bin is float16: two bytes per weight.
    weights = stored_bytes / (1024 * 1024) / 2
    moved = weights * (_COMPUTE_TYPE_WEIGHT_BYTES[compute_type]
                       - _COMPUTE_TYPE_WEIGHT_BYTES[reference])
    return max(1, int(round(figure + moved)))


def device_reason_i18n_key(reason) -> str:
    """The i18n key announcing a device reason.

    Unknown reasons resolve to the plain CPU message rather than to a key of
    their own, for the same reason as errors.error_i18n_key(): I18n.t() would
    otherwise have the screen reader read the code itself.
    """
    return DEVICE_REASON_I18N_KEYS.get(
        reason, DEVICE_REASON_I18N_KEYS[REASON_CPU_REQUESTED]
    )


def probe_hardware() -> HardwareProbe:
    """Ask the machine what it has. Never raises; unknown fields stay None.

    Three independent questions, each allowed to fail on its own:

    * how many CUDA devices CTranslate2 can drive — ctranslate2 may be missing
      or fail to load in a broken copy, so the import lives here rather than
      at module level;
    * VRAM and compute capability, read from the driver's own ``nvml.dll``
      through ctypes. nvidia-ml-py is deliberately not a dependency: it would
      ship to every user for one call that only matters on the machines that
      have the DLL anyway;
    * RAM, total and available, from GlobalMemoryStatusEx.

    Only NVML's failure becomes `cuda_probe_error`, and only once a device has
    actually been counted — see HardwareProbe for why the aggregate must never
    be read as a driver fault.
    """
    cuda_count = 0
    capability = None
    total_vram = None
    free_vram = None
    cuda_probe_error = None
    libraries_ok = None
    missing_libraries = ()
    # One bag for everything that failed, joined into driver_error at the end:
    # it is log-facing only, so it is more useful complete than tidy.
    problems = []

    ctranslate2 = None
    try:
        # Imported here, never at module level: a copy whose ctranslate2 is
        # missing or will not load must still open, and this probe still has
        # to answer on it rather than raise.
        import ctranslate2

        cuda_count = int(ctranslate2.get_cuda_device_count())
    except Exception as exc:
        problems.append(f"ctranslate2: {exc}")

    # Both helpers already answer with an error string instead of raising, and
    # both are still called inside a guard: "never raises" is this function's
    # promise to the UI, not something it may inherit from a helper that loads
    # a third-party driver DLL and calls into it through ctypes.
    if cuda_count > 0:
        try:
            capability, total_vram, free_vram, cuda_probe_error = _probe_nvml()
        except Exception as exc:
            cuda_probe_error = f"nvml: {exc}"
        if cuda_probe_error:
            problems.append(cuda_probe_error)

        # Only worth asking once a device has been counted: on a machine with
        # no NVIDIA card the libraries are missing for an uninteresting reason
        # and the answer changes nothing. The failure it *does* find is never
        # folded into cuda_probe_error — that field means "the card could not
        # be described", and this is the card being fine and the libraries not
        # being here.
        try:
            libraries_ok, missing_libraries, library_error = probe_cuda_libraries()
        except Exception as exc:
            libraries_ok, missing_libraries = None, ()
            library_error = f"cuda libraries: {exc}"
        if library_error:
            problems.append(library_error)

    # The precisions each device can run, asked of the same import. The card
    # only once one was counted: the question reads its properties, and on a
    # machine with none there is nothing to describe.
    cpu_types = cuda_types = None
    asked = () if ctranslate2 is None else (
        (DEVICE_CPU, DEVICE_CUDA) if cuda_count > 0 else (DEVICE_CPU,)
    )
    for device_id in asked:
        try:
            types = tuple(sorted(ctranslate2.get_supported_compute_types(device_id)))
        except Exception as exc:
            problems.append(f"compute types ({device_id}): {exc}")
            continue
        if device_id == DEVICE_CPU:
            cpu_types = types
        else:
            cuda_types = types

    try:
        total_ram, available_ram, ram_error = _probe_ram_mb()
    except Exception as exc:
        total_ram = available_ram = None
        ram_error = f"ram: {exc}"
    if ram_error:
        problems.append(ram_error)

    probe = HardwareProbe(
        cuda_available=cuda_count > 0,
        cuda_device_count=cuda_count,
        compute_capability=capability,
        total_vram_mb=total_vram,
        free_vram_mb=free_vram,
        total_ram_mb=total_ram,
        available_ram_mb=available_ram,
        cuda_probe_error=cuda_probe_error,
        driver_error="; ".join(problems) or None,
        cuda_libraries_ok=libraries_ok,
        missing_cuda_libraries=tuple(missing_libraries),
        cpu_compute_types=cpu_types,
        cuda_compute_types=cuda_types,
    )
    logging.info("[transcription] hardware probe: %s", probe)
    return probe


def register_cuda_library_directory(path) -> bool:
    """Make `path` a place the CUDA runtime libraries may be loaded from.

    Both halves of this matter and they are not the same thing.
    `os.add_dll_directory()` makes absolute-path loads resolve dependencies in
    registered folders. It does not by itself fix CTranslate2's legacy
    LoadLibrary lookup. The probe checks these absolute paths; immediately
    before a GPU load, prepare_cuda_libraries() retains the mappings so the
    engine can find them by name too.

    Answers "was it registered", which is not yet "can the GPU be used now":
    that stays `probe_cuda_libraries()`'s answer, and registering here
    invalidates its memoized one so the next call measures again. Part 4b asks
    both, in that order, when its download finishes — and
    `cuda_runtime_present_in()` before it, to know whether to download at all.

    Idempotent, and never raises: it runs as a download finishes, and a
    directory that turned out not to be there is a reason to keep using the
    CPU, not to take the app down.
    """
    try:
        # Emptiness is checked before abspath(), which resolves "" — and, on
        # Windows, "   ", since the loader strips trailing whitespace — to the
        # working directory, putting the whole of it on the DLL search path.
        text = str(path).strip() if path else ""
        if not text:
            return False
        resolved = os.path.abspath(text)
        if not os.path.isdir(resolved):
            return False
        key = os.path.normcase(resolved)
        with _cuda_library_lock:
            already_registered = key in _extra_cuda_library_dirs
        if already_registered:
            # Registered already, but still invalidate: a caller registering
            # the same directory twice is part 4b telling us its *contents*
            # changed. The directory under global_dir() is registered at
            # startup while it is still empty, the probe memoizes False, and
            # the download then lands in it — without this, that False stands
            # for the rest of the session and a user who paid for ~600 MB stays
            # on the CPU until they restart the app. One extra measurement per
            # download costs nothing next to the download.
            with _cuda_library_lock:
                _invalidate_cuda_library_answer()
            return True
        cookie = (
            os.add_dll_directory(resolved)
            if hasattr(os, "add_dll_directory")
            else None
        )
        with _cuda_library_lock:
            _extra_cuda_library_dirs[key] = (resolved, cookie)
            _invalidate_cuda_library_answer()
        logging.info("[transcription] CUDA library directory registered")
        return True
    except Exception as exc:
        # The report rather than exc_info=True: the same chain and frames,
        # through the one funnel the whole package logs an exception by
        # (errors.exception_report()).
        logging.warning(
            "[transcription] could not register a CUDA library directory: %s",
            errors.exception_report(exc),
        )
        return False


def unregister_cuda_library_directory(path) -> bool:
    """Take `path` off the places the CUDA libraries are loaded from.

    The inverse of register_cuda_library_directory(), for a folder the user
    stopped reusing. Answers whether it had been registered. Idempotent, and
    never raises. A library already mapped into the process stays mapped (the
    loader offers no way back), which is why the memoized answer is dropped: the
    next probe measures against the directories that are left.
    """
    try:
        text = str(path).strip() if path else ""
        if not text:
            return False
        key = os.path.normcase(os.path.abspath(text))
        with _cuda_library_lock:
            entry = _extra_cuda_library_dirs.pop(key, None)
            if entry is not None:
                _invalidate_cuda_library_answer()
        if entry is None:
            return False
        cookie = entry[1]
        if cookie is not None:
            try:
                cookie.close()
            except Exception:
                pass
        logging.info("[transcription] CUDA library directory unregistered")
        return True
    except Exception as exc:
        logging.warning("[transcription] could not unregister a CUDA library directory: %s",
                        errors.exception_report(exc))
        return False


def forget_cuda_library_answer() -> None:
    """Forget the memoized probe answer — for a caller that REMOVES libraries.

    `register_cuda_library_directory()` invalidates on its own, because adding
    a directory is the event that can turn a measured False into a True.
    Taking the libraries away is the same event pointing the other way, and it
    has no such call: the files simply stop being there and nothing in this
    process notices. A memoized True then outlives them — `cuda_usable()` keeps
    choosing the GPU, and every transcription until the app is restarted loads
    the model onto the card and dies on "Could not load library
    cublas64_12.dll". That is the hole part 4a closed on the disk side, on the
    side the disk cannot see.

    Also for the caller that is about to ask the question for real after
    something *outside* this module changed the answer: registering is the only
    invalidation there is, so a caller with nothing to register has no other
    way to force a fresh measurement.

    Idempotent, and never a decision of its own: the next
    `probe_cuda_libraries()` measures again and answers.
    """
    with _cuda_library_lock:
        _invalidate_cuda_library_answer()


def cuda_library_directories() -> tuple:
    """Every directory registered above, in registration order."""
    with _cuda_library_lock:
        return tuple(path for path, _cookie in _extra_cuda_library_dirs.values())


def cuda_runtime_present_in(path) -> bool:
    """Whether `path` holds every file in `_CUDA_RUNTIME_LIBRARIES`.

    Part 4b's "is it already here?", asked before spending a download rather
    than after: registering a directory answers only that the directory exists,
    and an empty one registers exactly as happily as a complete one. Presence
    is not loadability — a file that is there and broken still fails
    `probe_cuda_libraries()` — so this decides whether to fetch, never whether
    to use the GPU.
    """
    try:
        return bool(path) and all(
            os.path.isfile(os.path.join(path, name))
            for name in _CUDA_RUNTIME_LIBRARIES
        )
    except Exception:
        return False


def probe_cuda_libraries(directories=None):
    """(ok, missing, error) — will the libraries CTranslate2 needs load?

    `ok` is True or False when the question could be answered and **None when
    it could not be asked at all**, which is not the same thing and is why this
    is not a bare bool: `cuda_usable()` vetoes the GPU on a measured False and
    never on an unknown. Off Windows the library names are not these ones, and
    refusing CUDA there would be inventing an obstacle we did not observe.

    Without side effects — it loads a library and releases it again, and never
    asks CUDA for a context, which would cost hundreds of milliseconds and a
    slice of the very VRAM the decision is about. Memoized per process all the
    same: mapping cuBLAS and cuBLASLt before every transcription, only for
    CTranslate2 to map them again immediately afterwards, is ~600 MB of address
    space and two DllMain runs for an answer that cannot have changed in
    between. An `ok` of None is never memoized, so a transient fault does not
    become a permanent verdict.

    `directories` asks about a specific set instead — an explicit question,
    never answered from the cache and never written to it.
    """
    global _cuda_library_answer

    if not sys.platform.startswith("win"):
        # No error text: not asking is not failing, and this answer feeds
        # driver_error, which is the bag of what failed.
        return None, (), None

    if directories is not None:
        return _measure_cuda_libraries(tuple(directories))

    with _cuda_library_lock:
        cached = _cuda_library_answer
        generation = _cuda_library_generation

    if cached is not None:
        return cached

    # Measured outside the lock — it maps two large libraries and must not hold
    # a registration off meanwhile — so the generation captured above is what
    # decides whether this answer may still be stored. Without it an
    # invalidation landing mid-measurement is simply overwritten by the stale
    # answer that arrives a moment later, which is the same permanent False as
    # the shortcut above. Same shape as FasterWhisperBackend's `_generation`,
    # and for the same reason: the expensive half runs unlocked, so the decision
    # to keep its result is taken against a value captured before it started.
    answer = _measure_cuda_libraries(cuda_library_directories())
    if answer[0] is not None:
        with _cuda_library_lock:
            if generation == _cuda_library_generation:
                _cuda_library_answer = answer
    return answer


def _invalidate_cuda_library_answer() -> None:
    """Forget the memoized answer. Called holding `_cuda_library_lock`."""
    global _cuda_library_answer, _cuda_library_generation
    _cuda_library_answer = None
    _cuda_library_generation += 1


def _measure_cuda_libraries(directories):
    """(ok, missing, error) for `directories`, measured rather than recalled."""
    try:
        missing = []
        failures = []
        for name in _CUDA_RUNTIME_LIBRARIES:
            failure = _load_cuda_library(name, directories)
            if failure:
                missing.append(name)
                failures.append(failure)
        return not missing, tuple(missing), "; ".join(failures) or None
    except Exception as exc:
        # ctypes itself misbehaving, which is not an answer about the machine.
        return None, (), f"cuda libraries: {exc}"


def prepare_cuda_libraries():
    """Load and retain CUDA DLLs before entering CTranslate2 on Windows.

    The transient probe can succeed by absolute path while the engine's later
    bare-name load fails. After that failure CTranslate2 4.8.2 can hang on the
    next encode, even with a new model. Prepare first, verifying the legacy
    lookup while our mapping is held, and reject CUDA before native work if
    that fails. Failed preparations are retried after a runtime installation.
    """
    if not sys.platform.startswith("win"):
        return None, (), None
    with _cuda_preparation_lock:
        directories = cuda_library_directories()
        missing, failures = [], []
        for name in _CUDA_RUNTIME_LIBRARIES:
            if name in _prepared_cuda_libraries:
                continue
            failure = _load_cuda_library(name, directories, keep_loaded=True)
            if failure:
                missing.append(name)
                failures.append(failure)
        return not missing, tuple(missing), "; ".join(failures) or None


def _load_cuda_library(name, directories, *, keep_loaded=False):
    """None if `name` can be loaded, or every reason it could not.

    **`winmode=0` on the bare name is the whole point of this function.**
    ctypes does not perform a plain LoadLibrary: CPython forces
    LOAD_LIBRARY_SEARCH_DEFAULT_DIRS on every ctypes load
    (`ctypes/__init__.py::CDLL._load_library`), while CTranslate2 opens cuBLAS
    from inside ctranslate2.dll with a raw LoadLibrary that obeys the
    *process's* search order. The two agree under python.exe and disagree in a
    frozen build, whose PyInstaller bootloader calls the legacy SetDllDirectory
    and never SetDefaultDllDirectories. Measured on a real onedir build, for a
    DLL reachable only through PATH — which is exactly where the CUDA Toolkit
    installer puts it, and PyTorch and other ML tooling with it — the default
    flags made this check answer "missing" while the run itself would have
    loaded it. The user would then be told, falsely, that the CUDA libraries
    are not installed, and watch large-v3 run about an order of magnitude
    slower on the CPU with nothing to retry, because the veto happens before
    the run. `winmode=0` asks the question the way the run asks it.

    The registered directories keep the default flags on purpose: an absolute
    path there is a folder we put the libraries in ourselves, and those flags
    are what let a dependency like cuBLASLt resolve out of that same folder.

    With keep_loaded, retain the successful mapping and verify the bare-name
    lookup before returning. This is the GPU backend's preparation step;
    ordinary probes still release their temporary references so checking
    Settings does not prevent repairing/removing the runtime.

    Every failure is kept for the same reason `_load_nvml()` keeps its own: the
    last one alone says nothing about why the first was not enough.
    """
    failures = []
    candidates = (
        (name, 0),
        *((os.path.join(folder, name), None) for folder in directories),
    )
    for candidate, winmode in candidates:
        try:
            library = ctypes.WinDLL(candidate, winmode=winmode)
        except Exception as exc:
            failures.append(f"{candidate}: {exc}")
            continue
        if keep_loaded:
            try:
                legacy = ctypes.WinDLL(name, winmode=0)
            except Exception as exc:
                _release_library(library)
                failures.append(f"{name}: {exc}")
                continue
            _release_library(legacy)
            _prepared_cuda_libraries[name] = library
        else:
            _release_library(library)
        return None
    return "; ".join(failures)


def _release_library(library) -> None:
    """Undo the load, so the check leaves the process as it found it.

    ctypes never unloads a library, so without this a probe per transcription
    piles up references to a library the decision may well not use. Best effort
    on purpose: failing to release is not a reason to answer "unknown".
    """
    try:
        ctypes.windll.kernel32.FreeLibrary(ctypes.c_void_p(library._handle))
    except Exception:
        pass


def _load_nvml():
    """(library, error) — the first nvml.dll that loads, or None and why not.

    Every failure is kept, not just the last one: reporting only the last leaves
    the log blaming the legacy NVSMI path on every machine without an NVIDIA
    card, which is the least informative of the three and says nothing about
    why the normal locations failed.

    CDLL rather than WinDLL, matching pynvml, whose fallback paths these are:
    NVML is a cdecl library. On x64 the two calling conventions are the same, so
    this is about agreeing with the source of the convention, not about a bug.
    """
    failures = []
    for path in _NVML_LIBRARY_PATHS:
        try:
            return ctypes.CDLL(path), None
        except Exception as exc:
            failures.append(f"{path}: {exc}")
    return None, "; ".join(failures)


def _probe_nvml():
    """(capability, total_vram_mb, free_vram_mb, error) from nvml.dll.

    Device 0 only. On a single-GPU machine — which is what this is for — there
    is nothing to choose. On a multi-GPU one the two device 0s need not be the
    same card: NVML orders by PCI bus id while CUDA orders by FASTEST_FIRST
    unless CUDA_DEVICE_ORDER=PCI_BUS_ID is set. Guessing which pairing applies
    would be worse than reading the first card, since the figures only feed a
    size decision that already carries 25% of headroom.
    """
    if not sys.platform.startswith("win"):
        return None, None, None, "nvml: not Windows"

    class _NvmlMemory(ctypes.Structure):
        _fields_ = [
            ("total", ctypes.c_ulonglong),
            ("free", ctypes.c_ulonglong),
            ("used", ctypes.c_ulonglong),
        ]

    nvml, load_error = _load_nvml()
    if nvml is None:
        return None, None, None, f"nvml: {load_error}"

    initialised = False
    try:
        if nvml.nvmlInit_v2() != 0:
            return None, None, None, "nvml: nvmlInit_v2 failed"
        initialised = True

        handle = ctypes.c_void_p()
        if nvml.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(handle)) != 0:
            return None, None, None, "nvml: no handle for device 0"

        capability = None
        major = ctypes.c_int()
        minor = ctypes.c_int()
        if nvml.nvmlDeviceGetCudaComputeCapability(
            handle, ctypes.byref(major), ctypes.byref(minor)
        ) == 0:
            capability = (major.value, minor.value)

        total_mb = free_mb = None
        memory = _NvmlMemory()
        if nvml.nvmlDeviceGetMemoryInfo(handle, ctypes.byref(memory)) == 0:
            total_mb = int(memory.total // (1024 * 1024))
            free_mb = int(memory.free // (1024 * 1024))

        return capability, total_mb, free_mb, None
    except Exception as exc:
        # A driver too old for one of these entry points, or one that faults
        # inside them: either way the answer is "unknown", never a crash.
        return None, None, None, f"nvml: {exc}"
    finally:
        # Only after a successful init: nvmlShutdown() against a library that
        # never initialised is not a documented no-op, and this runs on the
        # path where the driver is already misbehaving.
        if initialised:
            try:
                nvml.nvmlShutdown()
            except Exception:
                pass


def _probe_ram_mb():
    """(total_ram_mb, available_ram_mb, error) from GlobalMemoryStatusEx.

    Available as well as total, because available is what the CPU path plans
    against — see available_memory_mb().
    """

    class _MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    try:
        if sys.platform.startswith("win"):
            status = _MemoryStatusEx()
            status.dwLength = ctypes.sizeof(_MemoryStatusEx)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return None, None, "ram: GlobalMemoryStatusEx failed"
            return (
                int(status.ullTotalPhys // (1024 * 1024)),
                int(status.ullAvailPhys // (1024 * 1024)),
                None,
            )

        page_size = os.sysconf("SC_PAGE_SIZE")
        total = page_size * os.sysconf("SC_PHYS_PAGES")
        available = page_size * os.sysconf("SC_AVPHYS_PAGES")
        return int(total // (1024 * 1024)), int(available // (1024 * 1024)), None
    except Exception as exc:
        return None, None, f"ram: {exc}"
