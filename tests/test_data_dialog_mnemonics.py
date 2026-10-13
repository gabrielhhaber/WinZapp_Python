"""Keyboard mnemonics of the group data, contact data, group invitation and
join-by-link dialogs, in every locale.

Reported: the buttons of the group data dialog, some of the contact data
dialog's and every control of the group invitation dialog had no Alt+letter at
all. Most of them showed a label shared with a menu item, a window title or
another dialog (add_member, open, save_as, copy_link...), where a mnemonic
could not be added without colliding somewhere else, so the controls of these
dialogs now have keys of their own.

Which letter a locale picks is its own decision; this checks that every
control has one and that no two controls of a window share it. The two data
dialogs are checked as one window: they are the same dialog class and share
most of their controls.
"""

from types import SimpleNamespace

import pytest

from tests.locales import registered_locale_codes
from tests.mnemonics import load_strings, mnemonic
from ui.accessible import split_mnemonic

#: ConversationDataDialog, group and one-to-one, every tab.
DATA_DIALOG_KEYS = (
    "back_btn",
    "profile_picture_btn",
    "group_invitation_title",
    "group_requests_button",
    "edit_group_name",
    "edit_group_description",
    "group_data_add_member_btn",
    "conversation_data_overview_label",
    "edit_contact_local",
    "delete_contact_local",
    "add_contact",
    "contact_data_add_to_group_btn",
    "conversation_sound_label",
    "conversation_sound_preview_play",
    "conversation_sound_custom_path_label",
    "group_media_filter_label",
    "group_media_list_label",
    "group_media_open_btn",
    "group_media_save_as_btn",
    "group_media_types_label",
)
#: GroupInvitationDialog.
INVITATION_KEYS = (
    "group_invitation_link",
    "group_invitation_copy_link",
    "group_invitation_save_qr",
    "group_invitation_refresh",
    "group_invitation_reset",
    "close",
)
#: GroupJoinDialog.
JOIN_KEYS = (
    "group_invitation_link",
    "group_join_preview",
    "group_join_action",
    "close",
)


@pytest.fixture(params=registered_locale_codes())
def strings(request):
    return load_strings(request.param)


@pytest.mark.parametrize("keys", [DATA_DIALOG_KEYS, INVITATION_KEYS, JOIN_KEYS])
def test_every_control_has_a_mnemonic(strings, keys):
    missing = [k for k in keys if mnemonic(strings[k]) is None]
    assert not missing


@pytest.mark.parametrize("keys", [DATA_DIALOG_KEYS, INVITATION_KEYS, JOIN_KEYS])
def test_no_two_controls_of_a_window_share_a_letter(strings, keys):
    letters = {}
    for key in keys:
        letters.setdefault(mnemonic(strings[key]), []).append(key)
    shared = {letter: found for letter, found in letters.items() if len(found) > 1}
    assert not shared


def test_the_sound_preview_button_keeps_its_letter_when_it_becomes_stop(strings):
    """One button, two labels (play / stop). Its stop letter must not belong to
    another control of the dialog; the same letter as play is the best case."""
    stop = mnemonic(strings["conversation_sound_preview_stop"])
    assert stop is not None
    others = {mnemonic(strings[k]) for k in DATA_DIALOG_KEYS
              if k != "conversation_sound_preview_play"}
    assert stop not in others


def test_the_dialogs_use_their_own_keys_not_the_shared_ones():
    """The shared labels stay without a mnemonic on purpose (they are menu
    items, window titles and buttons of other windows), so a control of these
    dialogs that went back to one would lose its Alt+letter silently."""
    import inspect

    from ui.dialogs import conversation_data_dialog, group_invitation_dialog

    data = inspect.getsource(conversation_data_dialog)
    for key in ("group_data_add_member_btn", "contact_data_add_to_group_btn",
                "conversation_data_overview_label", "conversation_sound_preview_play",
                "conversation_sound_preview_stop", "group_media_open_btn",
                "group_media_save_as_btn"):
        assert f'"{key}"' in data, key
    for shared in ('label=self._i18n.t("add_member")', 'label=self._i18n.t("open")',
                   'label=self._i18n.t("save_as")', 'label=self._i18n.t("select_group")',
                   'label=i18n.t("preview_sound_play")'):
        assert shared not in data, shared
    invitation = inspect.getsource(group_invitation_dialog)
    assert '"group_invitation_copy_link"' in invitation
    assert 'label=self._i18n.t("copy_link")' not in invitation


@pytest.mark.parametrize("key", ["group_invitation_title", "edit_group_name",
                                 "edit_group_description", "group_invitation_reset",
                                 "group_invitation_link", "group_join_preview"])
def test_a_label_reused_as_a_title_or_a_name_loses_its_marker(strings, key):
    """These labels are also a window title, a message box caption or a
    control's accessible name, where "&" would be shown or read as text."""
    plain, letter = split_mnemonic(strings[key])
    assert "&" not in plain.replace("&&", "")
    assert letter is not None


def test_the_invitation_dialog_title_and_caption_are_stripped():
    import inspect

    from ui.dialogs import conversation_data_dialog, group_invitation_dialog, group_join_dialog

    invitation = inspect.getsource(group_invitation_dialog)
    assert 'title=split_mnemonic(self._i18n.t("group_invitation_title"))[0]' in invitation
    assert 'split_mnemonic(self._i18n.t("group_invitation_reset"))[0]' in invitation
    join = inspect.getsource(group_join_dialog)
    assert 'SetName(split_mnemonic(self._i18n.t("group_invitation_link"))[0])' in join
    assert 'SetName(split_mnemonic(self._i18n.t("group_join_preview"))[0])' in join
    data = inspect.getsource(conversation_data_dialog)
    assert 'split_mnemonic(self._i18n.t("edit_group_name"))[0]' in data
    assert 'split_mnemonic(self._i18n.t("edit_group_description"))[0]' in data


def test_split_mnemonic_gives_a_title_without_the_marker():
    i18n = SimpleNamespace(t=lambda key: "Convite &para o grupo")
    assert split_mnemonic(i18n.t("group_invitation_title"))[0] == "Convite para o grupo"
