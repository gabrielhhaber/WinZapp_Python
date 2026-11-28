"""Writing-help window: fix the spelling of the draft in the composer, say it
another way, make it more professional or more encouraging, or translate it.

The four writing actions go to the AI providers the person set up in Settings
(their own consent, per provider); Translate goes to Google Translate (its own
consent, no account). The draft in the composer is never touched until
"Replace draft" is chosen, and nothing is ever sent by itself.

Besides the fixed actions the person can type an instruction of their own, and
ask for a suggested reply: the only action that sends the latest messages of
the chat (a consent of its own, asked again for every locked chat).

Owned by the conversation panel (ui/conversation_panel/ai_writing.py), which
builds it, runs it modally and tears it down. The window machinery is
TextAIResultDialog's.
"""
import wx

from app_paths import active_account_id, global_dir
from core.ai_credentials import CredentialStore
from core.ai_media import config as ai_config
from core.ai_media import gtranslate, writing
from core.ai_media.errors import DescriptionError
from core.ai_media.payload import prepare_media
from core.ai_media.prompts import instructions
from core.ai_media import usage as ai_usage
from core.ai_media.service import run_chain
from ui.dialogs.ai_result_dialog import provider_names
from ui.dialogs.ai_text_result_dialog import TextAIResultDialog

#: The action that goes to Google Translate instead of a provider.
TRANSLATE = "translate"


class WritingHelpDialog(TextAIResultDialog):
    """One draft, the chosen action and its result, next to the composer."""

    has_replace = True

    def __init__(self, panel, draft, locked, config, app_settings, recent="",
                 initial_action=None, auto_run=False):
        self.draft = draft
        #: The latest messages of the chat as one text ("" when there are none):
        #: only a suggested reply sends it.
        self._recent = recent
        self._initial_action = initial_action
        self._auto_run = bool(auto_run)
        self._consented_reply = False
        self._source_draft = draft
        self._store = CredentialStore(global_dir())
        self._providers = ai_config.chain(config, "writing", lambda p: p in self._store.saved())
        self._consented_writing = False
        chat = (panel.conversation or {}).get("remoteJid", "")
        super().__init__(panel, (active_account_id(), chat, "draft"), "writing", locked,
                         config, app_settings)

    def _on_open(self):
        if getattr(self, "_auto_run", False):
            self.result.SetFocus()
            wx.CallAfter(self._regenerate)
            return
        # Choosing an action is not permission to spend quota. Even remembered
        # provider consent must not turn opening this window into a request.
        self.status.ChangeValue(self.i18n.t("writing_choose_before_run"))
        self.action.SetFocus()

    def _regenerate_key(self):
        return "writing_run_selected"

    def title_key(self):
        return "writing_title"

    def _extra_controls(self, layout):
        layout.Add(wx.StaticText(self, label=self.i18n.t("writing_action_label")), 0, wx.LEFT, 8)
        # With no provider holding a key, only Translate is offered: an action
        # that can only fail wastes the person's time with a screen reader.
        if not self.draft.strip():
            self._actions = [writing.REPLY] if (self._providers and self._recent) else [TRANSLATE]
        else:
            self._actions = [a for a in writing.ACTIONS if a != writing.REPLY or self._recent] if self._providers else []
            self._actions.append(TRANSLATE)
        labels = [self.i18n.t(writing.ACTION_KEY[action]) if action != TRANSLATE
                  else self.i18n.t("writing_action_translate") for action in self._actions]
        self.action = wx.Choice(self, choices=labels, name=self.i18n.t("writing_action_label"))
        initial = getattr(self, "_initial_action", None)
        index = self._actions.index(initial) if initial in self._actions else 0
        self.action.SetSelection(index)
        self.action.Bind(wx.EVT_CHOICE, self._action_changed)
        layout.Add(self.action, 0, wx.EXPAND | wx.ALL, 8)
        layout.Add(wx.StaticText(self, label=self.i18n.t("ai_mode_instruction_label")), 0, wx.LEFT, 8)
        self.instruction = wx.TextCtrl(self, name=self.i18n.t("ai_mode_instruction_label"))
        self.instruction.SetMaxLength(writing.MAX_INSTRUCTION_CHARS)
        layout.Add(self.instruction, 0, wx.EXPAND | wx.ALL, 8)
        layout.Add(wx.StaticText(self, label=self.i18n.t("writing_target_label")), 0, wx.LEFT, 8)
        self.target = wx.Choice(self, choices=[self.i18n.t(key) for _code, key in gtranslate.TARGET_LANGUAGES],
                                name=self.i18n.t("writing_target_label"))
        default = gtranslate.target_for_app_language(self.i18n.language)
        self.target.SetSelection(next((i for i, (code, _key) in enumerate(gtranslate.TARGET_LANGUAGES)
                                       if code == default), 0))
        layout.Add(self.target, 0, wx.EXPAND | wx.ALL, 8)
        self._sync_target()

    def _selected(self):
        return self._actions[self.action.GetSelection()]

    def _action_changed(self, event):
        self._sync_target()
        event.Skip()

    def _sync_target(self):
        self.target.Enable(self._selected() == TRANSLATE)
        self.instruction.Enable(self._selected() == writing.CUSTOM)

    def _question(self):
        action = self._selected()
        if action == TRANSLATE:
            return "translate"
        return writing.action_question(action, self.instruction.GetValue())

    def _consent(self):
        if self._selected() == TRANSLATE:
            return self._consent_translate()
        providers = self._providers
        if not providers:
            raise DescriptionError("providers")
        if self._selected() == writing.CUSTOM and not self.instruction.GetValue().strip():
            raise DescriptionError("question")  # before any consent is asked
        # Only what leaves this computer is asked about: a local model
        # (loopback address) asks nothing, not even for a locked chat.
        providers = ai_config.outbound(self.config, providers)
        if not providers:
            return True
        if self._selected() == writing.REPLY:
            return self._consent_reply(providers)
        needed = [p for p in providers if p not in self.config["consented_writing"]]
        if self._consented_writing or (not needed and not self.session.locked):
            self._consented_writing = True
            return True
        text = self.i18n.t("ai_writing_consent").format(providers=provider_names(providers))
        for key in ai_config.notice_keys(providers, self.config):
            text += "\n\n" + self.i18n.t(key)
        if self.session.locked:
            text += "\n\n" + self.i18n.t("ai_locked_consent")
        accepted, remember = self._run_consent(text)
        if accepted and remember:
            def merge(old):
                value = dict(old) if isinstance(old, dict) else {}
                current = value.get("consented_writing", [])
                consented = set(current if isinstance(current, list) else [])
                value["consented_writing"] = sorted(consented | set(providers))
                return value
            try:
                self.app.update("ai_media", merge)
                self.config["consented_writing"] = sorted(set(self.config["consented_writing"]) | set(providers))
            except (OSError, RuntimeError):
                # Consent is valid for this session even if remembering failed.
                pass
        self._consented_writing = accepted
        return accepted

    def _consent_reply(self, providers):
        """Consent to send the latest messages of the chat (and the draft). Own
        remembered list; a locked chat is asked for every window and never
        remembered. Covers the draft as well, so no second question follows."""
        needed = [p for p in providers if p not in self.config["consented_reply"]]
        if self._consented_reply or (not needed and not self.session.locked):
            self._consented_reply = True
            return True
        text = self.i18n.t("ai_writing_reply_consent").format(
            providers=provider_names(providers), count=writing.REPLY_MESSAGES)
        for key in ai_config.notice_keys(providers, self.config):
            text += "\n\n" + self.i18n.t(key)
        if self.session.locked:
            text += "\n\n" + self.i18n.t("ai_locked_consent")
        accepted, remember = self._run_consent(text)
        if accepted and remember:
            try:
                self.app.update("ai_media", lambda old: ai_config.remember_consent(old, "reply", providers))
                self.config["consented_reply"] = sorted(set(self.config["consented_reply"]) | set(providers))
            except (OSError, RuntimeError):
                # Consent is valid for this session even if remembering failed.
                pass
        self._consented_reply = accepted
        return accepted

    def _worker(self, operation):
        if self._selected() == TRANSLATE:
            target = gtranslate.TARGET_LANGUAGES[self.target.GetSelection()][0]
            text = self.draft

            def work():
                operation.check()
                translated, _detected = gtranslate.translate_text(text, target,
                                                                  cancel_check=operation.check)
                return translated, "Google Translate"
            return work
        reply = self._selected() == writing.REPLY
        text = writing.reply_payload(self._recent, self.draft) if reply else self.draft
        media = prepare_media("writing", ai_config.utf8_bytes(text), "", self.config["profile"])
        providers = self._providers
        store = self._store
        config = dict(self.config)
        question = self._question()
        # The language the window shows, not re-read from settings (see
        # ai_result_dialog._start for why the dialog's own i18n is used).
        language = self.i18n.language

        def work():
            operation.check()
            return run_chain(operation, providers, ai_config.models_for(config, "writing"),
                             store.get, media, (), question,
                             instructions(language, config["profile"], "writing", context=reply),
                             config["profile"], usage_sink=ai_usage.persist,
                             endpoints=ai_config.endpoints(config))
        return work

    def _replace(self, event=None):
        """Put the latest answer in the composer, replacing the draft it came
        from: the draft is only ever replaced on this explicit choice."""
        if (not self._latest or not self._valid() or self.session.active is not None
                or self.panel.message_field.GetValue() != self._source_draft):
            return
        field = self.panel.message_field
        text = self._latest
        self._close()
        field.ChangeValue(text)
        # Let the composer notice the new text (send button, spelling, typing
        # state) without counting it as the person typing right now.
        restoring = getattr(self.panel, "_restoring_draft", False)
        self.panel._restoring_draft = True
        try:
            self.panel.on_change_message_field(None)
        finally:
            self.panel._restoring_draft = restoring
        field.SetFocus()
        field.SetInsertionPointEnd()
