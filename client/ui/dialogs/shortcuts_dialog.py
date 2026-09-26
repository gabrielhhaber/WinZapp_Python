"""
WinZapp – Keyboard Shortcuts Dialog
====================================
Shows all keyboard shortcuts grouped by section in a read-only text area.
Opened with F1 from any main-window panel, and also linked from the
"Quick tip" dialog that appears after first pairing.
"""

import wx


class ShortcutsDialog(wx.Dialog):
    """
    Modal dialog listing all WinZapp keyboard shortcuts grouped by section.

    Parameters
    ----------
    main_window : MainWindow
    """

    def __init__(self, main_window):
        i18n = main_window.i18n
        super().__init__(
            main_window,
            title=i18n.t("shortcuts_title"),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._mw = main_window
        self._build_ui(i18n)

    # ── UI ────────────────────────────────────────────────────────────────

    def _build_ui(self, i18n):
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        # Read-only text control with the shortcuts list
        text = self._build_text(i18n, self._mw)
        self._text_ctrl = wx.TextCtrl(
            panel,
            value=text,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP | wx.HSCROLL,
        )
        sizer.Add(self._text_ctrl, 1, wx.EXPAND | wx.ALL, 10)

        # Close button — also responds to Esc (wx.ID_CANCEL)
        btn_sizer = wx.StdDialogButtonSizer()
        close_btn = wx.Button(panel, wx.ID_CANCEL, i18n.t("close"))
        btn_sizer.AddButton(close_btn)
        btn_sizer.Realize()
        sizer.Add(btn_sizer, 0, wx.ALIGN_CENTER | wx.BOTTOM, 10)

        panel.SetSizer(sizer)

        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        self.SetSizer(outer)
        self.SetSize((520, 520))
        self.CenterOnParent()

        self._text_ctrl.SetFocus()

    # ── Content ───────────────────────────────────────────────────────────

    @staticmethod
    def _build_text(i18n, main_window=None) -> str:
        """Compose the shortcuts text from i18n keys."""
        def section(key):
            return f"── {i18n.t(key)} ──"

        # Alt+<letter> to focus the main navigation list uses whichever
        # letter the active locale marked with "&" in "main_nav" (see
        # MainWindow.create_accelerator_table's nav_letter) — "N" for
        # pt/es/pl, "M" for en — so the help text has to track that instead
        # of hardcoding "N".
        nav_letter = "N"
        _nav_label = i18n.t("main_nav")
        _amp = _nav_label.find("&")
        if 0 <= _amp < len(_nav_label) - 1 and _nav_label[_amp + 1].isalpha():
            nav_letter = _nav_label[_amp + 1].upper()

        lines = [
            section("shortcuts_nav_section"),
            i18n.t("shortcut_alt1_label"),
            i18n.t("shortcut_alt4_label"),
            i18n.t("shortcut_alt5_label"),
            i18n.t("shortcut_alt6_label"),
        ]
        # Alt+7 opens the locked-chats vault. Once the user chose to hide the
        # vault (hide_navigation) this dialog must not advertise it; a vault
        # never set up is visible in the navigation list too, so it is listed.
        vault = getattr(main_window, "_chat_lock_vault", None)
        if vault is not None and not (
                getattr(vault, "configured", False)
                and getattr(vault, "hide_navigation", False)):
            lines.append(i18n.t("shortcut_alt7_label"))
            if getattr(vault, "configured", False):
                lines.append(i18n.t("shortcut_ctrl_shift_k_vault_label"))
        lines += [
            i18n.t("shortcut_alt_nav_label").format(letter=nav_letter),
            i18n.t("shortcut_ctrl_comma_label"),
            i18n.t("shortcut_f1_label"),
            i18n.t("shortcut_ctrl_n_label"),
            i18n.t("shortcut_ctrl_shift_q_list_label"),
            i18n.t("shortcut_ctrl_shift_t_list_label"),
            i18n.t("shortcut_ctrl_shift_alt_m_label"),
            i18n.t("shortcut_ctrl_alt_shift_d_label"),
            i18n.t("shortcut_ctrl_alt_shift_q_label"),
            i18n.t("shortcut_ctrl_alt_shift_p_label"),
        ]
        # Ctrl+Alt+1..9 only exists at all when this process is running under
        # the multi-account system (see MainWindow._build_menubar's Accounts
        # menu / _account_hotkey_slots) — showing it unconditionally would
        # document a shortcut that does nothing for a single-account install.
        if main_window is not None and getattr(main_window, "account_id", None) and getattr(main_window, "registry", None):
            lines.append(i18n.t("shortcut_ctrl_alt_num_label"))
        lines += [
            "",
            section("shortcuts_conv_section"),
            i18n.t("shortcut_alt2_label"),
            i18n.t("shortcut_alt3_label"),
            i18n.t("shortcut_alt_t_label"),
            i18n.t("shortcut_ctrl_num_label"),
            i18n.t("shortcut_ctrl_shift_num_label"),
            i18n.t("shortcut_alt_shift_num_label"),
            i18n.t("shortcut_ctrl_alt_shift_num_label"),
            i18n.t("shortcut_ctrl_r_label"),
            i18n.t("shortcut_ctrl_shift_g_label"),
            # Also listed under "ações em massa" below, the way
            # shortcut_shift_home_label/shortcut_shift_end_label already are:
            # Space's primary meaning is playing the focused audio or video,
            # and someone looking for that will not think to read a mass-
            # actions section.
            i18n.t("shortcut_space_label"),
            i18n.t("shortcut_esc_ctrl_w_label"),
            i18n.t("shortcut_ctrl_shift_j_label"),
            i18n.t("shortcut_ctrl_shift_d_label"),
            i18n.t("shortcut_ctrl_shift_f_label"),
            i18n.t("shortcut_alt_r_label"),
            i18n.t("shortcut_ctrl_shift_e_label"),
            i18n.t("shortcut_ctrl_shift_o_label"),
            i18n.t("shortcut_ctrl_shift_r_label"),
            i18n.t("shortcut_ctrl_shift_p_label"),
            i18n.t("shortcut_alt_comma_label"),
            i18n.t("shortcut_alt_period_label"),
            i18n.t("shortcut_shift_left_label"),
            i18n.t("shortcut_shift_right_label"),
            i18n.t("shortcut_shift_pageup_label"),
            i18n.t("shortcut_shift_pagedown_label"),
            i18n.t("shortcut_shift_home_label"),
            i18n.t("shortcut_shift_end_label"),
            i18n.t("shortcut_delete_label"),
            i18n.t("shortcut_ctrl_c_label"),
            i18n.t("shortcut_ctrl_shift_c_label"),
            i18n.t("shortcut_alt_c_label"),
            i18n.t("shortcut_alt_e_label"),
            i18n.t("shortcut_alt_l_label"),
            i18n.t("shortcut_alt_shift_l_label"),
            i18n.t("shortcut_alt_shift_k_label"),
            i18n.t("shortcut_ctrl_shift_s_label"),
            i18n.t("shortcut_ctrl_shift_m_label"),
            i18n.t("shortcut_ctrl_shift_l_label"),
            i18n.t("shortcut_ctrl_shift_b_label"),
            i18n.t("shortcut_alt_shift_c_label"),
            i18n.t("shortcut_alt_shift_d_label"),
            i18n.t("shortcut_alt_shift_r_label"),
            i18n.t("shortcut_alt_shift_v_label"),
            i18n.t("shortcut_alt_shift_q_label"),
            i18n.t("shortcut_alt_shift_s_label"),
            "",
            section("shortcuts_bulk_section"),
            i18n.t("shortcut_space_label"),
            i18n.t("shortcut_ctrl_space_label"),
            i18n.t("shortcut_shift_down_label"),
            i18n.t("shortcut_shift_home_label"),
            i18n.t("shortcut_shift_end_label"),
            i18n.t("shortcut_ctrl_shift_space_label"),
            # Per-action shortcuts for the "Ações em massa" submenus (the
            # messages list first, then the chat list) — these work
            # regardless of the "Substituir atalhos..." setting the note
            # below describes, and do nothing while nothing is selected.
            i18n.t("shortcut_bulk_copy_label"),
            i18n.t("shortcut_bulk_forward_label"),
            i18n.t("shortcut_bulk_star_label"),
            i18n.t("shortcut_bulk_pin_label"),
            i18n.t("shortcut_bulk_save_label"),
            i18n.t("shortcut_bulk_delete_label"),
            i18n.t("shortcut_bulk_clear_chats_label"),
            i18n.t("shortcut_bulk_delete_chats_label"),
            i18n.t("shortcut_bulk_archive_chats_label"),
            i18n.t("shortcut_bulk_read_chats_label"),
            i18n.t("shortcut_bulk_unread_chats_label"),
            i18n.t("shortcut_bulk_override_note"),
            "",
            section("shortcuts_status_section"),
            i18n.t("shortcut_ctrl_left_label"),
            i18n.t("shortcut_ctrl_right_label"),
            "",
            section("shortcuts_calls_section"),
            i18n.t("shortcut_calls_alt_l_label"),
            i18n.t("shortcut_calls_ctrl_tab_label"),
            i18n.t("shortcut_calls_enter_label"),
            i18n.t("shortcut_calls_ctrl_shift_r_label"),
            i18n.t("shortcut_calls_f5_label"),
            "",
            section("shortcuts_call_window_section"),
            i18n.t("shortcut_call_ctrl_shift_q_label"),
            i18n.t("shortcut_call_ctrl_m_label"),
            i18n.t("shortcut_call_ctrl_p_label"),
            i18n.t("shortcut_call_ctrl_v_label"),
            i18n.t("shortcut_call_ctrl_c_label"),
            "",
            section("shortcuts_search_section"),
            i18n.t("shortcut_search_enter_label"),
            i18n.t("shortcut_search_shift_enter_label"),
            "",
            section("shortcuts_sync_section"),
            i18n.t("shortcut_f5_label"),
            i18n.t("shortcut_shift_f5_label"),
            i18n.t("shortcut_ctrl_shift_alt_b_label"),
            i18n.t("shortcut_ctrl_alt_shift_o_label"),
        ]
        return "\n".join(lines)
