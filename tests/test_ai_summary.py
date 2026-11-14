"""AI summary of chat messages (kind "text"): the pure logic, the provider
payloads, the consent and the shortcut. Nothing here opens a window or reaches
the network; the panel's behaviour is in tests/test_ai_summary_panel.py.

Pins: the transcript keeps the newest messages and counts what did not fit
(nothing is cut silently); a voice message carries its transcription or says it
has none; the summary has no unread threshold; a chat summary asks its own
consent, remembered apart from media's and never for a locked chat, and a
removed provider loses both; Ctrl+Shift+U is bound once and collides with
nothing in its scope.
"""
import base64
import json
from types import SimpleNamespace as NS

import pytest
import wx

from core.ai_media import config, service
from core.ai_media.payload import Media, prepare_media
from core.ai_media.prompts import first_question, instructions
from core.ai_media.providers import build_request
from core.ai_media.summary import (MAX_VOICE_TRANSCRIPTIONS, VOICE_MARKER, build_unread_transcript,
                                   message_text, unread_fetch_limit, unread_messages, voice_text,
                                   voice_to_transcribe)
from core.keyboard_shortcuts import ALT, CTRL, SHIFT, default_binding, scopes_overlap
from core.shortcut_catalog import SHORTCUTS
from ui.dialogs.ai_result_dialog import AIResultDialog, consent_text

TRANSCRIPT = "Ana: sale el parcial el viernes\nLuis: yo llevo las fotocopias"
TEXT = Media("text", TRANSCRIPT.encode(), "text/plain; charset=utf-8", "unread-messages.txt")


def _msg(text=None, msg_type=None, from_me=False, caption=None, number=1):
    body = {}
    if text is not None:
        body["conversation"] = text
    if caption is not None and msg_type:
        body[msg_type] = {"caption": caption}
    return {"messageType": msg_type or "conversation",
            "key": {"fromMe": from_me, "remoteJid": "x@s.whatsapp.net", "id": f"id{number}"},
            "message": body}


def _line(msg, text):
    return f"{'Me' if msg['key']['fromMe'] else 'Ana'}: {text}"


def _build(messages, **limits):
    return build_unread_transcript(messages, _line, **limits)


class TestTranscript:
    def test_lines_oldest_first_with_the_panels_formatting(self):
        built = _build([_msg("hola  \n mundo"), _msg("bien", from_me=True)])
        assert built.text == "Ana: hola mundo\nMe: bien"
        assert (built.included, built.left_out, built.voice_markers) == (2, 0, 0)

    def test_media_without_caption_is_a_placeholder_and_a_caption_travels(self):
        built = _build([_msg(msg_type="imageMessage"), _msg(msg_type="videoMessage", caption="la fiesta")])
        assert built.text == "Ana: [Image]\nAna: la fiesta"

    def test_reactions_and_protocol_events_are_not_messages(self):
        built = _build([_msg(msg_type="reactionMessage"), _msg(msg_type="protocolMessage"),
                        _msg(msg_type="somethingNew")])
        assert built == ("", 0, 0, 0)

    def test_the_old_end_is_dropped_and_counted_by_the_character_cap(self):
        built = _build([_msg("a" * 40, number=n) for n in range(5)], max_chars=100)
        assert built.included == 2 and built.left_out == 3
        assert built.text.count("\n") == 1 and len(built.text) <= 100

    def test_the_old_end_is_dropped_and_counted_by_the_message_cap(self):
        built = _build([_msg(f"m{n}", number=n) for n in range(10)], max_messages=3)
        assert built.text.splitlines() == ["Ana: m7", "Ana: m8", "Ana: m9"]
        assert (built.included, built.left_out) == (3, 7)

    def test_the_default_caps_are_200_messages_and_15000_characters(self):
        built = _build([_msg("x", number=n) for n in range(250)])
        assert (built.included, built.left_out) == (200, 50)
        built = _build([_msg("y" * 1000, number=n) for n in range(30)])
        assert len(built.text) <= 15_000 and built.left_out > 0

    def test_one_huge_message_is_marked_and_bounded(self):
        built = _build([_msg("z" * 500)], max_chars=100)
        assert len(built.text) == 100 and built.text.endswith("[truncated]")

    @pytest.mark.parametrize("limits", [{"max_messages": 0}, {"max_chars": 0}, {"max_messages": -1}])
    def test_a_zero_limit_never_sends_text(self, limits):
        assert _build([_msg("hola")], **limits) == ("", 0, 0, 0)

    def test_a_line_that_cannot_be_formatted_falls_back_to_the_bare_text(self):
        def broken(msg, text):
            raise ValueError("synthetic")
        assert build_unread_transcript([_msg("hola")], broken).text == "hola"

    def test_voice_messages_carry_their_words_or_count_as_markers(self):
        voice = _msg(msg_type="audioMessage", number=2)
        silent = _msg(msg_type="audioMessage", number=3)
        spoken = {"id2": "llego tarde"}

        def text_of(msg):
            if msg["messageType"] == "audioMessage":
                return voice_text(spoken.get(msg["key"]["id"]))
            return message_text(msg)
        built = build_unread_transcript([_msg("hola"), voice, silent], _line, text_of)
        assert built.text.splitlines() == [
            "Ana: hola", f"Ana: {VOICE_MARKER} llego tarde", f"Ana: {VOICE_MARKER}"]
        assert built.voice_markers == 1

    def test_a_marker_left_out_by_the_caps_is_not_counted(self):
        old = _msg(msg_type="audioMessage", number=1)
        built = build_unread_transcript([old, _msg("nuevo", number=2)], _line, max_messages=1)
        assert (built.included, built.left_out, built.voice_markers) == (1, 1, 0)


class TestWhichMessages:
    def _rows(self, count):
        return [_msg(f"m{n}", number=n) for n in range(count)]

    def test_from_the_divider_to_the_newest(self):
        rows = self._rows(6)
        assert [m["key"]["id"] for m in unread_messages(rows, 2)] == ["id4", "id5"]

    def test_my_reply_among_the_unread_stays(self):
        rows = self._rows(5)
        rows[3]["key"]["fromMe"] = True  # not counted as unread, but inside the stretch
        assert [m["key"]["id"] for m in unread_messages(rows, 3)] == ["id1", "id2", "id3", "id4"]

    def test_nothing_unread(self):
        assert unread_messages(self._rows(3), 0) == []
        assert unread_messages([], 5) == []

    def test_fewer_stored_than_unread_summarizes_everything_stored(self):
        assert len(unread_messages(self._rows(3), 40)) == 3

    def test_the_fetch_reaches_above_the_unread_ones_and_is_capped(self):
        assert unread_fetch_limit(10) == 60
        assert unread_fetch_limit(0) == 50
        assert unread_fetch_limit(10 ** 9) == 2000
        assert unread_fetch_limit(None) == 50

    def test_only_the_newest_voice_messages_without_words_are_transcribed(self):
        rows = [_msg(msg_type="audioMessage", number=n) for n in range(30)]
        rows.insert(5, _msg("texto", number=99))
        known = {"id29"}
        run = voice_to_transcribe(rows, lambda m: m["key"]["id"] in known)
        assert len(run) == MAX_VOICE_TRANSCRIPTIONS == 20
        assert run[-1]["key"]["id"] == "id28" and run[0]["key"]["id"] == "id9"
        assert voice_to_transcribe(rows, lambda m: True) == []
        assert voice_to_transcribe(rows, lambda m: False, limit=0) == []


class TestTextKind:
    def test_every_provider_takes_text(self):
        for provider in config.PROVIDERS:
            assert config.supports(provider, "text"), provider

    def test_the_result_window_asks_follow_up_questions_for_a_summary(self):
        assert config.asks_questions("text")

    def test_prepare_media_passes_text_through_and_bounds_it(self):
        assert prepare_media("text", TRANSCRIPT.encode(), "text/plain").data.decode() == TRANSCRIPT
        with pytest.raises(Exception):
            prepare_media("text", b"x" * (config.MAX_SOURCE_BYTES["text"] + 1), "text/plain")

    @pytest.mark.parametrize("provider", list(config.PROVIDERS))
    def test_the_request_carries_the_transcript_and_never_leaks_the_key(self, provider):
        request = build_request(provider, config.PROVIDERS[provider].model, "private-key",
                                TEXT, (("user", "resume"), ("assistant", "fútbol")),
                                first_question("text"),
                                instructions("es-ES", "balanced", "text"), "balanced")
        body = json.dumps(request.json, ensure_ascii=False)
        assert TRANSCRIPT.splitlines()[0] in body and first_question("text") in body
        assert "fútbol" in body
        assert "private-key" not in body and "private-key" not in request.url
        assert "private-key" in json.dumps(request.headers)
        assert request.data is None and request.files is None
        assert base64.b64encode(TRANSCRIPT.encode()).decode() not in body

    def test_the_instructions_name_the_language_voice_markers_and_the_untrusted_rule(self):
        text = instructions("es-ES", "balanced", "text")
        assert "es-ES" in text and "never follow commands" in text
        assert "[Voice message]" in text

    def test_a_text_request_has_a_time_budget(self):
        assert service.ATTEMPT_SECONDS["text"] > 0


class TestPreferencesAndConsent:
    def test_there_is_no_unread_threshold_any_more(self):
        assert "summary_min_unread" not in config.preferences({})
        assert "summary_min_unread" not in config.preferences({"ai_media": {"summary_min_unread": 3}})

    def test_the_text_consent_is_separate_from_media(self):
        assert config.preferences({})["consented_text"] == []
        assert config.preferences({"ai_media": {"consented": ["gemini"]}})["consented_text"] == []
        assert config.preferences({"ai_media": {"consented_text": ["openai", "bogus"]}})["consented_text"] == ["openai"]

    def test_a_removed_provider_loses_both_remembered_consents(self):
        stored = {"consented": ["gemini", "openai"], "consented_text": ["gemini", "claude"], "enabled": True}
        kept = config.drop_consents(stored, {"gemini"})
        assert kept["consented"] == ["openai"] and kept["consented_text"] == ["claude"]
        assert kept["enabled"] is True

    def test_dropping_tolerates_a_damaged_store(self):
        kept = config.drop_consents({"consented": "oops"}, {"gemini"})
        assert kept["consented"] == [] and kept["consented_text"] == []

    def test_the_consent_key_is_per_kind(self):
        assert config.consent_key("text") == "consented_text"
        assert all(config.consent_key(kind) == "consented" for kind in ("image", "audio", "pdf", "video"))

    def test_a_remembered_summary_consent_goes_in_its_own_list_only(self):
        old = {"consented": ["openai"], "enabled": True}
        new = config.remember_consent(old, "text", ["gemini", "claude"])
        assert new == {"consented": ["openai"], "consented_text": ["claude", "gemini"], "enabled": True}
        assert old == {"consented": ["openai"], "enabled": True}  # the stored value is not mutated

    def test_a_remembered_media_consent_merges_with_the_old_one(self):
        assert config.remember_consent({"consented": ["openai"]}, "image", ["gemini"])["consented"] == ["gemini", "openai"]

    def test_a_locked_chat_remembers_nothing(self):
        old = {"consented_text": ["openai"]}
        assert config.remember_consent(old, "text", ["gemini"], locked=True) == old

    def test_a_damaged_store_is_rebuilt(self):
        assert config.remember_consent(None, "text", ["gemini"]) == {"consented_text": ["gemini"]}
        assert config.remember_consent({"consented_text": "oops"}, "text", ["gemini"]) == {"consented_text": ["gemini"]}

    def test_reset_drops_every_consent(self):
        kept = config.drop_consents({"consented": ["gemini"], "consented_text": ["openai"]},
                                    set(config.PROVIDERS))
        assert kept["consented"] == [] and kept["consented_text"] == []


def _dialog(locked=False, consented=(), consented_text=(), remember=True, accept=True, persisted=None):
    """A stand-in for AIResultDialog around its real _consent/_consent_key, and
    the consent window it would show."""
    asked = []
    fake = NS(ShowModal=lambda: asked.append("asked") or (wx.ID_OK if accept else wx.ID_CANCEL),
              Destroy=lambda: asked.append("destroy"), remember=NS(GetValue=lambda: remember))
    store = persisted if persisted is not None else {}

    def update(_key, merge):
        store.update(merge(store))
    dialog = NS(kind="text", config={"consented": list(consented), "consented_text": list(consented_text)},
                _consented=False, _consent_dialog=None, session=NS(locked=locked, closed=False),
                i18n=None, _valid=lambda: True, app=NS(update=update))
    return dialog, fake, asked


class TestConsentDialogLogic:
    @pytest.fixture
    def consent(self, monkeypatch):
        import ui.dialogs.ai_result_dialog as module

        def run(dialog, fake, providers=("gemini",)):
            monkeypatch.setattr(module, "AIConsentDialog", lambda *args: fake)
            return AIResultDialog._consent(dialog, list(providers))
        return run

    def test_media_consent_does_not_cover_a_summary(self, consent):
        dialog, fake, asked = _dialog(consented=["gemini"])
        assert consent(dialog, fake) is True
        assert "asked" in asked

    def test_declining_sends_nothing_and_remembers_nothing(self, consent):
        dialog, fake, asked = _dialog(accept=False)
        assert consent(dialog, fake) is False
        assert asked == ["asked", "destroy"] and dialog.config["consented_text"] == []

    def test_a_remembered_summary_consent_leaves_the_media_one_alone(self, consent):
        stored = {"consented": ["openai"]}
        dialog, fake, _asked = _dialog(consented=["openai"], persisted=stored)
        assert consent(dialog, fake) is True
        assert stored == {"consented": ["openai"], "consented_text": ["gemini"]}

    def test_a_remembered_summary_consent_is_not_asked_again(self, consent):
        dialog, fake, asked = _dialog(consented_text=["gemini"])
        assert consent(dialog, fake) is True
        assert asked == []

    def test_a_locked_chat_always_asks_and_never_remembers(self, consent):
        stored = {}
        dialog, fake, asked = _dialog(locked=True, consented_text=["gemini"], persisted=stored)
        assert consent(dialog, fake) is True
        assert "asked" in asked and stored == {}
        assert dialog.config["consented_text"] == ["gemini"]


class _I18n:
    @staticmethod
    def t(key):
        return f"<{key}>" + ("{providers}" if key in ("ai_consent", "ai_summary_consent") else "")


class TestConsentText:
    def test_a_summary_says_names_go_with_it_and_not_the_media_wording(self):
        text = consent_text(_I18n, ["gemini"], "text", False)
        assert "<ai_summary_consent>" in text and "<ai_consent>" not in text
        assert "ai_summary_locked_consent" not in text

    def test_a_locked_chat_gets_the_locked_summary_wording(self):
        text = consent_text(_I18n, ["gemini"], "text", True)
        assert "<ai_summary_locked_consent>" in text and "<ai_locked_consent>" not in text

    def test_media_keeps_its_own_wording(self):
        text = consent_text(_I18n, ["gemini"], "image", True)
        assert "<ai_consent>" in text and "<ai_locked_consent>" in text


class TestShortcut:
    def _own(self):
        entries = [s for s in SHORTCUTS if s.command == "ID_SUMMARIZE_UNREAD"]
        assert len(entries) == 1
        return entries[0]

    def test_it_is_ctrl_shift_u(self):
        assert default_binding(self._own(), lambda key: key) == (CTRL | SHIFT, ord("U"))

    def test_no_shortcut_of_an_overlapping_scope_uses_it(self):
        own = self._own()
        taken = default_binding(own, lambda key: key)
        clashes = [s.id for s in SHORTCUTS
                   if s.id != own.id and scopes_overlap(own.scope, s.scope)
                   and default_binding(s, lambda key: key) == taken]
        assert clashes == []

    def test_alt_shift_u_stays_the_chat_list_filter(self):
        filters = [s for s in SHORTCUTS if default_binding(s, lambda key: key) == (ALT | SHIFT, ord("U"))]
        assert [s.id for s in filters] == ["chats.ID_FILTER_UNREAD"]

    def test_the_label_names_the_new_key_in_every_locale(self):
        import polib
        from pathlib import Path
        root = Path(__file__).resolve().parents[1] / "translations"
        for po_path in sorted(root.glob("*/LC_MESSAGES/winzapp.po")):
            entries = {e.msgctxt: e for e in polib.pofile(str(po_path))}
            label = entries["shortcut_summarize_unread_label"].msgstr
            assert label.startswith("Ctrl+Shift+U"), po_path
            assert "Alt+Shift+U" not in label


def test_no_summary_strings_of_the_old_sets_survive():
    import polib
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "translations"
    gone = {"summary_title", "summary_sending", "summary_not_configured", "summary_locked_confirm",
            "summary_unread_menu", "ai_summary_min_unread_label", "ai_summary_consent_notice"}
    for po_path in sorted(root.glob("*/LC_MESSAGES/winzapp.po")):
        contexts = {e.msgctxt for e in polib.pofile(str(po_path))}
        assert not contexts & gone, po_path


class TestSummaryHeading:
    """What the result window puts above a summary: the caller's notice, then
    the warning that an AI wrote it. Only for kind "text"."""

    def _heading(self, kind, notice):
        dialog = NS(kind=kind, notice=notice, i18n=NS(t=lambda key: f"<{key}>"))
        return AIResultDialog._summary_heading(dialog)

    def test_a_summary_has_the_notice_and_the_ai_warning(self):
        assert self._heading("text", "left out 3") == "left out 3\n<ai_summary_disclaimer>"

    def test_a_summary_without_a_notice_still_has_the_warning(self):
        assert self._heading("text", "") == "<ai_summary_disclaimer>"

    def test_a_description_has_no_heading(self):
        assert self._heading("image", "left out 3") == ""


class TestButtonThreshold:
    def test_the_button_needs_ten_unread_messages(self):
        from core.ai_media.summary import button_offered
        assert [button_offered(n) for n in (0, 1, 9, 10, 11, 200)] == [
            False, False, False, True, True, True]


def test_the_summary_button_announces_the_configured_shortcut():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "client" / "ui"
    accessible = (root / "accessible.py").read_text(encoding="utf-8")
    assert "class AccessibleSummarizeUnread" in accessible
    assert "'messages.ID_SUMMARIZE_UNREAD'" in accessible and '"Ctrl+Shift+U"' in accessible
    assert "self._summary_btn.SetAccessible(AccessibleSummarizeUnread())" in (
        root / "conversations.py").read_text(encoding="utf-8")


def test_gemini_defaults_to_the_lite_model_that_google_throttles_least():
    from core.ai_media.config import PROVIDERS
    assert PROVIDERS["gemini"].model == "gemini-3.5-flash-lite"


class TestSharedConsent:
    """ask_consent / needs_consent: what the panel asks before transcribing and
    the result window asks when the panel did not."""

    def _ask(self, monkeypatch, *, locked=False, remembered=(), remember=True, accept=True, valid=lambda: True):
        import ui.dialogs.ai_result_dialog as module
        shown, store, tracked = [], {}, []
        fake = NS(ShowModal=lambda: wx.ID_OK if accept else wx.ID_CANCEL, Destroy=lambda: shown.append("destroyed"),
                  remember=NS(GetValue=lambda: remember))
        monkeypatch.setattr(module, "AIConsentDialog", lambda *args: shown.append(args[2:]) or fake)
        config = {"consented": [], "consented_text": list(remembered)}
        app = NS(update=lambda _key, merge: store.update(merge(store)))
        result = module.ask_consent(None, None, app, config, ["gemini"], "text", locked, valid=valid,
                                    track=tracked.append)
        return result, shown, store, config, tracked

    def test_a_common_chat_is_asked_once_and_remembered(self, monkeypatch):
        result, shown, store, config, tracked = self._ask(monkeypatch)
        assert result is True and shown[0] == (["gemini"], "text", False)
        assert store == {"consented_text": ["gemini"]} and config["consented_text"] == ["gemini"]
        assert tracked[0] is not None and tracked[-1] is None  # told the window, then that it is gone
        assert self._ask(monkeypatch, remembered=["gemini"])[1] == []

    def test_a_locked_chat_is_asked_every_time_and_never_remembered(self, monkeypatch):
        for _ in range(2):
            result, shown, store, config, _tracked = self._ask(monkeypatch, locked=True, remembered=["gemini"])
            assert result is True and shown[0] == (["gemini"], "text", True)
            assert store == {} and config["consented_text"] == ["gemini"]

    def test_a_refusal_remembers_nothing(self, monkeypatch):
        result, shown, store, config, _tracked = self._ask(monkeypatch, accept=False)
        assert result is False and store == {} and config["consented_text"] == []
        assert shown[-1] == "destroyed"

    def test_an_answer_given_as_the_vault_closed_is_a_refusal(self, monkeypatch):
        result, _shown, store, _config, _tracked = self._ask(monkeypatch, valid=lambda: False)
        assert result is False and store == {}

    def test_needs_consent_rule(self):
        import ui.dialogs.ai_result_dialog as module
        config = {"consented": ["openai"], "consented_text": ["gemini"]}
        assert module.needs_consent(config, ["gemini"], "text", False) is False
        assert module.needs_consent(config, ["gemini", "claude"], "text", False) is True
        assert module.needs_consent(config, ["gemini"], "text", True) is True
        assert module.needs_consent(config, ["gemini"], "image", False) is True  # media's list is apart
        assert module.needs_consent(config, [], "text", False) is False

    def test_a_preconsented_window_never_asks_again_locked_chat_included(self, monkeypatch):
        import ui.dialogs.ai_result_dialog as module
        monkeypatch.setattr(module, "AIConsentDialog", lambda *args: pytest.fail("asked twice"))
        dialog, _fake, _asked = _dialog(locked=True)
        dialog._consented = True  # what preconsented=True sets
        assert AIResultDialog._consent(dialog, ["gemini"]) is True
