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
from types import SimpleNamespace
from unittest.mock import Mock
import pytest

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
        ):
            src = inspect.getsource(fn)
            assert "_user_is_admin" in src, f"{fn.__name__} does not check admin status"
            assert "group_action_no_permission" in src, f"{fn.__name__} has no permission-error path"


class TestGroupPermissionRefresh:
    @pytest.mark.parametrize('identity,admin,restrict,mode,edit,add', [
        ('111@c.us',False,False,'all_member_add',True,True),
        ('111@c.us',False,True,'admin_add',False,False),
        ('111@c.us',True,True,'admin_add',True,True),
        ('',False,False,'all_member_add',False,False),
    ])
    def test_both_tabs_follow_permissions_and_unknown_identity_is_not_admin(self,identity,admin,restrict,mode,edit,add):
        part=Mock(); part.GetItemCount.return_value=0
        stub=SimpleNamespace(_i18n=SimpleNamespace(t=lambda key:key),_name='Group',
            _overview_ctrl=Mock(),_part_list=part,_apply_subject_to_title=Mock(),
            _participant_row=lambda jid,flag:('Member','','Member',jid),
            _mw=SimpleNamespace(my_jid=identity,my_lid='',_phone_digits_equivalent=lambda a,b:a==b),
            _invitation_btn=Mock(),_requests_btn=Mock(),_add_members_btn=Mock(),_add_members_btn_overview=Mock(),_edit_name_btn=Mock(),_edit_desc_btn=Mock())
        # Refresh twice: the second response must revoke previously shown rights.
        for flag,restriction,member_mode in [(True,False,'all_member_add'),(admin,restrict,mode)]:
            ConversationDataDialog._populate_group_unsafe(stub,{'participants':[{'id':'111@c.us','isAdmin':flag}],
                'restrict':restriction,'memberAddMode':member_mode})
        assert stub._user_is_admin is (admin and bool(identity))
        stub._invitation_btn.Enable.assert_called_with(True)
        # Join requests are an admin's alone: the button follows admin status.
        stub._requests_btn.Show.assert_called_with(admin and bool(identity))
        stub._add_members_btn.Enable.assert_called_with(add)
        stub._add_members_btn_overview.Enable.assert_called_with(add)
        stub._edit_name_btn.Show.assert_called_with(edit)
        stub._edit_desc_btn.Show.assert_called_with(edit)

    @pytest.mark.parametrize('handler',[ConversationDataDialog._on_edit_group_name,
                                       ConversationDataDialog._on_edit_group_description])
    def test_edit_handler_uses_confirmed_edit_permission(self,handler):
        stub=SimpleNamespace(_user_is_admin=True,_user_can_edit_group=False,
            _show_group_action_error=Mock(),_i18n=SimpleNamespace(t=lambda key:key),_prompt_text_edit=Mock())
        handler(stub,None)
        stub._show_group_action_error.assert_called_once_with('group_action_no_permission')
        stub._prompt_text_edit.assert_not_called()


class TestPermissionAwareEditButtons:
    def test_buttons_start_hidden_until_permissions_arrive(self):
        build_src = inspect.getsource(ConversationDataDialog._build_group_ui)
        assert "_edit_name_btn" in build_src and "_edit_desc_btn" in build_src
        assert "_edit_name_btn.Hide()" in build_src
        assert "_edit_desc_btn.Hide()" in build_src

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
