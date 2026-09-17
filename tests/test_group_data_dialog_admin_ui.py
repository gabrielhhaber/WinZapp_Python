"""Structural tests for ConversationDataDialog's admin-related additions:
announcing admin status in the participants list, the participant context
menu (remove/promote/demote), and the admin-only edit-name/edit-description
buttons.

ConversationDataDialog is a wx.Dialog and cannot be instantiated without a
running wx.App, and _populate_group_unsafe()/the context-menu handlers all
build real wx.Menu/wx.Dialog objects — so, same approach as
tests/test_archived_context_menu.py, the wiring is checked structurally via
source inspection rather than by driving live wx widgets.
"""

import inspect

from ui.dialogs.conversation_data_dialog import ConversationDataDialog


class TestParticipantRowAnnouncesAdminStatus:
    def test_row_label_appends_the_admin_suffix_key_when_admin(self):
        # Built by _participant_row(), shared by the first fill and by the
        # in-place repaint after background @lid resolution.
        src = inspect.getsource(ConversationDataDialog._participant_row)
        assert "group_admin_suffix" in src
        assert "self._participant_row(" in inspect.getsource(
            ConversationDataDialog._populate_group_unsafe
        )

    def test_column_two_uses_the_translated_label_not_a_hardcoded_string(self):
        src = inspect.getsource(ConversationDataDialog._populate_group_unsafe)
        assert 'i18n.t("group_admin")' in src
        # The old bug: a bare English "admin" literal regardless of locale.
        assert '"admin" if' not in src

    def test_participant_names_and_admin_flags_are_tracked_in_parallel(self):
        src = inspect.getsource(ConversationDataDialog._populate_group_unsafe)
        assert "self._participant_names.append(p_name)" in src
        assert "self._participant_is_admin.append(is_admin_bool)" in src


class TestParticipantContextMenu:
    def test_bound_to_the_participants_list(self):
        src = inspect.getsource(ConversationDataDialog._build_group_ui)
        assert "_on_participant_context_menu" in src
        assert "EVT_CONTEXT_MENU" in src

    def test_menu_is_gated_on_user_is_admin(self):
        src = inspect.getsource(ConversationDataDialog._on_participant_context_menu)
        assert "self._user_is_admin" in src

    def test_offers_remove_and_a_promote_or_demote_toggle(self):
        src = inspect.getsource(ConversationDataDialog._on_participant_context_menu)
        assert "remove_member" in src
        assert "promote_to_admin" in src
        assert "demote_from_admin" in src

    def test_remove_member_confirms_before_calling_the_api(self):
        src = inspect.getsource(ConversationDataDialog._on_remove_member)
        assert "wx.MessageBox" in src
        assert "remove_member_confirm" in src
        assert "wx.YES_NO" in src
        assert "self._mw.remove_group_members" in src

    def test_each_action_rechecks_permission_before_acting(self):
        """Defense in depth: even though the menu already hides these
        options for non-admins, each handler independently re-checks
        _user_is_admin before doing anything irreversible."""
        for fn in (
            ConversationDataDialog._on_remove_member,
            ConversationDataDialog._on_promote_member,
            ConversationDataDialog._on_demote_member,
            ConversationDataDialog._on_edit_group_name,
            ConversationDataDialog._on_edit_group_description,
        ):
            src = inspect.getsource(fn)
            assert "_user_is_admin" in src, f"{fn.__name__} does not check admin status"
            assert "group_action_no_permission" in src, f"{fn.__name__} has no permission-error path"


class TestAddMembersButtonOnOverviewTabIsNotAdminGated:
    """Reported live: the Overview tab's own "Add member" button was
    disabled at construction and only re-enabled inside the
    `if self._user_is_admin:` block, right alongside the genuinely
    admin-only edit-name/edit-description buttons — making it unusable for
    every non-admin member, even though adding members is a normal-member
    action in WhatsApp by default (the server enforces the real
    restriction if the group's own settings actually require admin)."""

    def test_overview_button_is_not_disabled_at_construction(self):
        src = inspect.getsource(ConversationDataDialog._build_group_ui)
        assert "self._add_members_btn_overview.Disable()" not in src

    def test_overview_button_is_not_inside_the_admin_gate(self):
        populate_src = inspect.getsource(ConversationDataDialog._populate_group_unsafe)
        gate_start = populate_src.index("if self._user_is_admin:")
        gate_end = populate_src.index("else:", gate_start)
        gated_block = populate_src[gate_start:gate_end]
        assert "_add_members_btn_overview" not in gated_block

    def test_participants_tab_button_is_still_admin_gated(self):
        """Sanity check the other Add button's existing admin gating was
        left untouched by this fix."""
        build_src = inspect.getsource(ConversationDataDialog._build_group_ui)
        assert "self._add_members_btn.Disable()" in build_src

        populate_src = inspect.getsource(ConversationDataDialog._populate_group_unsafe)
        gate_start = populate_src.index("if self._user_is_admin:")
        gate_end = populate_src.index("else:", gate_start)
        gated_block = populate_src[gate_start:gate_end]
        assert "self._add_members_btn.Enable()" in gated_block


class TestAdminOnlyEditButtons:
    def test_buttons_start_hidden_and_are_shown_only_for_admins(self):
        build_src = inspect.getsource(ConversationDataDialog._build_group_ui)
        assert "_edit_name_btn" in build_src and "_edit_desc_btn" in build_src
        assert "_edit_name_btn.Hide()" in build_src
        assert "_edit_desc_btn.Hide()" in build_src

        populate_src = inspect.getsource(ConversationDataDialog._populate_group_unsafe)
        assert "_edit_name_btn.Show()" in populate_src
        assert "_edit_desc_btn.Show()" in populate_src
        assert "_edit_name_btn.Hide()" in populate_src
        assert "_edit_desc_btn.Hide()" in populate_src

    def test_edit_dialog_prefills_the_current_value(self):
        name_src = inspect.getsource(ConversationDataDialog._on_edit_group_name)
        assert "self._group_subject" in name_src

        desc_src = inspect.getsource(ConversationDataDialog._on_edit_group_description)
        assert "self._group_description" in desc_src

    def test_prompt_dialog_has_save_and_cancel_buttons(self):
        src = inspect.getsource(ConversationDataDialog._prompt_text_edit)
        assert 'wx.ID_OK, label=self._i18n.t("save_btn")' in src
        assert 'wx.ID_CANCEL, label=self._i18n.t("cancel")' in src

    def test_blank_field_is_supported_for_an_empty_description(self):
        """The prompt must not fall back to some placeholder — it should
        show a literally empty field when the group has no description."""
        src = inspect.getsource(ConversationDataDialog._prompt_text_edit)
        assert 'value=initial or ""' in src
