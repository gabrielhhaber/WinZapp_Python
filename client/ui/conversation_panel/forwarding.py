"""ForwardingMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

Moved verbatim out of ui/conversations.py. Methods run with ``self`` bound to
the ConversationsPanel instance, so every attribute set in
ConversationsPanel.__init__/init_UI is available here.
"""

import threading
import time
import wx
from core.utils import append_selected_marker, contact_dedup_key
from ui.dialogs.contact_list_picker import build_own_contact_rows
from ui.conversation_panel.text_helpers import message_caption
from ui.conversation_panel.selection_rules import (
    toggle_jid_selection,
    visible_jid_selected,
)


class ForwardingMixin:
    """Forwarding messages to other chats.
    """

    @staticmethod
    def _forward_target_chats(mw) -> tuple:
        """All chats offerable as a forward target: the main (non-archived)
        conversations_panel's own chats_list/chat_names, plus every archived
        chat from the separate ArchivedConversationsPanel that isn't already
        in that list, plus every saved contact no chat in either list
        covers. conversations_panel.chats_list alone only ever holds
        non-archived chats, so forwarding used to silently exclude every
        archived chat (not just groups) as a target — and a contact the
        user never opened a chat with was unreachable from this dialog
        entirely, even though the "Nova conversa" picker knows them."""
        panel     = mw.conversations_panel
        all_chats = list(panel.chats_list)
        all_names = list(panel.chat_names)
        seen_jids = {c.get("remoteJid", "") for c in all_chats}
        arch_panel = getattr(mw, "archived_conversations_panel", None)
        if arch_panel is not None:
            for chat, name in zip(arch_panel.chats_list, arch_panel.chat_names):
                jid = chat.get("remoteJid", "")
                if jid and jid not in seen_jids:
                    seen_jids.add(jid)
                    all_chats.append(chat)
                    all_names.append(name)
        ForwardingMixin._append_chatless_contacts(mw, all_chats, all_names)
        return all_chats, all_names

    @staticmethod
    def _append_chatless_contacts(mw, all_chats, all_names):
        """Append one forward target per saved contact that no chat in
        *all_chats* already covers, in place.

        Rows come from the shared build_own_contact_rows() — the same
        legitimacy rules as every other contact picker (saved contacts
        only, no group-presence junk, no unbridged @lids, one row per
        person via contact_dedup_key() across @lid/@c.us/@s.whatsapp.net
        and the Brazilian 8/9-digit mobile variant), so "what is a
        pickable contact" cannot drift between dialogs. A contact whose
        chat exists under any JID variant is skipped: the chat is already
        a target, and forward_message() resolves the JID for sending.

        Locked chats stay out of the forward dialog — that is the vault's
        promise — so a contact whose only chat is locked is skipped too
        rather than reappearing here by name.
        """
        seen_keys = {
            contact_dedup_key(mw, c.get("remoteJid", ""))
            for c in all_chats if c.get("remoteJid")
        }
        locked_rows = getattr(mw, "_locked_chat_rows", None) or ([], [])
        for locked in locked_rows[0]:
            jid = locked.get("remoteJid", "")
            if jid:
                seen_keys.add(contact_dedup_key(mw, jid))
        for name, _phone, entry in build_own_contact_rows(mw):
            jid = entry.get("remoteJid", "")
            key = contact_dedup_key(mw, jid) if jid else ""
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            all_chats.append(entry)
            all_names.append(name)

    def _on_menu_forward(self, msg: dict, msgs_list: list = None):
        """Open a conversation-picker dialog and forward to the chosen chats.

        Forwards *msg* alone, or every message in *msgs_list* when a mass
        forward supplied one. System events (group notices) are dropped from
        the batch rather than aborting it — one accidentally-selected join
        notice must not cost the user the whole selection — and only a batch
        left with nothing at all is refused outright.
        """
        msgs_to_forward = [m for m in (msgs_list or [msg]) if not self._is_system_event(m)]
        if not msgs_to_forward:
            self._reject_system_event_action(msg)
            return
        mw   = self.main_window
        i18n = mw.i18n
        dlg_label = (
            i18n.t("forward_selected_messages_title") if len(msgs_to_forward) > 1
            else i18n.t("forward_message")
        )

        # ── Collect available conversations ───────────────────────────────────
        all_chats, all_names = self._forward_target_chats(mw)
        if not all_chats:
            return

        # ── Build a simple picker dialog ──────────────────────────────────────
        dlg = wx.Dialog(
            self,
            title=dlg_label,
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
            size=(400, 480),
        )
        p     = wx.Panel(dlg)
        vsz   = wx.BoxSizer(wx.VERTICAL)

        vsz.Add(
            wx.StaticText(p, label=i18n.t("forward_search_label")),
            0, wx.LEFT | wx.TOP | wx.RIGHT, 6,
        )
        search_field = wx.TextCtrl(p, style=wx.TE_PROCESS_ENTER)
        search_field.SetHint(i18n.t("search_conversations"))
        vsz.Add(search_field, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        lst = wx.ListBox(p, choices=all_names, style=wx.LB_SINGLE)
        vsz.Add(lst, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        vsz.Add(
            wx.StaticText(p, label=i18n.t("forward_multiselect_hint")),
            0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6,
        )

        # Offered as soon as ANY message in the batch carries a caption, not
        # just the first one — the first is simply whichever row the mass
        # forward happened to hand over, and keying the offer on it hid the
        # checkbox (silently dropping every other caption) whenever that row
        # was a plain text message.
        chk_keep_caption = None
        if any(message_caption(m) for m in msgs_to_forward):
            chk_keep_caption = wx.CheckBox(p, label=i18n.t("forward_keep_caption"))
            chk_keep_caption.SetValue(True)
            vsz.Add(chk_keep_caption, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        # ── Selection state ──────────────────────────────────────────────────
        # lst is a single-selection wx.ListBox: native selection is only ever
        # "which row has keyboard focus", exactly like messages_list/
        # conversations_list's GetFocusedItem() elsewhere in this file. The
        # actual multi-select the mass forward acts on lives entirely in this
        # plain set, keyed by jid — mirroring self.selected_messages/
        # self.selected_chats, including the sound event, the "selected"/
        # "unselected"/"all_selected"/"all_unselected" announcements, and the
        # append/prepend "selected" text marker (selected_announcement_position
        # setting) — see _select_message_at()/_on_messages_list_key_down() for
        # the pattern this mirrors.
        #
        # An earlier version used wx.LB_EXTENDED instead, with its own native
        # multi-selection driven by hand through raw Win32 messages (so plain
        # Up/Down and letter type-ahead — which natively collapse an extended
        # selection to whatever row they land on — could be remapped to leave
        # it alone). That meant two independent copies of "what's selected"
        # that could drift out of sync with each other, reported live as
        # "comportamentos estranhos". LB_SINGLE removes the native multi-select
        # entirely, so arrows and type-ahead are simply left at their default
        # native behavior (event.Skip()) — there is nothing left for them to
        # collide with, and nothing custom left to keep in sync.
        selected_jids = set()

        def _jid_at(idx):
            if 0 <= idx < len(_filtered_chats):
                return _filtered_chats[idx].get("remoteJid", "")
            return ""

        def _row_text(idx):
            name = _filtered_names[idx] if idx < len(_filtered_names) else ""
            jid = _jid_at(idx)
            is_selected = bool(jid) and jid in selected_jids
            position = mw.settings.get("user_interface", {}).get(
                "selected_announcement_position", "end"
            )
            return append_selected_marker(name, i18n.t("selected_suffix"), position, is_selected)

        def _refresh_row(idx):
            if 0 <= idx < lst.GetCount():
                lst.SetString(idx, _row_text(idx))

        def _on_listbox_select(event):
            # Fires for any NATIVE focus change (arrow keys, letter
            # type-ahead, mouse click) — never for the SetSelection() calls
            # this dialog makes itself below, which is exactly the split
            # that's wanted: landing on a row already in selected_jids gets
            # the same audible cue selection_sound gives everywhere else,
            # without this dialog having to reimplement arrow/type-ahead
            # navigation by hand to get it.
            jid = _jid_at(lst.GetSelection())
            if jid and jid in selected_jids:
                self.selection_sound.play()
            event.Skip()

        lst.Bind(wx.EVT_LISTBOX, _on_listbox_select)

        def _selection_visible():
            """Whether any selected contact is among the rows listed right
            now — this dialog's search box rebinds _filtered_chats, and
            selected_jids survives it. Everything here derives the selection
            mode from this, gate and announcement alike."""
            return visible_jid_selected(
                selected_jids, (_jid_at(i) for i in range(lst.GetCount())))

        def _toggle_at(row):
            """Toggle the contact on *row*, exactly as Ctrl+Space always has —
            shared with plain Space once a selection exists (issue #99).

            Returns whether anything was toggled, so plain Space can hand the
            key back instead of dying on a row with no jid (an empty search
            result focuses nothing at all). Ctrl+Space ignores it and keeps
            swallowing the key, as it always has.
            """
            jid = _jid_at(row)
            if not jid:
                return False
            was_active = _selection_visible()
            # The set bookkeeping is the pure helper's; its own was_active is
            # the raw-set answer, which is the one this dialog must not use.
            now_selected, _ = toggle_jid_selection(selected_jids, jid)
            _refresh_row(row)
            if now_selected:
                self.selection_sound.play()
            mw.output(
                self._selection_mode_announcement(
                    i18n.t("selected" if now_selected else "unselected"),
                    was_active, _selection_visible()),
                interrupt=True)
            return True

        def _on_list_key_down(event):
            key   = event.GetKeyCode()
            ctrl  = event.ControlDown()
            shift = event.ShiftDown()
            count = lst.GetCount()
            focus = lst.GetSelection()

            if shift and key in (wx.WXK_DOWN, wx.WXK_NUMPAD_DOWN):
                target = (focus + 1) if focus >= 0 else 0
                if target < count:
                    lst.SetSelection(target)
                    jid = _jid_at(target)
                    if jid and jid not in selected_jids:
                        was_active = _selection_visible()
                        selected_jids.add(jid)
                        _refresh_row(target)
                        self.selection_sound.play()
                        mw.output(
                            self._selection_mode_announcement(
                                i18n.t("selected"), was_active, _selection_visible()),
                            interrupt=True)
                return

            if shift and key in (wx.WXK_HOME, wx.WXK_NUMPAD_HOME, wx.WXK_END, wx.WXK_NUMPAD_END):
                to_end = key in (wx.WXK_END, wx.WXK_NUMPAD_END)
                if count > 0:
                    focus0 = focus if focus >= 0 else 0
                    lo, hi = (focus0, count - 1) if to_end else (0, focus0)
                    was_active = _selection_visible()
                    newly = []
                    for i in range(lo, hi + 1):
                        jid = _jid_at(i)
                        if jid and jid not in selected_jids:
                            selected_jids.add(jid)
                            newly.append(i)
                    target = count - 1 if to_end else 0
                    lst.SetSelection(target)
                    for i in newly:
                        _refresh_row(i)
                    if newly:
                        self.selection_sound.play()
                        mw.output(
                            self._selection_mode_announcement(
                                i18n.t("selected"), was_active, _selection_visible()),
                            interrupt=True)
                return

            if ctrl and shift and key == wx.WXK_SPACE:
                # Select every contact, or clear the selection if everything
                # is already selected.
                row_jids = [_jid_at(i) for i in range(count)]
                real_jids = [j for j in row_jids if j]
                all_selected = bool(real_jids) and all(j in selected_jids for j in real_jids)
                was_active = _selection_visible()
                for i, jid in enumerate(row_jids):
                    if not jid:
                        continue
                    if all_selected:
                        selected_jids.discard(jid)
                    else:
                        selected_jids.add(jid)
                    _refresh_row(i)
                if real_jids:
                    if not all_selected:
                        self.selection_sound.play()
                    mw.output(
                        self._selection_mode_announcement(
                            i18n.t("all_unselected" if all_selected else "all_selected"),
                            was_active, _selection_visible()),
                        interrupt=True)
                return

            if ctrl and not shift and key == wx.WXK_SPACE:
                _toggle_at(focus)
                return

            # Plain Space keeps selecting once a selection the user can SEE
            # exists (issue #99); with nothing selected — or with every
            # selected contact hidden by the search box — it stays the native
            # key it always was, and so it does when the toggle itself refuses
            # (an empty search result focuses no row at all).
            if (key == wx.WXK_SPACE and not ctrl and not shift
                    and self._selection_mode_enabled() and _selection_visible()):
                if _toggle_at(focus):
                    return

            event.Skip()  # Arrows, letter type-ahead, everything else: native behavior

        lst.Bind(wx.EVT_KEY_DOWN, _on_list_key_down)

        btn_sizer  = wx.StdDialogButtonSizer()
        ok_btn     = wx.Button(p, wx.ID_OK,     label=i18n.t("forward_message"))
        cancel_btn = wx.Button(p, wx.ID_CANCEL, label=i18n.t("cancel"))
        btn_sizer.AddButton(ok_btn)
        btn_sizer.AddButton(cancel_btn)
        btn_sizer.Realize()
        vsz.Add(btn_sizer, 0, wx.EXPAND | wx.ALL, 6)

        p.SetSizer(vsz)
        dlg_sz = wx.BoxSizer(wx.VERTICAL)
        dlg_sz.Add(p, 1, wx.EXPAND)
        dlg.SetSizer(dlg_sz)
        dlg.Layout()

        # Filter list as user types
        _filtered_chats = list(all_chats)
        _filtered_names = list(all_names)

        def _on_search(event):
            nonlocal _filtered_chats, _filtered_names
            q = search_field.GetValue().strip().lower()
            if q:
                pairs = [(c, n) for c, n in zip(all_chats, all_names)
                         if q in n.lower()]
            else:
                pairs = list(zip(all_chats, all_names))
            _filtered_chats = [c for c, _ in pairs]
            _filtered_names = [n for _, n in pairs]
            # Re-rendered with the "selected" marker for whichever of these
            # are still in selected_jids — that set is keyed by identity, so
            # a search that narrows/widens the visible rows doesn't reset it
            # the way it would if selection lived in the native widget.
            lst.Set([_row_text(i) for i in range(len(_filtered_names))])
            if _filtered_names:
                # Focus only — does not itself add to selected_jids, so
                # confirming right away without ever pressing Ctrl+Space
                # falls through to the "nothing selected -> use the focused
                # item" behavior below rather than silently forwarding to
                # whatever the search happened to focus first.
                lst.SetSelection(0)

        search_field.Bind(wx.EVT_TEXT, _on_search)
        if all_names:
            lst.SetSelection(0)
        ok_btn.SetDefault()

        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return

        if selected_jids:
            sels = [i for i in range(len(_filtered_chats)) if _jid_at(i) in selected_jids]
        else:
            # No explicit multi-selection was made (Ctrl+Space never
            # pressed) — forward to whichever contact is currently focused,
            # same as this dialog's original single-target behavior.
            focus = lst.GetSelection()
            sels = [focus] if focus >= 0 else []
        dlg.Destroy()
        if not sels:
            return

        target_jids = []
        target_names = []
        for i in sels:
            if i >= len(_filtered_chats):
                continue
            jid = _filtered_chats[i].get("remoteJid", "")
            if jid:
                target_jids.append(jid)
                target_names.append(_filtered_names[i])
        if not target_jids:
            return

        # Uses WPP.chat.forwardMessagesV2 server-side (main_window.forward_message),
        # which forwards the actual message as WhatsApp does — media, documents,
        # audio, captions, etc. all come through, unlike re-extracting the text
        # and sending it as a brand-new message. The forwarded copy arrives back
        # through the normal WebSocket echo, same as any other outgoing message.
        targets = list(zip(target_jids, target_names))
        keep_captions = chk_keep_caption.GetValue() if chk_keep_caption else False

        def _do_forward():
            failed_names = set()
            for i, m in enumerate(msgs_to_forward):
                # A short gap between messages when forwarding several at
                # once: back-to-back forwardMessagesV2 calls with no pause
                # are the trigger for the transient failure forward_message()
                # retries against (see its own comment) — spacing them out
                # here means most of the time the retry never has to fire.
                if i > 0:
                    time.sleep(0.4)
                msg_key = m.get("key", {}) or {}
                source_jid = msg_key.get("remoteJid") or (self.conversation.get("remoteJid", "") if self.conversation else "")
                if not source_jid or not msg_key.get("id"):
                    continue
                # Decided per message, never once for the batch: the
                # caption-preserving path is a media resend, so handing it a
                # plain text message (which a mass forward mixes in freely)
                # would push that message through the media call.
                keep = keep_captions and bool(message_caption(m))
                f_names = self._forward_message_to_targets(
                    m, targets, keep_caption=keep, source_jid_override=source_jid
                )
                failed_names.update(f_names)

            if failed_names:
                wx.CallAfter(mw.error_sound.play)
                if len(targets) == 1:
                    wx.CallAfter(mw.output, i18n.t("forward_failed"))
                else:
                    wx.CallAfter(
                        mw.output,
                        i18n.t("forward_failed_multiple").format(names=", ".join(failed_names)),
                    )

        threading.Thread(target=_do_forward, daemon=True).start()

    def _forward_message_to_targets(self, msg: dict, targets: list, keep_caption: bool = False, source_jid_override: str = "") -> list:
        """Forward one message to each (jid, name) pair in *targets*, one at
        a time — so one failing recipient (e.g. a stale JID) doesn't abort
        delivery to the rest.

        keep_caption applies to THIS message and must already account for
        whether it actually carries a caption (see message_caption()) — it is
        not a batch-wide flag: the True branch is a media resend and would
        misroute a plain text message.
        """
        mw = self.main_window
        failed = []
        msg_key = msg.get("key", {}) or {}
        source_jid = source_jid_override or msg_key.get("remoteJid") or ""

        for jid, name in targets:
            if keep_caption:
                success = mw.resend_media_message_with_caption(msg, jid)
            else:
                success = mw.forward_message(source_jid, msg_key, jid, source_msg=msg)
            if not success:
                failed.append(name)
        return failed
