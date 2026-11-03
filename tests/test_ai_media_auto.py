"""Automatic online transcription and description: which messages qualify."""
import time

from core.ai_media import auto, config
from core.transcription.stored import TRANSCRIPTION_KEY


def _prefs(**over):
    base = {"enabled": True, "kinds": {k: True for k in config.KINDS},
            "auto_transcribe": True, "auto_describe": True,
            "order": list(config.PROVIDERS), "disabled": [], "consented": []}
    base.update(over)
    return base


def _msg(msg_type="audioMessage", id_="A1", from_me=False, seconds=5, **extra):
    inner = {"mimetype": "audio/ogg", "seconds": seconds, "fileLength": 1000}
    msg = {"messageType": msg_type, "key": {"id": id_, "fromMe": from_me},
           "message": {msg_type: inner}, "messageTimestamp": time.time()}
    msg.update(extra)
    return msg


def test_defaults_are_off():
    from core.ai_media.config import preferences

    class App(dict):
        pass
    prefs = preferences(App())
    assert prefs["auto_transcribe"] is False and prefs["auto_describe"] is False


def test_each_option_covers_its_own_kinds():
    assert auto.wanted_kind(_msg(), _prefs()) == "audio"
    assert auto.wanted_kind(_msg(), _prefs(auto_transcribe=False)) is None
    for kind_type, kind in (("imageMessage", "image"), ("stickerMessage", "sticker"),
                            ("videoMessage", "video")):
        msg = _msg(kind_type)
        assert auto.wanted_kind(msg, _prefs()) == kind
        assert auto.wanted_kind(msg, _prefs(auto_describe=False)) is None
        # Describing is independent of the transcription option.
        assert auto.wanted_kind(msg, _prefs(auto_transcribe=False)) == kind


def test_the_local_automatic_transcription_wins_for_voice_only():
    assert auto.wanted_kind(_msg(), _prefs(), local_auto_transcription=True) is None
    assert auto.wanted_kind(_msg("imageMessage"), _prefs(),
                            local_auto_transcription=True) == "image"


def test_what_is_left_alone():
    assert auto.wanted_kind(_msg(from_me=True), _prefs()) is None
    assert auto.wanted_kind(_msg(**{TRANSCRIPTION_KEY: {"at": 1.0, "text": "x"}}), _prefs()) is None
    assert auto.wanted_kind(_msg(seconds=60 * 60), _prefs()) is None
    old = _msg(messageTimestamp=time.time() - 3600)
    assert auto.wanted_kind(old, _prefs()) is None
    assert auto.wanted_kind(_msg(), _prefs(enabled=False)) is None
    kinds = {k: True for k in config.KINDS}
    kinds["audio"] = False
    assert auto.wanted_kind(_msg(), _prefs(kinds=kinds)) is None
    big = _msg("imageMessage")
    big["message"]["imageMessage"]["fileLength"] = 10 ** 9
    assert auto.wanted_kind(big, _prefs()) is None


def test_only_consented_providers_receive_anything():
    saved = set(config.PROVIDERS)
    some = [p for p in config.PROVIDERS if config.supports(p, "audio")]
    assert auto.usable_providers(_prefs(), "audio", saved) == []
    got = auto.usable_providers(_prefs(consented=some[:1]), "audio", saved)
    assert got == some[:1]
    assert auto.usable_providers(_prefs(consented=some), "audio", set()) == []


class _Box:
    def __init__(self, value):
        self.value = value

    def GetValue(self):
        return self.value

    def SetValue(self, value):
        self.value = value


def _options(local_on, answer, monkeypatch):
    import wx
    from types import SimpleNamespace
    from ui.dialogs.transcription_options import TranscriptionOptionsMixin

    asked = []
    monkeypatch.setattr(wx, "MessageBox",
                        lambda text, *a, **k: asked.append(text) or (wx.YES if answer else wx.NO))
    dialog = TranscriptionOptionsMixin()
    dialog.main_window = SimpleNamespace(i18n=SimpleNamespace(t=lambda key: key))
    dialog._transcription_auto_check = _Box(local_on)
    dialog._ai_page = SimpleNamespace(auto_transcribe=_Box(False))
    return dialog, asked


def test_ticking_the_local_option_reads_an_ai_page_never_opened_from_the_store(monkeypatch):
    """Turned on with nothing to replace, the AI page is not built for it."""
    from types import SimpleNamespace
    from core.ai_media import config as ai_config

    dialog, asked = _options(local_on=True, answer=True, monkeypatch=monkeypatch)
    del dialog._ai_page
    dialog.main_window._app_settings = object()
    dialog.main_window.settings = {}
    monkeypatch.setattr(ai_config, "preferences", lambda app: {"auto_transcribe": False})
    dialog._on_local_auto_toggle(SimpleNamespace(Skip=lambda: None))
    assert asked == [] and "_ai_page" not in vars(dialog)


def test_turning_the_online_option_on_asks_before_turning_the_local_one_off(monkeypatch):
    dialog, asked = _options(local_on=True, answer=True, monkeypatch=monkeypatch)
    assert dialog._confirm_online_replaces_local() is True
    assert dialog._transcription_auto_check.GetValue() is False
    assert asked == ["transcription_online_auto_replaces_local"]

    dialog, asked = _options(local_on=True, answer=False, monkeypatch=monkeypatch)
    assert dialog._confirm_online_replaces_local() is False
    assert dialog._transcription_auto_check.GetValue() is True

    dialog, asked = _options(local_on=False, answer=False, monkeypatch=monkeypatch)
    assert dialog._confirm_online_replaces_local() is True and asked == []


def test_turning_the_local_option_on_asks_before_turning_the_online_one_off(monkeypatch):
    from types import SimpleNamespace

    dialog, asked = _options(local_on=True, answer=True, monkeypatch=monkeypatch)
    dialog._ai_page.auto_transcribe.SetValue(True)
    event = SimpleNamespace(Skip=lambda: None)
    dialog._on_local_auto_toggle(event)
    assert dialog._ai_page.auto_transcribe.GetValue() is False
    assert dialog._transcription_auto_check.GetValue() is True
    assert asked == ["transcription_auto_replaces_online"]

    dialog, asked = _options(local_on=True, answer=False, monkeypatch=monkeypatch)
    dialog._ai_page.auto_transcribe.SetValue(True)
    dialog._on_local_auto_toggle(event)
    assert dialog._transcription_auto_check.GetValue() is False
    assert dialog._ai_page.auto_transcribe.GetValue() is True


def test_conversation_boxes_only_record_what_differs_from_the_scope():
    from core import conversation_auto_ai as conv

    jid = "5511@s.whatsapp.net"
    # Scope "all" (the default): everyone, except the ones turned off.
    settings = {}
    assert conv.allowed(settings, jid, conv.TRANSCRIBE)
    conv.set_allowed(settings, jid, conv.TRANSCRIBE, False)
    assert not conv.allowed(settings, jid, conv.TRANSCRIBE)
    assert conv.allowed(settings, jid, conv.DESCRIBE)
    assert conv.allowed(settings, "other@s.whatsapp.net", conv.TRANSCRIBE)
    conv.set_allowed(settings, jid, conv.TRANSCRIBE, True)
    assert settings[conv.SETTING] == {}

    # Scope "selected": nobody, except the ones turned on.
    settings = {conv.SCOPE_SETTING: conv.SCOPE_SELECTED}
    assert not conv.allowed(settings, jid, conv.TRANSCRIBE)
    assert not conv.allowed(settings, jid, conv.DESCRIBE)
    conv.set_allowed(settings, jid, conv.DESCRIBE, True)
    assert conv.allowed(settings, jid, conv.DESCRIBE)
    assert not conv.allowed(settings, jid, conv.TRANSCRIBE)
    assert not conv.allowed(settings, "other@s.whatsapp.net", conv.DESCRIBE)
    conv.set_allowed(settings, jid, conv.DESCRIBE, False)
    assert settings[conv.SETTING] == {}


def test_an_unknown_scope_reads_as_all_and_old_entries_still_hold():
    from core import conversation_auto_ai as conv

    assert conv.scope({conv.SCOPE_SETTING: "nonsense"}) == conv.SCOPE_ALL
    assert conv.scope({}) == conv.SCOPE_ALL
    # Entries written before the scope existed held only cleared boxes.
    old = {conv.SETTING: {"a@s.whatsapp.net": {"transcribe": False}}}
    assert not conv.allowed(old, "a@s.whatsapp.net", conv.TRANSCRIBE)
    assert conv.allowed(old, "a@s.whatsapp.net", conv.DESCRIBE)


def test_the_age_a_message_may_have_is_a_choice_with_a_safe_default():
    from core.transcription import auto_queue

    assert auto_queue.max_age_seconds({}) == auto_queue.MAX_AGE_SECONDS
    for minutes in auto_queue.AGE_CHOICES_MINUTES:
        settings = {"transcription": {"auto_max_age_minutes": minutes}}
        assert auto_queue.max_age_seconds(settings) == minutes * 60.0
    for junk in (0, 7, -5, "30", None, True, 10 ** 6):
        settings = {"transcription": {"auto_max_age_minutes": junk}}
        assert auto_queue.max_age_seconds(settings) == auto_queue.MAX_AGE_SECONDS


def test_a_longer_age_reaches_older_messages_online_and_locally():
    from core.transcription import auto_queue

    old = _msg(messageTimestamp=time.time() - 20 * 60)
    assert auto.wanted_kind(old, _prefs()) is None
    assert auto.wanted_kind(old, _prefs(), max_age=30 * 60) == "audio"
    assert auto.wanted_kind(old, _prefs(), max_age=15 * 60) is None
    assert not auto_queue.eligible(old)
    assert auto_queue.eligible(old, max_age=30 * 60)


def test_the_daily_ceiling_counts_per_day_and_zero_means_none():
    day = auto.day_key(1_700_000_000)
    config = {"auto_daily_limit": 2, "auto_usage": {"day": day, "count": 1}}
    assert not auto.limit_reached(config, 1_700_000_000)
    config["auto_usage"]["count"] = 2
    assert auto.limit_reached(config, 1_700_000_000)
    # Another day starts again from zero.
    assert not auto.limit_reached(config, 1_700_000_000 + 2 * 86400)
    assert auto.spent_today(config, 1_700_000_000 + 2 * 86400) == 0
    config["auto_daily_limit"] = 0
    assert not auto.limit_reached(config, 1_700_000_000)


def test_charging_counts_one_more_and_keeps_the_rest_of_the_block():
    now = 1_700_000_000
    block = {"enabled": True, "consented": ["gemini"]}
    once = auto.charged(block, now)
    twice = auto.charged(once, now)
    assert twice["auto_usage"] == {"day": auto.day_key(now), "count": 2}
    assert twice["enabled"] is True and twice["consented"] == ["gemini"]
    next_day = auto.charged(twice, now + 86400 * 2)
    assert next_day["auto_usage"]["count"] == 1
    assert auto.charged(None, now)["auto_usage"]["count"] == 1


def test_only_failures_that_repeat_pause_the_queue():
    from core.ai_media.errors import DescriptionError
    from core.ai_media.service import ChainFailed

    assert auto.pauses(ChainFailed([("gemini", "quota"), ("openai", "authentication")]))
    assert not auto.pauses(ChainFailed([("gemini", "quota"), ("openai", "network")]))
    assert not auto.pauses(DescriptionError("timeout"))
    assert auto.pauses(DescriptionError("quota"))
