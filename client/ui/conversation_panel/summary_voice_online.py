"""SummaryVoiceOnlineMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

The online worker of a summary's voice step: each voice message goes, one
after another, to the one provider and model the person chose and consented to
(no fallback to another provider), and the text is kept with its message like
a local run keeps it, as an online transcription. Runs on the progress
window's job thread, like the local worker. A failure counts the message as
failed (it stays a marker); one the next message would repeat (no key, refused
key, no quota or credit) ends the run with its reason.

Nothing here logs the text, the contact, a message id or a provider's answer.
"""
import copy
import logging
from types import SimpleNamespace

import wx

from app_paths import global_dir
from core.ai_credentials import CredentialError, CredentialStore
from core.ai_media import config as ai_config
from core.ai_media.errors import DescriptionError
from core.ai_media.payload import prepare_media
from core.ai_media.prompts import first_question, instructions
from core.ai_media.service import Operation, run_chain
from core.ai_media.summary_voice_choice import STOPPING_CATEGORIES
from core.ai_media.transcript import spoken_answer
from core.transcription import stored as stored_transcription
from ui.conversation_panel.media_paths import cached_media_path
from ui.conversation_panel.summary_voice import _LOCKED, voice_progress_tick


class SummaryVoiceOnlineMixin:
    """Transcribe a summary's voice messages with one AI provider."""

    def _summary_voice_online_worker(self, run, announce):
        """On the job's thread; the sentence it stopped with, or None."""
        mw = self.main_window
        i18n = mw.i18n
        store = CredentialStore(global_dir())
        stop_sentence = None
        try:
            for msg in run.order:
                if run.cancelled.is_set():
                    break
                if mw.is_chat_hidden_by_vault(run.jid):
                    stop_sentence = _LOCKED
                    break
                run.index += 1
                progress = (i18n.t("ai_summary_voice_progress").format(index=run.index, total=run.total)
                            if run.index > 1 else None)
                announce(progress, voice_progress_tick(run.index, run.total))
                operation = Operation("audio")
                with run.lock:
                    run.current = operation
                if run.cancelled.is_set():
                    operation.cancel()
                try:
                    text = self._summary_voice_online_one(msg, run, store, operation)
                except (DescriptionError, CredentialError) as exc:
                    category = getattr(exc, "category", "credentials")
                    if category == "cancelled":
                        run.cancelled.set()
                        break
                    run.outcome.failed += 1
                    logging.info("[ai-media] a summary voice message failed: %s", category)
                    if category in STOPPING_CATEGORIES:
                        stop_sentence = i18n.t(f"ai_error_{category}")
                        break
                    continue
                finally:
                    with run.lock:
                        run.current = None
                if not text:
                    continue  # No speech in it: it stays the marker.
                msg_id = (msg.get("key") or {}).get("id", "")
                run.outcome.texts[msg_id] = text
                result = SimpleNamespace(
                    text=text, language=None, language_probability=None, model_id=run.choice.provider,
                    backend=stored_transcription.ONLINE_BACKEND, vad_used=True)
                wx.CallAfter(self._store_bulk_transcription, run.jid, msg, result, False, None)
        except Exception as exc:
            logging.error("[ai-media] the summary online voice step stopped: %s", type(exc).__name__)
            run.outcome.failed += 1
            stop_sentence = i18n.t("ai_error_response")
        return stop_sentence

    def _summary_voice_online_one(self, msg, run, store, operation) -> str:
        """The transcription of one voice message ("" when it has no speech)."""
        mw = self.main_window
        snapshot = copy.deepcopy(msg)
        msg_id = (snapshot.get("key") or {}).get("id", "")
        info = (snapshot.get("message") or {}).get("audioMessage") or {}
        loader = self._ai_loader(snapshot, "audio", cached_media_path("audioMessage", msg_id), mw.key)
        operation.check()
        prepared = prepare_media("audio", loader(operation), str(info.get("mimetype") or ""), run.profile)
        operation.check()
        provider = run.choice.provider
        models = {provider: ai_config.transcribe_model(provider, run.choice.model)}
        answer, _provider = run_chain(
            operation, [provider], models, store.get, prepared, (), first_question("audio"),
            instructions(run.language, run.profile, "audio"), run.profile)
        return spoken_answer(answer or "")
