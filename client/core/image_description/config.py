"""Public provider catalogue and validated install-wide preferences."""
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class Provider:
    name: str
    model: str
    key_url: str
    billing_url: str
    privacy_url: str


PROVIDERS = {
    "openai": Provider("OpenAI", "gpt-4.1-mini", "https://platform.openai.com/api-keys",
                       "https://platform.openai.com/settings/organization/billing/overview",
                       "https://developers.openai.com/api/docs/guides/your-data"),
    "gemini": Provider("Google Gemini", "gemini-3.8-flash", "https://aistudio.google.com/api-keys",
                       "https://ai.google.dev/gemini-api/docs/billing",
                       "https://ai.google.dev/gemini-api/terms"),
}
PROFILES = ("fast", "balanced", "detailed")
MAX_SOURCE_BYTES = 20 * 1024 * 1024
MAX_PIXELS = 24_000_000
MAX_OUTPUT_CHARS = 12_000
MAX_QUESTION_CHARS = 2000
MAX_TURNS = 8
MAX_REQUESTS = 12


def preferences(app_settings):
    raw = app_settings.get("image_description")
    raw = raw if isinstance(raw, dict) else {}
    provider = raw.get("provider")
    provider = provider if isinstance(provider, str) and provider in PROVIDERS else "openai"
    profile = raw.get("profile")
    profile = profile if profile in PROFILES else "balanced"
    models = raw.get("models", {})
    models = models if isinstance(models, dict) else {}
    model = models.get(provider, PROVIDERS[provider].model)
    if not valid_model(model):
        model = PROVIDERS[provider].model
    return {"enabled": raw.get("enabled") is True, "provider": provider,
            "profile": profile, "model": model,
            "read_answers": raw.get("read_answers", True) is True,
            "consented": provider in raw.get("consented", [])
            if isinstance(raw.get("consented", []), list) else False}


def valid_model(model):
    # A model identifier, never an endpoint/path/query supplied by a user.
    return isinstance(model, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", model))


def eligible_photo(message):
    if not isinstance(message, dict) or message.get("messageType") != "imageMessage":
        return False
    body = message.get("message") or {}
    image = body.get("imageMessage") if isinstance(body, dict) else None
    key = message.get("key") or {}
    return isinstance(key, dict) and isinstance(image, dict) and not any(
        x.get("viewOnce") or x.get("isViewOnce")
        for x in (message, body, image)
    ) and isinstance(key.get("id"), str) and bool(key["id"]) and not any(
        c in key["id"] for c in ("/", "\\", "\x00")
    )
