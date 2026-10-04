"""ComposerMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

Moved verbatim out of ui/conversations.py. Methods run with ``self`` bound to
the ConversationsPanel instance, so every attribute set in
ConversationsPanel.__init__/init_UI is available here.
"""

import os
import tempfile
import threading
import wx
from ui.dialogs.emoji_picker import choose_and_insert_emoji
from core.attachment_types import DEFAULT_PASTED_AUDIO_AS, pasted_attachment_media_type
from core.link_preview import (
    fetch_link_preview,
    find_first_url,
)
from core.utils import (
    normalize_line_separators,
    to_editor_line_endings,
)
from core.spell_checker import (
    spell_check_active,
    value_index,
    windows_spellcheck_enabled,
)


# Keys that move the caret in the message field; each may land on a
# misspelled word.
_CARET_KEYS = frozenset((
    wx.WXK_LEFT, wx.WXK_RIGHT, wx.WXK_UP, wx.WXK_DOWN, wx.WXK_HOME,
    wx.WXK_END, wx.WXK_PAGEUP, wx.WXK_PAGEDOWN,
))


class ComposerMixin:
    """The message composer: spell check, link preview, emoji picker,
    key/char/paste handling and the call buttons next to it.
    """

    def _spell_check_enabled(self) -> bool:
        """Whether spell checking runs in the message field.

        Three-valued, set in Settings > Geral (`spell_check_mode`): follow
        Windows' own spelling setting (Settings > Time & language > Typing >
        Spelling — the default), or override it in either direction. The
        whole decision lives in spell_check_active() (core/spell_checker.py),
        which is pure and therefore testable without a wx.App; this only
        supplies its two inputs.

        Read on every keystroke rather than cached at construction, so both
        sources of truth take effect immediately — no restart, and no need
        for the settings dialog to reach into this panel. The registry read
        behind windows_spellcheck_enabled() is memoised for a couple of
        seconds precisely because of that call rate.
        """
        try:
            general = self.main_window.settings.get("general", {})
        except Exception:
            return True
        return spell_check_active(general, windows_spellcheck_enabled())

    def _play_spelling_error_sound(self):
        """Play the currently configured spelling-error Sound Event."""
        self.main_window.spelling_error_sound.play()

    def _cue_spelling_at_caret(self, *_):
        """Play the error sound if the caret just arrived at a misspelled word."""
        spell_checker = getattr(self, "_spell_checker", None)
        if spell_checker is None or not self._spell_check_enabled():
            return
        text = self.message_field.GetValue()
        position = self.message_field.GetInsertionPoint()
        width = 2 if os.name == "nt" and "\r\n" not in text else 1
        spell_checker.caret_moved(text, value_index(text, position, width))

    def _cue_spelling_at_caret_on_click(self, event):
        event.Skip()
        wx.CallAfter(self._cue_spelling_at_caret)

    def on_change_message_field(self, event):
        # Don't touch button visibility while recording or staging attachments.
        if self._is_recording or self._attachment_panel.IsShown():
            return
        msg = self.message_field.GetValue()
        spell_checker = getattr(self, "_spell_checker", None)
        if spell_checker is not None:
            if self._spell_check_enabled():
                spell_checker.text_changed(msg)
            else:
                # Keep the checker's view of the field current while it is
                # switched off, so re-enabling it mid-message does not read
                # the whole existing text as one freshly typed word and fire
                # the cue for something the user typed minutes ago.
                spell_checker.reset(msg)
        if msg.strip():
            self.send_message_btn.Show()
            self.record_voice_message_btn.Hide()
            self._record_voice_alt_btn.Hide()
            if hasattr(self, "_record_voice_system_btn"):
                self._record_voice_system_btn.Hide()
        else:
            self.send_message_btn.Hide()
            self.record_voice_message_btn.Show()
            self._record_voice_alt_btn.Show()
            if hasattr(self, "_record_voice_system_btn"):
                self._record_voice_system_btn.Show()
        # Sync typing status with WPPConnect (only on state transitions)
        if self.conversation is not None:
            jid = self.conversation.get("remoteJid", "")
            if jid and not jid.endswith("@newsletter"):
                is_group = jid.endswith("@g.us")
                now_typing = bool(msg.strip())
                if now_typing != self._is_typing:
                    self._is_typing = now_typing
                    self.main_window.send_typing_status(jid, now_typing, is_group)
        self._on_text_changed_mention_check()
        self._schedule_link_preview_check()

    # ── Outgoing link preview (see core/link_preview.py) ────────────────────

    _LINK_PREVIEW_DEBOUNCE_MS = 700

    def _schedule_link_preview_check(self):
        """Debounce URL detection: re-checked shortly after typing pauses,
        not on every keystroke — a preview fetch is a real HTTP request."""
        if self._link_preview_debounce_timer is not None:
            self._link_preview_debounce_timer.Stop()
        self._link_preview_debounce_timer = wx.CallLater(
            self._LINK_PREVIEW_DEBOUNCE_MS, self._check_link_preview_for_current_text
        )

    def _check_link_preview_for_current_text(self):
        url = find_first_url(self.message_field.GetValue())

        if url != self._link_preview_source_url and (
            self._pending_link_preview is not None or self._link_preview_source_url
        ):
            self._clear_link_preview()

        if not url or url == self._link_preview_dismissed_url:
            return
        if self._pending_link_preview is not None and self._link_preview_source_url == url:
            return  # already resolved for this exact URL

        self._link_preview_fetch_token += 1
        token = self._link_preview_fetch_token
        self._link_preview_source_url = url

        def _bg_fetch():
            preview = fetch_link_preview(url)
            wx.CallAfter(self._on_link_preview_fetched, token, url, preview)

        threading.Thread(target=_bg_fetch, daemon=True).start()

    def _on_link_preview_fetched(self, token, url, preview):
        # Superseded by a later fetch, or by the field being cleared, while
        # this one was still in flight on its own thread.
        if token != self._link_preview_fetch_token:
            return
        if find_first_url(self.message_field.GetValue()) != url:
            return
        if not preview:
            return  # no title/description available — silently no-op
        self._pending_link_preview = preview
        self._remove_link_preview_btn.Show()
        self.conversation_panel.Layout()

    def _on_remove_link_preview(self, event=None):
        """User explicitly dismissed the preview — stays dismissed for this
        exact URL until the field's URL actually changes to something else
        (mirrors WhatsApp Web's own composer: closing the card doesn't bring
        it right back while you keep typing around the same link)."""
        self._link_preview_dismissed_url = self._link_preview_source_url
        self._clear_link_preview()
        wx.CallAfter(self.message_field.SetFocus)

    def _clear_link_preview(self):
        self._link_preview_fetch_token += 1  # invalidate any in-flight fetch
        self._pending_link_preview = None
        self._link_preview_source_url = ""
        if self._remove_link_preview_btn.IsShown():
            self._remove_link_preview_btn.Hide()
            self.conversation_panel.Layout()

    def _on_open_emoji_picker(self, event):
        """Insert an emoji at the caret without leaving the message editor."""
        if (
            self.conversation is None
            or not self.conversation_panel.IsShown()
            or not self.message_field.IsShown()
            or not self.message_field.IsEnabled()
            or not self.message_field.IsEditable()
        ):
            return
        choose_and_insert_emoji(self, self.message_field, self.main_window.i18n)

    def _on_conversation_char_hook(self, event):
        if self._is_phantom_nvda_char(event):
            # Veto here too, not just in _on_message_field_char(): this hook
            # runs for the whole panel regardless of which child control
            # currently has focus, and the "type anywhere to reply" redirect
            # below treats 'ÿ' as an ordinary alnum character — chr(0xFF)
            # .isalnum() is True in Python — so with focus on the
            # conversations/messages list (the common case while browsing
            # with a screen reader) it was moving focus to message_field and
            # writing 'ÿ' into it via WriteText(), bypassing that other
            # veto entirely, since WriteText() never raises EVT_CHAR.
            return  # consume — do not insert, do not Skip()
        kc = event.GetKeyCode()
        # Intercept Esc and Enter when the mention suggestion list has focus so
        # they are handled here, before the accelerator table fires
        # close_conversation for Esc or any other panel-level binding.
        if hasattr(self, "_mention_panel") and self._mention_panel.IsShown():
            if kc == wx.WXK_ESCAPE:
                self._hide_mention_suggestions()
                wx.CallAfter(self.message_field.SetFocus)
                return  # do NOT Skip — blocks the Esc → close_conversation accelerator
            if wx.Window.FindFocus() is self._mention_list and kc in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
                idx = self._mention_list.GetSelection()
                if 0 <= idx < len(self._mention_suggestions):
                    name, jid = self._mention_suggestions[idx]
                    self._insert_mention(name, jid)
                return  # do NOT Skip
        if not self._should_redirect_char_to_message(event):
            event.Skip()
            return

        key_code = event.GetUnicodeKey()
        # EVT_CHAR_HOOK reports the raw (unshifted-case) key code for letters,
        # so A-Z always arrives uppercase regardless of Shift/Caps Lock — apply
        # the correct case ourselves instead of trusting the hook's casing.
        if ord("A") <= key_code <= ord("Z"):
            caps_on = wx.GetKeyState(wx.WXK_CAPITAL)
            upper = event.ShiftDown() != caps_on
            char = chr(key_code) if upper else chr(key_code).lower()
        else:
            char = chr(key_code)
        self.message_field.SetFocus()
        self.message_field.WriteText(char)

    def _should_redirect_char_to_message(self, event) -> bool:
        if self.conversation is None or not self.conversation_panel.IsShown():
            return False
        if not self.message_field.IsShown() or not self.message_field.IsEnabled():
            return False
        if self._is_recording:
            return False
        if event.ControlDown() or event.AltDown():
            return False
        if hasattr(event, "MetaDown") and event.MetaDown():
            return False

        key = event.GetUnicodeKey()
        if key == wx.WXK_NONE:
            return False
        if self._is_phantom_nvda_char(event):
            # chr(0xFF).isalnum() is True in Python, so the alnum check
            # below would otherwise wave this straight through — see
            # _is_phantom_nvda_char()'s docstring.
            return False
        try:
            # Only redirect alphanumeric characters — this prevents special
            # keys like Delete (127), Backspace (8), and other control/function
            # characters from being swallowed and written into the message field.
            if not chr(key).isalnum():
                return False
        except (ValueError, OverflowError):
            return False

        focus = wx.Window.FindFocus()
        if focus is self.message_field or isinstance(focus, wx.TextCtrl):
            return False

        return True

    def _sync_voice_call_button(self, jid: str):
        jid = str(jid or "")
        is_self_chat = bool(jid) and self.main_window._is_self_jid(jid)
        unavailable = jid.endswith(("@g.us", "@newsletter", "@broadcast")) or is_self_chat
        self._voice_call_btn.Show(bool(jid) and not unavailable)
        self._video_call_btn.Show(bool(jid) and not unavailable)
        self.conversation_panel.Layout()
        self.Layout()

    def _on_voice_call(self, _event=None):
        if not self.conversation:
            return
        jid = str(self.conversation.get("remoteJid") or "")
        name = self.conversation_name or self.conversation.get("name") or ""
        self.main_window.start_voice_call(jid, name)

    def _on_video_call(self, _event=None):
        if not self.conversation:
            return
        jid = str(self.conversation.get("remoteJid") or "")
        name = self.conversation_name or self.conversation.get("name") or ""
        self.main_window.start_video_call(jid, name)

    def _focus_is_in_a_text_entry(self) -> bool:
        """Whether the keyboard focus is somewhere the user is typing.

        Same test `_should_redirect_char_to_message()` already uses above.
        """
        focus = wx.Window.FindFocus()
        return focus is self.message_field or isinstance(focus, wx.TextCtrl)

    # The accelerator table lives on `conversation_panel`, the message field's
    # PARENT, so it sees these keys before the TextCtrl does -- that is how
    # Ctrl+Shift+A/E work from inside the editor. For the call keys that is a
    # trap rather than a feature: Ctrl+Shift+V is the universal "paste without
    # formatting" chord, and Ctrl+Alt+Shift+V is indistinguishable from
    # AltGr+Shift+V on pt-BR/pl layouts. Either one placed a REAL call to the
    # open contact, with no confirmation, straight from the message the user
    # was typing. A plain wx.TextCtrl binds neither chord to anything, so
    # ignoring them while typing restores exactly what every other app does
    # with them: nothing. Pressing the on-screen button still calls, because
    # that is unambiguous -- these guards are only on the accelerator path.
    def _on_accel_voice_call(self, event=None):
        if self._focus_is_in_a_text_entry():
            return
        self._on_voice_call(event)

    def _on_accel_video_call(self, event=None):
        if self._focus_is_in_a_text_entry():
            return
        self._on_video_call(event)

    def _on_message_field_key_down(self, event):
        """↓ moves focus to the mention list when suggestions are visible.
        Shift+Enter inserts a newline instead of sending — TE_PROCESS_ENTER
        makes plain Enter fire EVT_TEXT_ENTER (send) on this control, and
        wx's native multiline edit control only inserts a literal newline on
        Ctrl+Enter, with no Shift+Enter equivalent of its own (issue #16)."""
        kc = event.GetKeyCode()
        if kc == wx.WXK_DOWN and self._mention_panel.IsShown():
            if self._mention_list.GetCount() > 0:
                self._mention_list.SetFocus()
                self._mention_list.SetSelection(0)
            return  # consume — don't let the field handle ↓
        if kc in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER) and event.ShiftDown():
            # WriteText() inserts at the control's own insertion point (and
            # over any active selection) and lets the native control manage
            # the caret itself — necessary because on Windows a multiline
            # wx.TextCtrl stores line breaks internally as \r\n while
            # GetInsertionPoint()/SetInsertionPoint() count positions in that
            # native representation, not the \n-only positions GetValue()
            # reports. The previous approach (rebuild the whole value with a
            # plain "\n", then SetInsertionPoint(pos + 1)) assumed one
            # inserted character, but Windows silently expands it to two —
            # so the caret landed one character short, between the \r and
            # the \n. NVDA then kept announcing everything typed next as
            # still on the previous line (issue #48).
            self.message_field.WriteText("\n")
            self.on_change_message_field(None)
            return  # consume — don't send and don't double-insert
        if kc in _CARET_KEYS:
            # The caret has not moved yet; check once the control has.
            wx.CallAfter(self._cue_spelling_at_caret)
        event.Skip()

    @staticmethod
    def _is_phantom_nvda_char(event) -> bool:
        """True for the bogus U+00FF character that a screen reader's own
        modifier-key gestures (Windows+NVDA+Left/Right and others — issue
        #71 — and reportedly Alt+Tab as well) leak into whatever control is
        focused, or into the message field via the "type anywhere to reply"
        redirect below when it isn't (see _on_conversation_char_hook()).

        Reported live: each press of Windows+NVDA+Left/Right inserted one
        literal 'ÿ' into the message field, even though no text key was
        pressed and the same gestures type nothing in other applications.
        NVDA's own keyboard hook is supposed to swallow these combinations
        entirely; when the OS still emits a WM_CHAR for one anyway (observed
        specifically for Windows-key gestures NVDA intercepts), it carries
        the character U+00FF — not a value any real keyboard layout produces
        by pressing the Windows key plus an arrow. That makes it safe to
        veto unconditionally rather than trying to special-case NVDA's own
        modifier state, which wx never sees. Checked at every entry point
        that can put a character into the message field — see
        _on_conversation_char_hook() for the other one — because this exact
        code point is never a legitimate keystroke.
        """
        return event.GetUnicodeKey() == 0xFF

    def _on_message_field_char(self, event):
        if self._is_phantom_nvda_char(event):
            return  # veto — do not insert, do not Skip()
        event.Skip()

    def _on_text_field_paste(self, event):
        """Intercept pastes: non-text clipboard content becomes an
        attachment (see _paste_clipboard_as_attachment()); otherwise, Unicode
        line/paragraph separators become \n.

        Bound to every field here whose text ends up on WhatsApp — the
        message field and the attachment caption — and works off the control
        that raised the event, so adding another one is a single Bind().

        Rich clipboard sources — Google Docs, Word, websites, Apple apps —
        put U+2028 LINE SEPARATOR / U+2029 PARAGRAPH SEPARATOR where a plain
        editor stores \n. A wx.TextCtrl keeps them verbatim: the native
        control does not render them as breaks (a paste looks like a single
        long line), yet WhatsApp renders U+2029 as a paragraph break on the
        receiving side. The result is the "it looks fine here but arrives
        with weird breaks" report. Normalizing the pasted text here makes the
        field, the screen reader and the recipient all agree.
        """
        if not wx.TheClipboard.Open():
            event.Skip()
            return
        try:
            if self._paste_clipboard_as_attachment():
                return
            if not wx.TheClipboard.IsSupported(wx.DataFormat(wx.DF_UNICODETEXT)):
                event.Skip()
                return
            data = wx.TextDataObject()
            if not wx.TheClipboard.GetData(data):
                event.Skip()
                return
            text = data.GetText()
        finally:
            wx.TheClipboard.Close()
        target = event.GetEventObject()
        normalized = normalize_line_separators(text)
        # Multiline only: a screen reader needs CRLF to navigate the pasted
        # block line by line, and the send path collapses it back to a bare
        # newline before anything reaches WhatsApp. The caption field shares
        # this handler and
        # is single-line — it cannot navigate lines and would just hold the
        # control characters. See to_editor_line_endings().
        if normalized and getattr(target, "IsMultiLine", None) and target.IsMultiLine():
            normalized = to_editor_line_endings(normalized)
        if normalized != text and target is not None:
            # WriteText() replaces the current selection and fires EVT_TEXT,
            # keeping the mention check / send-button logic in sync.
            target.WriteText(normalized)
            return  # consume — the native paste must not run on top of this
        event.Skip()

    def _paste_from_messages_list(self) -> bool:
        """Paste clipboard content while the message history has focus.

        File/image clipboard formats keep their attachment semantics; text is
        inserted into the composer at its current caret position. This avoids
        Windows exposing a copied Explorer file as a text path and makes Ctrl+V
        behave consistently whether focus is in the history or the composer.
        """
        if self.conversation is None:
            return False

        if not wx.TheClipboard.Open():
            self.message_field.SetFocus()
            return True

        text = None
        try:
            if self._paste_clipboard_as_attachment():
                return True
            if wx.TheClipboard.IsSupported(wx.DataFormat(wx.DF_UNICODETEXT)):
                data = wx.TextDataObject()
                if wx.TheClipboard.GetData(data):
                    # message_field is multiline; same reasoning as
                    # _on_text_field_paste().
                    text = to_editor_line_endings(data.GetText())
        finally:
            wx.TheClipboard.Close()

        self.message_field.SetFocus()
        if text is not None:
            self.message_field.WriteText(text)
        return True

    def _paste_clipboard_as_attachment(self) -> bool:
        """Ctrl+V of non-text clipboard content (files copied in Explorer, or
        an image copied from a browser/screenshot tool) attaches it directly
        — same shortcut the official WhatsApp client offers — instead of
        doing nothing useful in a plain wx.TextCtrl. Files skip the picker
        dialog entirely and land straight in the attachment panel with the
        caption field focused, exactly like choosing them there would.

        Must be called with the clipboard already open (the caller,
        _on_text_field_paste, holds it for its own checks too — nested
        wx.TheClipboard.Open() calls fail). Returns True when it staged
        something, so the caller must not also run the native/text paste.
        """
        if self.conversation is None:
            return False

        if wx.TheClipboard.IsSupported(wx.DataFormat(wx.DF_FILENAME)):
            data = wx.FileDataObject()
            if wx.TheClipboard.GetData(data):
                paths = [p for p in data.GetFilenames() if os.path.isfile(p)]
                if paths:
                    # An audio file goes out as audio or as a document by the
                    # user's choice (Settings > Files and saving), so the row
                    # shown while sending already has the type WhatsApp will
                    # show — not one a later refresh corrects.
                    pasted_audio_as = self.main_window.settings.get(
                        "general", {}).get("pasted_audio_as", DEFAULT_PASTED_AUDIO_AS)
                    for path in paths:
                        self._staged_attachments.append(
                            {
                                "path": path,
                                "media_type": pasted_attachment_media_type(
                                    path, pasted_audio_as),
                            }
                        )
                    self._show_attachment_panel()
                    return True

        if wx.TheClipboard.IsSupported(wx.DataFormat(wx.DF_BITMAP)):
            data = wx.BitmapDataObject()
            if wx.TheClipboard.GetData(data):
                bitmap = data.GetBitmap()
                if bitmap.IsOk():
                    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                    tmp.close()
                    if bitmap.SaveFile(tmp.name, wx.BITMAP_TYPE_PNG):
                        self._staged_attachments.append(
                            {"path": tmp.name, "media_type": "image"}
                        )
                        self._show_attachment_panel()
                        return True

        return False

    def _on_mention_list_key_down(self, event):
        """Keyboard navigation inside the mention suggestion list."""
        kc = event.GetKeyCode()

        if kc in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            idx = self._mention_list.GetSelection()
            if 0 <= idx < len(self._mention_suggestions):
                name, jid = self._mention_suggestions[idx]
                self._insert_mention(name, jid)
            return

        if kc == wx.WXK_ESCAPE:
            self._hide_mention_suggestions()
            self.message_field.SetFocus()
            return

        if kc == wx.WXK_BACK:
            # Backspace: remove last char from message field and update filter
            pos = self.message_field.GetInsertionPoint()
            if pos > 0:
                text = self.message_field.GetValue()
                self.message_field.ChangeValue(text[:pos - 1] + text[pos:])
                self.message_field.SetInsertionPoint(pos - 1)
                wx.CallAfter(self._on_mention_list_after_char)
            return

        # ↑ / ↓ — let ListBox move the selection naturally; NVDA reads it
        event.Skip()

    def _on_mention_list_char(self, event):
        """Printable chars typed in the list are redirected to the message field."""
        uc = event.GetUnicodeKey()
        if uc == wx.WXK_NONE or uc < 32:
            event.Skip()
            return
        ch = chr(uc)
        pos = self.message_field.GetInsertionPoint()
        text = self.message_field.GetValue()
        self.message_field.ChangeValue(text[:pos] + ch + text[pos:])
        self.message_field.SetInsertionPoint(pos + 1)
        wx.CallAfter(self._on_mention_list_after_char)

    def _on_mention_list_after_char(self):
        """Update mention suggestions after a char was injected from the list."""
        i18n = self.main_window.i18n
        start, query = self._get_mention_query()
        if start is None:
            self._hide_mention_suggestions()
            self.main_window.output(i18n.t("mention_no_suggestions"), interrupt=True)
            self.message_field.SetFocus()
            return
        self._mention_query = query
        self._update_mention_suggestions(query)
        # Return focus to the list so the user can keep typing or navigate
        if self._mention_suggestions:
            self._mention_list.SetFocus()
            self._mention_list.SetSelection(0)

    def _on_mention_list_selected_mouse(self, event):
        """Mouse click or double click on mention list item inserts it."""
        idx = self._mention_list.GetSelection()
        if 0 <= idx < len(self._mention_suggestions):
            name, jid = self._mention_suggestions[idx]
            self._insert_mention(name, jid)
