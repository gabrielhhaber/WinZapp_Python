"""Public provider catalogue, media kinds and validated install-wide preferences.

One table (``PROVIDERS``) says which provider takes which kind of media; the
service, the settings page and the message menu all read it, so a provider is
added in exactly one place.
"""
from dataclasses import dataclass
import re

from . import local_url

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
#: ride on "image": a video is described from still frames of it).
_CAPABILITY = {"image": "image", "sticker": "image", "video": "image",
               "audio": "audio", "pdf": "pdf", "text": "text", "writing": "text"}


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
    # A language model server the person runs (Ollama, LM Studio): no key, no
    # default model (only the person knows what is installed) and an address
    # that, while it is this computer, never sends anything out
    # (core/ai_media/local_url.py, docs/reference/ai-media.md "Local models").
    local: bool = False
    default_url: str = ""


@dataclass(frozen=True)
class TranscribeModel:
    """A reviewed voice-message model. The name is a proper name, shown as is."""
    id: str
    name: str
    # Public Preview at the provider: it may change or be withdrawn unannounced.
    preview: bool = False


PROVIDERS = {
    "gemini": Provider(
        "Google Gemini", "gemini-3.5-flash-lite", "https://aistudio.google.com/api-keys",
        "https://ai.google.dev/gemini-api/docs/billing", "https://ai.google.dev/gemini-api/terms",
        frozenset({"image", "audio", "pdf", "text"})),
    "openai": Provider(
        "OpenAI", "gpt-4.1-mini", "https://platform.openai.com/api-keys",
        "https://platform.openai.com/settings/organization/billing/overview",
        "https://developers.openai.com/api/docs/guides/your-data",
        frozenset({"image", "audio", "pdf", "text"}), transcribe_model="gpt-4o-mini-transcribe"),
    "claude": Provider(
        "Claude", "claude-sonnet-5", "https://console.anthropic.com/settings/keys",
        "https://console.anthropic.com/settings/billing",
        "https://www.anthropic.com/legal/privacy",
        frozenset({"image", "pdf", "text"})),
    "groq": Provider(
        "Groq", "qwen/qwen3.8-27b", "https://console.groq.com/keys",
        "https://console.groq.com/settings/billing", "https://groq.com/privacy-policy",
        frozenset({"image", "audio", "text"}), transcribe_model="whisper-large-v3-turbo"),
    "openrouter": Provider(
        "OpenRouter", "google/gemini-3.5-flash-lite", "https://openrouter.ai/settings/keys",
        "https://openrouter.ai/settings/credits", "https://openrouter.ai/privacy",
        frozenset({"image", "pdf", "text"})),
    "mistral": Provider(
        "Mistral", "ministral-3b-latest", "https://console.mistral.ai/api-keys",
        "https://mistral.ai/pricing/api/", "https://legal.mistral.ai/terms/privacy-policy/",
        frozenset({"image", "audio", "pdf", "text"}), transcribe_model="voxtral-mini-transcribe-3",
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
    # Local servers speak the OpenAI chat protocol and take pictures and text
    # (a vision model for pictures). The ids are not "ollama"/"lm_studio": message
    # translation already uses those for its own, separate setting.
    "local_ollama": Provider("Ollama", "", "", "", "", frozenset({"image", "text"}),
                             local=True, default_url="http://localhost:11434/v1"),
    "local_lm_studio": Provider("LM Studio", "", "", "", "", frozenset({"image", "text"}),
                                local=True, default_url="http://localhost:1234/v1"),
}
#: The providers that need an API key (the credential store and message
#: translation know these only).
KEYED_PROVIDERS = {p: spec for p, spec in PROVIDERS.items() if not spec.local}
PROFILES = ("fast", "balanced", "detailed")
#: What a video sends: a mosaic of frames spaced over it, or one frame.
VIDEO_FRAMES = ("several", "one")

MAX_PIXELS = 24_000_000
#: Largest original of each kind. Everything travels inline (base64) in one
#: request and Gemini's inline limit is 20 MB for the whole request, which a
#: 14 MiB original just fits once base64 has added a third. Images are
#: re-encoded below 8 MiB before sending, so they may start larger.
_INLINE_MAX = 14 * 1024 * 1024
#: A video never travels: only frames of it do, so its original may be
#: larger than what fits inline.
MAX_SOURCE_BYTES = {"image": 20 * 1024 * 1024, "sticker": 20 * 1024 * 1024,
                    "audio": _INLINE_MAX, "pdf": _INLINE_MAX, "video": 64 * 1024 * 1024,
                    "text": 200_000, "writing": 20_000}
def utf8_bytes(text):
    """The text as UTF-8, never raising.

    wx can hand back a lone UTF-16 surrogate (half of an emoji typed or
    inserted into the composer); a strict encode raised on it (#797). A
    well-formed pair is joined into its character, a lone half becomes U+FFFD.
    """
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:
        joined = text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
        return joined.encode("utf-8", "replace")


#: Longest answer kept; a converted PDF or a long voice message is far longer
#: than a description, and cutting it silently would drop content.
MAX_OUTPUT_CHARS = {"image": 12_000, "sticker": 12_000, "video": 12_000,
                    "audio": 50_000, "pdf": 100_000,
                    # A translated text message (core/message_translation.py).
                    "text": 32_000, "writing": 8_000}
#: Token budget asked of the provider, by kind.
MAX_OUTPUT_TOKENS = {"image": 1800, "sticker": 1200, "video": 2200,
                     "audio": 8000, "pdf": 16000, "text": 3000, "writing": 2000}
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
    """Pictures, videos and chat summaries get follow-up questions; audio and
    PDF are converted to text in one go."""
    return kind in ("image", "sticker", "video", "text")


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
    from . import routes, usage  # both read PROVIDERS from this module
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
    # A local server's address: the default until the person saves one; a saved
    # address that does not pass the strict reading is "" (the provider then
    # stays out of the chain) instead of falling back to another host.
    urls = raw.get("local_urls")
    urls = urls if isinstance(urls, dict) else {}
    local_urls = {p: (local_url.valid_url(urls[p]) if p in urls else spec.default_url)
                  for p, spec in PROVIDERS.items() if spec.local}
    voice = raw.get("transcribe_models")
    voice = voice if isinstance(voice, dict) else {}
    kinds = raw.get("kinds")
    kinds = kinds if isinstance(kinds, dict) else {}
    consented = raw.get("consented")
    consented = [p for p in consented if p in PROVIDERS] if isinstance(consented, list) else []
    consented_text = raw.get("consented_text")
    consented_text = [p for p in consented_text if p in PROVIDERS] \
        if isinstance(consented_text, list) else []
    consented_writing = raw.get("consented_writing")
    consented_writing = [p for p in consented_writing if p in PROVIDERS] \
        if isinstance(consented_writing, list) else []
    consented_context = raw.get("consented_context")
    consented_context = [p for p in consented_context if p in PROVIDERS]         if isinstance(consented_context, list) else []
    consented_reply = raw.get("consented_reply")
    consented_reply = [p for p in consented_reply if p in PROVIDERS]         if isinstance(consented_reply, list) else []
    profile = raw.get("profile")
    return {
        "enabled": raw.get("enabled") is True,
        "order": order,
        # Optional order per kind of task; a kind without one uses "order".
        "routes": routes.validated(raw.get("routes")),
        "disabled": sorted(off),
        "models": chosen,
        # Providers following WinZapp's recommended model (nothing valid saved).
        # (A local provider has no recommended model: the person picks one.)
        "auto_models": sorted(p for p in KEYED_PROVIDERS if not valid_model(models.get(p))),
        "local_urls": local_urls,
        # The voice-message model of each provider that offers a choice.
        "transcribe_models": {p: transcribe_model(p, voice.get(p))
                              for p, spec in PROVIDERS.items() if spec.transcribe_choices},
        "kinds": {k: _flag(kinds.get(k), True) for k in KINDS},
        "profile": profile if profile in PROFILES else "balanced",
        "video_frames": raw.get("video_frames") if raw.get("video_frames") in VIDEO_FRAMES else "several",
        "read_answers": _flag(raw.get("read_answers"), True),
        # Fill the result window paragraph by paragraph while a text answer
        # arrives (core/ai_media/streaming.py). Off by default: a first version
        # whose effect on each screen reader is still to be confirmed.
        "ai_streaming": _flag(raw.get("ai_streaming"), False),
        # Opt-in context for the description of a picture, sticker or video:
        # the caption and the message a reply quotes are text other people
        # wrote, so they only travel when asked for (core/ai_media/context.py).
        "context_caption": _flag(raw.get("context_caption"), False),
        "context_quote": _flag(raw.get("context_quote"), False),
        # A backlog too long for one summary request is summarized in parts,
        # one request each plus a final one (core/ai_media/summary_parts.py).
        # Off by default: the ordinary summary drops the older messages and
        # says how many, and makes one request.
        "summary_hierarchy": _flag(raw.get("summary_hierarchy"), False),
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
        # Counters per provider of answers and failure categories, never any
        # content (core/ai_media/usage.py).
        "usage": usage.validated(raw.get("usage")),
        "consented": sorted(set(consented)),
        # Chat summaries send the TEXT of unread messages to a provider — a
        # bigger ask than one photo, so they carry their own consent list.
        "consented_text": sorted(set(consented_text)),
        # Writing help sends the person's own draft to a provider; translation
        # sends message text to Google's web endpoint. Each carries its own
        # consent, separate from the media one.
        "consented_writing": sorted(set(consented_writing)),
        # Sending the caption or the quoted message with a media request, and
        # the latest messages of the chat with a suggested reply: each is
        # more than the media or the draft alone, so each asks on its own.
        "consented_context": sorted(set(consented_context)),
        "consented_reply": sorted(set(consented_reply)),
        "consented_translate": raw.get("consented_translate") is True,
    }


def consent_key(kind):
    """The stored list a kind's remembered consent lives in. Chat summaries send
    the text of the messages themselves, so theirs is kept apart from media's."""
    return {"text": "consented_text", "context": "consented_context",
            "reply": "consented_reply"}.get(kind, "consented")


def remember_consent(old, kind, providers, locked=False):
    """The stored "ai_media" dict after the person agreed for ``providers``.

    A new dict (``old`` may be damaged or missing): the providers are added to
    the kind's own list and the other lists stay as they are. Never for a locked
    chat: its consent is asked every time, so ``old`` comes back unchanged."""
    value = dict(old) if isinstance(old, dict) else {}
    if locked:
        return value
    key = consent_key(kind)
    current = value.get(key, [])
    consented = set(current if isinstance(current, list) else [])
    value[key] = sorted(consented | set(providers))
    return value


def drop_consents(value, removed):
    """``value`` (the stored "ai_media" dict) without the remembered consent of
    the ``removed`` providers, in both lists: a provider whose key is removed
    must ask again, for media and for chat text, if it is ever set up again."""
    for key in ("consented", "consented_text", "consented_writing", "consented_context",
                "consented_reply"):
        kept = value.get(key, [])
        value[key] = [p for p in kept if p not in removed] if isinstance(kept, list) else []
    return value


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


def is_local(provider):
    return provider in PROVIDERS and PROVIDERS[provider].local


def local_ready(config, provider):
    """A local provider with a usable address and a model the person chose.
    Without a model there is nothing to ask for: it stays out of the chain."""
    return (is_local(provider) and bool((config.get("models") or {}).get(provider))
            and bool((config.get("local_urls") or {}).get(provider)))


def is_cloud_model(model):
    """Whether an Ollama model id is one that Ollama runs in its cloud: the
    local server answers on this computer but forwards the prompt and the
    images there. Ollama names them with a "-cloud" or ":cloud" suffix."""
    return isinstance(model, str) and model.strip().lower().endswith(("-cloud", ":cloud"))


def cloud_model(config, provider):
    """Whether the model chosen for the local ``provider`` is a cloud one."""
    return is_local(provider) and is_cloud_model((config.get("models") or {}).get(provider, ""))


def sends_out(config, provider):
    """Whether what goes to ``provider`` leaves this computer. Every online
    provider does; a local one when its address is not this computer's (then
    it is a remote server) or its chosen model is a cloud one (this computer
    forwards it): both ask for consent like any other. An unknown address
    counts as leaving: the answer errs towards asking."""
    if not is_local(provider):
        return True
    if cloud_model(config, provider):
        return True
    return not local_url.is_loopback_url((config.get("local_urls") or {}).get(provider, ""))


def local_status(config, provider):
    """Why a local provider is, or is not, in the chain: ``"bad_url"`` (no
    usable address), ``"no_model"`` (none chosen), ``"remote"`` (ready, but the
    address is not this computer), ``"cloud"`` (ready, but the model runs in
    Ollama's cloud) or ``"ready"`` (ready, on this computer)."""
    if not (config.get("local_urls") or {}).get(provider):
        return "bad_url"
    if not (config.get("models") or {}).get(provider):
        return "no_model"
    if cloud_model(config, provider):
        return "cloud"
    return "remote" if sends_out(config, provider) else "ready"


def outbound(config, providers):
    """The providers of ``providers`` whose data leaves this computer: the only
    ones a consent names and remembers."""
    return [p for p in providers if sends_out(config, p)]


def has_local(providers):
    """Whether a local server is among ``providers`` (slow models: longer waits)."""
    return any(is_local(p) for p in providers)


def endpoints(config):
    """The address of each local provider, for ``service.run_chain``."""
    return dict(config.get("local_urls") or {})


def _usable(config, provider, has_key):
    return local_ready(config, provider) if is_local(provider) else has_key(provider)


def chain(config, kind, has_key, prefer=None):
    """Providers to try for ``kind``, in the person's order: on, holding a
    saved key (a local server: with an address and a chosen model), and
    accepting this kind of media. ``prefer`` (the provider that
    answered first) is tried first so follow-ups keep one voice."""
    from . import routes  # routes.py reads PROVIDERS from this module
    # A route of the kind (core/ai_media/routes.py) replaces the general
    # order; every filter below still applies to its providers.
    order = routes.route_for(config, kind) or config["order"]
    result = [p for p in order
              if p not in config["disabled"] and supports(p, kind) and _usable(config, p, has_key)]
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
             "pdf": "ai_result_pdf_title", "text": "ai_result_summary_title"}

#: i18n key of what a provider may do with what it receives, said in every
#: consent before media can reach it (its unpaid terms may train on it).
NOTICE_KEY = {"gemini": "ai_gemini_notice", "mistral": "ai_mistral_notice",
              # Only an outbound local provider reaches a consent (``outbound``):
              # its address is a server that is not this computer.
              "local_ollama": "ai_local_remote_notice", "local_lm_studio": "ai_local_remote_notice"}


def notice_keys(providers, config=None):
    """The provider notices for ``providers``, in their order. With ``config``
    a local provider whose model is a cloud one gets the cloud notice, not the
    remote-address one (its address may well be this computer)."""
    keys = []
    for p in providers:
        if p not in NOTICE_KEY:
            continue
        if config is not None and cloud_model(config, p):
            if not local_url.is_loopback_url((config.get("local_urls") or {}).get(p, "")):
                keys.append(NOTICE_KEY[p])
            keys.append("ai_local_cloud_model_notice")
        else:
            keys.append(NOTICE_KEY[p])
    return list(dict.fromkeys(keys))


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
