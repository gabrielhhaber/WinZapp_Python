"""AutoAIMediaMixin — part of MainWindow (see main.py).

Transcribes received voice messages and describes received pictures, stickers
and videos on arrival with the online AI providers of Settings > Transcriptions
& Descriptions, and keeps the text in the message's line, when "Transcribe
received voice messages automatically" or "Describe received pictures,
stickers and videos automatically" is on (both off by default). Which messages
qualify and which providers may be used: core/ai_media/auto.py. This is the
glue: the arrival hook, the request, and storing the result.

The local counterpart is main_window/auto_transcription.py, and it shares its
rules: never a window, never a sound, never speech for a message; locked
chats are skipped; one run at a time; nothing about a message in the log.

Only providers the person consented to are used: turning the option on asks
for that consent (ui/dialogs/ai_settings_page.py), and a provider added later
is not sent anything until it has it.
"""

import logging
import threading
import time

import wx

from app_paths import global_dir
from app_settings import AppSettings
from core import conversation_auto_ai
from core.ai_credentials import CredentialStore
from core.ai_media import auto as ai_auto
from core.ai_media import config as ai_config
from core.ai_media.payload import prepare_media
from core.ai_media.prompts import first_question, instructions
from core.ai_media.errors import DescriptionError
from core.ai_media import usage as ai_usage
from core.ai_media.service import Operation, run_chain
from core.ai_media.transcript import spoken_answer
from core.transcription import auto_queue
from core.transcription import stored as stored_transcription
from core.utils import decrypt_bytes
from ui.conversation_panel.media_paths import cached_media_path


class AutoAIMediaMixin:
    """Automatic online transcription and description, for MainWindow."""

    def _ai_media_preferences(self):
        app = getattr(self, "_app_settings", None) or AppSettings(global_dir())
        return ai_config.preferences(app)

    def _conversation_allows_auto_ai(self, remote_jid: str, kind: str) -> bool:
        """The conversation's "describe" box (data window) for pictures,
        stickers and videos. Voice is decided by _auto_ai_config_for(): its
        box can turn the online transcription on, not only let it act."""
        if kind == "audio":
            return True
        return conversation_auto_ai.allowed(
            self.settings, self._normalize_jid(remote_jid), conversation_auto_ai.DESCRIBE)

    def _auto_ai_config_for(self, remote_jid: str, config: dict):
        """(config, local) for one conversation: the online options with
        "transcribe automatically" replaced by the conversation's own box
        when the user set it, and whether the local automatic transcription
        runs there (it wins over the online one, as it does install-wide)."""
        wanted = conversation_auto_ai.transcription_wanted(
            self.settings, self._normalize_jid(remote_jid),
            conversation_auto_ai.TRANSCRIBE_ONLINE, config.get("auto_transcribe"))
        return dict(config, auto_transcribe=wanted), self._local_auto_transcription_for(remote_jid)

    def _maybe_auto_ai_media(self, remote_jid: str, msg: dict) -> None:
        """Queue *msg* if the online automatic options ask for it (wx thread,
        for messages that just arrived; cheap when both are off)."""
        try:
            config, local = self._auto_ai_config_for(remote_jid, self._ai_media_preferences())
            if not (config["auto_transcribe"] or config["auto_describe"]):
                return
            kind = ai_auto.wanted_kind(
                msg, config, local,
                max_age=auto_queue.max_age_seconds(self.settings))
            if kind is None or self.is_chat_locked(remote_jid):
                return
            if not self._conversation_allows_auto_ai(remote_jid, kind):
                return
            if ai_auto.limit_reached(config):
                self._say_auto_ai_limit_once(config)
                return
            if not ai_auto.usable_providers(config, kind, CredentialStore(global_dir()).saved()):
                return
            msg_id = (msg.get("key") or {}).get("id", "")
            self._auto_ai_queue_get().submit((remote_jid, msg_id), (remote_jid, msg_id, msg, kind))
        except Exception as exc:
            # Whatever this does must never cost the arrival of a message.
            logging.warning("[ai-media] could not queue an automatic request: %s",
                            type(exc).__name__)

    def _auto_ai_queue_get(self):
        queue = getattr(self, "_auto_ai_queue", None)
        if queue is None:
            queue = auto_queue.AutoTranscriptionQueue(self._run_auto_ai_media)
            self._auto_ai_queue = queue
        return queue

    # ── The ceiling and the pause ────────────────────────────────────────────

    def _charge_auto_ai_request(self) -> bool:
        """Count one request against today's ceiling, before it is made (a
        failed one reached the provider too). False when the ceiling is used."""
        lock = getattr(self, "_auto_ai_charge_lock", None)
        if lock is None:
            lock = self._auto_ai_charge_lock = threading.Lock()
        app = getattr(self, "_app_settings", None) or AppSettings(global_dir())
        with lock:
            if ai_auto.limit_reached(ai_config.preferences(app)):
                wx.CallAfter(self._say_auto_ai_limit_once, ai_config.preferences(app))
                return False
            app.update("ai_media", ai_auto.charged)
        return True

    def _say_auto_ai_limit_once(self, config) -> None:
        """One sentence a day: the ceiling was reached and nothing more is sent."""
        day = ai_auto.day_key()
        if getattr(self, "_auto_ai_limit_said_on", None) == day:
            return
        self._auto_ai_limit_said_on = day
        self.output(self.i18n.t("auto_media_limit_reached").format(
            limit=config.get("auto_daily_limit", 0)))

    def _pause_auto_ai_media(self) -> None:
        """Every provider refused for a reason that repeats: stop for an hour
        and say so once (again only after the settings were applied)."""
        started = self._auto_ai_queue_get().pause(ai_auto.PAUSE_SECONDS)
        if started and not getattr(self, "_auto_ai_pause_said", False):
            self._auto_ai_pause_said = True
            wx.CallAfter(self.output, self.i18n.t("auto_media_paused_provider"))

    def resume_auto_ai_media(self) -> None:
        """Settings > Transcriptions & Descriptions was applied: what paused the
        queue may have been fixed there (a key, a provider)."""
        queue = getattr(self, "_auto_ai_queue", None)
        if queue is not None:
            queue.resume()
        self._auto_ai_pause_said = False

    def stop_auto_ai_media(self) -> None:
        """At shutdown: nothing more is sent."""
        queue = getattr(self, "_auto_ai_queue", None)
        if queue is not None:
            queue.stop()

    # ── On the queue's worker thread ─────────────────────────────────────────

    def _run_auto_ai_media(self, item) -> None:
        remote_jid, msg_id, msg, kind = item
        try:
            # Checked again: the queue may have held this for a while, and the
            # options or the chat's lock may have changed since.
            config, local = self._auto_ai_config_for(remote_jid, self._ai_media_preferences())
            if self.is_chat_locked(remote_jid) \
                    or not self._conversation_allows_auto_ai(remote_jid, kind):
                return
            if ai_auto.wanted_kind(dict(msg, messageTimestamp=0), config, local) != kind:
                return
            store = CredentialStore(global_dir())
            providers = ai_auto.usable_providers(config, kind, store.saved())
            if not providers:
                return
            msg_type = msg.get("messageType", "")
            path = cached_media_path(msg_type, msg_id)
            if not self._auto_fetch_media(msg, path):
                return
            limit = ai_config.MAX_SOURCE_BYTES[kind]
            cap = limit * 4 // 3 + 1024
            with open(path, "rb") as stream:
                encrypted = stream.read(cap + 1)
            if len(encrypted) > cap:
                return
            info = (msg.get("message") or {}).get(msg_type) or {}
            mime = str(info.get("mimetype") or "") if isinstance(info, dict) else ""
            frames = config.get("video_frames", "several")
            prepared = prepare_media(kind, decrypt_bytes(encrypted, self.key), mime,
                                     config["profile"], frames)
            # Counted when the request is about to leave, not when it was
            # queued: a message whose media never arrived costs nothing.
            if not self._charge_auto_ai_request():
                return
            language = getattr(self.i18n, "language", "")
            answer, provider = run_chain(
                Operation(kind, local=ai_config.has_local(providers)), providers,
                ai_config.models_for(config, kind), store.get, prepared, (),
                first_question(kind), instructions(language, config["profile"], kind, frames),
                config["profile"], usage_sink=ai_usage.persist,
                endpoints=ai_config.endpoints(config))
        except Exception as exc:
            # The type and nothing else: the text of these exceptions can
            # quote a path, a key or the message.
            logging.info("[ai-media] automatic request ended without text: %s",
                         type(exc).__name__)
            if isinstance(exc, DescriptionError) and ai_auto.pauses(exc):
                self._pause_auto_ai_media()
            return
        text = spoken_answer(answer or "")
        if text:
            wx.CallAfter(self._store_auto_ai_media, remote_jid, msg_id, text, provider, kind)

    # ── Back on the wx thread ────────────────────────────────────────────────

    def _store_auto_ai_media(self, remote_jid, msg_id, text, provider, kind) -> None:
        """Keep *text* as the inline text of the message and redraw its row."""
        current = stored_transcription.find_record(
            self._transcription_copies(remote_jid, msg_id), msg_id)
        described = kind != "audio"
        if current is None or stored_transcription.has_decision(current, described):
            # The user got there first (a manual run, or a deletion). Only for
            # this category: a video already transcribed is still described.
            return
        previous = current.get(stored_transcription.TRANSCRIPTION_KEY)
        result = type("AutoResult", (), {
            "text": text, "language": None, "language_probability": None,
            "model_id": provider, "backend": stored_transcription.ONLINE_BACKEND,
            "vad_used": True})()
        now = stored_transcription.next_decision_time(time.time(), previous)
        # `previous` keeps the other category's result (a video's transcription).
        value = stored_transcription.with_result(previous, stored_transcription.record_from_result(
            result, now, inline=True,
            kind=stored_transcription.DESCRIPTION_KIND if described else None))
        try:
            answer = self.store_message_transcription(remote_jid, msg_id, value)
        except Exception as exc:
            logging.warning("[ai-media] storing an automatic text failed: %s", type(exc).__name__)
            return
        if answer != stored_transcription.SAVE_STORED or self.is_chat_hidden_by_vault(remote_jid):
            return
        panel = getattr(self, "conversations_panel", None)
        conversation = getattr(panel, "conversation", None)
        if (isinstance(conversation, dict)
                and self._normalize_jid(conversation.get("remoteJid", ""))
                == self._normalize_jid(remote_jid)):
            repaint = getattr(panel, "_repaint_or_repopulate", None)
            if callable(repaint):
                try:
                    repaint([msg_id])
                except Exception as exc:
                    logging.warning("[ai-media] repainting an automatic text failed: %s",
                                    type(exc).__name__)
