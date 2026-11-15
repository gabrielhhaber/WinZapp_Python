"""How a summary's voice messages are transcribed: local, online or not at all.

Pure logic (core/ai_media/summary_voice_choice.py), the questions
(SummaryVoiceChoiceMixin), the online worker (SummaryVoiceOnlineMixin) and how
the voice step honours the choice. No window is built: the dialogs are stubs.
"""
import threading
from types import SimpleNamespace as NS

import pytest

from core.ai_media import summary_voice_choice as svc
from core.ai_media.config import preferences
from core.ai_media.errors import DescriptionError
from ui.conversation_panel import summary_voice as voice
from ui.conversation_panel import summary_voice_online as online
from ui.conversation_panel.summary_voice import SummaryVoiceMixin, VoiceOutcome, _VoiceRun
from ui.conversation_panel.summary_voice_choice import SummaryVoiceChoiceMixin
from ui.conversation_panel.summary_voice_online import SummaryVoiceOnlineMixin

LOCAL = svc.VoiceChoice(svc.MODE_LOCAL)
ONLINE = svc.VoiceChoice(svc.MODE_ONLINE, "openai", "")


def audio(n):
    return {"key": {"id": f"id{n}", "remoteJid": "j"}, "messageType": "audioMessage",
            "message": {"audioMessage": {"mimetype": "audio/ogg"}}}


class TestSavedChoice:
    def test_nothing_saved_is_local(self):
        assert svc.saved_choice(None) == LOCAL
        assert svc.saved_choice({}) == LOCAL
        assert svc.saved_choice({svc.KEY: "damaged"}) == LOCAL

    def test_an_unknown_mode_is_local(self):
        assert svc.saved_choice({svc.KEY: {"mode": "telepathy"}}).mode == svc.MODE_LOCAL

    def test_remember_round_trips_and_keeps_the_rest(self):
        stored = svc.remember({"enabled": True}, ONLINE)
        assert stored["enabled"] is True
        assert svc.saved_choice(stored) == ONLINE

    def test_remember_survives_a_damaged_block(self):
        assert svc.saved_choice(svc.remember("damaged", svc.VoiceChoice(svc.MODE_NONE))).mode == svc.MODE_NONE


class TestUsable:
    def test_online_without_a_provider_falls_back_to_local(self):
        assert svc.usable(ONLINE, []) == LOCAL

    def test_a_gone_provider_gives_the_first_available_one(self):
        chosen = svc.usable(svc.VoiceChoice(svc.MODE_ONLINE, "gone", "x"), ["groq", "openai"])
        assert chosen.mode == svc.MODE_ONLINE and chosen.provider == "groq"

    def test_a_model_the_provider_does_not_list_becomes_its_default(self):
        chosen = svc.usable(svc.VoiceChoice(svc.MODE_ONLINE, "mistral", "evil/model"), ["mistral"])
        assert chosen.model == "voxtral-mini-transcribe-3"

    def test_none_stays_none(self):
        assert svc.usable(svc.VoiceChoice(svc.MODE_NONE), ["openai"]).mode == svc.MODE_NONE


class TestProviders:
    def config(self, **extra):
        return {**preferences(NS(get=lambda key: {"enabled": True, **extra})), }

    def test_only_providers_with_a_key_and_a_transcription_model(self):
        config = self.config()
        found = svc.online_providers(config, {"openai"})
        assert found == ["openai"]

    def test_a_disabled_provider_is_not_offered(self):
        config = self.config(disabled=["openai"])
        assert svc.online_providers(config, {"openai"}) == []

    def test_model_choices_only_where_the_provider_offers_them(self):
        assert svc.model_choices("openai") == ()
        assert [m for m, _name in svc.model_choices("mistral")][0] == "voxtral-mini-transcribe-3"
        assert svc.model_choices("nobody") == ()


class TestPendingAndConsent:
    def test_pending_voice_skips_messages_with_text(self):
        rows = [audio(1), audio(2), {"messageType": "conversation", "message": {}}]
        assert [m["key"]["id"] for m in svc.pending_voice(rows, lambda m: m["key"]["id"] == "id1")] == ["id2"]

    def test_the_consent_names_count_provider_and_cost(self):
        i18n = NS(t=lambda key: {"ai_summary_voice_online_consent": "{count} to {provider}",
                                 "ai_metadata_notice": "meta", "ai_locked_consent": "locked"}.get(key, key))
        text = svc.consent_text(i18n, "openai", 3, False)
        assert text.startswith("3 to OpenAI") and "meta" in text and "locked" not in text
        assert "locked" in svc.consent_text(i18n, "openai", 3, True)


class Panel(SummaryVoiceChoiceMixin):
    def __init__(self, *, answer=None, providers=("openai",), saved=None, locked=False):
        self.answer = answer
        self.asked = []
        self.remembered = []
        self._providers = list(providers)
        self._saved = saved or LOCAL
        self.locked = locked
        self.main_window = NS(i18n=NS(t=lambda key: key), _shutting_down=False,
                              is_chat_hidden_by_vault=lambda jid: self.locked,
                              is_chat_locked=lambda jid: self.locked)

    def _summary_voice_providers(self, config):
        return self._providers

    def _summary_voice_saved(self, app):
        return self._saved

    def _summary_voice_remember(self, app, choice):
        self.remembered.append(choice)

    def _summary_voice_ask(self, count, providers, initial, jid):
        self.asked.append((count, list(providers), initial))
        return self.answer


class TestDecide:
    def test_the_dialog_starts_from_the_saved_choice_and_the_answer_is_remembered(self):
        panel = Panel(answer=ONLINE, saved=ONLINE)
        assert panel._summary_voice_decide(None, {}, "j", 4) == ONLINE
        assert panel.asked[0][0] == 4 and panel.asked[0][2].provider == "openai"
        assert panel.remembered == [ONLINE]

    def test_cancelling_remembers_nothing_and_returns_none(self):
        panel = Panel(answer=None)
        assert panel._summary_voice_decide(None, {}, "j", 1) is None
        assert panel.remembered == []

    def test_a_chat_hidden_meanwhile_drops_the_answer(self):
        panel = Panel(answer=ONLINE, locked=True)
        assert panel._summary_voice_decide(None, {}, "j", 1) is None
        assert panel.remembered == []

    def test_online_saved_without_providers_starts_local(self):
        panel = Panel(answer=LOCAL, providers=(), saved=ONLINE)
        panel._summary_voice_decide(None, {}, "j", 1)
        assert panel.asked[0][2] == LOCAL

    def test_the_dialog_is_asked_even_for_a_remembered_choice(self):
        panel = Panel(answer=LOCAL, saved=svc.VoiceChoice(svc.MODE_NONE))
        panel._summary_voice_decide(None, {}, "j", 1)
        assert len(panel.asked) == 1


class StepPanel(SummaryVoiceMixin, SummaryVoiceOnlineMixin):
    def __init__(self):
        self.output = []
        self.ran = []
        self.main_window = NS(i18n=NS(t=lambda key: key, language="pt-BR"),
                              output=lambda text, interrupt=False: self.output.append(text),
                              settings={}, is_chat_hidden_by_vault=lambda jid: False)
        self._summary_voice_run = None

    def _ai_settings(self):
        return NS(), {"profile": "balanced"}

    def _summary_stored_voice_text(self, msg):
        return None

    def _transcription_in_progress(self):
        return False

    def _run_summary_voice(self, run):
        self.ran.append(run)
        return None


class TestStepHonoursTheChoice:
    def step(self, panel, choice, rows):
        done = []
        panel._summary_voice_step(rows, "j", done.append, False, choice=choice)
        return done

    def test_none_transcribes_nothing_and_goes_on(self):
        panel = StepPanel()
        done = self.step(panel, svc.VoiceChoice(svc.MODE_NONE), [audio(1), audio(2)])
        assert panel.ran == [] and len(done) == 1
        assert done[0].texts == {} and done[0].over_limit == 0 and done[0].failed == 0

    def test_online_runs_with_the_choice_and_the_summary_profile(self):
        panel = StepPanel()
        done = self.step(panel, ONLINE, [audio(1)])
        assert panel.ran[0].choice == ONLINE and panel.ran[0].profile == "balanced"
        assert panel.ran[0].language == "pt-BR" and len(done) == 1

    def test_online_is_not_blocked_by_a_local_transcription(self):
        panel = StepPanel()
        panel._transcription_in_progress = lambda: True
        self.step(panel, ONLINE, [audio(1)])
        assert len(panel.ran) == 1 and panel.output == []

    def test_local_still_refuses_while_another_transcription_runs(self):
        panel = StepPanel()
        panel._transcription_in_progress = lambda: True
        assert self.step(panel, LOCAL, [audio(1)]) == []
        assert panel.ran == [] and panel.output == ["ai_summary_voice_busy"]

    def test_over_the_limit_is_counted_for_the_notice(self):
        panel = StepPanel()
        done = self.step(panel, LOCAL, [audio(n) for n in range(25)])
        assert panel.ran[0].total == 20
        assert done[0].over_limit == 5


class OnlinePanel(SummaryVoiceOnlineMixin):
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.stored = []
        self.main_window = NS(i18n=NS(t=lambda key: key), is_chat_hidden_by_vault=lambda jid: False)

    def _summary_voice_online_one(self, msg, run, store, operation):
        result = self.behaviour(msg)
        if isinstance(result, Exception):
            raise result
        return result

    def _store_bulk_transcription(self, *args):
        self.stored.append(args)


@pytest.fixture(autouse=True)
def _inline_wx_and_store(monkeypatch):
    monkeypatch.setattr(online, "CredentialStore", lambda directory: NS())
    monkeypatch.setattr(online, "global_dir", lambda: "")
    monkeypatch.setattr(online.wx, "CallAfter", lambda fn, *args: fn(*args), raising=False)


def make_run(rows):
    return _VoiceRun("j", rows, VoiceOutcome(), lambda outcome: None, False, ONLINE)


class TestOnlineWorker:
    def work(self, panel, run):
        return panel._summary_voice_online_worker(run, lambda text, tick: None)

    def test_each_message_is_transcribed_and_kept_as_an_online_result(self):
        panel = OnlinePanel(lambda msg: "hola " + msg["key"]["id"])
        run = make_run([audio(1), audio(2)])
        assert self.work(panel, run) is None
        assert run.outcome.texts == {"id1": "hola id1", "id2": "hola id2"}
        result = panel.stored[0][2]
        assert result.backend == "online" and result.model_id == "openai" and result.text == "hola id1"

    def test_a_failure_counts_and_the_others_go_on(self):
        panel = OnlinePanel(lambda msg: DescriptionError("server") if msg["key"]["id"] == "id1" else "ok")
        run = make_run([audio(1), audio(2)])
        assert self.work(panel, run) is None
        assert run.outcome.failed == 1 and run.outcome.texts == {"id2": "ok"}

    @pytest.mark.parametrize("category", ["quota", "credits", "authentication", "credentials"])
    def test_a_failure_the_next_message_would_repeat_ends_the_run_with_its_reason(self, category):
        panel = OnlinePanel(lambda msg: DescriptionError(category))
        run = make_run([audio(1), audio(2)])
        assert self.work(panel, run) == f"ai_error_{category}"
        assert run.outcome.failed == 1 and run.index == 1

    def test_cancelling_stops_the_run_and_is_not_a_failure(self):
        panel = OnlinePanel(lambda msg: DescriptionError("cancelled"))
        run = make_run([audio(1), audio(2)])
        self.work(panel, run)
        assert run.cancelled.is_set() and run.outcome.failed == 0 and run.index == 1

    def test_no_speech_stays_a_marker(self):
        panel = OnlinePanel(lambda msg: "")
        run = make_run([audio(1)])
        self.work(panel, run)
        assert run.outcome.texts == {} and panel.stored == []

    def test_a_hidden_chat_stops_the_run_without_a_word(self):
        panel = OnlinePanel(lambda msg: "x")
        panel.main_window.is_chat_hidden_by_vault = lambda jid: True
        run = make_run([audio(1)])
        assert self.work(panel, run) is voice._LOCKED and run.outcome.texts == {}

    def test_an_unexpected_error_ends_the_run_and_is_counted(self):
        panel = OnlinePanel(lambda msg: RuntimeError("boom"))
        panel._summary_voice_online_one = lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))
        run = make_run([audio(1)])
        assert self.work(panel, run) == "ai_error_response" and run.outcome.failed == 1


class TestOnlineConsent:
    def run(self, monkeypatch, locked):
        import ui.dialogs.ai_result_dialog as dialog
        seen = {}

        def fake(parent, i18n, app, config, providers, kind, is_locked, valid=None, track=None, text=None):
            seen.update(providers=providers, kind=kind, locked=is_locked, text=text)
            return True
        monkeypatch.setattr(dialog, "ask_consent", fake)
        panel = Panel(locked=locked)
        panel.main_window.i18n = NS(t=lambda key: {"ai_summary_voice_online_consent": "{count}/{provider}"}.get(key, key))
        assert panel._summary_voice_online_consent(None, {}, "j", ONLINE, 3) is True
        return seen

    def test_the_provider_audio_kind_and_text_reach_the_consent(self, monkeypatch):
        seen = self.run(monkeypatch, False)
        assert seen["providers"] == ["openai"] and seen["kind"] == "audio" and seen["locked"] is False
        assert seen["text"].startswith("3/OpenAI")

    def test_a_locked_chat_is_asked_as_locked(self, monkeypatch):
        assert self.run(monkeypatch, True)["locked"] is True


class TestAskConsentText:
    def test_the_text_replaces_the_wording_and_is_left_alone_when_none(self, monkeypatch):
        import ui.dialogs.ai_result_dialog as dialog
        made = []

        class Fake:
            def __init__(self, *args, **kwargs):
                made.append(kwargs)
                self.remember = NS(GetValue=lambda: False)

            def ShowModal(self):
                return dialog.wx.ID_CANCEL

            def Destroy(self):
                pass
            __bool__ = lambda self: True
        monkeypatch.setattr(dialog, "AIConsentDialog", Fake)
        config = {"consented": [], "consented_text": []}
        args = (None, NS(), NS(), config, ["openai"], "audio", False)
        assert dialog.ask_consent(*args, text="mine") is False
        assert dialog.ask_consent(*args) is False
        assert made == [{"text": "mine"}, {}]
