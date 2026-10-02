"""AcceleratorsMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

Moved verbatim out of ui/conversations.py. Methods run with ``self`` bound to
the ConversationsPanel instance, so every attribute set in
ConversationsPanel.__init__/init_UI is available here.
"""

import wx
from core.conversation_view import mnemonic_letter


class AcceleratorsMixin:
    """Keyboard accelerator tables for the conversations list and the open
    conversation.
    """

    # ── Accelerators ────────────────────────────────────────────────────────

    def create_accelerator_table(self):
        self.ID_CTRL_F              = wx.NewIdRef()
        self.ID_CTRL_N              = wx.NewIdRef()
        self.ID_DELETE_CONV         = wx.NewIdRef()
        self.ID_ALT_SHIFT_C_LIST    = wx.NewIdRef()  # copy number from chat list
        self.ID_CONV_DATA_LIST      = wx.NewIdRef()
        self.ID_TOGGLE_READ_LIST    = wx.NewIdRef()
        self.ID_MUTE_LIST           = wx.NewIdRef()
        self.ID_BLOCK_LIST          = wx.NewIdRef()
        self.ID_CLEAR_LIST          = wx.NewIdRef()
        self.ID_ARCHIVE_LIST        = wx.NewIdRef()
        self.ID_LOCK_LIST           = wx.NewIdRef()
        self.ID_PIN_LIST            = wx.NewIdRef()
        self.ID_CLOSE_CONV_LIST     = wx.NewIdRef()
        # Alt+2 / Alt+3 exist on conversation_panel's own table, and that panel
        # is HIDDEN while no conversation is open — so with nothing open the
        # two combos reached no handler at all and the user got silence, which
        # reads as a broken shortcut (issue #86). Duplicated here, on the
        # always-present panel table, purely so there is somewhere to say
        # "no chat is open". With a conversation open and focus in the chat
        # list they simply delegate to the real handlers, which is what Alt+2
        # ("go to messages") is supposed to do from there anyway.
        self.ID_ALT_2_LIST          = wx.NewIdRef()
        self.ID_ALT_3_LIST          = wx.NewIdRef()
        # A panel switch leaves the open conversation hidden, so Alt+M
        # (messages) is an explicit ask for it: it reveals it in its panel and
        # moves focus there, as the native mnemonic of the label inside the
        # hidden pane cannot (panel_visibility).
        self.ID_ALT_M_LIST          = wx.NewIdRef()
        # ── Mass actions (only act while conversations are selected) ─────────
        # One shortcut per entry of the chat list's "Ações em massa" submenu,
        # for the same reason the messages list has its own set (see
        # create_accel_conversation's ID_BULK_* block): with Settings >
        # Interface do usuário > "Substituir atalhos por ações em massa..."
        # off, that submenu used to be the only way to reach them.
        # Letters mirror the single-chat shortcut where Ctrl+Alt+Shift+<letter>
        # is free — L(impar/clear) — and fall back to a mnemonic where it is
        # already an app-wide shortcut: archive is Ctrl+Shift+Q but
        # Ctrl+Alt+Shift+Q exits WinZapp, and read/unread share Ctrl+Shift+M
        # but Ctrl+Alt+Shift+M marks every chat as read — so archive uses A
        # and the two read states get one shortcut each (R/U) instead of a
        # toggle, matching the submenu, which offers them separately.
        # Delete keeps the Delete key it already has, plus Ctrl+Shift — the
        # same combo the messages list uses for its own bulk delete, which
        # never collides: conversation_panel's table wins while a conversation
        # is open, this one applies otherwise (same split as plain Delete).
        self.ID_BULK_CLEAR_CHATS    = wx.NewIdRef()  # clear selected    (Ctrl+Alt+Shift+L)
        self.ID_BULK_DELETE_CHATS   = wx.NewIdRef()  # delete selected   (Ctrl+Shift+Delete)
        self.ID_BULK_ARCHIVE_CHATS  = wx.NewIdRef()  # archive selected  (Ctrl+Alt+Shift+A)
        self.ID_BULK_READ_CHATS     = wx.NewIdRef()  # mark read         (Ctrl+Alt+Shift+R)
        self.ID_BULK_UNREAD_CHATS   = wx.NewIdRef()  # mark unread       (Ctrl+Alt+Shift+U)
        CS = wx.ACCEL_CTRL | wx.ACCEL_SHIFT
        AS = wx.ACCEL_ALT | wx.ACCEL_SHIFT
        CAS = wx.ACCEL_CTRL | wx.ACCEL_ALT | wx.ACCEL_SHIFT
        i18n = self.main_window.i18n
        accel_tbl = wx.AcceleratorTable([
            (wx.ACCEL_ALT,    ord(mnemonic_letter(i18n.t("messages"), "M")), self.ID_ALT_M_LIST),
            (wx.ACCEL_CTRL,   ord("F"),        self.ID_CTRL_F),
            (wx.ACCEL_CTRL,   ord("N"),        self.ID_CTRL_N),
            (wx.ACCEL_NORMAL, wx.WXK_DELETE,   self.ID_DELETE_CONV),
            (AS,              ord("C"),         self.ID_ALT_SHIFT_C_LIST),
            (CS,              ord("D"),         self.ID_CONV_DATA_LIST),
            (CS,              ord("M"),         self.ID_TOGGLE_READ_LIST),
            (AS,              ord("S"),         self.ID_MUTE_LIST),
            (CS,              ord("B"),         self.ID_BLOCK_LIST),
            (CS,              ord("L"),         self.ID_CLEAR_LIST),
            # Ctrl+Shift+Q, not plain Ctrl+Q: archiving is destructive-ish
            # (drops the conversation out of the main list) and Ctrl+Q sits
            # right next to other single-Ctrl combos a user can easily
            # fat-finger while just trying to navigate the list.
            (CS,              ord("Q"),         self.ID_ARCHIVE_LIST),
            (CS,              ord("T"),         self.ID_LOCK_LIST),
            (wx.ACCEL_CTRL,   ord("P"),         self.ID_PIN_LIST),
            (wx.ACCEL_CTRL,   ord("W"),         self.ID_CLOSE_CONV_LIST),
            (wx.ACCEL_ALT,    ord("2"),         self.ID_ALT_2_LIST),
            (wx.ACCEL_ALT,    ord("3"),         self.ID_ALT_3_LIST),
            (CAS,             ord("L"),         self.ID_BULK_CLEAR_CHATS),
            (CS,              wx.WXK_DELETE,    self.ID_BULK_DELETE_CHATS),
            (CAS,             ord("A"),         self.ID_BULK_ARCHIVE_CHATS),
            (CAS,             ord("R"),         self.ID_BULK_READ_CHATS),
            (CAS,             ord("U"),         self.ID_BULK_UNREAD_CHATS),
        ])
        self.SetAcceleratorTable(accel_tbl)
        self.Bind(wx.EVT_MENU, self.on_ctrl_f,                    id=self.ID_CTRL_F)
        self.Bind(wx.EVT_MENU, self._on_new_conversation,         id=self.ID_CTRL_N)
        self.Bind(wx.EVT_MENU, self._on_accel_delete_conv,        id=self.ID_DELETE_CONV)
        self.Bind(wx.EVT_MENU, self._on_accel_copy_number_list,   id=self.ID_ALT_SHIFT_C_LIST)
        self.Bind(wx.EVT_MENU, self._on_accel_conversation_data_list, id=self.ID_CONV_DATA_LIST)
        self.Bind(wx.EVT_MENU, self._on_accel_toggle_read_list,    id=self.ID_TOGGLE_READ_LIST)
        self.Bind(wx.EVT_MENU, self._on_accel_mute_list,           id=self.ID_MUTE_LIST)
        self.Bind(wx.EVT_MENU, self._on_accel_block_list,          id=self.ID_BLOCK_LIST)
        self.Bind(wx.EVT_MENU, self._on_accel_clear_list,          id=self.ID_CLEAR_LIST)
        self.Bind(wx.EVT_MENU, self._on_accel_archive_list,        id=self.ID_ARCHIVE_LIST)
        self.Bind(wx.EVT_MENU, self._on_accel_lock_list,           id=self.ID_LOCK_LIST)
        self.Bind(wx.EVT_MENU, self._on_accel_pin_list,            id=self.ID_PIN_LIST)
        self.Bind(wx.EVT_MENU, self.on_context_menu_close,         id=self.ID_CLOSE_CONV_LIST)
        self.Bind(wx.EVT_MENU, self._on_list_jump_last,            id=self.ID_ALT_2_LIST)
        self.Bind(wx.EVT_MENU, self._on_list_jump_unread,          id=self.ID_ALT_3_LIST)
        self.Bind(wx.EVT_MENU, self._on_list_focus_messages,       id=self.ID_ALT_M_LIST)
        self.Bind(wx.EVT_MENU, self._on_accel_bulk_clear_chats,    id=self.ID_BULK_CLEAR_CHATS)
        self.Bind(wx.EVT_MENU, self._on_accel_bulk_delete_chats,   id=self.ID_BULK_DELETE_CHATS)
        self.Bind(wx.EVT_MENU, self._on_accel_bulk_archive_chats,  id=self.ID_BULK_ARCHIVE_CHATS)
        self.Bind(wx.EVT_MENU, self._on_accel_bulk_read_chats,     id=self.ID_BULK_READ_CHATS)
        self.Bind(wx.EVT_MENU, self._on_accel_bulk_unread_chats,   id=self.ID_BULK_UNREAD_CHATS)

    def create_accel_conversation(self):
        self.ID_DESCRIBE_PHOTO = wx.NewIdRef()
        # ── Navigation / recording ──────────────────────────────────────────
        self.ID_CTRL_R          = wx.NewIdRef()  # record voice            (Ctrl+R)
        self.ID_CTRL_SHIFT_G    = wx.NewIdRef()  # record, other mode      (Ctrl+Shift+G)
        self.ID_CTRL_SHIFT_H    = wx.NewIdRef()  # microphone + system     (Ctrl+Shift+H)
        self.ID_ALT_2           = wx.NewIdRef()  # jump to last message    (Alt+2)
        self.ID_ESC             = wx.NewIdRef()  # close conversation      (Esc)
        self.CTRL_W             = wx.NewIdRef()  # close conversation      (Ctrl+W)
        self.ID_CTRL_SHIFT_D    = wx.NewIdRef()  # conv data / discard     (Ctrl+Shift+D)
        # ── Attachment / media ───────────────────────────────────────────────
        self.ID_CTRL_SHIFT_A    = wx.NewIdRef()  # add attachment          (Ctrl+Shift+A)
        self.ID_CTRL_SHIFT_B    = wx.NewIdRef()  # block contact           (Ctrl+Shift+B)
        # ── Message-level ────────────────────────────────────────────────────
        self.ID_ALT_FOCUS_FIELD = wx.NewIdRef()  # focus message field     (Alt+<msg-label mnemonic>)
        self.ID_ALT_FOCUS_LIST  = wx.NewIdRef()  # focus messages list     (Alt+<list-label mnemonic>)
        self.ID_ALT_R           = wx.NewIdRef()  # reply                   (Alt+R)
        self.ID_ALT_SHIFT_D     = wx.NewIdRef()  # message data            (Alt+Shift+D)
        self.ID_CTRL_SHIFT_E    = wx.NewIdRef()  # forward                 (Ctrl+Shift+E)
        self.ID_CTRL_SHIFT_P    = wx.NewIdRef()  # pause/resume recording  (Ctrl+Shift+P)
        self.ID_CTRL_SHIFT_R    = wx.NewIdRef()  # react to message        (Ctrl+Shift+R)
        self.ID_DELETE_MSG      = wx.NewIdRef()  # delete focused message  (Delete)
        self.ID_CTRL_C          = wx.NewIdRef()  # copy message            (Ctrl+C)
        self.ID_CTRL_SHIFT_C    = wx.NewIdRef()  # copy caption (photo/video/doc) (Ctrl+Shift+C)
        self.ID_ALT_C           = wx.NewIdRef()  # show text popup         (Alt+C)
        self.ID_ALT_E           = wx.NewIdRef()  # edit message            (Alt+E)
        self.ID_ALT_L           = wx.NewIdRef()  # read-more (truncated)   (Alt+L)
        self.ID_ALT_SHIFT_L     = wx.NewIdRef()  # announce message status (Alt+Shift+L)
        self.ID_ALT_SHIFT_K     = wx.NewIdRef()  # announce message date   (Alt+Shift+K)
        # ── Conversation-level ───────────────────────────────────────────────
        self.ID_CTRL_SHIFT_S    = wx.NewIdRef()  # save as / download      (Ctrl+Shift+S)
        self.ID_CTRL_SHIFT_M    = wx.NewIdRef()  # toggle read / unread    (Ctrl+Shift+M)
        self.ID_CTRL_SHIFT_L    = wx.NewIdRef()  # clear conversation      (Ctrl+Shift+L)
        # ── Search / unread jump ─────────────────────────────────────────────
        self.ID_CTRL_SHIFT_F    = wx.NewIdRef()  # open search panel       (Ctrl+Shift+F)
        self.ID_ALT_3           = wx.NewIdRef()  # jump to unread sep      (Alt+3)
        self.ID_ALT_U           = wx.NewIdRef()  # jump to unread sep      (Alt+U)
        # ── Message bookmarks ────────────────────────────────────────────────
        self.ID_BOOKMARK        = [wx.NewIdRef() for _ in range(10)]  # set/jump (Ctrl+0..9)
        self.ID_BOOKMARK_REMOVE = [wx.NewIdRef() for _ in range(10)]  # remove   (Ctrl+Shift+0..9)
        # ── Temporary (this-conversation-only) bookmarks ─────────────────────
        self.ID_TEMP_BOOKMARK        = [wx.NewIdRef() for _ in range(10)]  # set/jump (Alt+Shift+0..9)
        self.ID_TEMP_BOOKMARK_REMOVE = [wx.NewIdRef() for _ in range(10)]  # remove   (Ctrl+Alt+Shift+0..9)
        # ── Group actions ────────────────────────────────────────────────────
        self.ID_ALT_SHIFT_R     = wx.NewIdRef()  # reply privately         (Alt+Shift+R)
        self.ID_ALT_SHIFT_E     = wx.NewIdRef()  # recent reactions        (Alt+Shift+E)
        self.ID_ALT_SHIFT_M     = wx.NewIdRef()  # mentions                (Alt+Shift+M)
        self.ID_ALT_SHIFT_C     = wx.NewIdRef()  # copy phone number       (Alt+Shift+C)
        self.ID_ALT_SHIFT_V     = wx.NewIdRef()  # converse with           (Alt+Shift+V)
        self.ID_CTRL_SHIFT_V    = wx.NewIdRef()  # voice call              (Ctrl+Shift+V)
        self.ID_CTRL_ALT_SHIFT_V = wx.NewIdRef() # video call              (Ctrl+Alt+Shift+V)
        self.ID_ALT_SHIFT_Q     = wx.NewIdRef()  # goto quoted message     (Alt+Shift+Q)
        self.ID_ALT_SHIFT_S     = wx.NewIdRef()  # mute / unmute           (Alt+Shift+S)
        # ── Message star ─────────────────────────────────────────────────────
        self.ID_CTRL_SHIFT_O    = wx.NewIdRef()  # star message            (Ctrl+Shift+O)
        # ── Mass actions (only act while messages are selected) ──────────────
        # One shortcut per entry of the context menu's "Ações em massa"
        # submenu. Deliberately a family of their own instead of relying on
        # the single-message shortcuts being remapped by Settings > Interface
        # do usuário > "Substituir atalhos por ações em massa...": that
        # setting is exactly what a user turns OFF to keep acting on the
        # focused message while a selection exists, and with it off the
        # submenu used to be the only way to reach these at all.
        # Letters follow the single-message shortcut where that letter is
        # free — C(opy), E (forward, Ctrl+Shift+E), S(ave) — and fall back to
        # a mnemonic where Ctrl+Alt+Shift+<letter> is already an app-wide
        # shortcut: star is Ctrl+Shift+O but Ctrl+Alt+Shift+O toggles offline
        # mode, and pin is Ctrl+Shift+P but Ctrl+Alt+Shift+P is the global
        # audio play/pause, so those two use F (favoritar) and X (fixar).
        # Delete keeps the Delete key it already has, plus Ctrl+Shift.
        self.ID_BULK_COPY       = wx.NewIdRef()  # copy selected           (Ctrl+Alt+Shift+C)
        self.ID_BULK_FORWARD    = wx.NewIdRef()  # forward selected        (Ctrl+Alt+Shift+E)
        self.ID_BULK_STAR       = wx.NewIdRef()  # star selected           (Ctrl+Alt+Shift+F)
        self.ID_BULK_PIN        = wx.NewIdRef()  # pin selected            (Ctrl+Alt+Shift+X)
        self.ID_BULK_SAVE       = wx.NewIdRef()  # save selected           (Ctrl+Alt+Shift+S)
        self.ID_BULK_DELETE     = wx.NewIdRef()  # delete selected         (Ctrl+Shift+Delete)
        # ── Audio speed ──────────────────────────────────────────────────────
        self.ID_ALT_COMMA       = wx.NewIdRef()  # decrease audio speed    (Alt+,)
        self.ID_ALT_PERIOD      = wx.NewIdRef()  # increase audio speed    (Alt+.)
        self.ID_CTRL_PERIOD     = wx.NewIdRef()  # insert emoji            (Ctrl+.)

        CS  = wx.ACCEL_CTRL | wx.ACCEL_SHIFT
        AS  = wx.ACCEL_ALT  | wx.ACCEL_SHIFT
        CAS = wx.ACCEL_CTRL | wx.ACCEL_ALT | wx.ACCEL_SHIFT

        # message_label's own native mnemonic ("&" in "type_message"/
        # "reply_to"/"reply_to_group", all deliberately kept on the same
        # letter across translations) is supposed to redirect Alt+<letter>
        # focus to whichever control follows it — but that relies entirely
        # on wx/Windows re-scanning sibling controls at key-press time, and
        # showing _remove_quote_btn when entering reply mode (see
        # _on_menu_reply) was observed to break that redirect: Alt+<letter>
        # stopped moving focus to message_field once "Responder a Fulano"
        # replaced the default label. Binding the same letter as an
        # explicit accelerator that unconditionally focuses message_field
        # makes it work the same way in every state, independent of that
        # native mnemonic mechanism.
        focus_field_letter = mnemonic_letter(
            self.main_window.i18n.t("type_message"), "D")
        # Same reasoning as message_label's mnemonic above, for the
        # "&Mensagens" label over messages_list: showing _search_panel (see
        # on_ctrl_f) was observed to break that native redirect the same
        # way, leaving Alt+M unable to move focus into the messages list
        # while the in-conversation search bar was open.
        focus_list_letter = mnemonic_letter(
            self.main_window.i18n.t("messages"), "M")

        accel_tbl = wx.AcceleratorTable([
            (CS,               ord("Y"),          self.ID_DESCRIBE_PHOTO),
            (wx.ACCEL_ALT,     ord(focus_field_letter), self.ID_ALT_FOCUS_FIELD),
            (wx.ACCEL_ALT,     ord(focus_list_letter),  self.ID_ALT_FOCUS_LIST),
            (wx.ACCEL_CTRL,    ord("R"),         self.ID_CTRL_R),
            (wx.ACCEL_ALT,     ord("2"),         self.ID_ALT_2),
            (wx.ACCEL_NORMAL,  wx.WXK_ESCAPE,    self.ID_ESC),
            (wx.ACCEL_CTRL,    ord("W"),          self.CTRL_W),
            (CS,               ord("D"),          self.ID_CTRL_SHIFT_D),
            (CS,               ord("A"),          self.ID_CTRL_SHIFT_A),
            (CS,               ord("B"),          self.ID_CTRL_SHIFT_B),
            (wx.ACCEL_ALT,     ord("R"),          self.ID_ALT_R),
            (AS,               ord("D"),          self.ID_ALT_SHIFT_D),
            (CS,               ord("E"),          self.ID_CTRL_SHIFT_E),
            (CS,               ord("P"),          self.ID_CTRL_SHIFT_P),
            (CS,               ord("R"),          self.ID_CTRL_SHIFT_R),
            # G for "gravar": Ctrl+R records in the default mode, this one in
            # the other (stereo / mono). Not Ctrl+Alt+R: that is AltGr+R, which
            # types "®" on US-International and would be taken from the editor.
            (CS,               ord("G"),          self.ID_CTRL_SHIFT_G),
            (CS,               ord("H"),          self.ID_CTRL_SHIFT_H),
            (wx.ACCEL_NORMAL,  wx.WXK_DELETE,     self.ID_DELETE_MSG),
            (wx.ACCEL_CTRL,    ord("C"),          self.ID_CTRL_C),
            (CS,               ord("C"),          self.ID_CTRL_SHIFT_C),
            (wx.ACCEL_ALT,     ord("C"),          self.ID_ALT_C),
            (wx.ACCEL_ALT,     ord("E"),          self.ID_ALT_E),
            (wx.ACCEL_ALT,     ord("L"),          self.ID_ALT_L),
            (AS,               ord("L"),          self.ID_ALT_SHIFT_L),
            (AS,               ord("K"),          self.ID_ALT_SHIFT_K),
            (CS,               ord("S"),          self.ID_CTRL_SHIFT_S),
            (CS,               ord("M"),          self.ID_CTRL_SHIFT_M),
            (CS,               ord("L"),          self.ID_CTRL_SHIFT_L),
            (CS,               ord("F"),          self.ID_CTRL_SHIFT_F),
            (wx.ACCEL_ALT,     ord("3"),          self.ID_ALT_3),
            (wx.ACCEL_ALT,     ord("U"),          self.ID_ALT_U),
            (wx.ACCEL_ALT,     ord("u"),          self.ID_ALT_U),
            (wx.ACCEL_CTRL,    ord("L"),          self.ID_ALT_U),
            (wx.ACCEL_CTRL,    ord("l"),          self.ID_ALT_U),
            (AS,               ord("R"),          self.ID_ALT_SHIFT_R),
            (AS,               ord("E"),          self.ID_ALT_SHIFT_E),
            (AS,               ord("M"),          self.ID_ALT_SHIFT_M),
            (AS,               ord("C"),          self.ID_ALT_SHIFT_C),
            (AS,               ord("V"),          self.ID_ALT_SHIFT_V),
            (CS,               ord("V"),          self.ID_CTRL_SHIFT_V),
            (CAS,              ord("V"),          self.ID_CTRL_ALT_SHIFT_V),
            (AS,               ord("Q"),          self.ID_ALT_SHIFT_Q),
            (AS,               ord("S"),          self.ID_ALT_SHIFT_S),
            (CS,               ord("O"),           self.ID_CTRL_SHIFT_O),
            (wx.ACCEL_ALT,     ord(","),           self.ID_ALT_COMMA),
            (wx.ACCEL_ALT,     ord("."),           self.ID_ALT_PERIOD),
            (wx.ACCEL_CTRL,    ord("."),           self.ID_CTRL_PERIOD),
            (CAS,              ord("C"),           self.ID_BULK_COPY),
            (CAS,              ord("E"),           self.ID_BULK_FORWARD),
            (CAS,              ord("F"),           self.ID_BULK_STAR),
            (CAS,              ord("X"),           self.ID_BULK_PIN),
            (CAS,              ord("S"),           self.ID_BULK_SAVE),
            (CS,               wx.WXK_DELETE,      self.ID_BULK_DELETE),
        ] + [
            (wx.ACCEL_CTRL, ord(str(d)), self.ID_BOOKMARK[d]) for d in range(10)
        ] + [
            (CS,            ord(str(d)), self.ID_BOOKMARK_REMOVE[d]) for d in range(10)
        ] + [
            # Alt+Shift+<digit> / Ctrl+Alt+Shift+<digit>: temporary bookmarks.
            # Both combos were verified to reach the app for all ten digits —
            # including the zeros, where the extra Alt is what keeps them from
            # matching the Windows IME hotkey that eats plain Ctrl+Shift+0
            # (see MainWindow._set_bookmark_zero_hotkey).
            (AS,            ord(str(d)), self.ID_TEMP_BOOKMARK[d]) for d in range(10)
        ] + [
            (CAS,           ord(str(d)), self.ID_TEMP_BOOKMARK_REMOVE[d]) for d in range(10)
        ])
        self.conversation_panel.SetAcceleratorTable(accel_tbl)
        self.Bind(wx.EVT_MENU, self._on_accel_focus_field,          id=self.ID_ALT_FOCUS_FIELD)
        self.Bind(wx.EVT_MENU, self._on_accel_focus_list,           id=self.ID_ALT_FOCUS_LIST)
        self.Bind(wx.EVT_MENU, self.on_record_voice_message,       id=self.ID_CTRL_R)
        self.Bind(wx.EVT_MENU, self._on_record_alternate_mode,     id=self.ID_CTRL_SHIFT_G)
        self.Bind(wx.EVT_MENU, self._on_record_system_audio,       id=self.ID_CTRL_SHIFT_H)
        self.Bind(wx.EVT_MENU, self._on_accel_jump_last,           id=self.ID_ALT_2)
        self.Bind(wx.EVT_MENU, self._on_escape_conversation,        id=self.ID_ESC)
        self.Bind(wx.EVT_MENU, self.close_conversation,            id=self.CTRL_W)
        self.Bind(wx.EVT_MENU, self._on_ctrl_shift_d,              id=self.ID_CTRL_SHIFT_D)
        self.Bind(wx.EVT_MENU, self.on_add_attachment,             id=self.ID_CTRL_SHIFT_A)
        self.Bind(wx.EVT_MENU, self._on_action_save_as,            id=self.ID_CTRL_SHIFT_S)
        self.Bind(wx.EVT_MENU, self._on_accel_reply,               id=self.ID_ALT_R)
        self.Bind(wx.EVT_MENU, self._on_describe_photo,            id=self.ID_DESCRIBE_PHOTO)
        self.Bind(wx.EVT_MENU, self._on_accel_message_data,        id=self.ID_ALT_SHIFT_D)
        self.Bind(wx.EVT_MENU, self._on_accel_forward,             id=self.ID_CTRL_SHIFT_E)
        self.Bind(wx.EVT_MENU, self._on_ctrl_shift_p,              id=self.ID_CTRL_SHIFT_P)
        self.Bind(wx.EVT_MENU, self._on_accel_react,               id=self.ID_CTRL_SHIFT_R)
        self.Bind(wx.EVT_MENU, self._on_accel_delete_message,      id=self.ID_DELETE_MSG)
        self.Bind(wx.EVT_MENU, self._on_accel_copy_message,        id=self.ID_CTRL_C)
        self.Bind(wx.EVT_MENU, self._on_accel_copy_caption,        id=self.ID_CTRL_SHIFT_C)
        self.Bind(wx.EVT_MENU, self._on_accel_show_text_popup,     id=self.ID_ALT_C)
        self.Bind(wx.EVT_MENU, self._on_accel_edit_message,        id=self.ID_ALT_E)
        self.Bind(wx.EVT_MENU, self._on_read_more,                 id=self.ID_ALT_L)
        self.Bind(wx.EVT_MENU, self._on_accel_msg_status,          id=self.ID_ALT_SHIFT_L)
        self.Bind(wx.EVT_MENU, self._on_accel_msg_datetime,        id=self.ID_ALT_SHIFT_K)
        self.Bind(wx.EVT_MENU, self._on_accel_block,               id=self.ID_CTRL_SHIFT_B)
        self.Bind(wx.EVT_MENU, self._on_accel_toggle_read,         id=self.ID_CTRL_SHIFT_M)
        self.Bind(wx.EVT_MENU, self._on_accel_clear,               id=self.ID_CTRL_SHIFT_L)
        self.Bind(wx.EVT_MENU, self._on_accel_open_search,         id=self.ID_CTRL_SHIFT_F)
        self.Bind(wx.EVT_MENU, self._on_accel_jump_unread,         id=self.ID_ALT_3)
        self.Bind(wx.EVT_MENU, self._on_accel_jump_unread,         id=self.ID_ALT_U)
        self.Bind(wx.EVT_MENU, self._on_accel_reply_private,       id=self.ID_ALT_SHIFT_R)
        self.Bind(wx.EVT_MENU, self._on_accel_recent_reactions,    id=self.ID_ALT_SHIFT_E)
        self.Bind(wx.EVT_MENU, self._on_accel_mentions,            id=self.ID_ALT_SHIFT_M)
        self.Bind(wx.EVT_MENU, self._on_accel_copy_number_speak,   id=self.ID_ALT_SHIFT_C)
        self.Bind(wx.EVT_MENU, self._on_accel_alt_shift_v,         id=self.ID_ALT_SHIFT_V)
        self.Bind(wx.EVT_MENU, self._on_accel_voice_call,          id=self.ID_CTRL_SHIFT_V)
        self.Bind(wx.EVT_MENU, self._on_accel_video_call,          id=self.ID_CTRL_ALT_SHIFT_V)
        self.Bind(wx.EVT_MENU, self._on_accel_goto_quoted,         id=self.ID_ALT_SHIFT_Q)
        self.Bind(wx.EVT_MENU, self._on_accel_mute,                id=self.ID_ALT_SHIFT_S)
        self.Bind(wx.EVT_MENU, self._on_accel_star,                 id=self.ID_CTRL_SHIFT_O)
        self.Bind(wx.EVT_MENU, self._on_audio_speed_decrease,      id=self.ID_ALT_COMMA)
        self.Bind(wx.EVT_MENU, self._on_audio_speed_increase,      id=self.ID_ALT_PERIOD)
        self.Bind(wx.EVT_MENU, self._on_open_emoji_picker,          id=self.ID_CTRL_PERIOD)
        self.Bind(wx.EVT_MENU, self._on_accel_bulk_copy,            id=self.ID_BULK_COPY)
        self.Bind(wx.EVT_MENU, self._on_accel_bulk_forward,         id=self.ID_BULK_FORWARD)
        self.Bind(wx.EVT_MENU, self._on_accel_bulk_star,            id=self.ID_BULK_STAR)
        self.Bind(wx.EVT_MENU, self._on_accel_bulk_pin,             id=self.ID_BULK_PIN)
        self.Bind(wx.EVT_MENU, self._on_accel_bulk_save,            id=self.ID_BULK_SAVE)
        self.Bind(wx.EVT_MENU, self._on_accel_bulk_delete,          id=self.ID_BULK_DELETE)
        for _d in range(10):
            self.Bind(wx.EVT_MENU, lambda e, d=_d: self._on_bookmark_set_or_jump(d), id=self.ID_BOOKMARK[_d])
            self.Bind(wx.EVT_MENU, lambda e, d=_d: self._on_bookmark_remove(d),      id=self.ID_BOOKMARK_REMOVE[_d])
            self.Bind(wx.EVT_MENU, lambda e, d=_d: self._on_temp_bookmark_set_or_jump(d), id=self.ID_TEMP_BOOKMARK[_d])
            self.Bind(wx.EVT_MENU, lambda e, d=_d: self._on_temp_bookmark_remove(d),      id=self.ID_TEMP_BOOKMARK_REMOVE[_d])
