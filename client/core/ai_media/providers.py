"""Stateless official REST adapters. No uploads, remote threads or auto retries.

Every provider is described by two pure functions: ``build_request`` (what to
send) and ``parse_answer`` (what came back). Nothing here touches the network
or the filesystem, so each provider's payload shape is tested directly.
"""
import base64
from typing import NamedTuple, Optional

from .config import MAX_OUTPUT_CHARS, MAX_OUTPUT_TOKENS, PROVIDERS, supports, valid_model
from .errors import DescriptionError


class Request(NamedTuple):
    url: str
    headers: dict
    json: Optional[dict] = None
    data: Optional[dict] = None    # form fields of a multipart request
    files: Optional[dict] = None   # file part of a multipart request


#: Groq documents about 4 MB of base64 for an image in one request (not
#: exercised live here); a larger photo skips Groq instead of failing late.
GROQ_IMAGE_BASE64_LIMIT = 4_000_000
_OPENAI = "https://api.openai.com/v1"
_GEMINI = "https://generativelanguage.googleapis.com/v1beta"
_CLAUDE = "https://api.anthropic.com/v1/messages"
_GROQ = "https://api.groq.com/openai/v1"
_OPENROUTER = "https://openrouter.ai/api/v1"
#: OpenAI-compatible chat endpoints.
_CHAT_BASE = {"groq": _GROQ, "openrouter": _OPENROUTER}


def _b64(media):
    return base64.b64encode(media.data).decode("ascii")


def _data_url(media):
    return f"data:{media.mime};base64,{_b64(media)}"


def is_transcription(provider, media):
    """Audio goes to a dedicated transcription endpoint where the provider has
    one (OpenAI, Groq); Gemini takes audio as ordinary chat input."""
    return media.kind == "audio" and bool(PROVIDERS[provider].transcribe_model)


def build_request(provider, model, key, media, history, question, instructions, profile):
    if provider not in PROVIDERS or not supports(provider, media.kind):
        raise DescriptionError("request")
    if not valid_model(model):
        raise DescriptionError("request")
    if is_transcription(provider, media):
        return _transcription(provider, key, media)
    tokens = MAX_OUTPUT_TOKENS[media.kind]
    if provider == "openai":
        messages = [{"role": role, "content": text} for role, text in history]
        if media.kind == "pdf":
            part = {"type": "input_file", "filename": media.filename, "file_data": _data_url(media)}
        else:
            part = {"type": "input_image", "image_url": _data_url(media),
                    "detail": "low" if profile == "fast" else "high"}
        messages.append({"role": "user", "content": [part, {"type": "input_text", "text": question}]})
        return Request(f"{_OPENAI}/responses", {"Authorization": f"Bearer {key}"},
                       {"model": model, "store": False, "instructions": instructions,
                        "input": messages, "max_output_tokens": tokens})
    if provider == "gemini":
        contents = [{"role": "model" if role == "assistant" else "user", "parts": [{"text": text}]}
                    for role, text in history]
        contents.append({"role": "user", "parts": [
            {"inline_data": {"mime_type": media.mime, "data": _b64(media)}}, {"text": question}]})
        return Request(f"{_GEMINI}/models/{model}:generateContent", {"x-goog-api-key": key},
                       {"systemInstruction": {"parts": [{"text": instructions}]},
                        "contents": contents, "generationConfig": {"maxOutputTokens": tokens}})
    if provider == "claude":
        messages = [{"role": role, "content": text} for role, text in history]
        block_type = "document" if media.kind == "pdf" else "image"
        block = {"type": block_type,
                 "source": {"type": "base64", "media_type": media.mime, "data": _b64(media)}}
        messages.append({"role": "user", "content": [block, {"type": "text", "text": question}]})
        return Request(_CLAUDE, {"x-api-key": key, "anthropic-version": "2023-06-01"},
                       {"model": model, "max_tokens": tokens, "system": instructions,
                        "messages": messages})
    if provider == "groq" and media.kind in ("image", "sticker") and len(_b64(media)) > GROQ_IMAGE_BASE64_LIMIT:
        raise DescriptionError("request")  # this provider only: the chain moves on to the next
    # groq and openrouter speak the OpenAI chat-completions dialect.
    messages = [{"role": "system", "content": instructions}]
    messages += [{"role": role, "content": text} for role, text in history]
    if media.kind == "pdf":
        part = {"type": "file", "file": {"filename": media.filename, "file_data": _data_url(media)}}
    else:
        part = {"type": "image_url", "image_url": {"url": _data_url(media)}}
    messages.append({"role": "user", "content": [{"type": "text", "text": question}, part]})
    return Request(f"{_CHAT_BASE[provider]}/chat/completions", {"Authorization": f"Bearer {key}"},
                   {"model": model, "messages": messages, "max_tokens": tokens})


def _transcription(provider, key, media):
    """Multipart upload. The language is left to the model's own detection: a
    forced language would garble a voice message in any other."""
    base = _OPENAI if provider == "openai" else _GROQ
    return Request(f"{base}/audio/transcriptions", {"Authorization": f"Bearer {key}"},
                   data={"model": PROVIDERS[provider].transcribe_model, "response_format": "json"},
                   files={"file": (media.filename, media.data, media.mime)})


def parse_answer(provider, body, kind):
    limit = MAX_OUTPUT_CHARS[kind]
    try:
        if provider == "openai" and "text" in body and "output" not in body:
            text = body["text"]  # audio transcription
        elif provider == "openai":
            if body.get("status") in ("incomplete", "failed", "cancelled"):
                raise DescriptionError("response")
            parts = [part for item in body.get("output", []) if item.get("type") == "message"
                     for part in item.get("content", [])]
            if any(p.get("type") == "refusal" for p in parts):
                raise DescriptionError("refusal")
            text = "\n".join(p["text"] for p in parts if p.get("type") == "output_text")
        elif provider == "gemini":
            candidates = body.get("candidates") or []
            if body.get("promptFeedback", {}).get("blockReason") or any(
                c.get("finishReason") in ("SAFETY", "RECITATION", "PROHIBITED_CONTENT", "BLOCKLIST")
                for c in candidates
            ):
                raise DescriptionError("refusal")
            if any(c.get("finishReason") == "MAX_TOKENS" for c in candidates):
                raise DescriptionError("response")
            text = "\n".join(p["text"] for c in candidates[:1]
                             for p in c.get("content", {}).get("parts", [])
                             if "text" in p and not p.get("thought"))
        elif provider == "claude":
            if body.get("stop_reason") == "refusal":
                raise DescriptionError("refusal")
            if body.get("stop_reason") == "max_tokens":
                raise DescriptionError("response")
            text = "\n".join(b["text"] for b in body.get("content", []) if b.get("type") == "text")
        elif provider in _CHAT_BASE:
            if "text" in body and "choices" not in body:
                text = body["text"]  # audio transcription
            else:
                choice = body["choices"][0]
                message = choice["message"]
                if message.get("refusal") or choice.get("finish_reason") == "content_filter":
                    raise DescriptionError("refusal")
                if choice.get("finish_reason") == "length":
                    raise DescriptionError("response")
                text = message.get("content")
        else:
            raise DescriptionError("request")
        if not isinstance(text, str) or not text.strip():
            raise DescriptionError("response")
        text = text.strip()
        if kind in ("audio", "pdf") and len(text) > limit:
            # A transcript or converted PDF must not lose its ending while
            # being presented as complete. This local limit ends the action.
            raise DescriptionError("output_limit")
        return text[:limit]
    except (KeyError, IndexError, TypeError, AttributeError):
        raise DescriptionError("response") from None
