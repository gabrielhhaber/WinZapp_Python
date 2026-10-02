"""Real native-control wiring; CI only, never on an NVDA user's desktop."""
from types import SimpleNamespace

import pytest
import wx

from app_settings import AppSettings
from core.ai_credentials import CredentialStore
from core.i18n import I18n
from tests.conftest import hidden_frame, destroy_now
from ui.dialogs.image_description_dialog import ImageDescriptionDialog, PhotoConsentDialog
from ui.dialogs.image_description_settings import ImageDescriptionSettingsPage

pytestmark = pytest.mark.wxgui


@pytest.fixture
def context(wx_app, tmp_path, monkeypatch):
    frame = hidden_frame()
    frame.settings = {"general": {"language": "en-US"}}
    frame.i18n = I18n(frame)
    frame.app_settings = AppSettings(str(tmp_path))
    frame._mark_dirty = lambda: None
    monkeypatch.setattr("ui.dialogs.image_description_settings.global_dir", lambda: str(tmp_path))
    monkeypatch.setattr("ui.dialogs.image_description_dialog.wx.CallAfter", lambda *args, **kw: None)
    yield frame, tmp_path
    destroy_now(frame)


def test_result_and_question_are_keyboard_readable_native_controls(context):
    frame, path = context
    panel = wx.Panel(frame)
    panel.main_window = frame
    config = {"provider": "openai", "model": "gpt-4.1-mini", "consented": False,
              "profile": "balanced", "read_answers": False}
    dialog = ImageDescriptionDialog(panel, ("account", "chat", "message"), False, config,
                                    frame.app_settings, lambda: b"")
    try:
        assert dialog.result.GetWindowStyleFlag() & wx.TE_READONLY
        assert dialog.result.GetWindowStyleFlag() & wx.TE_MULTILINE
        assert dialog.question.GetWindowStyleFlag() & wx.TE_MULTILINE
        assert dialog.result.GetName() == "Description and answers"
        assert "Ctrl+Enter" in dialog.question.GetName()
        status_label = dialog.status.GetPrevSibling()
        assert isinstance(status_label, wx.StaticText)
        assert status_label.GetLabel() == frame.i18n.t("ai_status")
        assert dialog.GetParent() is panel
    finally:
        dialog.Destroy()


def test_locked_consent_cannot_be_remembered(context):
    frame, _ = context
    dialog = PhotoConsentDialog(frame, frame.i18n, "gemini", locked=True)
    try:
        assert not dialog.remember.IsEnabled()
    finally:
        dialog.Destroy()


def test_saved_key_is_deliberately_readable_via_tab_control(context):
    frame, path = context
    CredentialStore(path).set("openai", "synthetic-key")
    notebook = wx.Notebook(frame)
    page = ImageDescriptionSettingsPage(notebook, frame)
    notebook.AddPage(page, "Photo description")
    assert page.GetBestSize().height <= 600
    assert page.scrollIntoView
    assert page.notice.GetName() == frame.i18n.t("ai_settings_help")
    assert page.notice.GetPrevSibling().GetLabel() == frame.i18n.t("ai_settings_help")
    assert page.status.GetName() == frame.i18n.t("ai_status")
    assert page.status.GetPrevSibling().GetLabel() == frame.i18n.t("ai_status")
    assert page.status.GetWindowStyleFlag() & wx.TE_MULTILINE
    assert page.key.GetValue() == "" and page.key.GetWindowStyleFlag() & wx.TE_PASSWORD
    page._show_key(None)
    assert page.revealed.GetValue() == "synthetic-key"
    assert page.revealed.IsShown() and page.revealed.GetWindowStyleFlag() & wx.TE_READONLY
    page._show_key(None)
    assert not page.revealed.IsShown() and page.revealed.GetValue() == ""


def test_optional_technical_help_has_native_readable_text_and_close_button(context, monkeypatch):
    frame, _ = context
    notebook = wx.Notebook(frame)
    page = ImageDescriptionSettingsPage(notebook, frame)
    seen = []

    class HelpDialog(wx.Dialog):
        def ShowModal(self):
            text, close = self.GetChildren()
            assert isinstance(text, wx.TextCtrl)
            assert text.GetWindowStyleFlag() & wx.TE_READONLY
            assert text.GetWindowStyleFlag() & wx.TE_MULTILINE
            assert text.GetName() == frame.i18n.t("ai_technical_info")
            assert "store=false" in text.GetValue()
            assert close.GetId() == wx.ID_CANCEL
            seen.append(True)
            return wx.ID_CANCEL

    monkeypatch.setattr("ui.dialogs.image_description_settings.wx.Dialog", HelpDialog)
    page._technical_info(None)
    assert seen == [True]


def test_model_list_is_a_named_native_choice_and_selection_is_explicit(context, monkeypatch):
    from core.image_description.model_catalog import ModelOption
    import ui.dialogs.image_description_models as module
    frame, path = context
    CredentialStore(path).set("openai", "synthetic-key")
    frame.output = lambda text: None
    notebook = wx.Notebook(frame)
    page = ImageDescriptionSettingsPage(notebook, frame)
    assert isinstance(page.model_choice, wx.Choice)
    assert page.model_choice.GetName() == frame.i18n.t("ai_model_choice")
    assert page.model_choice.GetPrevSibling().GetLabel() == frame.i18n.t("ai_model_choice")
    assert not page.model_choice.IsEnabled()
    monkeypatch.setattr(module, "fetch_models", lambda *args: (
        ModelOption("gpt-4.1-mini", "GPT-4.1 Mini"), ModelOption("gpt-4.1", "GPT-4.1")))
    monkeypatch.setattr(module, "submit", lambda work, complete: complete(work(), None))
    monkeypatch.setattr(module.wx, "CallAfter", lambda f, *args: f(*args))
    page._fetch_models(None)
    assert page.model_choice.IsEnabled() and page.model_choice.GetCount() == 2
    assert page.model.GetValue() == "gpt-4.1-mini" and page.model_choice.GetSelection() == 0
    page.model_choice.SetSelection(1)
    page._select_model(SimpleNamespace(Skip=lambda: None))
    assert page.model.GetValue() == "gpt-4.1"


@pytest.mark.parametrize("hidden", [False, True])
def test_settings_page_is_appended_without_moving_connection_or_exposing_vault(context, hidden):
    from cryptography.fernet import Fernet
    from core.chat_lock_vault import ChatLockVault
    from ui.dialogs.settings_dialog import SettingsDialog
    from tests.test_settings_checkboxes_roundtrip import _make_frame
    _, path = context
    frame = _make_frame({"general": {"language": "en-US"}})
    frame.app_settings = AppSettings(str(path))
    frame._chat_lock_vault = ChatLockVault(Fernet.generate_key())
    frame._chat_lock_vault.configure("246810", "reveal-code")
    frame._chat_lock_vault.set_hide_navigation(hidden)
    frame._chat_lock_unlocked = False
    dialog = None
    try:
        dialog = SettingsDialog(frame)
        index = dialog._notebook.FindPage(dialog._image_description_page)
        assert index == (14 if hidden else 15)
        assert dialog._notebook.GetPage(4) is dialog._conn_page
        assert dialog._chat_lock_tab_shown is (not hidden)
    finally:
        destroy_now(dialog if dialog is not None else frame)
