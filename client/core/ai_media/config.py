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
#: The inline payload each kind needs from a provider ("sticker" and "video"
#: ride on "image": a video is described from one still frame of it).
_CAPABILITY = {"image": "image", "sticker": "image", "video": "image",
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
    ocr_model: str = ""
    # i18n key of a note shown in the provider window before the model field,
    # when the model chosen there is not the one every kind goes to.
    model_note: str = ""
    # Voice-message models the person may choose instead of transcribe_model
    # (which must be one of them): see TranscribeModel.
    transcribe_choices: tuple = ()


@dataclass(frozen=True)
class TranscribeModel:
    """A reviewed voice-message model. The name is a proper name, shown as is."""
    id: str
    name: str
    # Public Preview at the provider: it may change or be withdrawn unannounced.
    preview: bool = False


PROVIDERS = {
    "gemini": Provider(
        "Google Gemini", "gemini-3.8-flash", "https://aistudio.google.com/api-keys",
        "https://ai.google.dev/gemini-api/docs/billing", "https://ai.google.dev/gemini-api/terms",
        frozenset({"image", "audio", "pdf"})),
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
    "mistral": Provider(
        "Mistral", "ministral-3b-latest", "https://console.mistral.ai/api-keys",
        "https://mistral.ai/pricing/api/", "https://legal.mistral.ai/terms/privacy-policy/",
        frozenset({"image", "audio", "pdf"}), transcribe_model="voxtral-mini-transcribe-3",
        # The two models Mistral's file-transcription guide accepts (checked on
        # docs.mistral.ai 2026-10-28), both Public Preview since 2026-10-20.
        # Mini is the default: the one tested live. Transcribe 2 is left out:
        # the guide lists any other model under "Invalid model" (not tested
        # live). The realtime model takes a stream, not a file.
        transcribe_choices=(
            TranscribeModel("voxtral-mini-transcribe-3", "Voxtral Mini Transcribe 3", preview=True),
            TranscribeModel("voxtral-small-transcribe-3", "Voxtral Small Transcribe 3",
                            preview=True),
        ),
        # The major alias, not "-latest": a new OCR generation may change the
        # answer's shape, and a minor id is retired within months (OCR 4.0).
        ocr_model="mistral-ocr-4", model_note="ai_mistral_model_note"),
}
PROFILES = ("fast", "balanced", "detailed")

MAX_PIXELS = 24_000_000
#: Largest original of each kind. Everything travels inline (base64) in one
#: request and Gemini's inline limit is 20 MB for the whole request, which a
#: 14 MiB original just fits once base64 has added a third. Images are
#: re-encoded below 8 MiB before sending, so they may start larger.
_INLINE_MAX = 14 * 1024 * 1024
#: A video never travels: only one frame of it does, so its original may be
#: larger than what fits inline.
MAX_SOURCE_BYTES = {"image": 20 * 1024 * 1024, "sticker": 20 * 1024 * 1024,
                    "audio": _INLINE_MAX, "pdf": _INLINE_MAX, "video": 64 * 1024 * 1024}
#: Longest answer kept; a converted PDF or a long voice message is far longer
#: than a description, and cutting it silently would drop content.
MAX_OUTPUT_CHARS = {"image": 12_000, "sticker": 12_000, "video": 12_000,
                    "audio": 50_000, "pdf": 100_000,
                    # A translated text message (core/message_translation.py).
                    "text": 32_000}
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


def _count(value, default, ceiling):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return default
    return min(value, ceiling)


def _usage(value):
    day = value.get("day") if isinstance(value, dict) else None
    count = value.get("count") if isinstance(value, dict) else None
    if not isinstance(day, str) or isinstance(count, bool) or not isinstance(count, int) \
            or count < 0:
        return {"day": "", "count": 0}
    return {"day": day, "count": count}


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
    voice = raw.get("transcribe_models")
    voice = voice if isinstance(voice, dict) else {}
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
        # The voice-message model of each provider that offers a choice.
        "transcribe_models": {p: transcribe_model(p, voice.get(p))
                              for p, spec in PROVIDERS.items() if spec.transcribe_choices},
        "kinds": {k: _flag(kinds.get(k), True) for k in KINDS},
        "profile": profile if profile in PROFILES else "balanced",
        "read_answers": _flag(raw.get("read_answers"), True),
        # Leave the Transcribe/Describe button out of the controls after the
        # message list; the message menu still offers it. Off by default.
        "hide_action_button": _flag(raw.get("hide_action_button"), False),
        # Done on arrival, with no window (core/ai_media/auto.py). Off by
        # default: every received voice message or picture would otherwise
        # leave the computer for a provider without anyone asking.
        "auto_transcribe": _flag(raw.get("auto_transcribe"), False),
        "auto_describe": _flag(raw.get("auto_describe"), False),
        # A ceiling on the requests made on arrival, per day, and how many
        # were made today (core/ai_media/auto.py). 0 = no ceiling.
        "auto_daily_limit": _count(raw.get("auto_daily_limit"), 50, 10000),
        "auto_usage": _usage(raw.get("auto_usage")),
        "consented": sorted(set(consented)),
    }


def valid_model(model):
    # A model identifier, never an endpoint/path/query supplied by a user.
    # Slash allowed: OpenRouter and Groq ids are "vendor/model".
    return isinstance(model, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:-]{0,99}", model)) \
        and ".." not in model and "//" not in model


def transcribe_model(provider, model):
    """The voice-message model ``provider`` gets: ``model`` when it is on that
    provider's reviewed list, else its default. Whatever the settings file or
    a caller says, an id outside the list is never sent for audio."""
    spec = PROVIDERS[provider]
    if any(choice.id == model for choice in spec.transcribe_choices):
        return model
    return spec.transcribe_model


def models_for(config, kind):
    """The model each provider is asked for ``kind``: audio goes to the chosen
    voice-message model, everything else to the model of the provider window."""
    if kind != "audio":
        return config["models"]
    return {**config["models"], **(config.get("transcribe_models") or {})}


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

#: i18n key of what a provider may do with what it receives, said in every
#: consent before media can reach it (its unpaid terms may train on it).
NOTICE_KEY = {"gemini": "ai_gemini_notice", "mistral": "ai_mistral_notice"}


def notice_keys(providers):
    """The provider notices for ``providers``, in their order."""
    return [NOTICE_KEY[p] for p in providers if p in NOTICE_KEY]


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
