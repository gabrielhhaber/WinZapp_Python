"""Stateless official REST adapters. No uploads, remote threads or auto retries."""
import base64

from .config import MAX_OUTPUT_CHARS, valid_model
from .errors import DescriptionError


def build_request(provider, model, key, image, history, question, instructions, profile):
    if not valid_model(model):
        raise DescriptionError("request")
    encoded = base64.b64encode(image.data).decode("ascii")
    if provider == "openai":
        messages = [{"role": role, "content": text} for role, text in history]
        messages.append({"role": "user", "content": [
            {"type": "input_image", "image_url": f"data:{image.mime};base64,{encoded}",
             "detail": "low" if profile == "fast" else "high"},
            {"type": "input_text", "text": question},
        ]})
        return ("https://api.openai.com/v1/responses", {"Authorization": f"Bearer {key}"},
                {"model": model, "store": False, "instructions": instructions,
                 "input": messages, "max_output_tokens": 1800})
    if provider == "gemini":
        contents = [{"role": "model" if role == "assistant" else "user", "parts": [{"text": text}]}
                    for role, text in history]
        contents.append({"role": "user", "parts": [
            {"inline_data": {"mime_type": image.mime, "data": encoded}}, {"text": question}]})
        return (f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                {"x-goog-api-key": key}, {"systemInstruction": {"parts": [{"text": instructions}]},
                "contents": contents, "generationConfig": {"maxOutputTokens": 2200}})
    raise DescriptionError("request")


def parse_answer(provider, body):
    try:
        if provider == "openai":
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
        else:
            raise DescriptionError("request")
        if not isinstance(text, str) or not text.strip():
            raise DescriptionError("response")
        return text.strip()[:MAX_OUTPUT_CHARS]
    except (KeyError, TypeError, AttributeError):
        raise DescriptionError("response") from None
