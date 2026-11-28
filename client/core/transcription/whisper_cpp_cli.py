"""whisper-cli's command line, and how to read what it writes. Pure functions.

Everything whisper_cpp_backend needs to know about the program it runs and
nothing about running it, so the parts that decide something — which flags,
what a line of stderr means, which failure the user hears — are tested without
a process, a model or Windows. The flags are those of examples/cli/cli.cpp at
release b4938, read on 2026-10-05:

* `-l` defaults to **"en"**, not to detection. Forgetting it is not an error:
  a Portuguese note comes back as fluent, invented English. So the language
  is always passed, and "auto" whenever the request does not name one.
* `-pp` prints "<function>: progress = N%" lines to stderr — the only progress
  the program reports, and what the job's progress callback is fed with.
* `-oj -of <base>` writes `<base>.json`. The text is read from there, never
  from stdout, where the program prints the transcription with timestamps:
  stdout goes to the null device, so the words never pass through a buffer
  or a log of ours on their way to the result.
* `-ng` keeps a CUDA build off the GPU; `--vad -vm <file>` runs the Silero
  voice-activity filter before decoding.

Two readings are **not yet confirmed on a real run** and are written to cope
either way: the detected language is taken from the JSON's `result.language`
and, failing that, from the stderr line "auto-detected language: xx (p = …)";
the probability only exists in that line.
"""

from __future__ import annotations

import json
import os
import re

from core.transcription import errors
from core.transcription.backend import TranscriptionSegment

LANGUAGE_AUTO = "auto"

# Leave a core for everything else on the machine — Chromium running WhatsApp
# Web and, above all, the screen reader, whose speech stalls first when every
# core is busy decoding — and stop at eight, which bounds how much of a large
# machine one voice note may take.
_MAX_THREADS = 8

# Whisper's language codes are two letters, or three for a handful
# ("haw", "yue"). Anything else is not passed: an argument starting with "-"
# would be read as a flag.
_LANGUAGE_CODE = re.compile(r"^[a-z]{2,3}$")

_PROGRESS = re.compile(r"progress\s*=\s*(\d{1,3})\s*%")
_DETECTED = re.compile(
    r"auto-detected language:\s*([a-z]{2,3})(?:\s*\(p\s*=\s*([0-9.]+)\))?"
)

# Windows status codes a process can die with before whisper.cpp prints a
# word, as the unsigned values subprocess reports them.
_STATUS_DLL_NOT_FOUND = 0xC0000135
_STATUS_ENTRYPOINT_NOT_FOUND = 0xC0000139
_STATUS_ILLEGAL_INSTRUCTION = 0xC000001D

# The card ran out of memory. Checked first: these lines also say "CUDA",
# which the "CUDA is missing" table would otherwise claim.
_VRAM_MARKERS = (
    "cudamalloc failed",
    "cuda error: out of memory",
    "cuda_error_out_of_memory",
    "cublas_status_alloc_failed",
    "failed to allocate cuda",
    "out of memory on device",
)
_RAM_MARKERS = (
    "bad_alloc",
    "insufficient memory",
    "not enough memory",
    "cannot allocate memory",
    "failed to allocate",
)
_GENERIC_MEMORY_MARKERS = ("out of memory",)
# A card this build cannot drive: no driver, a driver older than CUDA 12.4,
# or an architecture it has no kernels for.
_CUDA_MISSING_MARKERS = (
    "no cuda-capable device",
    "cuda driver version is insufficient",
    "cuda_error_insufficient_driver",
    "cuda_error_no_device",
    "failed to initialize cuda",
    "no kernel image",
    "invalid device function",
)
# The prepared WAV could not be read. It is WinZapp's own file, so this is the
# conversion's failure (audio_prep's rule), not the recording's.
_AUDIO_MARKERS = ("failed to read audio", "failed to open audio", "as wav file",
                  "failed to read wav")
# The model file would not load: a damaged or foreign .bin.
_MODEL_MARKERS = (
    "failed to initialize whisper context",
    "failed to load model",
    "invalid model data",
    "bad magic",
    "unknown tensor",
)
# A line saying the voice-activity filter failed carries "vad" and one of
# these. Not "vad" alone: a run with the filter on logs its loading on lines
# like "whisper_vad_init…", and a failure that merely happened after them —
# memory, the model — must not be retried without the filter for nothing.
# "unknown argument" is a build that does not know --vad at all.
_VAD_FAILURE_WORDS = ("fail", "error", "unknown argument", "invalid")

_TAIL_CHARS = 800


def default_thread_count(cpu_count) -> int:
    """Threads for `-t`: all cores but one, at least one, at most eight."""
    try:
        cores = int(cpu_count or 0)
    except (TypeError, ValueError):
        cores = 0
    return max(1, min(_MAX_THREADS, cores - 1))


def language_argument(language) -> str:
    """What `-l` is given: the request's language, or "auto" to detect it."""
    code = str(language or "").strip().lower()
    return code if _LANGUAGE_CODE.match(code) else LANGUAGE_AUTO


def build_command(executable, model_path, audio_path, output_base, language,
                  threads, use_gpu, vad_model_path=None, prompt="") -> list:
    """The whole argument list. `output_base` is the JSON path minus ".json".

    `prompt` is the user's vocabulary, already reduced to plain ASCII
    (vocabulary.ascii_prompt()); it goes after the other flags so that it is
    the one argument whose text is never a flag, whatever it holds."""
    command = [
        executable,
        "-m", model_path,
        "-f", audio_path,
        "-l", language_argument(language),
        "-t", str(int(threads)),
        "-pp",
        "-oj",
        "-of", output_base,
    ]
    if not use_gpu:
        command.append("-ng")
    if vad_model_path:
        command += ["--vad", "-vm", vad_model_path]
    if prompt:
        command += ["--prompt", prompt]
    return command


def cli_paths(cwd, paths) -> list:
    """`paths` relative to `cwd`, the folder whisper-cli is run from.

    `cwd` is the program's own folder, and that is a decision, not a
    convenience. ggml can load its backend DLLs (ggml-*.dll) by scanning a
    folder, and whether this release scans the working directory too was not
    verified: a shared folder such as %LOCALAPPDATA% as the working directory
    would let any ggml-*.dll lying there be loaded into the process. The
    program's folder holds only what came out of the verified zip.

    Relative paths are the hedge for the user name. Arguments reach the
    program in the system code page unless it converts the wide command line
    itself (also unverified), so a profile folder the code page cannot spell
    would make every absolute path unopenable. From the program's folder, a
    model or %TEMP% file on the same drive is reached as "..\\..\\Temp\\x"
    and the part of the path holding the user name is never spelled out —
    the working directory itself goes over the wide API. A path on another
    drive has no relative form and stays absolute.
    """
    relative = []
    for path in paths:
        absolute = os.path.abspath(path)
        try:
            relative.append(os.path.relpath(absolute, cwd))
        except ValueError:
            relative.append(absolute)
    return relative


def scan_progress(carry, new_text):
    """(latest percentage or None, unfinished last line) for a stderr chunk.

    `carry` is what the previous call returned: a chunk can end in the middle
    of "progress = 4" with the "2%" still to come, and reading that as 4% would
    make the bar jump back.
    """
    text = (carry or "") + (new_text or "")
    complete, _sep, unfinished = text.rpartition("\n")
    matches = _PROGRESS.findall(complete)
    percent = min(100, int(matches[-1])) if matches else None
    return percent, unfinished


def parse_output_json(raw):
    """(segments, language or None) from the `-oj` file's bytes or text.

    `strict=False` because a control character the program did not escape
    inside one segment's text would otherwise make the parser refuse the whole
    transcription. Offsets are milliseconds.
    Raises ValueError for anything that is not the expected object; its text
    names a position, never the content.
    """
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    data = json.loads(text, strict=False)
    if not isinstance(data, dict):
        raise ValueError("the JSON output is not an object")
    language = None
    result = data.get("result")
    if isinstance(result, dict):
        language = _language_code(result.get("language"))
    segments = []
    for item in data.get("transcription") or ():
        if not isinstance(item, dict):
            continue
        offsets = item.get("offsets") if isinstance(item.get("offsets"), dict) else {}
        segments.append(
            TranscriptionSegment(
                start=_seconds(offsets.get("from")),
                end=_seconds(offsets.get("to")),
                text=str(item.get("text") or "").strip(),
            )
        )
    return tuple(segments), language


def parse_detected_language(stderr_text):
    """(language, probability) from the "auto-detected language" line, or Nones."""
    match = _DETECTED.search(stderr_text or "")
    if not match:
        return None, None
    try:
        probability = float(match.group(2)) if match.group(2) else None
    except ValueError:
        probability = None
    return match.group(1), probability


def vad_unsupported(stderr_text) -> bool:
    """Whether the program refused `--vad`/`-vm` as unknown arguments.

    Asked whatever the exit status: whisper-cli's argument parser may print
    "error: unknown argument" and exit with 0, which would otherwise read as a
    run that produced no output (an internal error) rather than a build that
    does not know the filter. That exit status is unconfirmed on a real run;
    this answer does not depend on it.
    """
    return any(
        "unknown argument" in line and ("vad" in line or "-vm" in line)
        for line in (stderr_text or "").lower().splitlines()
    )


def looks_like_vad_failure(stderr_text) -> bool:
    """Whether a failed run failed in the voice-activity filter."""
    return any(
        "vad" in line and _matches(line, _VAD_FAILURE_WORDS)
        for line in (stderr_text or "").lower().splitlines()
    )


def classify_failure(returncode, stderr_text, device_name):
    """A non-zero exit as the TranscriptionError the user can act on.

    Same discipline as faster_whisper_backend.classify_backend_error(): matched
    by fragment, memory before "CUDA is missing" because an allocation failure
    names CUDA too, and anything unrecognised is BACKEND_ERROR with the
    evidence in the detail — never a confident wrong diagnosis.
    """
    code = int(returncode) & 0xFFFFFFFF
    message = (stderr_text or "").lower()
    detail = f"exit {code:#x}: {log_tail(stderr_text)}"
    on_gpu = device_name == "cuda"

    if _matches(message, _VRAM_MARKERS):
        return errors.TranscriptionError(errors.INSUFFICIENT_VRAM, detail)
    if _matches(message, _RAM_MARKERS):
        return errors.TranscriptionError(errors.INSUFFICIENT_RAM, detail)
    if _matches(message, _GENERIC_MEMORY_MARKERS):
        code_ = errors.INSUFFICIENT_VRAM if on_gpu else errors.INSUFFICIENT_RAM
        return errors.TranscriptionError(code_, detail)
    if _matches(message, _CUDA_MISSING_MARKERS):
        return errors.TranscriptionError(errors.CUDA_UNAVAILABLE, detail)
    if code in (_STATUS_DLL_NOT_FOUND, _STATUS_ENTRYPOINT_NOT_FOUND):
        # Windows refused to start it. The CUDA build needs the NVIDIA
        # driver's own DLLs, which no download of ours can supply; the CPU
        # build needs only what came in its zip, so a missing one means the
        # install is damaged.
        if on_gpu:
            return errors.TranscriptionError(errors.CUDA_UNAVAILABLE, detail)
        return errors.TranscriptionError(errors.WHISPER_CPP_CORRUPTED, detail)
    if _matches(message, _AUDIO_MARKERS):
        return errors.TranscriptionError(errors.FFMPEG_FAILED, detail)
    if _matches(message, _MODEL_MARKERS):
        return errors.TranscriptionError(errors.MODEL_CORRUPTED, detail)
    if code == _STATUS_ILLEGAL_INSTRUCTION:
        # A processor without an instruction set this build was compiled for.
        # Nothing the user can change from WinZapp, so the generic sentence —
        # but the log says what it was.
        return errors.TranscriptionError(
            errors.BACKEND_ERROR, f"illegal instruction (processor too old?); {detail}"
        )
    return errors.TranscriptionError(errors.BACKEND_ERROR, detail)


def log_tail(stderr_text) -> str:
    """The end of stderr, fit for an error detail.

    Lines that start like a timed segment ("[00:00:01.000 --> …]") are dropped:
    the program prints those to stdout, which is never read, but should a
    build ever send them to stderr the words would ride into the log here.
    """
    lines = [
        line for line in (stderr_text or "").splitlines()
        if line.strip() and not line.lstrip().startswith("[")
    ]
    return "\n".join(lines)[-_TAIL_CHARS:]


def _matches(message, markers) -> bool:
    return any(marker in message for marker in markers)


def _language_code(value):
    code = str(value or "").strip().lower()
    return code if _LANGUAGE_CODE.match(code) else None


def _seconds(milliseconds) -> float:
    try:
        return max(0.0, float(milliseconds) / 1000.0)
    except (TypeError, ValueError):
        return 0.0
