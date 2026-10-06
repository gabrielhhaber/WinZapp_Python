"""Public provider catalogue, media kinds and validated install-wide preferences.

One table (``PROVIDERS``) says which provider takes which kind of media; the
service, the settings page and the message menu all read it, so a provider is
added in exactly one place.
"""
from dataclasses import dataclass
import re

#: Media kinds the feature handles. A sticker is described like an image.
KINDS = ("image", "sticker", "video", "audio", "pdf")
#: The kind each WhatsApp message type is processed as.
KIND_BY_MESSAGE_TYPE = {
    "imageMessage": "image",
    "stickerMessage": "sticker",
    "videoMessage": "video",
    "audioMessage": "audio",
    "documentMessage": "pdf",
}
#: The inline payload each kind needs from a provider ("sticker" rides on "image").
_CAPABILITY = {"image": "image", "sticker": "image", "video": "video",
               "audio": "audio", "pdf": "pdf"}


@dataclass(frozen=True)
class Provider:
    name: str
    model: str
    key_url: str
    billing_url: str
    privacy_url: str
    # What the official API of this provider accepts as inline input today.
    capabilities: frozenset
    # Audio goes to a dedicated transcription model, not the chat model.
    transcribe_model: str = ""


PROVIDERS = {
    "gemini": Provider(
        "Google Gemini", "gemini-3.8-flash", "https://aistudio.google.com/api-keys",
        "https://ai.google.dev/gemini-api/docs/billing", "https://ai.google.dev/gemini-api/terms",
        frozenset({"image", "video", "audio", "pdf"})),
    "openai": Provider(
        "OpenAI", "gpt-4.1-mini", "https://platform.openai.com/api-keys",
        "https://platform.openai.com/settings/organization/billing/overview",
        "https://developers.openai.com/api/docs/guides/your-data",
        frozenset({"image", "audio", "pdf"}), transcribe_model="gpt-4o-mini-transcribe"),
    "claude": Provider(
        "Claude", "claude-sonnet-5", "https://console.anthropic.com/settings/keys",
        "https://console.anthropic.com/settings/billing",
        "https://www.anthropic.com/legal/privacy",
        frozenset({"image", "pdf"})),
    "groq": Provider(
        "Groq", "qwen/qwen3.8-27b", "https://console.groq.com/keys",
        "https://console.groq.com/settings/billing", "https://groq.com/privacy-policy",
        frozenset({"image", "audio"}), transcribe_model="whisper-large-v3-turbo"),
    "openrouter": Provider(
        "OpenRouter", "google/gemini-3.5-flash-lite", "https://openrouter.ai/settings/keys",
        "https://openrouter.ai/settings/credits", "https://openrouter.ai/privacy",
        frozenset({"image", "pdf"})),
}
PROFILES = ("fast", "balanced", "detailed")

MAX_PIXELS = 24_000_000
#: Largest original of each kind. Everything travels inline (base64) in one
#: request and Gemini's inline limit is 20 MB for the whole request, which a
#: 14 MiB original just fits once base64 has added a third. Images are
#: re-encoded below 8 MiB before sending, so they may start larger.
_INLINE_MAX = 14 * 1024 * 1024
MAX_SOURCE_BYTES = {"image": 20 * 1024 * 1024, "sticker": 20 * 1024 * 1024,
                    "audio": _INLINE_MAX, "pdf": _INLINE_MAX, "video": _INLINE_MAX}
#: Longest answer kept; a converted PDF or a long voice message is far longer
#: than a description, and cutting it silently would drop content.
MAX_OUTPUT_CHARS = {"image": 12_000, "sticker": 12_000, "video": 12_000,
                    "audio": 50_000, "pdf": 100_000}
#: Token budget asked of the provider, by kind.
MAX_OUTPUT_TOKENS = {"image": 1800, "sticker": 1200, "video": 2200,
                     "audio": 8000, "pdf": 16000}
#: Largest response body read.
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_QUESTION_CHARS = 2000
MAX_TURNS = 8
MAX_REQUESTS = 12


def capability(kind):
    return _CAPABILITY[kind]


def supports(provider, kind):
    return provider in PROVIDERS and capability(kind) in PROVIDERS[provider].capabilities


def asks_questions(kind):
    """Only pictures and videos get follow-up questions; audio and PDF are
    converted to text in one go."""
    return kind in ("image", "sticker", "video")


def _flag(value, default):
    return value if isinstance(value, bool) else default


def preferences(app_settings):
    """The validated ``ai_media`` block of the install-wide settings.

    ``providers`` is the ordered list of provider ids the person has not
    switched off; the order is the fallback order. Unknown ids are dropped and
    providers missing from the saved order are appended in catalogue order.
    """
    raw = app_settings.get("ai_media")
    raw = raw if isinstance(raw, dict) else {}
    saved = raw.get("order")
    order = [p for p in saved if p in PROVIDERS] if isinstance(saved, list) else []
    order = list(dict.fromkeys(order))
    order += [p for p in PROVIDERS if p not in order]
    off = raw.get("disabled")
    off = {p for p in off if p in PROVIDERS} if isinstance(off, list) else set()
    models = raw.get("models")
    models = models if isinstance(models, dict) else {}
    chosen = {}
    for provider, spec in PROVIDERS.items():
        model = models.get(provider)
        chosen[provider] = model if valid_model(model) else spec.model
    kinds = raw.get("kinds")
    kinds = kinds if isinstance(kinds, dict) else {}
    consented = raw.get("consented")
    consented = [p for p in consented if p in PROVIDERS] if isinstance(consented, list) else []
    profile = raw.get("profile")
    return {
        "enabled": raw.get("enabled") is True,
        "order": order,
        "disabled": sorted(off),
        "models": chosen,
        # Providers following WinZapp's recommended model (nothing valid saved).
        "auto_models": sorted(p for p in PROVIDERS if not valid_model(models.get(p))),
        "kinds": {k: _flag(kinds.get(k), True) for k in KINDS},
        "profile": profile if profile in PROFILES else "balanced",
        "read_answers": _flag(raw.get("read_answers"), True),
        "consented": sorted(set(consented)),
    }


def valid_model(model):
    # A model identifier, never an endpoint/path/query supplied by a user.
    # Slash allowed: OpenRouter and Groq ids are "vendor/model".
    return isinstance(model, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:-]{0,99}", model)) \
        and ".." not in model and "//" not in model


def chain(config, kind, has_key, prefer=None):
    """Providers to try for ``kind``, in the person's order: on, holding a
    saved key, and accepting this kind of media. ``prefer`` (the provider that
    answered first) is tried first so follow-ups keep one voice."""
    result = [p for p in config["order"]
              if p not in config["disabled"] and supports(p, kind) and has_key(p)]
    if prefer in result:
        result.remove(prefer)
        result.insert(0, prefer)
    return result


def _flat(value):
    return value if isinstance(value, dict) else {}


def _view_once(message):
    body = _flat(message.get("message"))
    inner = [_flat(body.get(t)) for t in
             ("imageMessage", "videoMessage", "audioMessage", "stickerMessage", "documentMessage")]
    return any(x.get("viewOnce") or x.get("isViewOnce") for x in (message, body, *inner))


def eligible_kind(message):
    """The kind this message is processed as, or None. View-once media, an
    unusable id and (for documents) anything but a PDF are never offered."""
    if not isinstance(message, dict):
        return None
    kind = KIND_BY_MESSAGE_TYPE.get(message.get("messageType"))
    key = message.get("key")
    key = key if isinstance(key, dict) else {}
    identifier = key.get("id")
    if kind is None or not isinstance(identifier, str) or not identifier or any(
            c in identifier for c in ("/", "\\", "\x00")):
        return None
    if _view_once(message):
        return None
    if kind == "pdf":
        document = _flat(_flat(message.get("message")).get("documentMessage"))
        mime = str(document.get("mimetype") or "").split(";")[0].strip().lower()
        if mime != "application/pdf":
            return None
    return kind


#: i18n key of the context-menu item (and of the focus-time announcement) for
#: each kind: one verb per kind, shared by the menu and Ctrl+Shift+I.
MENU_KEY = {"image": "ai_describe_image_menu", "sticker": "ai_describe_sticker_menu",
            "video": "ai_describe_video_menu", "audio": "ai_transcribe_audio_menu",
            "pdf": "ai_pdf_accessible_menu"}
#: i18n key of the window title per kind.
TITLE_KEY = {"image": "ai_result_description_title", "sticker": "ai_result_description_title",
             "video": "ai_result_description_title", "audio": "ai_result_transcription_title",
             "pdf": "ai_result_pdf_title"}


def offered_kind(message, config, saved):
    """The kind to offer on ``message`` right now, or None.

    Needs the feature switched on, the kind switched on, and at least one
    provider that is on, holds a key (``saved``: the providers with a saved
    key) and takes this kind: offering an action that can only fail wastes the
    person's time with a screen reader.
    """
    kind = eligible_kind(message)
    if kind is None or not config["enabled"] or not config["kinds"].get(kind, True):
        return None
    return kind if chain(config, kind, lambda provider: provider in saved) else None
