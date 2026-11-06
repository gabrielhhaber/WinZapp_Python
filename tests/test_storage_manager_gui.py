"""Native control construction is CI-only through the wxgui gate."""
from types import SimpleNamespace

import pytest
import wx

from core.i18n import I18n
from core.storage_manager.service import StorageService
from core.storage_manager.store import StorageStore
from core.storage_manager.references import References
from tests.conftest import hidden_frame, destroy_now
from ui.dialogs.storage_manager import StorageManagerDialog
from ui.dialogs.storage_rules import RuleEditor
from ui.dialogs.storage_matching import StorageMatchingDialog
from ui.dialogs.storage_sensitive import StorageDetailsDialog

pytestmark = pytest.mark.wxgui


def test_manager_builds_native_tabs_without_scanning_or_selecting(wx_app, tmp_path):
    frame = hidden_frame()
    frame.settings = {"general": {"language": "en-US"}}
    frame.i18n = I18n(frame)
    frame.speak_output = SimpleNamespace(output=lambda text: None)
    calls = []
    service = StorageService(StorageStore(str(tmp_path)), None,
        lambda prefs: calls.append("roots"), lambda cancel: References({}), wx.CallAfter)
    dialog = None
    try:
        dialog = StorageManagerDialog(frame, frame, service)
        assert dialog.book.GetPageCount() == 6
        assert not calls and not service.jobs.busy and not dialog.selected
        assert not dialog.apply_button.IsEnabled()
        assert dialog.summary.GetWindowStyleFlag() & wx.TE_READONLY
        assert isinstance(dialog.files, wx.ListCtrl) and isinstance(dialog.options, wx.ScrolledWindow)
        assert dialog.files.HasCheckBoxes()
        assert dialog.search.GetWindowStyleFlag() & wx.TE_PROCESS_ENTER
        assert dialog.options.action.GetSelection() == 0
        frame._storage_context = SimpleNamespace(capture_chats=lambda: None,
                                                allowed_chats=lambda: {"synthetic": "Synthetic public chat"})
        with StorageMatchingDialog(dialog, dialog, service.preferences) as matching:
            assert matching.chats.has_options()
            matching.clear_private_state()
            assert not matching.chats.has_options()
        with StorageDetailsDialog(dialog, "Synthetic named content", service.visibility_epoch) as details:
            assert details.text.GetValue() == "Synthetic named content"
            details.clear_private_state()
            assert details.text.GetValue() == ""
        with RuleEditor(dialog, frame.i18n) as rule:
            assert not rule.enabled.GetValue() and rule.mode.GetCount() == 2
            rule.category.SetSelection(2)
            rule.changed()
            assert rule.mode.GetCount() == 3 and not rule.media_type.IsEnabled()
    finally:
        if dialog is not None:
            dialog.Destroy()
        destroy_now(frame)
