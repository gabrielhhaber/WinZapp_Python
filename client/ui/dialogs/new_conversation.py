"""
new_conversation.py — WinZapp "Nova conversa" dialog.

Lets the user search contacts / existing chats by name or phone number and
navigate to the selected conversation.  Also provides buttons to open the
New Group and New Contact dialogs.

Ctrl+N shortcut is registered in ConversationsPanel.
"""

import re
import threading
import wx

from core.utils import format_number, is_phone_like, looks_like_binary_blob, contact_dedup_key


class NewConversationDialog(wx.Dialog):
    """Search for a contact or number and open a conversation."""

    def __init__(self, main_window):
        self._mw = main_window
        i18n = main_window.i18n
        super().__init__(
            main_window,
            title=i18n.t("new_conversation_title"),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._results: list = []  # list of (display_name, jid, chat_or_None)
        self._build_ui(i18n)
        self._do_search("")  # populate the list immediately, before any typing
        self.SetMinSize((440, 380))
        self.SetSize((440, 480))
        self.CentreOnParent()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self, i18n):
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        # Search field
        search_label = wx.StaticText(panel, label=i18n.t("search_name_or_number"))
        sizer.Add(search_label, 0, wx.LEFT | wx.TOP, 10)

        self._search_field = wx.TextCtrl(
            panel, style=wx.TE_DONTWRAP | wx.TE_PROCESS_ENTER
        )
        self._search_field.Bind(wx.EVT_TEXT, self._on_search_text)
        self._search_field.Bind(wx.EVT_TEXT_ENTER, self._on_search_enter)
        sizer.Add(self._search_field, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 10)

        # Results list
        self._results_list = wx.ListCtrl(
            panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL
        )
        self._results_list.InsertColumn(0, i18n.t("conversations"), width=380)
        self._results_list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_activate)
        sizer.Add(self._results_list, 1, wx.EXPAND | wx.ALL, 10)

        # Buttons row
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)

        self._new_group_btn = wx.Button(panel, label=i18n.t("new_group"))
        self._new_group_btn.Bind(wx.EVT_BUTTON, self._on_new_group)
        btn_sizer.Add(self._new_group_btn, 0, wx.RIGHT, 8)

        self._new_contact_btn = wx.Button(panel, label=i18n.t("new_contact"))
        self._new_contact_btn.Bind(wx.EVT_BUTTON, self._on_new_contact)
        btn_sizer.Add(self._new_contact_btn, 0, wx.RIGHT, 8)

        btn_sizer.AddStretchSpacer()

        close_btn = wx.Button(panel, wx.ID_CANCEL, label=i18n.t("close"))
        btn_sizer.Add(close_btn, 0)

        sizer.Add(btn_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        panel.SetSizer(sizer)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        self.SetSizer(outer)

        self._search_field.SetFocus()

    # ── Search logic ──────────────────────────────────────────────────────────

    def _on_search_text(self, event):
        query = self._search_field.GetValue().strip()
        self._do_search(query)

    def _on_search_enter(self, event):
        """Enter in the search field: if exactly one result, activate it."""
        if len(self._results) == 1:
            self._open_conversation(0)
        elif self._results_list.GetItemCount() > 0:
            self._results_list.Focus(0)
            self._results_list.Select(0)
            self._results_list.SetFocus()

    def _dedup_key(self, jid: str) -> str:
        """Canonical key identifying *the same person* across JID formats.

        See core.utils.contact_dedup_key() — shared with attach_contact_dialog.py.
        """
        return contact_dedup_key(self._mw, jid)

    def _name_is_usable(self, name) -> bool:
        """Whether a resolved name is a real display name.

        Raw JIDs and phone-number fallbacks (nameless groups resolving to
        their 18-digit id, contacts with only a number) must not appear in
        the list — NVDA would read out the raw digits and they are useless
        as a pick-this-contact label. Reuses MainWindow._is_bad_contact_name
        when available (groups/private chats follow the same rules as
        everywhere else in the app), with a local fallback otherwise.
        """
        if not name or not isinstance(name, str):
            return False
        is_bad = getattr(self._mw, "_is_bad_contact_name", None)
        if is_bad is not None:
            return not is_bad(name)
        name = name.strip()
        return bool(name) and not name.isdigit() and not is_phone_like(name) \
            and not looks_like_binary_blob(name)

    def _do_search(self, query: str):
        self._results = []
        self._results_list.DeleteAllItems()

        mw       = self._mw
        i18n     = mw.i18n
        qlow     = query.lower()
        seen     = set()

        def _name_for_chat(chat):
            jid = chat.get("remoteJid", "")
            return (
                mw._resolve_contact_name(chat)
                or mw.find_name_through_messages(chat)
                or chat.get("pushName", "")
                or mw.find_jid_through_messages(chat)
                or format_number(jid)
            )

        # ── Search existing chats ─────────────────────────────────────────────
        for jid, chat in mw.chats.items():
            if not jid or jid.endswith(("@g.us", "@broadcast", "@newsletter")):
                continue
            name = _name_for_chat(chat)
            if not self._name_is_usable(name):
                continue
            if qlow in name.lower() or qlow in format_number(jid).lower():
                if self._dedup_key(jid) not in seen:
                    seen.add(self._dedup_key(jid))
                    self._results.append((name, jid, chat))

        # contacts is also a name cache for group participants and other
        # identities learned from events. Only WhatsApp contacts or contacts
        # explicitly created in WinZapp belong in this picker.
        for jid, contact in mw.contacts.items():
            if not jid or jid.endswith(("@g.us", "@broadcast", "@newsletter")):
                continue
            is_saved_contact = (
                contact.get("isMyContact") is True
                or contact.get("isMe") is True
                or (
                    contact.get("isSaved") is True
                    and bool((contact.get("name") or "").strip())
                )
            )
            if not is_saved_contact:
                continue
            key = self._dedup_key(jid)
            if jid.endswith("@lid") and key == jid.split("@", 1)[0]:
                continue
            if key in seen:
                continue
            name = contact.get("name") or contact.get("pushName") or format_number(jid)
            if not self._name_is_usable(name):
                continue
            if qlow in name.lower() or qlow in format_number(jid).lower():
                seen.add(key)
                self._results.append((name, jid, None))

        # ── If query looks like a phone number, add direct option ─────────────
        digits = re.sub(r"\D", "", query)
        if len(digits) >= 7:
            direct_jid = digits + "@s.whatsapp.net"
            if self._dedup_key(direct_jid) not in seen:
                display = format_number(direct_jid)
                self._results.append((display, direct_jid, None))

        # Sort alphabetically, case-insensitively, so the list is predictable
        # for keyboard/screen-reader navigation instead of dict-insertion order.
        self._results.sort(key=lambda r: r[0].lower())
        for name, jid, chat in self._results:
            self._results_list.Append((name,))

        if not self._results:
            self._results_list.Append((self._mw.i18n.t("no_results"),))
        else:
            # First row focused+selected by default, same as every other
            # list in the app (conversations, messages) — without moving
            # keyboard focus there: the search field keeps it, so typing
            # keeps filtering uninterrupted, but Enter/Tab immediately act
            # on row 0 instead of requiring an explicit arrow-down first.
            self._results_list.Focus(0)
            self._results_list.Select(0)

    # ── Activation ────────────────────────────────────────────────────────────

    def _on_activate(self, event):
        self._open_conversation(event.GetIndex())

    def _open_conversation(self, index: int):
        if index < 0 or index >= len(self._results):
            return
        name, jid, chat = self._results[index]
        if chat is None:
            chat = {"remoteJid": jid, "pushName": name}
        self.EndModal(wx.ID_OK)
        mw = self._mw
        norm_jid, existing = self._find_existing_chat(mw, jid)
        if existing is None:
            chat["remoteJid"] = norm_jid
            mw.chats[norm_jid] = chat
            mw._schedule_set_chats()
        else:
            chat = existing
        # Navigate after the dialog is gone
        wx.CallAfter(mw.conversations_panel.navigate_to_conversation, chat)
        wx.CallAfter(mw.conversations_panel.message_field.SetFocus)

    @staticmethod
    def _find_existing_chat(mw, jid: str):
        """Normalize `jid` and look for a chat already covering that person.

        Returns (norm_jid, existing_chat_or_None). Three JID formats can all
        refer to the very same contact — @c.us vs @s.whatsapp.net, @lid vs
        the phone number WhatsApp bridges it to, and (for Brazilian mobiles)
        the number with or without its 9th digit — and a chat may already
        exist under any one of them depending on which form WPPConnect used
        when its event first arrived, or which form the user typed. Missing
        this reuse doesn't just create a cosmetic duplicate: the newly
        created chat is a dead end nothing ever routes to (the real
        conversation lives under the other JID), so its first message stays
        "not confirmed" forever and the sidebar shows the same contact
        twice — reported live 2026-09-03 for a manually-added local contact
        whose typed number carried the 9th digit while WhatsApp's own
        pn-lid resolution for that number did not.
        """
        norm_jid = mw._normalize_jid(jid)
        get_chat = getattr(mw, "get_chat", None)
        existing = get_chat(norm_jid) if get_chat is not None else None
        if existing is None:
            existing = mw.chats.get(norm_jid) or mw.chats.get(jid)
        if existing is None:
            target_key = contact_dedup_key(mw, norm_jid)
            for existing_jid, existing_chat in mw.chats.items():
                if existing_jid.endswith("@g.us"):
                    continue
                if contact_dedup_key(mw, existing_jid) == target_key:
                    existing = existing_chat
                    break
        return norm_jid, existing

    # ── Sub-dialogs ───────────────────────────────────────────────────────────

    def _on_new_group(self, event):
        from ui.dialogs.new_group import NewGroupDialog
        dlg = NewGroupDialog(self._mw)
        dlg.ShowModal()
        dlg.Destroy()

    def _on_new_contact(self, event):
        from ui.dialogs.new_contact import NewContactDialog
        dlg = NewContactDialog(self._mw, parent=self)
        if dlg.ShowModal() == wx.ID_OK:
            jid  = dlg.result_jid
            name = dlg.result_name
            dlg.Destroy()
            if jid:
                self.EndModal(wx.ID_OK)
                mw = self._mw
                norm_jid, chat = self._find_existing_chat(mw, jid)
                if chat is None:
                    chat = {"remoteJid": norm_jid, "pushName": name}
                    mw.chats[norm_jid] = chat
                    mw._schedule_set_chats()
                wx.CallAfter(mw.conversations_panel.navigate_to_conversation, chat)
                wx.CallAfter(mw.conversations_panel.message_field.SetFocus)
        else:
            dlg.Destroy()
