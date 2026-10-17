"""Real native-control wiring; CI only, never on an NVDA user's desktop."""
from types import SimpleNamespace

import pytest
import wx

from app_settings import AppSettings
from core.ai_credentials import CredentialStore
from core.ai_media import config as ai_config
from core.i18n import I18n
from tests.conftest import hidden_frame, destroy_now
from ui.accessible import AccessibleAskQuestion
from ui.dialogs.ai_result_dialog import AIConsentDialog, AIResultDialog
from ui.dialogs.ai_settings_page import AIProviderDialog, AISettingsPage

pytestmark = pytest.mark.wxgui


@pytest.fixture
def context(wx_app, tmp_path, monkeypatch):
    frame = hidden_frame()
    frame.settings = {"general": {"language": "en-US"}}
    frame.i18n = I18n(frame)
    frame._app_settings = AppSettings(str(tmp_path))
    monkeypatch.setattr("ui.dialogs.ai_settings_page.global_dir", lambda: str(tmp_path))
    monkeypatch.setattr("ui.dialogs.ai_result_dialog.wx.CallAfter", lambda *args, **kw: None)
    yield frame, tmp_path
    destroy_now(frame)


def plain(frame, key):
    """A control's name: its label without the mnemonic marker."""
    return frame.i18n.t(key).replace("&", "")


def result_window(frame, kind="image"):
    panel = wx.Panel(frame)
    panel.main_window = frame
    config = ai_config.preferences(frame._app_settings)
    return panel, AIResultDialog(panel, ("account", "chat", "message"), kind, False, config,
                                 frame._app_settings, lambda token: b"", "image/jpeg")


def test_result_and_question_are_keyboard_readable_native_controls(context):
    frame, path = context
    panel, dialog = result_window(frame)
    try:
        assert dialog.result.GetWindowStyleFlag() & wx.TE_READONLY
        assert dialog.result.GetWindowStyleFlag() & wx.TE_MULTILINE
        assert dialog.question.GetWindowStyleFlag() & wx.TE_MULTILINE
        assert dialog.result.GetName() == frame.i18n.t("ai_result")
        status_label = dialog.status.GetPrevSibling()
        assert isinstance(status_label, wx.StaticText)
        assert status_label.GetLabel() == frame.i18n.t("status")
        assert dialog.GetParent() is panel
    finally:
        dialog.Destroy()


def test_no_shortcut_or_mnemonic_is_written_into_a_label_or_name(context):
    """Shortcuts are announced by the controls' accessible objects, never by
    their text (a mnemonic would also fire on the bare letter)."""
    frame, _ = context
    _, dialog = result_window(frame)
    try:
        for control in (dialog.question, dialog.ask, dialog.copy, dialog.regenerate, dialog.cancel):
            text = control.GetName() if isinstance(control, wx.TextCtrl) else control.GetLabel()
            assert "&" not in text and "Ctrl" not in text and "Alt" not in text, text
        assert isinstance(dialog.question.GetAccessible(), AccessibleAskQuestion)
        assert isinstance(dialog.ask.GetAccessible(), AccessibleAskQuestion)
    finally:
        dialog.Destroy()


@pytest.mark.parametrize("kind", ["audio", "pdf"])
def test_audio_and_pdf_are_converted_in_one_go_without_a_question_box(context, kind):
    frame, _ = context
    _, dialog = result_window(frame, kind)
    try:
        assert dialog.question is None and dialog.ask is None
        # The first request is deferred with wx.CallAfter (patched out here), so the window is idle.
        assert dialog.regenerate.IsEnabled() and not dialog.cancel.IsEnabled()
    finally:
        dialog.Destroy()


def test_the_window_title_names_what_is_being_done(context):
    frame, _ = context
    for kind, key in (("image", "ai_result_description_title"), ("audio", "ai_result_transcription_title"),
                      ("pdf", "ai_result_pdf_title")):
        _, dialog = result_window(frame, kind)
        try:
            assert dialog.GetTitle() == frame.i18n.t(key)
        finally:
            dialog.Destroy()


def test_locked_consent_cannot_be_remembered_and_names_every_provider(context):
    frame, _ = context
    dialog = AIConsentDialog(frame, frame.i18n, ["gemini", "openai"], "image", locked=True)
    try:
        assert not dialog.remember.IsEnabled()
        text = next(child for child in dialog.GetChildren() if isinstance(child, wx.TextCtrl))
        assert "Google Gemini" in text.GetValue() and "OpenAI" in text.GetValue()
    finally:
        dialog.Destroy()


def settings_page(frame):
    notebook = wx.Notebook(frame)
    page = AISettingsPage(notebook, frame, on_change=lambda: None)
    notebook.AddPage(page, frame.i18n.t("tab_ai_accessibility"))
    return notebook, page


def test_provider_list_is_a_named_plain_listbox_with_state_in_the_item_text(context):
    frame, path = context
    CredentialStore(path).set("openai", "synthetic-key")
    _, page = settings_page(frame)
    assert isinstance(page.providers, wx.ListBox) and not isinstance(page.providers, wx.CheckListBox)
    assert page.providers.GetName() == plain(frame, "ai_provider_list_label")
    assert page.providers.GetCount() == len(ai_config.PROVIDERS)
    rows = [page.providers.GetString(i) for i in range(page.providers.GetCount())]
    openai = next(row for row in rows if row.startswith("OpenAI"))
    assert frame.i18n.t("ai_key_saved") in openai and frame.i18n.t("ai_provider_state_enabled") in openai
    assert page.GetBestSize().height <= 600


def test_provider_list_has_the_first_provider_selected_so_it_lands_there(context):
    frame, _ = context
    _, page = settings_page(frame)
    assert page.providers.GetSelection() == 0


def test_page_controls_are_plain_named_controls(context):
    frame, _ = context
    _, page = settings_page(frame)
    assert page.notice.GetName() == plain(frame, "ai_settings_help")
    assert page.notice.GetPrevSibling().GetLabel() == frame.i18n.t("ai_settings_help")
    assert page.status.GetName() == plain(frame, "ai_status_label")
    assert page.status.GetWindowStyleFlag() & wx.TE_MULTILINE
    assert set(page.toggles) == set(ai_config.KINDS) and all(isinstance(c, wx.CheckBox) for c in page.toggles.values())
    assert isinstance(page.profile, wx.Choice) and page.profile.GetName() == plain(frame, "ai_profile")


def provider_window(frame, path, provider="openai", **state):
    values = {"key": "", "deleted": False, "model": ai_config.PROVIDERS[provider].model, "enabled": True}
    values.update(state)
    return AIProviderDialog(frame, frame, provider, values, CredentialStore(path), False)


def test_saved_key_is_masked_and_deliberately_readable_on_request(context):
    frame, path = context
    CredentialStore(path).set("openai", "synthetic-key")
    window = provider_window(frame, path)
    try:
        assert window.key.GetValue() == "" and window.key.GetWindowStyleFlag() & wx.TE_PASSWORD
        window._show_key(None)
        assert window.revealed.GetValue() == "synthetic-key"
        assert window.revealed.IsShown() and window.revealed.GetWindowStyleFlag() & wx.TE_READONLY
        window._show_key(None)
        assert not window.revealed.IsShown() and window.revealed.GetValue() == ""
    finally:
        window.Destroy()


def test_manual_model_fields_follow_the_automatic_checkbox(context):
    frame, path = context
    for pinned in (False, True):
        window = provider_window(frame, path, model="gpt-4.1" if pinned else "")
        try:
            assert window.automatic.GetValue() is (not pinned)
            for control in (window.model, window.model_choice, window.get_models):
                assert control.IsShown() is pinned
            assert window.model.IsEnabled()
        finally:
            window.Destroy()


def test_provider_window_opens_with_the_configured_key_filled_in(context):
    frame, path = context
    window = provider_window(frame, path, key="synthetic-key")
    try:
        assert window.key.GetValue() == "synthetic-key"
        assert window.key_state.GetLabel() == frame.i18n.t("ai_key_saved")
    finally:
        window.Destroy()


def test_provider_window_says_what_the_provider_handles(context):
    frame, path = context
    window = provider_window(frame, path, "claude")
    try:
        labels = " ".join(c.GetLabel() for c in window.GetChildren()[0].GetChildren() if isinstance(c, wx.StaticText))
        assert frame.i18n.t("ai_kind_pdf") in labels and frame.i18n.t("ai_kind_audio") not in labels
    finally:
        window.Destroy()


def test_model_list_is_a_named_native_choice_and_selection_is_explicit(context, monkeypatch):
    from core.ai_media.model_catalog import ModelOption
    import ui.dialogs.ai_provider_models as module
    frame, path = context
    CredentialStore(path).set("openai", "synthetic-key")
    frame.output = lambda text: None
    window = provider_window(frame, path)
    try:
        assert isinstance(window.model_choice, wx.Choice)
        assert window.model_choice.GetName() == plain(frame, "ai_model_choice")
        assert not window.model_choice.IsEnabled()
        monkeypatch.setattr(module, "fetch_models", lambda *args: (
            ModelOption("gpt-4.1-mini", "GPT-4.1 Mini"), ModelOption("gpt-4.1", "GPT-4.1")))
        monkeypatch.setattr(module, "submit", lambda work, complete: complete(work(), None))
        monkeypatch.setattr(module.wx, "CallAfter", lambda f, *args: f(*args))
        window._fetch_models(None)
        assert window.model_choice.IsEnabled() and window.model_choice.GetCount() == 2
        assert window.model.GetValue() == "gpt-4.1-mini" and window.model_choice.GetSelection() == 0
        window.model_choice.SetSelection(1)
        window._select_model(SimpleNamespace(Skip=lambda: None))
        assert window.model.GetValue() == "gpt-4.1"
    finally:
        window.Destroy()


def test_optional_technical_help_has_native_readable_text_and_close_button(context, monkeypatch):
    frame, _ = context
    _, page = settings_page(frame)
    seen = []

    class HelpDialog(wx.Dialog):
        def ShowModal(self):
            text, close = self.GetChildren()
            assert isinstance(text, wx.TextCtrl)
            assert text.GetWindowStyleFlag() & wx.TE_READONLY
            assert text.GetWindowStyleFlag() & wx.TE_MULTILINE
            assert text.GetName() == plain(frame, "ai_technical_info")
            assert "store=false" in text.GetValue()
            assert close.GetId() == wx.ID_CANCEL
            seen.append(True)
            return wx.ID_CANCEL

    monkeypatch.setattr("ui.dialogs.ai_settings_page.wx.Dialog", HelpDialog)
    page._technical_info(None)
    assert seen == [True]


@pytest.mark.parametrize("hidden", [False, True])
def test_settings_page_is_appended_without_moving_connection_or_exposing_vault(context, hidden):
    from cryptography.fernet import Fernet
    from core.chat_lock_vault import ChatLockVault
    from ui.dialogs.settings_dialog import SettingsDialog
    from tests.test_settings_checkboxes_roundtrip import _make_frame
    _, path = context
    frame = _make_frame({"general": {"language": "en-US"}})
    frame._app_settings = AppSettings(str(path))
    frame._chat_lock_vault = ChatLockVault(Fernet.generate_key())
    frame._chat_lock_vault.configure("246810", "reveal-code")
    frame._chat_lock_vault.set_hide_navigation(hidden)
    frame._chat_lock_unlocked = False
    dialog = None
    try:
        dialog = SettingsDialog(frame)
        index = dialog._notebook.FindPage(dialog._ai_page)
        assert index == 7
        assert dialog._notebook.GetPage(dialog._notebook.GetPageCount() - 1) is dialog._conn_page
        assert dialog._chat_lock_tab_shown is (not hidden)
        assert dialog._notebook.GetPageText(index) == frame.i18n.t("tab_ai_accessibility")
    finally:
        destroy_now(dialog if dialog is not None else frame)
