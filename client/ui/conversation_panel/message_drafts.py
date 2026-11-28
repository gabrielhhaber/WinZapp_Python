"""The message field's text stays with its conversation (core/message_drafts.py).

Left behind by navigate_to_conversation() and _close_conversation_core() before
the conversation changes, and put back once the new one is set. The text
being edited is not a draft: edit mode is left on the way out, as Esc would.
"""

from core import message_drafts
from core.message_drafts import MessageDrafts
from core.utils import to_editor_line_endings
from ui.conversation_panel.draft_persistence import DraftPersistenceMixin


class MessageDraftsMixin(DraftPersistenceMixin):
    """Per-conversation drafts of the message field."""

    def _message_drafts(self) -> MessageDrafts:
        drafts = getattr(self, "_message_drafts_store", None)
        if drafts is None:
            drafts = self._message_drafts_store = MessageDrafts()
            loader = getattr(self, "_load_persisted_drafts", None)
            if callable(loader):
                loader(drafts)
        return drafts

    def _draft_key(self, conversation):
        jid = (conversation or {}).get("remoteJid", "") if isinstance(conversation, dict) else ""
        if not jid:
            return ""
        normalize = getattr(self.main_window, "_normalize_jid", None)
        return normalize(jid) if callable(normalize) else jid

    def _draft_mode(self) -> str:
        return message_drafts.mode(getattr(self.main_window, "settings", None))

    def _stash_message_draft(self) -> None:
        """Leaving the conversation: keep what the field holds as its draft,
        or throw it away, as Settings says; then empty the field. Nothing to
        keep while a message is being edited. With KEEP_IN_FIELD the field is
        left as it is, as before drafts existed.

        Called before anything else of the switch resets the composer
        (mentions, the attachment panel), or the draft would lose them. A
        caption the attachment panel took from the field (attachments.py) is
        the draft when the field is empty, and leaves with it; the link
        preview of the text goes too, or Enter in the next conversation
        would send this one's card there."""
        mode = self._draft_mode()
        if mode == message_drafts.KEEP_IN_FIELD:
            return
        key = self._draft_key(self.conversation)
        if getattr(self, "_editing_message_id", None) is not None:
            self._on_cancel_edit()
        else:
            text = self.message_field.GetValue()
            caption = getattr(self, "_caption_field", None)
            if not text.strip() and caption is not None:
                text = caption.GetValue()
            if mode == message_drafts.DISCARD:
                self._message_drafts().take(key, self._same_chat)
            elif key:
                self._message_drafts().stash(
                    key, text,
                    getattr(self, "_pending_mentions", ()),
                    getattr(self, "_pending_mention_display_names", None))
        caption = getattr(self, "_caption_field", None)
        if caption is not None and caption.GetValue():
            caption.SetValue("")
        clear_preview = getattr(self, "_clear_link_preview", None)
        if callable(clear_preview):
            clear_preview()
        self._link_preview_dismissed_url = ""
        self._put_in_message_field("")
        # The field is empty now and the draft is in the store: save that.
        if hasattr(self, "_persist_message_drafts"):
            self._draft_live = None
            self._persist_message_drafts()

    def _restore_message_draft(self) -> None:
        """Opening the conversation: put its draft back in the field, with its
        mentions; an empty field when it has none, or when drafts are thrown
        away. With KEEP_IN_FIELD the field is left as it is."""
        mode = self._draft_mode()
        if mode == message_drafts.KEEP_IN_FIELD:
            return
        draft = self._message_drafts().take(self._draft_key(self.conversation), self._same_chat)
        if draft is None or mode == message_drafts.DISCARD:
            self._put_in_message_field("")
            return
        self._pending_mentions = list(draft.mentions)
        self._pending_mention_display_names = dict(draft.mention_names)
        self._rebuild_mention_pills()
        # Windows line breaks: with bare line feeds NVDA reads the block as
        # one line (core/utils.to_editor_line_endings()).
        self._put_in_message_field(to_editor_line_endings(draft.text))
        if hasattr(self, "_set_live_draft"):
            self._set_live_draft(self._draft_key(self.conversation), draft.text)
        check_preview = getattr(self, "_schedule_link_preview_check", None)
        if callable(check_preview):
            check_preview()

    def _same_chat(self, stored: str, jid: str) -> bool:
        equivalent = getattr(self.main_window, "_chat_jids_equivalent", None)
        return bool(callable(equivalent) and equivalent(stored, jid))

    def _put_in_message_field(self, text: str) -> None:
        """Set the field without telling the contact "typing..." and without a
        spelling cue for text nobody just typed (on_change_message_field()
        reads _restoring_draft)."""
        if self.message_field.GetValue() == text:
            return
        self._restoring_draft = True
        try:
            self.message_field.SetValue(text)
            self.message_field.SetInsertionPointEnd()
        finally:
            self._restoring_draft = False
