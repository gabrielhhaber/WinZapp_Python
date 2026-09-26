"""ChatLockMixin — part of MainWindow (see main_window/__init__.py).

Moved verbatim out of main.py. Methods run with ``self`` bound to the
MainWindow instance, so every attribute set in MainWindow.__init__ is
available here.
"""

import logging
import wx
from ui.chat_lock import (
    ChatLockChangePinDialog,
    ChatLockRecoveryDialog,
    ChatLockRevealDialog,
    ChatLockSetupDialog,
    ChatLockUnlockDialog,
    ID_FORGOT_PIN,
    RecoveryKeyDialog,
)
from core.chat_lock_vault import (
    ChatLockVault,
    VaultStateError,
    jid_fingerprint,
    validate_pin,
    validate_reveal_code,
)


class ChatLockMixin:
    """The locked-chats vault: PIN, reveal code, timeout and the locked
    conversations panel.
    """

    _CHAT_LOCK_VAULT_KEY = "chat_lock_vault_v1"
    _CHAT_LOCK_INDEX_KEY = "chat_lock_index_v1"

    def _load_chat_lock_vault(self):
        """Restore the per-account vault before any chat list is built.

        A keyed JID fingerprint index is deliberately stored beside the
        encrypted state.  If that state is damaged, the index still lets the
        list fail closed and keep previously locked chats hidden without
        writing phone-number-bearing JIDs to plaintext metadata.
        """
        raw_index = self.db.get_metadata_json(self._CHAT_LOCK_INDEX_KEY, [])
        self._chat_lock_fingerprints = {
            value for value in raw_index
            if isinstance(value, str) and len(value) == 64
        } if isinstance(raw_index, list) else set()
        token = self.db.get_metadata(self._CHAT_LOCK_VAULT_KEY)
        self._chat_lock_state_error = False
        try:
            self._chat_lock_vault = ChatLockVault.load(self.key, token)
        except VaultStateError:
            # Never log the encrypted token or its JIDs. The keyed index keeps
            # the affected chats hidden until the user explicitly resets the
            # local account rather than silently exposing them.
            logging.exception("[chat-lock] local vault state is unreadable")
            self._chat_lock_vault = None
            self._chat_lock_state_error = True
        self._chat_lock_unlocked = False
        self._chat_lock_timeout_timer = None
        self._locked_chat_rows = ([], [])

    def _chat_lock_candidates(self, jid: str) -> set[str]:
        if not jid:
            return set()
        candidates = {jid}
        try:
            normalized = self._normalize_jid(jid)
        except Exception:
            normalized = jid
        if normalized:
            candidates.add(normalized)
        lid_to_phone = getattr(self, "_lid_to_phone", {})
        phone_to_lid = getattr(self, "_phone_to_lid", {})
        for candidate in tuple(candidates):
            mapped = lid_to_phone.get(candidate) or phone_to_lid.get(candidate)
            if mapped:
                candidates.add(mapped)
        chat = getattr(self, "chats", {}).get(jid)
        if isinstance(chat, dict) and chat.get("remoteJid"):
            candidates.add(chat["remoteJid"])
        return {candidate for candidate in candidates if candidate}

    def _chat_lock_canonical_jid(self, jid: str) -> str:
        candidates = self._chat_lock_candidates(jid)
        phone = next((value for value in candidates if value.endswith("@s.whatsapp.net")), "")
        if phone:
            return phone
        normalized = self._normalize_jid(jid) if jid else ""
        return normalized or jid

    def _persist_chat_lock_vault(self):
        vault = getattr(self, "_chat_lock_vault", None)
        if vault is None:
            raise VaultStateError("locked-conversation state is unavailable")
        fingerprints = sorted(
            jid_fingerprint(self.key, jid) for jid in vault.locked_jids
        )
        # Write the fail-closed index first. is_chat_locked() consults both
        # sources, so an interrupted write can hide one chat too many but can
        # never expose a newly locked chat.
        self.db.set_metadata_json(self._CHAT_LOCK_INDEX_KEY, fingerprints)
        self.db.set_metadata(self._CHAT_LOCK_VAULT_KEY, vault.encrypted_token())
        self._chat_lock_fingerprints = set(fingerprints)

    def is_chat_locked(self, jid: str) -> bool:
        candidates = self._chat_lock_candidates(jid)
        vault = getattr(self, "_chat_lock_vault", None)
        fingerprints = getattr(self, "_chat_lock_fingerprints", set())
        return (
            vault is not None
            and any(vault.is_locked(candidate) for candidate in candidates)
        ) or any(
            jid_fingerprint(self.key, candidate) in fingerprints
            for candidate in candidates
        )

    def chat_lock_navigation_visible(self) -> bool:
        vault = getattr(self, "_chat_lock_vault", None)
        if vault is None:
            return False
        # The privacy option is literal: unlocking through the secret search
        # code must not make a previously hidden navigation row suddenly
        # disclose that the vault exists. The open panel already provides its
        # own close/settings controls for the current session.
        # A vault nobody set up yet has nothing to hide: keep the row (and
        # Alt+7) visible so the feature can be found; it opens an empty list.
        return not vault.configured or not vault.hide_navigation

    def _refresh_chat_lock_navigation(self):
        panel = getattr(self, "navigation_panel", None)
        if panel is not None:
            panel.rebuild_items()

    def _chat_lock_error(self, message_key: str, **values):
        message = self.i18n.t(message_key).format(**values)
        wx.MessageBox(message, self.app_name, wx.OK | wx.ICON_WARNING, self)

    def _show_recovery_key(self, recovery_code: str) -> bool:
        dialog = RecoveryKeyDialog(self, self.i18n, recovery_code)
        try:
            return dialog.ShowModal() == wx.ID_OK
        finally:
            dialog.Destroy()

    def _verify_chat_lock_pin(self, vault: ChatLockVault, pin: str) -> bool:
        """Verify a PIN and persist the restart-resistant attempt state."""
        remaining = vault.remaining_pin_lockout_seconds()
        if remaining:
            self._chat_lock_error("chat_lock_wait", seconds=remaining)
            return False
        if vault.verify_pin(pin):
            if vault.clear_pin_failures():
                self._persist_chat_lock_vault()
            return True
        remaining = vault.record_pin_failure()
        self._persist_chat_lock_vault()
        key = "chat_lock_wait" if remaining else "chat_lock_wrong_pin"
        self._chat_lock_error(key, seconds=remaining)
        return False

    def _configure_chat_lock_vault(self) -> bool:
        dialog = ChatLockSetupDialog(self, self.i18n)
        try:
            while dialog.ShowModal() == wx.ID_OK:
                pin = dialog.pin.GetValue()
                confirm = dialog.confirm.GetValue()
                reveal = dialog.reveal.GetValue()
                if not validate_pin(pin):
                    self._chat_lock_error("chat_lock_pin_invalid")
                    dialog.pin.SetFocus()
                    continue
                if pin != confirm:
                    self._chat_lock_error("chat_lock_pin_mismatch")
                    dialog.confirm.SetFocus()
                    continue
                if not validate_reveal_code(reveal):
                    self._chat_lock_error("chat_lock_reveal_invalid")
                    dialog.reveal.SetFocus()
                    continue
                candidate = ChatLockVault(self.key)
                recovery_code = candidate.configure(pin, reveal)
                if not self._show_recovery_key(recovery_code):
                    return False
                self._chat_lock_vault = candidate
                self._chat_lock_state_error = False
                self._persist_chat_lock_vault()
                self.output(self.i18n.t("chat_lock_configured"), interrupt=True)
                return True
            return False
        finally:
            dialog.Destroy()

    def _recover_chat_lock_pin(self) -> bool:
        vault = getattr(self, "_chat_lock_vault", None)
        if vault is None:
            self._chat_lock_error("chat_lock_state_unavailable")
            return False
        dialog = ChatLockRecoveryDialog(self, self.i18n)
        try:
            while dialog.ShowModal() == wx.ID_OK:
                recovery = dialog.recovery.GetValue()
                pin = dialog.pin.GetValue()
                confirm = dialog.confirm.GetValue()
                if not vault.verify_recovery_code(recovery):
                    self._chat_lock_error("chat_lock_recovery_invalid")
                    dialog.recovery.SetFocus()
                    continue
                if not validate_pin(pin):
                    self._chat_lock_error("chat_lock_pin_invalid")
                    dialog.pin.SetFocus()
                    continue
                if pin != confirm:
                    self._chat_lock_error("chat_lock_pin_mismatch")
                    dialog.confirm.SetFocus()
                    continue
                previous_token = vault.encrypted_token()
                new_recovery = vault.reset_pin(recovery, pin)
                if not self._show_recovery_key(new_recovery):
                    # Reset mutated the in-memory vault. Restore the exact
                    # encrypted pre-reset state so cancelling the code display
                    # leaves the old PIN and recovery code fully usable.
                    self._chat_lock_vault = ChatLockVault.load(
                        self.key, previous_token
                    )
                    return False
                self._persist_chat_lock_vault()
                return True
            return False
        finally:
            dialog.Destroy()

    def unlock_chat_lock_vault(self, *, show_panel=True) -> bool:
        vault = getattr(self, "_chat_lock_vault", None)
        if vault is None:
            self._chat_lock_error("chat_lock_state_unavailable")
            return False
        if not vault.configured:
            return False
        if self._chat_lock_unlocked:
            if show_panel:
                self.show_locked_chats_panel()
            return True

        dialog = ChatLockUnlockDialog(self, self.i18n)
        try:
            while True:
                result = dialog.ShowModal()
                if result == int(ID_FORGOT_PIN):
                    if self._recover_chat_lock_pin():
                        self._chat_lock_unlocked = True
                        break
                    # Recovery may have rotated and then rolled back the vault
                    # object when the new recovery-key display was cancelled.
                    # Keep this dialog's verifier on the restored object.
                    vault = self._chat_lock_vault
                    dialog.pin.SetFocus()
                    continue
                if result != wx.ID_OK:
                    return False
                if not self._verify_chat_lock_pin(vault, dialog.pin.GetValue()):
                    dialog.pin.SetValue("")
                    dialog.pin.SetFocus()
                    continue
                self._chat_lock_unlocked = True
                break
        finally:
            dialog.Destroy()

        self._refresh_chat_lock_navigation()
        self._arm_chat_lock_timeout()
        if show_panel:
            self.show_locked_chats_panel()
        return True

    def unlock_chat_lock_settings(self) -> bool:
        """Authenticate or set up the vault without opening its chat list."""
        vault = getattr(self, "_chat_lock_vault", None)
        if vault is None:
            self._chat_lock_error("chat_lock_state_unavailable")
            return False
        if not vault.configured:
            if not self._configure_chat_lock_vault():
                return False
            self._chat_lock_unlocked = True
            self._refresh_chat_lock_navigation()
            self._arm_chat_lock_timeout()
            return True
        return self.unlock_chat_lock_vault(show_panel=False)

    def try_reveal_locked_chats(self, value: str) -> bool:
        vault = getattr(self, "_chat_lock_vault", None)
        if vault is None or not vault.configured or not vault.hide_navigation:
            return False
        if not vault.authorizes_reveal(value):
            return False
        self.conversations_panel.search_field.ChangeValue("")
        self.add_chats_to_ui()
        self.unlock_chat_lock_vault(show_panel=True)
        return True

    def lock_chat(self, jid: str):
        vault = getattr(self, "_chat_lock_vault", None)
        if vault is None and self._chat_lock_state_error:
            self._chat_lock_error("chat_lock_state_unavailable")
            return
        if vault is None or not vault.configured:
            if not self._configure_chat_lock_vault():
                return
            vault = self._chat_lock_vault
        elif not self._chat_lock_unlocked:
            if not self.unlock_chat_lock_vault(show_panel=False):
                return

        canonical = self._chat_lock_canonical_jid(jid)
        vault.lock_chat(canonical)
        self._persist_chat_lock_vault()
        cp = getattr(self, "conversations_panel", None)
        if cp is not None and cp.conversation is not None:
            if self.is_chat_locked(cp.conversation.get("remoteJid", "")):
                cp.close_conversation_for_panel_switch()
        self._chat_lock_unlocked = False
        self._cancel_chat_lock_timeout()
        self._refresh_chat_lock_navigation()
        self._schedule_set_chats()
        self.output(self.i18n.t("chat_lock_chat_locked"), interrupt=True)

    def unlock_chat(self, jid: str):
        vault = getattr(self, "_chat_lock_vault", None)
        if vault is None or not self._chat_lock_unlocked:
            return
        for candidate in self._chat_lock_candidates(jid):
            vault.unlock_chat(candidate)
        self._persist_chat_lock_vault()
        self._schedule_set_chats()
        self.touch_chat_lock_timeout()
        self.output(self.i18n.t("chat_lock_chat_unlocked"), interrupt=True)

    def _forget_chat_lock(self, jid: str):
        """Remove every local vault identity for a conversation being deleted.

        This is deliberately not gated by the unlocked UI state: once the user
        confirms deletion, retaining an invisible stale entry would make a
        later conversation with the same JID unexpectedly reappear locked.
        """
        candidates = self._chat_lock_candidates(jid)
        vault = getattr(self, "_chat_lock_vault", None)
        if vault is not None:
            before = vault.locked_jids
            for candidate in candidates:
                vault.unlock_chat(candidate)
            if vault.locked_jids != before:
                self._persist_chat_lock_vault()
            return

        fingerprints = getattr(self, "_chat_lock_fingerprints", set())
        removed = {
            jid_fingerprint(self.key, candidate) for candidate in candidates
        }
        remaining = fingerprints - removed
        if remaining != fingerprints:
            self.db.set_metadata_json(
                self._CHAT_LOCK_INDEX_KEY, sorted(remaining)
            )
            self._chat_lock_fingerprints = remaining

    def set_chat_lock_navigation_hidden(self, hide: bool):
        vault = getattr(self, "_chat_lock_vault", None)
        if vault is None or not self._chat_lock_unlocked:
            return
        vault.set_hide_navigation(hide)
        self._persist_chat_lock_vault()
        self._refresh_chat_lock_navigation()
        self.touch_chat_lock_timeout()

    def _cancel_chat_lock_timeout(self):
        timer = getattr(self, "_chat_lock_timeout_timer", None)
        if timer is not None:
            try:
                timer.Stop()
            except Exception:
                logging.exception("[chat-lock] could not stop auto-lock timer")
        self._chat_lock_timeout_timer = None

    def _arm_chat_lock_timeout(self):
        vault = getattr(self, "_chat_lock_vault", None)
        if (
            vault is None
            or not vault.configured
            or not getattr(self, "_chat_lock_unlocked", False)
            or vault.auto_lock_minutes <= 0
        ):
            self._cancel_chat_lock_timeout()
            return
        delay_ms = vault.auto_lock_minutes * 60 * 1000
        timer = getattr(self, "_chat_lock_timeout_timer", None)
        if timer is not None:
            try:
                timer.Restart(delay_ms)
                return
            except Exception:
                logging.exception("[chat-lock] could not restart auto-lock timer")
                self._cancel_chat_lock_timeout()
        self._chat_lock_timeout_timer = wx.CallLater(
            delay_ms,
            self._on_chat_lock_timeout,
        )

    def touch_chat_lock_timeout(self):
        """Restart the inactivity timeout after input in an open vault."""
        if getattr(self, "_chat_lock_unlocked", False):
            self._arm_chat_lock_timeout()

    def _on_chat_lock_char_hook(self, event):
        """Track keyboard activity and provide a global emergency close key."""
        try:
            modifiers = event.GetModifiers()
            if (
                modifiers == (wx.MOD_CONTROL | wx.MOD_SHIFT)
                and event.GetKeyCode() == ord("K")
                and getattr(self, "_chat_lock_unlocked", False)
            ):
                self.lock_chat_vault()
                return
            self.touch_chat_lock_timeout()
        except Exception:
            logging.exception("[chat-lock] activity hotkey handler failed")
        event.Skip()

    def _on_chat_lock_timeout(self):
        self._chat_lock_timeout_timer = None
        if not getattr(self, "_chat_lock_unlocked", False):
            return
        self.lock_chat_vault(silent=True)
        self.output(self.i18n.t("chat_lock_timed_out"), interrupt=True)

    def set_chat_lock_timeout_minutes(self, minutes: int):
        vault = getattr(self, "_chat_lock_vault", None)
        if vault is None or not self._chat_lock_unlocked:
            return
        vault.set_auto_lock_minutes(minutes)
        self._persist_chat_lock_vault()
        self._arm_chat_lock_timeout()

    def change_chat_lock_pin(self):
        vault = getattr(self, "_chat_lock_vault", None)
        if vault is None or not self._chat_lock_unlocked:
            return
        self._cancel_chat_lock_timeout()
        dialog = ChatLockChangePinDialog(self, self.i18n)
        try:
            while dialog.ShowModal() == wx.ID_OK:
                if not self._verify_chat_lock_pin(vault, dialog.current.GetValue()):
                    dialog.current.SetValue("")
                    dialog.current.SetFocus()
                    continue
                pin = dialog.pin.GetValue()
                if not validate_pin(pin):
                    self._chat_lock_error("chat_lock_pin_invalid")
                    dialog.pin.SetFocus()
                    continue
                if pin != dialog.confirm.GetValue():
                    self._chat_lock_error("chat_lock_pin_mismatch")
                    dialog.confirm.SetFocus()
                    continue
                previous_token = vault.encrypted_token()
                recovery_code = vault.change_pin(dialog.current.GetValue(), pin)
                if not self._show_recovery_key(recovery_code):
                    self._chat_lock_vault = ChatLockVault.load(
                        self.key, previous_token
                    )
                    return
                self._persist_chat_lock_vault()
                self.output(self.i18n.t("chat_lock_pin_changed"), interrupt=True)
                return
        finally:
            dialog.Destroy()
            if self._chat_lock_unlocked:
                self._arm_chat_lock_timeout()

    def change_chat_lock_reveal_code(self):
        vault = getattr(self, "_chat_lock_vault", None)
        if vault is None or not self._chat_lock_unlocked:
            return
        self._cancel_chat_lock_timeout()
        dialog = ChatLockRevealDialog(self, self.i18n)
        try:
            while dialog.ShowModal() == wx.ID_OK:
                if not self._verify_chat_lock_pin(vault, dialog.pin.GetValue()):
                    dialog.pin.SetValue("")
                    dialog.pin.SetFocus()
                    continue
                if not validate_reveal_code(dialog.reveal.GetValue()):
                    self._chat_lock_error("chat_lock_reveal_invalid")
                    dialog.reveal.SetFocus()
                    continue
                vault.change_reveal_code(
                    dialog.pin.GetValue(), dialog.reveal.GetValue()
                )
                self._persist_chat_lock_vault()
                self.output(self.i18n.t("chat_lock_reveal_changed"), interrupt=True)
                return
        finally:
            dialog.Destroy()
            if self._chat_lock_unlocked:
                self._arm_chat_lock_timeout()

    def show_locked_chats_panel(self):
        vault = getattr(self, "_chat_lock_vault", None)
        never_set_up = vault is not None and not vault.configured
        if not never_set_up and not getattr(self, "_chat_lock_unlocked", False):
            if not self.unlock_chat_lock_vault(show_panel=False):
                return
        self.conversations_panel.Hide()
        if hasattr(self, "archived_conversations_panel"):
            self.archived_conversations_panel.Hide()
        if hasattr(self, "status_panel"):
            self.status_panel.Hide()
        if hasattr(self, "calls_panel"):
            self.calls_panel.Hide()
        panel = self.locked_conversations_panel
        chats, names = getattr(self, "_locked_chat_rows", ([], []))
        panel.set_all_chats(chats, names)
        panel.Show()
        self.content_panel.Layout()
        panel.restore_selection()
        self.touch_chat_lock_timeout()

    def lock_chat_vault(self, *, silent=False, show_conversations=True):
        self._cancel_chat_lock_timeout()
        self._chat_lock_unlocked = False
        cp = getattr(self, "conversations_panel", None)
        if cp is not None and cp.conversation is not None:
            if self.is_chat_locked(cp.conversation.get("remoteJid", "")):
                cp.close_conversation_for_panel_switch()
        panel = getattr(self, "locked_conversations_panel", None)
        if panel is not None:
            panel.set_all_chats([], [])
            panel.Hide()
        self._refresh_chat_lock_navigation()
        if show_conversations and hasattr(self, "conversations_panel"):
            self.conversations_panel.conversations_label.Show()
            self.conversations_panel.conversations_list.Show()
            self.conversations_panel.Show()
            self.content_panel.Layout()
            self.conversations_panel._restore_conversation_selection()
        if not silent:
            self.output(self.i18n.t("chat_lock_closed"), interrupt=True)

    def open_locked_conversation(self, chat: dict):
        jid = chat.get("remoteJid", "")
        if not self._chat_lock_unlocked or not self.is_chat_locked(jid):
            return
        self.touch_chat_lock_timeout()
        if hasattr(self, "archived_conversations_panel"):
            self.archived_conversations_panel.Hide()
        if hasattr(self, "locked_conversations_panel"):
            self.locked_conversations_panel.Hide()
        if hasattr(self, "status_panel"):
            self.status_panel.Hide()
        if hasattr(self, "calls_panel"):
            self.calls_panel.Hide()
        self.conversations_panel.conversations_label.Hide()
        self.conversations_panel.conversations_list.Hide()
        self.conversations_panel.Show()
        self.content_panel.Layout()
        self.conversations_panel.navigate_to_conversation(chat)
