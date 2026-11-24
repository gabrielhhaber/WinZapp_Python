"""A transcription in the message's own line, and the key that decides how.

Alt+Shift+N is the action Enter and Space do not do on a voice message: with
"Enter and Space transcribe" off (Enter plays) it transcribes the message into
its line of the list; with it on, it plays or pauses the message and Enter and
Space transcribe. ConversationsPanel needs a wx.App, so the methods run against
a plain stub, and nothing here opens a window.
"""

import ast
import pathlib
import re
import types

import pytest

from core.transcription import stored
from tests.locales import load_strings, registered_locale_codes
from ui import transcription_flow
from ui.conversations import ConversationsPanel

CLIENT = pathlib.Path(__file__).resolve().parents[1] / "client"
LOCALES = list(registered_locale_codes())
TEXT = "olá, tudo bem com você"


class _I18n:
    def __init__(self, locale="pt-BR"):
        self.language = locale
        self._table = load_strings(locale)

    def t(self, key):
        return self._table[key]


def _voice(inline=True, text=TEXT, seconds=12, msg_id="A1"):
    msg = {
        "key": {"id": msg_id, "remoteJid": "1@s.whatsapp.net", "fromMe": False},
        "messageType": "audioMessage",
        "message": {"audioMessage": {"seconds": seconds, "ptt": True,
                                     "mimetype": "audio/ogg; codecs=opus"}},
    }
    if text is not None:
        msg[stored.TRANSCRIPTION_KEY] = {"text": text, "at": 1.0, **({"inline": True} if inline else {})}
    return msg


class _Stub:
    _get_message_content = ConversationsPanel._get_message_content
    _format_duration = ConversationsPanel._format_duration
    _enter_transcribes_voice = ConversationsPanel._enter_transcribes_voice
    activate_message = ConversationsPanel.activate_message
    _space_toggles_playback = ConversationsPanel._space_toggles_playback
    _is_separator = ConversationsPanel._is_separator
    _on_menu_toggle_voice_action = ConversationsPanel._on_menu_toggle_voice_action
    _on_accel_transcribe_inline = ConversationsPanel._on_accel_transcribe_inline
    _video_default_action = ConversationsPanel._video_default_action

    def __init__(self, enter_transcribes=False, locale="pt-BR"):
        ui = {"enter_transcribes_voice_messages": True} if enter_transcribes else {}
        self.main_window = types.SimpleNamespace(
            i18n=_I18n(locale), app_name="WinZapp", settings={"user_interface": ui}
        )
        self.played = []
        self.transcribed = []
        self._sorted_messages = []
        self.messages_list = types.SimpleNamespace(GetFirstSelected=lambda: 0)
        # Nothing selected: the bulk form (issue #469) is not in play.
        self.selected_messages = set()

    def _bulk_shortcuts_enabled(self):
        return True

    def _toggle_audio_message_playback(self, msg):
        self.played.append(msg["key"]["id"])

    def _on_menu_transcribe_inline(self, msg):
        self.transcribed.append(msg["key"]["id"])

    def _use_conversation_video_media_viewer_dialog(self):
        return True

    def open_media_viewer_for_message(self, msg, restore_index=None):
        self.played.append("viewer:" + msg["key"]["id"])

    def _play_toggle_video_message(self, msg):
        self.played.append("inplace:" + msg["key"]["id"])


def _video(text=TEXT, inline=True, gif=False, msg_id="V1"):
    msg = {
        "key": {"id": msg_id, "remoteJid": "1@s.whatsapp.net", "fromMe": False},
        "messageType": "videoMessage",
        "message": {"videoMessage": {"seconds": 30, "mimetype": "video/mp4",
                                     **({"gifPlayback": True} if gif else {})}},
    }
    if text is not None:
        msg[stored.TRANSCRIPTION_KEY] = {"text": text, "at": 1.0, **({"inline": True} if inline else {})}
    return msg


class TestVideos:
    def test_the_row_carries_the_inline_text(self):
        row = _Stub()._get_message_content(_video())
        assert row.endswith(f"{_I18n().t('transcription_row_label')}: {TEXT}")

    def test_by_default_enter_opens_it_and_the_key_transcribes(self):
        panel = _Stub()
        panel.activate_message(_video(text=None))
        panel._on_menu_toggle_voice_action(_video(text=None))
        assert panel.played == ["viewer:V1"] and panel.transcribed == ["V1"]

    def test_set_to_transcribe_enter_transcribes_and_the_key_opens_it(self):
        panel = _Stub(enter_transcribes=True)
        panel.activate_message(_video(text=None))
        panel._on_menu_toggle_voice_action(_video(text=None))
        assert panel.transcribed == ["V1"] and panel.played == ["viewer:V1"]

    def test_space_transcribes_when_set_to(self):
        panel = _Stub(enter_transcribes=True)
        assert panel._space_toggles_playback(_video(text=None)) is True
        assert panel.transcribed == ["V1"]

    def test_classic_mode_plays_it_in_place(self):
        panel = _Stub(enter_transcribes=True)
        panel._use_conversation_video_media_viewer_dialog = lambda: False
        panel._on_menu_toggle_voice_action(_video(text=None))
        assert panel.played == ["inplace:V1"]

    def test_a_gif_is_never_transcribed_by_enter(self):
        panel = _Stub(enter_transcribes=True)
        panel.activate_message(_video(text=None, gif=True))
        assert panel.transcribed == []


def _audio_document(inline=True, text=TEXT):
    msg = {
        "key": {"id": "D1", "remoteJid": "1@s.whatsapp.net", "fromMe": False},
        "messageType": "documentMessage",
        "message": {"documentMessage": {"fileName": "aula.mp3", "mimetype": "audio/mpeg"}},
    }
    if text is not None:
        msg[stored.TRANSCRIPTION_KEY] = {"text": text, "at": 1.0, **({"inline": True} if inline else {})}
    return msg


class TestAudioDocuments:
    """An mp3 sent as a document is transcribed like a voice note (is_transcribable),
    so Alt+Shift+N's text has to reach its line too."""

    def _stub(self):
        stub = _Stub()
        stub._download_progress = {}
        stub._format_filesize = lambda size: ""
        return stub

    def test_the_row_carries_the_inline_text(self):
        row = self._stub()._get_message_content(_audio_document())
        assert row.endswith(f"aula.mp3, {_I18n().t('transcription_row_label')}: {TEXT}")

    def test_one_opened_in_the_window_leaves_the_row_short(self):
        row = self._stub()._get_message_content(_audio_document(inline=False))
        assert _I18n().t("transcription_row_label") not in row

    def _opening(self, enter_transcribes):
        stub = _Stub(enter_transcribes=enter_transcribes)
        stub.open_media_message = lambda msg: stub.played.append("open:" + msg["key"]["id"])
        return stub

    def test_by_default_enter_opens_it_and_the_key_transcribes(self):
        panel = self._opening(False)
        panel.activate_message(_audio_document(text=None))
        panel._on_menu_toggle_voice_action(_audio_document(text=None))
        assert panel.played == ["open:D1"] and panel.transcribed == ["D1"]

    def test_set_to_transcribe_enter_and_space_transcribe_and_the_key_opens_it(self):
        """Reported: with the setting on, Enter still played an audio file
        sent as a document, and Alt+Shift+N still transcribed it."""
        panel = self._opening(True)
        panel.activate_message(_audio_document(text=None))
        assert panel._space_toggles_playback(_audio_document(text=None)) is True
        panel._on_menu_toggle_voice_action(_audio_document(text=None))
        assert panel.transcribed == ["D1", "D1"] and panel.played == ["open:D1"]

    def test_space_never_opens_a_document(self):
        panel = self._opening(False)
        assert panel._space_toggles_playback(_audio_document(text=None)) is False
        assert panel.played == []

    def test_a_pdf_is_never_transcribed_by_enter(self):
        panel = self._opening(True)
        pdf = _audio_document(text=None)
        pdf["message"]["documentMessage"] = {"fileName": "a.pdf", "mimetype": "application/pdf"}
        panel.activate_message(pdf)
        assert panel.transcribed == [] and panel.played == ["open:D1"]

    def test_the_key_on_a_pdf_still_says_it_has_no_audio(self):
        """Reviewer: with the setting on, Alt+Shift+N opened a PDF too, the
        same as Enter; only an audio or video sent as a file swaps keys."""
        from ui.conversation_panel.message_menu import inline_toggle_menu_key
        panel = self._opening(True)
        pdf = _audio_document(text=None)
        pdf["message"]["documentMessage"] = {"fileName": "a.pdf", "mimetype": "application/pdf"}
        panel._on_menu_toggle_voice_action(pdf)
        assert panel.played == [] and panel.transcribed == ["D1"]
        assert inline_toggle_menu_key(pdf, True) == "transcribe_inline_menu"

    def test_the_menu_names_what_the_key_does(self):
        from ui.conversation_panel.message_menu import inline_toggle_menu_key
        assert inline_toggle_menu_key(_audio_document(), True) == "open"
        assert inline_toggle_menu_key(_audio_document(), False) == "transcribe_inline_menu"


class TestReadMoreOnALongDocumentLine:
    def test_the_button_offers_the_rest_of_the_line(self):
        long_text = "palavra " * 200
        shown = []

        class _Button:
            def Show(self):
                shown.append(True)

            def Hide(self):
                shown.append(False)

        stub = types.SimpleNamespace(
            _message_list_mode="classic",
            _sorted_messages=[_audio_document(text=long_text)],
            _is_separator=lambda msg: False,
            _render_message_line=lambda msg: "x" * 600,
            _LIST_CTRL_TEXT_LIMIT=511,
            _read_more_btn=_Button(),
            main_window=types.SimpleNamespace(settings={}),
            conversation_panel=types.SimpleNamespace(Layout=lambda: None),
        )
        ConversationsPanel._update_read_more_button(stub, 0)
        assert shown == [True]
        assert stub._read_more_remainder == "x" * 89


class TestTheRow:
    def test_the_text_follows_the_duration(self):
        row = _Stub()._get_message_content(_voice())
        i18n = _I18n()
        assert row == (
            f"{i18n.t('message_type_voice_message')}, {i18n.t('duration')}: "
            f"{_Stub()._format_duration(12)}, {i18n.t('transcription_row_label')}: {TEXT}"
        )

    def test_a_transcription_opened_in_the_window_leaves_the_row_short(self):
        row = _Stub()._get_message_content(_voice(inline=False))
        assert _I18n().t("transcription_row_label") not in row

    def test_no_transcription_no_clause(self):
        row = _Stub()._get_message_content(_voice(text=None))
        assert _I18n().t("transcription_row_label") not in row

    def test_a_note_of_unknown_length_still_carries_its_text(self):
        row = _Stub()._get_message_content(_voice(seconds=None))
        i18n = _I18n()
        assert row == (
            f"{i18n.t('message_type_voice_message')}, "
            f"{i18n.t('transcription_row_label')}: {TEXT}"
        )


class TestEnterAndSpace:
    def test_by_default_they_play(self):
        panel = _Stub()
        panel.activate_message(_voice())
        assert panel.played == ["A1"] and panel.transcribed == []

    def test_set_to_transcribe_they_transcribe_instead(self):
        panel = _Stub(enter_transcribes=True)
        panel.activate_message(_voice())
        assert panel.transcribed == ["A1"] and panel.played == []

    def test_space_follows_the_same_choice(self):
        panel = _Stub(enter_transcribes=True)
        assert panel._space_toggles_playback(_voice()) is True
        assert panel.transcribed == ["A1"] and panel.played == []

    def test_only_an_explicit_true_changes_anything(self):
        panel = _Stub()
        panel.main_window.settings["user_interface"]["enter_transcribes_voice_messages"] = "yes"
        panel.activate_message(_voice())
        assert panel.played == ["A1"]


class TestAltShiftN:
    def test_with_enter_playing_it_transcribes_into_the_line(self):
        panel = _Stub()
        panel._on_menu_toggle_voice_action(_voice())
        assert panel.transcribed == ["A1"] and panel.played == []

    def test_with_enter_transcribing_it_plays_instead(self):
        panel = _Stub(enter_transcribes=True)
        panel._on_menu_toggle_voice_action(_voice())
        assert panel.played == ["A1"] and panel.transcribed == []

    def test_the_two_keys_never_do_the_same_thing(self):
        for setting in (False, True):
            panel = _Stub(enter_transcribes=setting)
            panel.activate_message(_voice())
            panel._on_menu_toggle_voice_action(_voice())
            assert sorted([panel.played, panel.transcribed]) == [["A1"], ["A1"]]

    def test_the_key_acts_on_the_focused_message(self):
        panel = _Stub()
        panel._sorted_messages = [_voice(msg_id="B2")]
        panel._on_accel_transcribe_inline(None)
        assert panel.transcribed == ["B2"]


class TestWhatIsSaid:
    def test_the_text_then_the_voice_filter_warning(self):
        from core.transcription import narration

        i18n = _I18n("en-US")
        note = narration.Note(narration.VAD_UNAVAILABLE_I18N_KEY)
        sentence = transcription_flow.inline_speech(i18n, "hello\nthere", (note,))
        assert sentence.startswith("Transcription: hello there")
        assert i18n.t(narration.VAD_UNAVAILABLE_I18N_KEY) in sentence

    def test_without_a_warning_it_is_only_the_text(self):
        assert transcription_flow.inline_speech(_I18n("en-US"), "hi", ()) == (
            "Transcription: hi"
        )


class TestTheWiring:
    def test_the_key_is_in_the_messages_table_and_bound(self):
        # Spacing-tolerant: the table is column-aligned, and a reformat must
        # not fail this.
        source = (CLIENT / "ui/conversation_panel/accelerators.py").read_text(encoding="utf-8")
        assert re.search(r'ord\("N"\),\s*self\.ID_ALT_SHIFT_N', source)
        assert re.search(r"self\._on_accel_transcribe_inline,\s*id=self\.ID_ALT_SHIFT_N", source)

    def test_it_is_in_the_catalogue_with_alt_shift_n(self):
        from core.shortcut_catalog import SHORTCUTS

        row = next(s for s in SHORTCUTS if s.id == "messages.ID_ALT_SHIFT_N")
        assert (row.mod, row.key) == (5, ord("N"))
        assert row.label == "shortcut_alt_shift_n_label"

    def test_the_f1_list_describes_it(self):
        source = (CLIENT / "ui/dialogs/shortcuts_dialog.py").read_text(encoding="utf-8")
        assert 'i18n.t("shortcut_alt_shift_n_label")' in source

    def test_the_menu_item_names_the_action_the_setting_leaves(self):
        source = (CLIENT / "ui/conversation_panel/message_menu.py").read_text(encoding="utf-8")
        assert '"play_voice_message_menu"' in source
        assert '"transcribe_inline_menu"' in source
        assert "\\tAlt+Shift+N" in source

    def test_the_setting_is_off_by_default_in_both_defaults(self):
        import json

        from core.utils import DEFAULT_SETTINGS

        assert DEFAULT_SETTINGS["user_interface"]["enter_transcribes_voice_messages"] is False
        defaults = json.loads((CLIENT / "data/settings_default.json").read_text(encoding="utf-8"))
        assert defaults["user_interface"]["enter_transcribes_voice_messages"] is False


@pytest.mark.parametrize("locale", LOCALES)
class TestTheStringsExplainIt:
    def test_the_checkbox_names_both_keys_and_both_ways(self, locale):
        label = load_strings(locale)["ui_enter_transcribes_voice"]
        assert label.count("Alt+Shift+N") == 2
        assert "Enter" in label

    def test_the_f1_line_and_the_menu_say_local(self, locale):
        table = load_strings(locale)
        words = {"tr-TR": ("yerel", "bu bilgisayarda"), "pl": ("lokaln",), "ar": ("محلي",), "cs": ("místn",)}.get(locale, ("local",))
        for key in ("transcribe_inline_menu", "shortcut_alt_shift_n_label"):
            assert any(w in table[key].lower() for w in words), (locale, key)

    def test_the_spoken_sentence_carries_the_text(self, locale):
        assert "{text}" in load_strings(locale)["transcription_inline_spoken"]


def test_online_result_in_the_line_survives_a_selected_offline_model():
    # An online provider's text, put in the line from the AI result window,
    # is shown whichever offline model is selected: the user asked for that
    # exact text and the offline selection says nothing about it.
    from core.transcription import stored

    record = stored.record_from_result(
        type("R", (), {"text": "ola   mundo\nfim", "language": None,
                       "language_probability": None, "model_id": "gemini",
                       "backend": stored.ONLINE_BACKEND, "vad_used": True})(),
        100.0, inline=True)
    msg = {stored.TRANSCRIPTION_KEY: record}
    for selection in (None, ("auto", "auto"), ("faster-whisper", "small")):
        assert stored.inline_row_text(msg, selection) == "ola mundo fim"


def test_online_result_keeps_the_offline_one_under_other_models():
    from core.transcription import stored

    offline = {"text": "offline", "model_id": "small", "backend": "faster-whisper",
               "at": 1.0, "inline": True}
    online = stored.record_from_result(
        type("R", (), {"text": "online", "language": None, "language_probability": None,
                       "model_id": "openai", "backend": stored.ONLINE_BACKEND,
                       "vad_used": True})(), 2.0, inline=True)
    both = stored.with_result(offline, online)
    assert both["text"] == "online"
    assert stored.saved_transcription({stored.TRANSCRIPTION_KEY: both},
                                      ("faster-whisper", "small"))["text"] == "online"
    assert [r["text"] for r in stored.results_of(both)] == ["online", "offline"]


def test_a_description_in_the_line_of_a_picture_a_sticker_and_a_video():
    from core.transcription import stored

    for msg_type in ("imageMessage", "stickerMessage", "videoMessage"):
        assert stored.can_carry_transcription({"messageType": msg_type})
    record = stored.record_from_result(
        type("R", (), {"text": "um gato\nna janela", "language": None,
                       "language_probability": None, "model_id": "gemini",
                       "backend": stored.ONLINE_BACKEND, "vad_used": True})(),
        10.0, inline=True, kind=stored.DESCRIPTION_KIND)
    msg = {"messageType": "imageMessage", stored.TRANSCRIPTION_KEY: record}
    assert stored.inline_row_text(msg) == "um gato na janela"
    assert stored.inline_row_label_key(msg) == "description_row_label"
    plain = {"messageType": "audioMessage", stored.TRANSCRIPTION_KEY: dict(record, kind=None)}
    assert stored.inline_row_label_key(plain) == "transcription_row_label"


def test_a_picture_keeps_its_description_through_a_resync_copy():
    from core.transcription import stored

    record = {"text": "um gato", "at": 5.0, "inline": True, "kind": "description"}
    old = {"messageType": "imageMessage", "key": {"id": "X"}}
    kept = stored.with_known_transcription(old, {stored.TRANSCRIPTION_KEY: record})
    assert kept[stored.TRANSCRIPTION_KEY]["text"] == "um gato"


def test_media_tab_video_follows_the_enter_setting():
    """The data dialog's Media tab sent every video to the viewer before the
    panel could look at the setting."""
    from ui.dialogs.conversation_data_dialog import ConversationDataDialog
    for enter_transcribes, expected in ((True, ["activate:V1"]), (False, ["viewer:V1"])):
        panel = _Stub(enter_transcribes=enter_transcribes)
        calls = []
        panel.open_media_viewer_for_message = lambda msg: calls.append("viewer:" + msg["key"]["id"])
        panel.activate_message = lambda msg: calls.append("activate:" + msg["key"]["id"])
        dialog = types.SimpleNamespace(
            _selected_media_message=lambda: _video(text=None),
            _conversation_panel=lambda: panel,
            _panel_is_on_this_chat=lambda: True,
            _invoke=lambda call: call(),
        )
        ConversationDataDialog._on_media_activated(dialog, None)
        assert calls == expected
