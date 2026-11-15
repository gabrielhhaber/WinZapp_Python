"""How a summary's voice messages are transcribed: pure logic, wx-free.

A summary needs the words of its voice messages. The person picks, for each
summary that has voice messages without a saved transcription, one of three
ways (ui/conversation_panel/summary_voice_choice.py asks):

* ``local``  - the offline Whisper models, as before (the default);
* ``online`` - one AI provider's transcription model, chosen here apart from
  the model that writes the summary. Its audio leaves the computer, so it has
  its own consent (remembered per provider in the media consent list);
* ``none``   - no transcription at all: the voice messages stay markers.

The last choice is kept as the next dialog's starting point; it never skips
the dialog and never sends anything by itself.
"""
from typing import NamedTuple

from . import config as ai_config
from .config import PROVIDERS

MODE_LOCAL = "local"
MODE_ONLINE = "online"
MODE_NONE = "none"
MODES = (MODE_LOCAL, MODE_ONLINE, MODE_NONE)

#: The key of the last choice in the stored "ai_media" settings.
KEY = "summary_voice"

#: Failures that end the run: the next message would fail the same way, and
#: another request would only spend quota or credit again.
STOPPING_CATEGORIES = frozenset({"credentials", "authentication", "quota", "credits", "providers"})


class VoiceChoice(NamedTuple):
    mode: str
    provider: str = ""
    model: str = ""


def saved_choice(raw) -> VoiceChoice:
    """The last choice kept in the stored "ai_media" dict (damaged or missing
    values give the local default)."""
    value = raw.get(KEY) if isinstance(raw, dict) else None
    value = value if isinstance(value, dict) else {}
    mode = value.get("mode")
    provider = value.get("provider")
    model = value.get("model")
    return VoiceChoice(
        mode if mode in MODES else MODE_LOCAL,
        provider if isinstance(provider, str) else "",
        model if isinstance(model, str) else "")


def remember(old, choice) -> dict:
    """The stored "ai_media" dict with ``choice`` kept; a new dict."""
    value = dict(old) if isinstance(old, dict) else {}
    value[KEY] = {"mode": choice.mode, "provider": choice.provider, "model": choice.model}
    return value


def online_providers(config, saved_keys) -> list:
    """Providers that can transcribe audio now, in the person's order: on,
    holding a saved key, with a transcription model."""
    return [p for p in ai_config.chain(config, "audio", lambda p: p in saved_keys)
            if PROVIDERS[p].transcribe_model]


def model_choices(provider) -> tuple:
    """((model id, display name), ...) the person may pick for ``provider``;
    empty when it has one fixed transcription model."""
    spec = PROVIDERS.get(provider)
    return tuple((c.id, c.name) for c in spec.transcribe_choices) if spec else ()


def usable(choice, providers) -> VoiceChoice:
    """``choice`` made safe to show: an online choice whose provider is gone
    (key removed, switched off) falls back to the first provider that can
    transcribe, or to local when there is none; the model is always one the
    provider accepts."""
    if choice.mode != MODE_ONLINE:
        return VoiceChoice(choice.mode)
    if not providers:
        return VoiceChoice(MODE_LOCAL)
    provider = choice.provider if choice.provider in providers else providers[0]
    return VoiceChoice(MODE_ONLINE, provider, ai_config.transcribe_model(provider, choice.model))


def pending_voice(messages, has_text) -> list:
    """The voice messages of ``messages`` with no stored transcription."""
    return [m for m in messages
            if isinstance(m, dict) and m.get("messageType") == "audioMessage" and not has_text(m)]


def consent_text(i18n, provider, count, locked) -> str:
    """What the person is told before the voice messages leave the computer:
    how many, to whom, and that it may use quota or cost money."""
    message = i18n.t("ai_summary_voice_online_consent").format(
        count=count, provider=PROVIDERS[provider].name)
    message += "\n\n" + i18n.t("ai_metadata_notice")
    for key in ai_config.notice_keys([provider]):
        message += "\n\n" + i18n.t(key)
    if locked:
        message += "\n\n" + i18n.t("ai_locked_consent")
    return message
