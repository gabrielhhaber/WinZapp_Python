"""MainWindow: the sounds and announcements of edited messages (issue #824).

The rules (which option wins, what is stored) are core/edit_alerts.py; this
mixin only resolves files, plays them and speaks or notifies. Hooked from
MessageEventsMixin._apply_possible_edit() (someone else's edit arriving live)
and from the conversation panel once WhatsApp accepted an edit of yours
(ui/conversation_panel/text_sending.py).
"""

import logging
import os
import time

from core import edit_alerts
from core.conversation_view import (
    archived_chat_stays_silent, archived_panel_is_shown, conversation_in_view,
)
from core.notification_burst import notification_allowed
from core.quiet_hours import is_quiet_hours_active
from core.sound_system import (
    DEFAULT_PACK_ID, Sound, resolve_alert_tone_path, resolve_sound_event_path,
    sound_event_enabled, sound_event_override_path,
)


class EditAlertsMixin:

    def _edit_alert_jid(self, remote_jid: str) -> str:
        """The JID a conversation's own edit choices are filed under: its
        normalized form, or the phone/@lid twin the dialog may have used."""
        jid = self._normalize_jid(remote_jid) if hasattr(self, "_normalize_jid") else remote_jid
        twins = [getattr(self, "_lid_to_phone", {}).get(jid, ""),
                 getattr(self, "_phone_to_lid", {}).get(jid, "")]
        return edit_alerts.stored_jid(self.settings, [jid] + twins)

    def _edit_event_sound(self, scope: str) -> tuple:
        """``(enabled, path)`` of the Sound Events entry behind *scope*."""
        key = edit_alerts.SOUND_EVENTS[scope]
        active_pack = self.get_active_sound_pack()
        pack_id = active_pack.get("id") if active_pack else DEFAULT_PACK_ID
        events_cfg = self.settings.get("sound_events", {}).get(pack_id, {})
        path = resolve_sound_event_path(
            active_pack, self._default_sound_pack, key,
            sound_event_override_path(events_cfg, key))
        return sound_event_enabled(events_cfg.get(key), key), path

    def _edit_sound_path(self, remote_jid: str, scope: str) -> str:
        """The file to play for *scope* in this conversation, or "" for none."""
        choice, custom = edit_alerts.sound_choice(
            self.settings, self._edit_alert_jid(remote_jid), scope)
        enabled, event_path = self._edit_event_sound(scope)
        plan = edit_alerts.sound_plan(choice, enabled)
        if not plan:
            return ""
        if plan == "alert":
            path = resolve_alert_tone_path(
                self.get_active_sound_pack(), self._default_sound_pack, choice, custom)
            if path and os.path.isfile(path):
                return path
        return event_path

    def _play_edit_sound(self, remote_jid: str, scope: str,
                         respect_quiet_hours: bool = False) -> None:
        if respect_quiet_hours and is_quiet_hours_active():
            return
        path = self._edit_sound_path(remote_jid, scope)
        if not path:
            return
        cache = getattr(self, "_notification_sound_cache", None)
        if cache is None:
            cache = self._notification_sound_cache = {}
        sound = cache.get(path)
        if sound is None:
            try:
                sound = cache[path] = Sound(self.sound_system, path)
            except Exception:
                logging.warning("[edit] could not load the sound for an edit")
                return
        sound.play()

    def play_edit_saved_sound(self, remote_jid: str) -> None:
        """Main thread: WhatsApp accepted an edit of yours."""
        try:
            self._play_edit_sound(remote_jid, edit_alerts.SAVED)
        except Exception:
            logging.exception("[edit] the saved-edit sound failed")

    def play_edited_background_sound(self, remote_jid: str) -> None:
        """The sound of a background notification for an edit (queued by the
        NotificationManager, which already applied Do Not Disturb)."""
        self._play_edit_sound(remote_jid, edit_alerts.BACKGROUND)

    def _maybe_notify_edit(self, remote_jid: str, record: dict) -> None:
        """Someone else's edit of *record* (already holding the new text) was
        just applied: sound, speech or notification, by the same rules as a
        reaction (mute, archive, locked chats, the burst limit)."""
        try:
            if (record.get("key") or {}).get("fromMe"):
                return
            if not edit_alerts.edit_is_recent(record, time.time()):
                return
            general = self.settings.get("general", {})
            muted = self.is_chat_muted(remote_jid)
            locked = bool(getattr(self, "is_chat_locked", lambda _jid: False)(remote_jid))
            archived = self.is_chat_archived(remote_jid) and not locked
            if muted and general.get("keep_muted_chats_silent_when_open", True):
                return
            jid = self._edit_alert_jid(remote_jid)

            from core.notification_manager import (
                format_notification_body, format_notification_title,
            )
            body = format_notification_body(record, self, self.i18n)
            window_active = (
                not getattr(self, "_window_hidden", False)
                and self.IsShown() and not self.IsIconized() and self.IsActive()
            )
            if window_active:
                cp = getattr(self, "conversations_panel", None)
                in_view = conversation_in_view(cp)
                current_jid = cp.conversation.get("remoteJid", "") if in_view else ""
                is_current = (
                    cp._matches_open_conversation(remote_jid)
                    if in_view and hasattr(cp, "_matches_open_conversation")
                    else (current_jid == remote_jid and bool(current_jid))
                )
                if (muted or locked) and not is_current:
                    return
                if archived and archived_chat_stays_silent(
                        is_current, archived_panel_is_shown(self)):
                    return
                if not notification_allowed(self):
                    return
                self._play_edit_sound(remote_jid, edit_alerts.FOREGROUND)
                if edit_alerts.toggle_on(self.settings, jid, edit_alerts.SPEAK_FOREGROUND):
                    title = format_notification_title(record, self, self.i18n)
                    self.output(self.i18n.t("fg_edited_msg").format(name=title) + f": {body}")
                return

            if muted or archived or locked:
                return
            if not general.get("notifications_enabled", True):
                return
            if not notification_allowed(self):
                return
            if not edit_alerts.toggle_on(self.settings, jid, edit_alerts.NOTIFY_BACKGROUND):
                # No notification asked for this edit; its sound alone, when on.
                self._play_edit_sound(remote_jid, edit_alerts.BACKGROUND,
                                      respect_quiet_hours=True)
                return
            if not general.get("show_tray_icon", True) or not hasattr(self, "notification_manager"):
                # No toast can be shown: the sound alone, as when no notification is asked for.
                self._play_edit_sound(remote_jid, edit_alerts.BACKGROUND,
                                      respect_quiet_hours=True)
                return
            from core.notification_manager import (
                EDIT_SOUND_EVENT, background_notification_content, notification_content_level,
            )
            content = background_notification_content(
                notification_content_level(self.settings),
                format_notification_title(record, self, self.i18n, toast=True),
                self.i18n.t("notif_edited_message").format(text=body),
                self.i18n.t("notif_hidden_edit"),
            )
            if content is None:
                self.notification_manager.send_sound_only(remote_jid, sound_event=EDIT_SOUND_EVENT)
            else:
                self.notification_manager.send(*content, remote_jid, sound_event=EDIT_SOUND_EVENT)
        except Exception:
            logging.exception("[_maybe_notify_edit] failed")
