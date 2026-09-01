"""Tests distinguishing voice messages (PTT / mensagem de voz) from generic audio files.

Feature: In both the conversation messages list and the chat list preview,
WinZapp must distinguish PTT voice notes ("mensagem de voz" / "voice message")
from generic attached audio files ("áudio" / "audio").
"""

import sys
import types
from unittest.mock import MagicMock

import pytest

try:
    import wx
    import wx.adv
except ImportError:
    for _mod in ("wx", "wx.adv"):
        if _mod not in sys.modules:
            mod = types.ModuleType(_mod)
            if "." not in _mod:
                mod.__path__ = []
            sys.modules[_mod] = mod
    class _FakeWxModule(types.ModuleType):
        ACC_OK = 0
        ACC_NOT_IMPLEMENTED = -1
        def __getattr__(self, name):
            if name == "__file__":
                return "<fake_wx>"
            if name == "CallAfter":
                return lambda fn, *a, **k: fn(*a, **k)
            if name.startswith("ID_") or name.startswith("wxID_") or name in ("HORIZONTAL", "VERTICAL", "EXPAND", "ALL"):
                return 1000
            if name in ("Frame", "Panel", "Dialog", "Accessible", "Timer", "App", "Window", "Control", "Button"):
                return object
            return MagicMock
    sys.modules["wx"].__class__ = _FakeWxModule
    sys.modules["wx.adv"].__class__ = _FakeWxModule
    wx = sys.modules["wx"]

try:
    import accessible_output2
    from accessible_output2 import outputs
except ImportError:
    if "accessible_output2" not in sys.modules:
        sys.modules["accessible_output2"] = types.ModuleType("accessible_output2")
    sys.modules["accessible_output2.outputs"] = types.ModuleType("accessible_output2.outputs")
    sys.modules["accessible_output2"].outputs = sys.modules["accessible_output2.outputs"]

try:
    import sound_lib
    from sound_lib import stream, output, main, effects
except ImportError:
    for _mod in (
        "sound_lib",
        "sound_lib.output",
        "sound_lib.stream",
        "sound_lib.main",
        "sound_lib.effects",
    ):
        if _mod not in sys.modules:
            mod = types.ModuleType(_mod)
            if "." not in _mod:
                mod.__path__ = []
            sys.modules[_mod] = mod

    sys.modules["sound_lib.main"].bass_call = lambda *a, **k: None
    sys.modules["sound_lib.stream"].FileStream = object
    sys.modules["sound_lib.output"].Output = object
    sys.modules["sound_lib.effects"].Tempo = object

from core.utils import is_voice_message
from ui.conversations import ConversationsPanel


class _FakeI18n:
    def __init__(self, lang="pt-BR"):
        self.lang = lang
        self.dict = {
            "pt-BR": {
                "message_type_audio": "áudio",
                "message_type_voice_message": "mensagem de voz",
                "duration": "duração",
                "minute": "minuto",
                "minutes": "minutos",
                "second": "segundo",
                "seconds": "segundos",
                "and": "e",
                "photo": "foto",
                "video": "vídeo",
                "document": "documento",
                "sticker": "figurinha",
                "contact_label": "contato",
            },
            "en-US": {
                "message_type_audio": "audio",
                "message_type_voice_message": "voice message",
                "duration": "duration",
                "minute": "minute",
                "minutes": "minutes",
                "second": "second",
                "seconds": "seconds",
                "and": "and",
                "photo": "photo",
                "video": "video",
                "document": "document",
                "sticker": "sticker",
                "contact_label": "contact",
            }
        }

    def t(self, key):
        return self.dict.get(self.lang, {}).get(key, key)


class _FakeConvPanel:
    _get_message_content = ConversationsPanel._get_message_content
    _format_duration = ConversationsPanel._format_duration
    _get_quoted_preview = ConversationsPanel._get_quoted_preview
    _resolve_mentions_in_text = ConversationsPanel._resolve_mentions_in_text

class _FakeConvPanel(ConversationsPanel):
    def __init__(self, lang="pt-BR", vm_mode="voice_message"):
        self.main_window = types.SimpleNamespace(
            i18n=_FakeI18n(lang),
            settings={
                "accessibility": {"show_link_previews": True},
                "user_interface": {"voice_message_mode": vm_mode},
            },
        )
        self.contact_names = {}
        self._download_progress = {}


class TestIsVoiceMessageHelper:
    def test_voice_message_with_ptt_in_audio_message(self):
        msg = {
            "messageType": "audioMessage",
            "message": {"audioMessage": {"seconds": 72, "ptt": True}},
        }
        assert is_voice_message(msg) is True

    def test_voice_message_with_is_ptt(self):
        msg = {
            "messageType": "audioMessage",
            "message": {"audioMessage": {"seconds": 72, "isPtt": True}},
        }
        assert is_voice_message(msg) is True

    def test_voice_message_with_top_level_ptt(self):
        msg = {
            "type": "ptt",
            "message": {"audioMessage": {"seconds": 72}},
        }
        assert is_voice_message(msg) is True

    def test_voice_message_with_is_voice_recording(self):
        msg = {
            "messageType": "audioMessage",
            "_is_voice_recording": True,
            "message": {"audioMessage": {"seconds": 72}},
        }
        assert is_voice_message(msg) is True

    def test_generic_audio_file(self):
        msg = {
            "messageType": "audioMessage",
            "message": {"audioMessage": {"seconds": 72, "ptt": False}},
        }
        assert is_voice_message(msg) is False

    def test_the_bare_ptt_message_type_is_enough(self):
        """"ptt" IS the voice-note type. A record carrying only the type — no
        inner audioMessage to read a flag off — used to fall through to the
        flag check and be reported as a plain audio file, which was harmless
        while both were one media category and is not now that they are two."""
        assert is_voice_message({"messageType": "ptt"}) is True
        assert is_voice_message({"messageType": "ptt", "message": {}}) is True

    def test_a_bare_audio_message_type_is_not_a_voice_note(self):
        """The other half of the same guard: absent any ptt evidence, an
        audioMessage stays an audio file."""
        assert is_voice_message({"messageType": "audioMessage"}) is False
        assert is_voice_message({"messageType": "audio", "message": {}}) is False


class TestConversationGetMessageContent:
    def test_ptt_voice_message_content_pt_br_distinct_mode(self):
        panel = _FakeConvPanel("pt-BR", vm_mode="voice_message")
        msg = {
            "messageType": "audioMessage",
            "message": {"audioMessage": {"seconds": 72, "ptt": True}},
        }
        assert panel._get_message_content(msg) == "mensagem de voz, duração: 1 minuto e 12 segundos"

    def test_ptt_voice_message_content_pt_br_classic_audio_mode(self):
        panel = _FakeConvPanel("pt-BR", vm_mode="audio")
        msg = {
            "messageType": "audioMessage",
            "message": {"audioMessage": {"seconds": 72, "ptt": True}},
        }
        assert panel._get_message_content(msg) == "áudio, duração: 1 minuto e 12 segundos"

    def test_generic_audio_message_content_pt_br(self):
        panel = _FakeConvPanel("pt-BR", vm_mode="voice_message")
        msg = {
            "messageType": "audioMessage",
            "message": {"audioMessage": {"seconds": 72, "ptt": False}},
        }
        assert panel._get_message_content(msg) == "áudio, duração: 1 minuto e 12 segundos"

    def test_ptt_voice_message_content_en_us(self):
        panel = _FakeConvPanel("en-US", vm_mode="voice_message")
        msg = {
            "messageType": "audioMessage",
            "message": {"audioMessage": {"seconds": 72, "ptt": True}},
        }
        assert panel._get_message_content(msg) == "voice message, duration: 1 minute and 12 seconds"

    def test_generic_audio_message_content_en_us(self):
        panel = _FakeConvPanel("en-US", vm_mode="voice_message")
        msg = {
            "messageType": "audioMessage",
            "message": {"audioMessage": {"seconds": 72, "ptt": False}},
        }
        assert panel._get_message_content(msg) == "audio, duration: 1 minute and 12 seconds"


class TestQuotedAudioPreview:
    def test_quoted_ptt_preview_distinct_mode(self):
        panel = _FakeConvPanel("pt-BR", vm_mode="voice_message")
        quoted = {
            "messageType": "audioMessage",
            "audioMessage": {"ptt": True},
        }
        assert panel._get_quoted_preview(quoted) == "Mensagem de voz"

    def test_quoted_ptt_preview_classic_mode(self):
        panel = _FakeConvPanel("pt-BR", vm_mode="audio")
        quoted = {
            "messageType": "audioMessage",
            "audioMessage": {"ptt": True},
        }
        assert panel._get_quoted_preview(quoted) == "Áudio"

    def test_quoted_generic_audio_preview(self):
        panel = _FakeConvPanel("pt-BR", vm_mode="voice_message")
        quoted = {
            "messageType": "audioMessage",
            "audioMessage": {"ptt": False},
        }
        assert panel._get_quoted_preview(quoted) == "Áudio"


class TestMainWindowLastMsgPreview:
    def test_last_msg_preview_voice_message_distinct_mode(self):
        from main import MainWindow
        win = types.SimpleNamespace()
        win.i18n = _FakeI18n("pt-BR")
        win.settings = {"user_interface": {"voice_message_mode": "voice_message"}}
        win._counts_as_last_message = MainWindow._counts_as_last_message
        win._resolve_contact_name = lambda *a, **k: ""
        win._preview_sender_from_jid = lambda *a, **k: ""
        msg = {
            "key": {"fromMe": False},
            "messageType": "audioMessage",
            "message": {"audioMessage": {"seconds": 72, "ptt": True}},
            "messageTimestamp": 1000,
        }
        chat = {
            "messages": {"messages": {"records": [msg]}}
        }
        preview = MainWindow._last_msg_preview(win, chat)
        assert "mensagem de voz 1:12" in preview

    def test_last_msg_preview_voice_message_classic_audio_mode(self):
        from main import MainWindow
        win = types.SimpleNamespace()
        win.i18n = _FakeI18n("pt-BR")
        win.settings = {"user_interface": {"voice_message_mode": "audio"}}
        win._counts_as_last_message = MainWindow._counts_as_last_message
        win._resolve_contact_name = lambda *a, **k: ""
        win._preview_sender_from_jid = lambda *a, **k: ""
        msg = {
            "key": {"fromMe": False},
            "messageType": "audioMessage",
            "message": {"audioMessage": {"seconds": 72, "ptt": True}},
            "messageTimestamp": 1000,
        }
        chat = {
            "messages": {"messages": {"records": [msg]}}
        }
        preview = MainWindow._last_msg_preview(win, chat)
        assert "áudio 1:12" in preview

    def test_last_msg_preview_generic_audio(self):
        from main import MainWindow
        win = types.SimpleNamespace()
        win.i18n = _FakeI18n("pt-BR")
        win.settings = {"user_interface": {"voice_message_mode": "voice_message"}}
        win._counts_as_last_message = MainWindow._counts_as_last_message
        win._resolve_contact_name = lambda *a, **k: ""
        win._preview_sender_from_jid = lambda *a, **k: ""
        msg = {
            "key": {"fromMe": False},
            "messageType": "audioMessage",
            "message": {"audioMessage": {"seconds": 72, "ptt": False}},
            "messageTimestamp": 1000,
        }
        chat = {
            "messages": {"messages": {"records": [msg]}}
        }
        preview = MainWindow._last_msg_preview(win, chat)
        assert "áudio 1:12" in preview


class TestStatusAudioDistinction:
    def test_status_content_label_distinct_mode(self):
        from status_panel import _status_content_label
        i18n = _FakeI18n("pt-BR")
        settings = {"user_interface": {"voice_message_mode": "voice_message"}}
        ptt_obj = {"audioMessage": {"ptt": True}}
        audio_obj = {"audioMessage": {"ptt": False}}
        assert _status_content_label("audioMessage", ptt_obj, i18n, settings) == "mensagem de voz"
        assert _status_content_label("audioMessage", audio_obj, i18n, settings) == "áudio"

    def test_status_content_label_classic_audio_mode(self):
        from status_panel import _status_content_label
        i18n = _FakeI18n("pt-BR")
        settings = {"user_interface": {"voice_message_mode": "audio"}}
        ptt_obj = {"audioMessage": {"ptt": True}}
        audio_obj = {"audioMessage": {"ptt": False}}
        assert _status_content_label("audioMessage", ptt_obj, i18n, settings) == "áudio"
        assert _status_content_label("audioMessage", audio_obj, i18n, settings) == "áudio"


class TestTheDefaultMode:
    """The default is "voice_message": distinguishing a voice note from an
    audio file is the normal behaviour, not the opt-in one.

    It used to be "audio", and the switch is only half of the change — see
    TestTheDefaultModeMigration for the other half. What this class pins is
    that there is exactly *one* answer to "what does WinZapp do when nothing
    is saved": a `.get("voice_message_mode", ...)` fallback that disagrees
    with DEFAULT_SETTINGS is a silent per-call-site divergence, and there are
    nine of those fallback reads across five modules.
    """

    def test_the_default_distinguishes_voice_notes(self):
        from core.utils import DEFAULT_SETTINGS
        assert DEFAULT_SETTINGS["user_interface"]["voice_message_mode"] == "voice_message"

    def test_the_seed_file_agrees_with_it(self):
        import json
        from pathlib import Path
        from core.utils import DEFAULT_SETTINGS
        seed = json.loads(
            (Path(__file__).resolve().parents[1]
             / "client" / "data" / "settings_default.json").read_text(encoding="utf-8")
        )
        assert (seed["user_interface"]["voice_message_mode"]
                == DEFAULT_SETTINGS["user_interface"]["voice_message_mode"])

    def test_no_call_site_falls_back_to_a_different_mode(self):
        """Scans the client for a literal fallback rather than testing each
        one by hand: a new call site copied from an old one is exactly how the
        previous default would come back."""
        import re
        from pathlib import Path
        from core.utils import DEFAULT_SETTINGS

        expected = DEFAULT_SETTINGS["user_interface"]["voice_message_mode"]
        # Both quote styles, and \s matches the newline of the one call site
        # that wraps its arguments (settings_dialog._load_ui_page).
        pattern = re.compile(
            r"""['"]voice_message_mode['"]\s*,\s*['"]([a-z_]*)['"]""")
        client = Path(__file__).resolve().parents[1] / "client"
        divergent = []
        found_any = 0
        for path in client.rglob("*.py"):
            if "api" in path.parts or "node" in path.parts:
                continue
            for found in pattern.findall(path.read_text(encoding="utf-8")):
                found_any += 1
                if found != expected:
                    divergent.append(f"{path.name}: {found}")
        assert divergent == []
        # A scan that matches nothing passes vacuously, which is how a regex
        # broken by a reformat would go unnoticed.
        assert found_any >= 8, f"the scan matched only {found_any} call sites"


class TestTheDefaultModeMigration:
    """core.utils.migrate_voice_message_mode_default().

    Changing the default alone would have reached nobody: settings.json is
    seeded from settings_default.json, so every existing install has the
    literal old value saved. The users who asked for this change are exactly
    the ones with such a file.
    """

    @staticmethod
    def _settings(mode=..., **general):
        settings = {"user_interface": {}, "general": dict(general)}
        if mode is not ...:
            settings["user_interface"]["voice_message_mode"] = mode
        return settings

    def test_a_saved_audio_becomes_voice_message(self):
        from core.utils import migrate_voice_message_mode_default
        settings = self._settings("audio")
        assert migrate_voice_message_mode_default(settings) is True
        assert settings["user_interface"]["voice_message_mode"] == "voice_message"

    def test_it_records_that_it_ran(self):
        from core.utils import (VOICE_MESSAGE_MODE_MIGRATION_FLAG,
                                migrate_voice_message_mode_default)
        settings = self._settings("audio")
        migrate_voice_message_mode_default(settings)
        assert settings["general"][VOICE_MESSAGE_MODE_MIGRATION_FLAG] is True

    def test_it_only_runs_once(self):
        """The whole point of the flag: a user who goes back to Configuracoes
        and picks "Audio" again must keep it."""
        from core.utils import migrate_voice_message_mode_default
        settings = self._settings("audio")
        migrate_voice_message_mode_default(settings)
        settings["user_interface"]["voice_message_mode"] = "audio"   # user's choice
        assert migrate_voice_message_mode_default(settings) is False
        assert settings["user_interface"]["voice_message_mode"] == "audio"

    def test_a_file_already_on_the_new_mode_only_gains_the_flag(self):
        from core.utils import migrate_voice_message_mode_default
        settings = self._settings("voice_message")
        assert migrate_voice_message_mode_default(settings) is True
        assert settings["user_interface"]["voice_message_mode"] == "voice_message"

    def test_a_missing_value_is_left_absent(self):
        """backfill_missing_defaults() writes the new default straight after;
        inserting it here would only duplicate that."""
        from core.utils import migrate_voice_message_mode_default
        settings = self._settings()
        assert migrate_voice_message_mode_default(settings) is True
        assert "voice_message_mode" not in settings["user_interface"]

    @pytest.mark.parametrize("junk", ["banana", "", 3, None, ["audio"]])
    def test_an_unrecognized_value_is_not_rewritten(self, junk):
        from core.utils import migrate_voice_message_mode_default
        settings = self._settings(junk)
        assert migrate_voice_message_mode_default(settings) is True
        assert settings["user_interface"]["voice_message_mode"] == junk

    def test_a_missing_section_does_not_raise(self):
        from core.utils import migrate_voice_message_mode_default
        for settings in ({}, {"user_interface": None}, {"general": "texto"}):
            assert migrate_voice_message_mode_default(settings) is True

    def test_junk_settings_are_refused(self):
        from core.utils import migrate_voice_message_mode_default
        assert migrate_voice_message_mode_default(None) is False
        assert migrate_voice_message_mode_default("texto") is False

    def test_it_has_its_own_flag(self):
        """Separate from the media-types migration: an install that already
        ran that one must still be moved onto the new default."""
        from core.utils import (VOICE_MEDIA_TYPE_MIGRATION_FLAG,
                                VOICE_MESSAGE_MODE_MIGRATION_FLAG,
                                migrate_voice_message_mode_default)
        assert VOICE_MESSAGE_MODE_MIGRATION_FLAG != VOICE_MEDIA_TYPE_MIGRATION_FLAG
        settings = self._settings("audio", **{VOICE_MEDIA_TYPE_MIGRATION_FLAG: True})
        assert migrate_voice_message_mode_default(settings) is True
        assert settings["user_interface"]["voice_message_mode"] == "voice_message"

    def test_a_second_launch_keeps_what_the_user_picked_afterwards(self):
        """Both migrations end to end through MainWindow._migrate_settings(),
        over a settings.json that carries no flags — i.e. the state a real
        install is in the moment it updates, not a pre-flagged fixture.

        The launch after the update must change nothing and save nothing:
        every value here is byte-identical to what the migration produces,
        so only the flags tell "the user picked this" from "we never ran".
        """
        from main import MainWindow

        class _Stub:
            _migrate_settings = MainWindow._migrate_settings

            def __init__(self, settings):
                self.settings = settings
                self.save_calls = 0

            def save_settings(self):
                self.save_calls += 1

        settings = {
            "user_interface": {"voice_message_mode": "audio",
                               "group_media_default_types": ["photos", "audios"]},
            "storage": {"auto_download_media_types": ["audios"]},
            "general": {},
        }
        mw = _Stub(settings)
        mw._migrate_settings()
        assert settings["user_interface"]["voice_message_mode"] == "voice_message"
        assert settings["user_interface"]["group_media_default_types"] == [
            "photos", "audios", "voice_messages"]
        assert settings["storage"]["auto_download_media_types"] == [
            "audios", "voice_messages"]
        assert mw.save_calls == 1

        # The user goes to Configuracoes and puts both back the way they were.
        settings["user_interface"]["voice_message_mode"] = "audio"
        settings["user_interface"]["group_media_default_types"] = ["photos", "audios"]
        settings["storage"]["auto_download_media_types"] = ["audios"]

        mw._migrate_settings()          # next launch

        assert settings["user_interface"]["voice_message_mode"] == "audio"
        assert settings["user_interface"]["group_media_default_types"] == [
            "photos", "audios"]
        assert settings["storage"]["auto_download_media_types"] == ["audios"]
        assert mw.save_calls == 1, "an already-migrated settings.json was rewritten"

    def test_the_loader_runs_it_and_saves(self):
        """A migration that runs without reaching disk re-runs every launch —
        and would undo the user's choice each time. _migrate_settings() is
        where it belongs, and its save_settings() call is what makes it
        one-shot."""
        import inspect
        from main import MainWindow
        src = inspect.getsource(MainWindow._migrate_settings)
        assert "migrate_voice_message_mode_default(self.settings)" in src
        assert "changed = True" in src
        assert "self.save_settings()" in src


class _KeyEchoI18n:
    """Returns the key itself, so an assertion names the string it expects
    instead of a translation that could be reworded without failing."""

    def t(self, key):
        return key


class _MwWithMode:
    """format_notification_body() only ever reads main_window.settings here."""

    def __init__(self, mode=None):
        ui = {} if mode is None else {"voice_message_mode": mode}
        self.settings = {"user_interface": ui}


def _audio_msg(ptt):
    inner = {"seconds": 5}
    if ptt:
        inner["ptt"] = True
    return {"messageType": "audioMessage", "message": {"audioMessage": inner}}


class TestTheToastFollowsTheSameSetting:
    """The toast is often the ONLY announcement a backgrounded message gets, so
    it is the worst place for the audio/voice label to be the one that
    disagrees with the rest of the app. It used to say "voice message" for
    every audioMessage, whatever the user had picked in Settings."""

    def test_an_audio_file_is_not_announced_as_a_voice_note(self):
        from core.notification_manager import format_notification_body
        body = format_notification_body(
            _audio_msg(ptt=False), _MwWithMode("voice_message"), _KeyEchoI18n())
        assert body.startswith("notif_audio")

    def test_a_voice_note_still_is_one(self):
        from core.notification_manager import format_notification_body
        body = format_notification_body(
            _audio_msg(ptt=True), _MwWithMode("voice_message"), _KeyEchoI18n())
        assert body.startswith("notif_voice_message")

    @pytest.mark.parametrize("ptt", [True, False])
    def test_audio_mode_reads_both_as_audio_the_way_it_always_did(self, ptt):
        """Picking "Audio (read them all as audio)" is a choice the user can
        still make, and it has to reach the toast too — otherwise the setting
        means one thing in the conversation and another in the banner."""
        from core.notification_manager import format_notification_body
        body = format_notification_body(
            _audio_msg(ptt), _MwWithMode("audio"), _KeyEchoI18n())
        assert body.startswith("notif_voice_message")

    def test_the_duration_survives(self):
        from core.notification_manager import format_notification_body
        body = format_notification_body(
            _audio_msg(ptt=False), _MwWithMode("voice_message"), _KeyEchoI18n())
        assert body == "notif_audio (0:05)"

    @pytest.mark.parametrize("mw", [
        None,                       # reachable, and what other tests pass
        _MwWithMode(None),          # settings.json predating the option
    ])
    def test_no_settings_falls_back_to_the_default(self, mw):
        """The fallback has to agree with DEFAULT_SETTINGS, or a missing value
        would announce the opposite of what a saved one does."""
        from core.notification_manager import format_notification_body
        from core.utils import DEFAULT_SETTINGS
        assert DEFAULT_SETTINGS["user_interface"]["voice_message_mode"] == "voice_message"
        body = format_notification_body(_audio_msg(ptt=False), mw, _KeyEchoI18n())
        assert body.startswith("notif_audio")

    def test_the_new_key_is_in_every_locale(self):
        """A key missing from a language file renders as the raw key name in
        the UI, so "notif_audio" would be read aloud verbatim."""
        import json
        import pathlib
        langs = pathlib.Path("client/languages")
        for code in ("pt-BR", "pt-PT", "en-US", "es-ES", "pl"):
            data = json.loads((langs / f"{code}.json").read_text(encoding="utf-8"))
            assert data.get("notif_audio"), f"notif_audio missing from {code}"
            assert data["notif_audio"] != data["notif_voice_message"], (
                f"{code} gives the audio and voice toasts the same text, "
                "which is the bug this fixes")
