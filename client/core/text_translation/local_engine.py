"""Translating with an offline model, in this process.

Decisions worth more than the code:

* **Nothing is imported at module level** that a copy of WinZapp may lack
  (ctranslate2, tokenizers, sentencepiece, py3langid). ``missing_components()``
  answers which are absent, and a missing one is the error
  ``translation_engine_missing``, never an ImportError in the conversation.
* **One model stays loaded, and only for a while.** Loading NLLB-200 600M
  takes a second or two, so a chat being translated message by message keeps
  it; ``IDLE_UNLOAD_SECONDS`` after the last use it is dropped, because each
  account is a process of its own and a 3 GB model held by each of them for
  nothing is memory the user's other programs need. Changing model or device
  unloads the previous one first.
* **The processor by default, and half its cores.** A chat message is short;
  int8 on the processor translates it in well under a second with the 600M
  model, and leaving half the cores free is what keeps the screen reader
  answering while it runs. The graphics card is the user's choice, prepared
  the way the transcription prepares it (``prepare_cuda_libraries()``: a
  failed CUDA load can hang CTranslate2 on the next call), and it falls back
  to the processor when the card cannot load the model.
* **Long text is cut into lines and sentences** before it reaches the model:
  these models were trained on sentences, and fed a long paragraph they drop
  or repeat parts of it. The line breaks of the message are kept.
* **The source language** is the user's choice, or detected (py3langid) for
  the two families that need it (NLLB-200, M2M-100). Text already in the
  target language comes back unchanged, which the conversation reports as
  "already in the target language".
"""

from __future__ import annotations

import importlib.util
import logging
import os
import re
import threading

from core.ai_media.errors import DescriptionError
from core.text_translation import languages, local_catalog, local_store

DEVICE_CPU = "cpu"
DEVICE_CUDA = "cuda"
DEVICES = (DEVICE_CPU, DEVICE_CUDA)

IDLE_UNLOAD_SECONDS = 600
#: A line longer than this is cut at its sentence ends.
MAX_SEGMENT_CHARS = 400
BATCH_SIZE = 8
BEAM_SIZE = 4

_SENTENCE_END = re.compile(r"(?<=[.!?。！？؟।])\s+")

_MBART_TOKENS = frozenset(languages.MBART50_TOKENS.values())

_lock = threading.RLock()
_loaded = None  # (key, translator, tokenizer)
_idle_timer = None


def _components(model, source_auto):
    names = ["ctranslate2"]
    names.append("sentencepiece" if _spm(model.tokenizer_file) else "tokenizers")
    if model.needs_source and source_auto:
        names.append("py3langid")
    return names


def missing_components(model, source_auto=True, find_spec=importlib.util.find_spec):
    """The Python packages this model needs that this copy lacks."""
    missing = []
    for name in _components(model, source_auto):
        try:
            if find_spec(name) is None:
                missing.append(name)
        except (ImportError, ValueError):
            missing.append(name)
    return tuple(missing)


def supports(model, code):
    """Whether ``model`` can translate into or from language ``code``."""
    if model is None:
        return False
    if model.family == local_catalog.FAMILY_NLLB:
        return languages.nllb_code(code) is not None
    if model.family == local_catalog.FAMILY_MADLAD:
        return languages.madlad_code(code) is not None
    if model.family == local_catalog.FAMILY_MBART50:
        return languages.mbart50_code(code) is not None
    return languages.m2m100_code(code) is not None


def segments(text):
    """``text`` as [(piece, separator)]: lines, and sentences of long lines.

    ``"".join(piece + separator)`` is the text again, so the translation is
    put back together with the same breaks. Blank pieces are not translated.
    """
    parts = []
    lines = text.split("\n")
    for index, line in enumerate(lines):
        end = "\n" if index < len(lines) - 1 else ""
        if len(line) <= MAX_SEGMENT_CHARS:
            parts.append((line, end))
            continue
        sentences = _SENTENCE_END.split(line)
        for position, sentence in enumerate(sentences):
            parts.append((sentence, end if position == len(sentences) - 1 else " "))
    return parts


def language_tokens(model, source, target):
    """(prefix, suffix, target_prefix) of every segment for ``model``.

    ``source`` is None for the families that do not take one.
    """
    family = model.family
    if family == local_catalog.FAMILY_NLLB:
        return [languages.nllb_code(source)], ["</s>"], [languages.nllb_code(target)]
    if family == local_catalog.FAMILY_MBART50:
        return [languages.mbart50_code(source)], ["</s>"], [languages.mbart50_code(target)]
    if family == local_catalog.FAMILY_M2M100:
        return ([f"__{languages.m2m100_code(source)}__"], ["</s>"],
                [f"__{languages.m2m100_code(target)}__"])
    if family == local_catalog.FAMILY_SMALL100:
        return [f"__{languages.m2m100_code(target)}__"], ["</s>"], None
    # "▁" first: what the tokenizer itself makes of "<2pt> text" (SentencePiece
    # prefixes the dummy space before the language token).
    return ["▁", f"<2{languages.madlad_code(target)}>"], ["</s>"], None


def strip_target_prefix(tokens, target_prefix):
    """The hypothesis without the language token the decoder was started with."""
    if target_prefix and tokens[:len(target_prefix)] == target_prefix:
        return tokens[len(target_prefix):]
    return tokens


def detect(text, classify=None):
    """The language code of ``text`` (py3langid), or None."""
    try:
        if classify is None:
            import py3langid

            classify = py3langid.classify
        code, _score = classify(text)
    except (ImportError, OSError):
        # No detector is not "no answer": reading the text as English would
        # translate it as if it were.
        raise DescriptionError("translation_engine_missing") from None
    except Exception as exc:
        logging.info("[translation] language detection failed: %s", type(exc).__name__)
        return None
    return languages.detected_code(code)


def resolve_source(model, text, source, target, classify=None):
    """(source code for the model or None, already in the target?).

    A detected language the model does not know is read as English, the
    language every model here translates best from.
    """
    if source:
        return (source if model.needs_source else None), source == target
    if not model.needs_source:
        return None, False
    found = detect(text, classify)
    if found is not None and (found == target or (
            _base(found) == _base(target) and _base(found) != "zh")):
        return found, True
    if found is None or not supports(model, found):
        found = "en"
    return found, False


def _base(code):
    return str(code).split("-")[0]


class _HFTokenizer:
    def __init__(self, path):
        import tokenizers

        self._tok = tokenizers.Tokenizer.from_file(path)

    def encode(self, text):
        return self._tok.encode(text, add_special_tokens=False).tokens

    def decode(self, tokens):
        ids = [self._tok.token_to_id(token) for token in tokens]
        return self._tok.decode([i for i in ids if i is not None], skip_special_tokens=True)


class _SPTokenizer:
    def __init__(self, path):
        import sentencepiece

        self._sp = sentencepiece.SentencePieceProcessor(model_file=path)

    def encode(self, text):
        return self._sp.encode(text, out_type=str)

    def decode(self, tokens):
        return self._sp.decode_pieces([token for token in tokens if not _special(token)])


def _spm(name):
    """Whether a tokenizer file is a SentencePiece model (not tokenizer.json)."""
    return name.endswith(".model")


def _special(token):
    return token in ("</s>", "<s>", "<pad>", "<unk>") or (
        token.startswith("__") and token.endswith("__")) or token in _MBART_TOKENS


def _threads():
    return max(1, (os.cpu_count() or 2) // 2)


def _open(model, directory, device):
    """(translator, tokenizer, device used) for ``model``; the card falls back
    to the processor when it cannot load it."""
    import ctranslate2

    path = os.path.join(directory, model.tokenizer_file)
    tokenizer = _SPTokenizer(path) if _spm(path) else _HFTokenizer(path)
    if device == DEVICE_CUDA and _cuda_ready(ctranslate2):
        for compute in ("int8_float16", "float16"):
            try:
                return ctranslate2.Translator(directory, device="cuda",
                                              compute_type=compute), tokenizer, DEVICE_CUDA
            except (RuntimeError, ValueError) as exc:
                logging.info("[translation] %s on the graphics card (%s) failed: %s",
                             model.id, compute, type(exc).__name__)
    translator = ctranslate2.Translator(directory, device="cpu", compute_type="int8",
                                        inter_threads=1, intra_threads=_threads())
    return translator, tokenizer, DEVICE_CPU


def _cuda_ready(ctranslate2):
    try:
        if ctranslate2.get_cuda_device_count() <= 0:
            return False
        from core.transcription import device as transcription_device

        ready, _missing, _detail = transcription_device.prepare_cuda_libraries()
        return ready is not False
    except Exception as exc:
        logging.info("[translation] CUDA is not usable: %s", type(exc).__name__)
        return False


def _load(model, device, root):
    global _loaded
    directory = local_store.model_path(model, root)
    key = (directory, device)
    if _loaded is not None and _loaded[0] == key:
        return _loaded[1], _loaded[2]
    unload()
    try:
        translator, tokenizer, used = _open(model, directory, device)
    except ImportError:
        raise DescriptionError("translation_engine_missing") from None
    except Exception as exc:
        logging.warning("[translation] could not load %s: %s", model.id, type(exc).__name__)
        # Its own category, apart from a text that failed: loading fails again
        # for every message (a model too large for the memory), so automatic
        # translation pauses on it instead of trying at each arrival.
        raise DescriptionError("translation_local_load") from None
    logging.info("[translation] loaded %s on %s", model.id, used)
    _loaded = (key, translator, tokenizer)
    return translator, tokenizer


def unload(should_cancel=None):
    """Drop the loaded model, if any. While a translation holds the model this
    waits for it, unless *should_cancel* says to stop waiting: then False."""
    global _loaded, _idle_timer
    while not _lock.acquire(timeout=0.2):
        if should_cancel is not None and should_cancel():
            return False
    try:
        if _idle_timer is not None:
            _idle_timer.cancel()
            _idle_timer = None
        _loaded = None
    finally:
        _lock.release()
    return True


def _schedule_unload():
    global _idle_timer
    if _idle_timer is not None:
        _idle_timer.cancel()
    timer = threading.Timer(IDLE_UNLOAD_SECONDS, lambda: _unload_if_idle(timer))
    timer.daemon = True
    _idle_timer = timer
    timer.start()


def _unload_if_idle(timer):
    # The callback waits for the lock while a translation runs; by then it is
    # no longer the current timer, and the model it would drop was just used.
    with _lock:
        if _idle_timer is timer:
            unload()


def translate(model_id, text, target, source=None, device=DEVICE_CPU, should_cancel=None,
              root=None, classify=None):
    """``text`` in language ``target`` (a code of ``languages``).

    ``source`` None means detect it. Raises DescriptionError with a safe key.
    """
    model = local_catalog.get_model(model_id)
    if model is None or not local_store.is_installed(model, root):
        raise DescriptionError("translation_model_missing")
    if not supports(model, target) or (source and not supports(model, source)):
        raise DescriptionError("translation_language")
    if missing_components(model, source_auto=not source):
        raise DescriptionError("translation_engine_missing")
    source_code, same = resolve_source(model, text, source, target, classify)
    if same:
        return text
    prefix, suffix, target_prefix = language_tokens(model, source_code, target)
    parts = segments(text)
    with _lock:
        translator, tokenizer = _load(model, device, root)
        try:
            pending = [index for index, (piece, _sep) in enumerate(parts) if piece.strip()]
            done = {}
            for start in range(0, len(pending), BATCH_SIZE):
                if should_cancel is not None and should_cancel():
                    raise DescriptionError("cancelled")
                chunk = pending[start:start + BATCH_SIZE]
                sources = [prefix + tokenizer.encode(parts[i][0].strip()) + suffix for i in chunk]
                longest = max(len(tokens) for tokens in sources)
                results = translator.translate_batch(
                    sources, [target_prefix] * len(sources) if target_prefix else None,
                    beam_size=BEAM_SIZE, max_batch_size=BATCH_SIZE,
                    max_decoding_length=min(1024, longest * 3 + 16))
                for index, result in zip(chunk, results):
                    tokens = strip_target_prefix(result.hypotheses[0], target_prefix)
                    done[index] = tokenizer.decode(tokens).strip()
        except DescriptionError:
            raise
        except Exception as exc:
            logging.warning("[translation] %s failed: %s", model.id, type(exc).__name__)
            raise DescriptionError("translation_local_failed") from None
        finally:
            _schedule_unload()
    return "".join(_rejoined(parts, done))


def _rejoined(parts, done):
    for index, (piece, separator) in enumerate(parts):
        if index in done:
            lead = piece[:len(piece) - len(piece.lstrip())]
            yield lead + done[index] + separator
        else:
            yield piece + separator
