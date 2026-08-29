"""One-to-one chats get the same dialog shape as groups, Media tab included.

WhatsApp itself offers a media list for private chats, and this dialog used to
be two different things depending on the chat: a wx.Notebook with Overview /
Participants / Media for a group, and a single flat page for a private chat. So
someone who had learned where things are in a group found none of it in a
private conversation.

Now both are notebooks built by the same methods — the private one is the group
one minus Participants. The tab order, the control order inside Media and the
shortcuts have to match, which is exactly what a screen-reader user relies on
and what a second copy of the code would have drifted away from.

Needs a real wx.App: unlike most wx classes in this suite, the point here is
the dialog's actual construction, so binding unbound methods onto a stub would
test nothing. See conftest.py's wx_app fixture.
"""

import threading

import wx
import pytest

from core.i18n import I18n
from core.sound_system import DEFAULT_PACK_ID
from ui.dialogs.conversation_data_dialog import ConversationDataDialog

# Constructs a REAL top-level wx dialog - see the wxgui marker in pytest.ini.
pytestmark = pytest.mark.wxgui

PRIVATE_JID = "5511900000000@s.whatsapp.net"
GROUP_JID = "120363000000000000@g.us"


class _FakeDb:
    def get_messages_asc(self, jid, limit=None):
        return []


class _FakeMainWindow(wx.Frame):
    """The dialog parents itself to main_window, so this has to be a real
    wx.Window. Everything else is the handful of attributes construction and
    the background fetch touch."""

    def __init__(self):
        super().__init__(None)
        # settings first: I18n reads it during construction.
        self.settings = {"conversation_sounds": {}, "alert_tones": {}}
        self.i18n = I18n(self)
        self.i18n.get_language()
        self.db = _FakeDb()
        self.chats = {}
        self.contacts = {}
        self._lid_to_phone = {}
        self._default_sound_pack = {"name": "Default", "path": ""}
        self._sound_packs = {DEFAULT_PACK_ID: {"name": "Default", "path": ""}}
        self.conversations_panel = None

    def get_active_sound_pack(self):
        return self._default_sound_pack

    def _resolve_contact_name(self, chat):
        return "Fulano"

    def find_name_through_messages(self, chat):
        return ""

    def get_contact_profile(self, jid):
        return {}

    def get_group_info(self, jid):
        return {"participants": []}

    def resolve_lid_jids_via_api(self, jids):
        return None


@pytest.fixture
def make_dialog(wx_app, monkeypatch):
    """Build the real dialog, with its background fetch thread suppressed.

    _fetch_data() runs on its own thread and calls back through wx.CallAfter,
    which would land after the test has destroyed the dialog."""
    monkeypatch.setattr(threading, "Thread", lambda *a, **k: _NoThread())
    made = []

    def _make(jid):
        frame = _FakeMainWindow()
        dlg = ConversationDataDialog(frame, {"remoteJid": jid, "name": "Grupo X"})
        made.append((dlg, frame))
        return dlg

    yield _make
    for dlg, frame in made:
        dlg.Destroy()
        frame.Destroy()


class _NoThread:
    def start(self):
        pass


def _tab_labels(dialog):
    nb = dialog._notebook
    return [nb.GetPageText(i) for i in range(nb.GetPageCount())]


class TestThePrivateDialogIsANotebook:
    def test_it_has_a_notebook_at_all(self, make_dialog):
        dialog = make_dialog(PRIVATE_JID)
        assert isinstance(dialog._notebook, wx.Notebook)

    def test_overview_first_media_second(self, make_dialog):
        dialog = make_dialog(PRIVATE_JID)
        i18n = dialog._i18n
        assert _tab_labels(dialog) == [
            i18n.t("group_overview_tab"), i18n.t("group_media_tab"),
        ]

    def test_there_is_no_participants_tab(self, make_dialog):
        """The one part of the group dialog that has no meaning here."""
        dialog = make_dialog(PRIVATE_JID)
        assert dialog._i18n.t("group_participants_tab") not in _tab_labels(dialog)
        assert not hasattr(dialog, "_part_list")


class TestTheGroupDialogIsUnchanged:
    def test_it_still_has_three_tabs_in_the_same_order(self, make_dialog):
        dialog = make_dialog(GROUP_JID)
        i18n = dialog._i18n
        assert _tab_labels(dialog) == [
            i18n.t("group_overview_tab"),
            i18n.t("group_participants_tab"),
            i18n.t("group_media_tab"),
        ]

    def test_media_is_still_the_last_tab(self, make_dialog):
        """Its index is what a returning user's muscle memory holds."""
        dialog = make_dialog(GROUP_JID)
        assert dialog._notebook.GetPageText(2) == dialog._i18n.t("group_media_tab")


class TestTheMediaTabIsTheSameInBoth:
    """Built by one method rather than copied, so this is checking the wiring
    reaches both — not that two copies happen to agree today."""

    CONTROLS = (
        "_media_filter_radio", "_media_list_label", "_media_list",
        "_media_open_btn", "_media_save_btn", "_media_label",
        "_media_types_label", "_media_types_list",
    )

    @pytest.mark.parametrize("jid", [PRIVATE_JID, GROUP_JID])
    def test_every_media_control_exists(self, make_dialog, jid):
        dialog = make_dialog(jid)
        for name in self.CONTROLS:
            assert getattr(dialog, name, None) is not None, name

    @pytest.mark.parametrize("jid", [PRIVATE_JID, GROUP_JID])
    def test_the_media_controls_live_on_the_media_page(self, make_dialog, jid):
        """Parented to the page, not the dialog panel — otherwise they draw
        outside the tab."""
        dialog = make_dialog(jid)
        media_page = dialog._media_list.GetParent()
        assert dialog._notebook.FindPage(media_page) != wx.NOT_FOUND
        for name in self.CONTROLS:
            assert getattr(dialog, name).GetParent() is media_page, name

    @pytest.mark.parametrize("jid", [PRIVATE_JID, GROUP_JID])
    def test_the_type_checkboxes_are_all_present(self, make_dialog, jid):
        from core.utils import GROUP_MEDIA_TYPES
        dialog = make_dialog(jid)
        assert dialog._media_types_list.GetItemCount() == len(GROUP_MEDIA_TYPES)

    @pytest.mark.parametrize("jid", [PRIVATE_JID, GROUP_JID])
    def test_a_row_is_preselected_in_the_type_list(self, make_dialog, jid):
        """Arriving by Tab must announce the first media type, not an empty
        selection."""
        dialog = make_dialog(jid)
        assert dialog._media_types_list.GetFocusedItem() == 0


class TestTheOverviewTabKeptItsContents:
    """The private overview is the old flat page moved into a tab; nothing
    should have been lost on the way."""

    def test_the_profile_field_is_there(self, make_dialog):
        dialog = make_dialog(PRIVATE_JID)
        assert dialog._info_ctrl is not None

    def test_it_lives_on_the_overview_page(self, make_dialog):
        dialog = make_dialog(PRIVATE_JID)
        overview = dialog._info_ctrl.GetParent()
        assert dialog._notebook.FindPage(overview) == 0

    def test_the_contact_buttons_are_parented_to_the_page(self, make_dialog):
        """They are rebuilt in place whenever the local contact changes; the
        dialog panel would put them outside the tab."""
        dialog = make_dialog(PRIVATE_JID)
        assert dialog._contact_panel is dialog._info_ctrl.GetParent()

    def test_the_sound_picker_is_there(self, make_dialog):
        dialog = make_dialog(PRIVATE_JID)
        assert dialog._sound_combo is not None


class TestTheTitleStillSaysWhichKindOfChatItIs:
    def test_private(self, make_dialog):
        dialog = make_dialog(PRIVATE_JID)
        assert dialog._i18n.t("conversation_data") in dialog.GetTitle()

    def test_group(self, make_dialog):
        dialog = make_dialog(GROUP_JID)
        assert dialog._i18n.t("group_data") in dialog.GetTitle()
