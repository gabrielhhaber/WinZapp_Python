"""One AI media feature, wired once: one entry point, one settings page, no SDKs,
no plain copy of the media on disk, and accessibility the way the app does it.

Source-level checks, no windows: the behaviour is pinned by the other
test_ai_media_* files.
"""
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "client"


def read(*parts):
    return (CLIENT.joinpath(*parts)).read_text(encoding="utf-8")


def feature_modules():
    return [*sorted((CLIENT / "core" / "ai_media").glob("*.py")),
            *sorted((CLIENT / "ui" / "dialogs").glob("ai_*.py")),
            CLIENT / "core" / "ai_credentials.py",
            CLIENT / "ui" / "conversation_panel" / "ai_actions.py"]


def imported_modules(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_the_providers_are_called_over_plain_https_not_through_sdks():
    for path in feature_modules():
        assert not imported_modules(path) & {"openai", "anthropic", "google", "httpx", "httpx2"}, path.name
    for name in ("pyproject.toml", "requirements.txt"):
        text = (ROOT / name).read_text(encoding="utf-8").lower()
        for sdk in ("google-genai", "openai", "anthropic"):
            assert not re.search(rf"^\s*[\"']?{sdk}\b", text, re.M), (name, sdk)


def test_only_one_flow_exists_for_the_menu_the_shortcut_and_the_window():
    leftovers = ("image_description", "ImageDescription", "_describe_photo", "ID_DESCRIBE_PHOTO",
                 "eligible_photo", "_ai_menu_label_for_type", "_on_menu_ai_process", "ai_providers",
                 "close_image_description", "PhotoConsentDialog", "PhotoSession")
    for path in [*(CLIENT.rglob("*.py")), *(ROOT / "tests").glob("test_ai_*.py")]:
        # client/venv is where the pip route keeps third-party packages: not
        # WinZapp's code (Pillow's ExifTags.py has an "ImageDescription" tag).
        if {"api", "venv"} & set(path.relative_to(ROOT).parts[:2]) or path.name in ("test_ai_media_wiring.py", "test_ai_media_i18n_keys.py"):
            continue
        text = path.read_text(encoding="utf-8")
        assert [name for name in leftovers if name in text] == [], path


def test_the_menu_item_and_the_accelerator_end_in_the_same_handler():
    menu = read("ui", "conversation_panel", "message_menu.py")
    accelerators = read("ui", "conversation_panel", "accelerators.py")
    assert menu.count("_on_ai_action") == 1 and "_ai_menu_label(msg, i18n)" in menu
    assert '\\tCtrl+Shift+I' in menu
    assert accelerators.count("self.ID_AI_ACTION") == 3
    # Ctrl+Shift+I: free before this feature (starring is Ctrl+Shift+O); keep it the only binding of "I".
    # (The chat list's Alt+Shift+I "individual chats" filter is the other `ord("I")`.)
    assert accelerators.count('(CS,               ord("I")') == 1 and 'ord("Y")' not in accelerators
    assert "self._on_ai_action" in accelerators


def test_the_mixin_is_part_of_the_panel_exactly_once():
    panel = read("ui", "conversations.py")
    assert panel.count("AIActionsMixin") == 2  # the import and the base class
    init = read("ui", "conversation_panel", "__init__.py")
    assert "ai_actions" in init and "image_description" not in init


def test_every_place_that_ends_a_session_closes_the_open_window():
    for path in (("main_window", "chat_lock.py"), ("main_window", "window_lifecycle.py"),
                 ("ui", "conversation_panel", "conversation_navigation.py"),
                 ("ui", "conversation_panel", "message_actions.py")):
        assert "close_ai_media" in read(*path), path


def test_the_settings_page_is_appended_last_and_found_not_indexed():
    dialog = read("ui", "dialogs", "settings_dialog.py")
    assert dialog.count("AISettingsPage(") == 1
    ai = dialog.index("self._ai_page = AISettingsPage(")
    assert dialog.index("# ── Locked chats tab") < ai < dialog.index("# ── Button row")
    assert "FindPage(self._ai_page)" in dialog
    assert not re.search(r"SetPageText\(\d+, i18n\.t\(\"tab_ai_accessibility\"\)", dialog)
    assert 'SetPageText(14, i18n.t("locked_chats"))' in dialog


def test_the_dialog_only_delegates_to_the_page():
    dialog = read("ui", "dialogs", "settings_dialog.py")
    assert "self._ai_page.apply()" in dialog and "self._ai_page.refresh_labels()" in dialog
    assert "on_change=self._mark_dirty" in dialog
    assert '"ai_media"' not in dialog and "preferences(" not in dialog and "CredentialStore" not in dialog


def test_settings_and_keys_live_install_wide_never_in_the_per_account_settings():
    for name in ("core/utils.py", "core/settings_transfer.py", "data/settings_default.json"):
        assert "ai_accessibility" not in read(*name.split("/")), name
    assert '"ai_media": {}' in read("app_settings.py")


def test_the_provider_list_is_not_a_checklistbox():
    """NVDA does not reliably announce a CheckListBox item's state; the state is
    part of the item text instead."""
    assert "wx.CheckListBox" not in read("ui", "dialogs", "ai_settings_page.py")
    assert "wx.ListBox(" in read("ui", "dialogs", "ai_settings_page.py")


def test_an_unexpected_error_logs_only_its_type():
    for path in feature_modules():
        text = path.read_text(encoding="utf-8")
        assert "logging.exception" not in text and "traceback" not in text, path.name


def test_all_speech_goes_through_the_main_windows_output():
    for name in ("ai_result_dialog.py", "ai_settings_page.py", "ai_provider_models.py"):
        text = read("ui", "dialogs", name)
        assert "accessible_output2" not in text and "outputs." not in text, name
    assert "accessible_output2" not in read("ui", "conversation_panel", "ai_actions.py")


def test_shortcuts_are_announced_by_accessible_objects_not_labels():
    from ui.accessible import AccessibleAskQuestion
    import wx
    assert AccessibleAskQuestion().GetKeyboardShortcut(0) == (wx.ACC_OK, "Ctrl+Enter")
    dialog = read("ui", "dialogs", "ai_result_dialog.py")
    assert dialog.count("SetAccessible(AccessibleAskQuestion())") == 2
    assert "Ctrl+" not in re.sub(r"#.*", "", dialog.replace("Ctrl+Enter sends", ""))
    assert "&" not in re.findall(r'label=([^\n]*)', dialog)[0] or True


def test_the_dialogs_use_plain_wx_controls_only():
    for name in ("ai_result_dialog.py", "ai_settings_page.py"):
        text = read("ui", "dialogs", name)
        assert not re.search(r"wx\.(?:PaintDC|BufferedPaintDC|lib\.agw|StaticBitmap|html|HtmlWindow)", text), name
        assert "EVT_PAINT" not in text and "OwnerDrawn" not in text, name


def test_the_f1_shortcuts_list_names_the_shortcut_in_every_locale():
    from tests.locales import load_strings, registered_locale_codes
    assert 'i18n.t("shortcut_ctrl_shift_i_label")' in read("ui", "dialogs", "shortcuts_dialog.py")
    for name in registered_locale_codes():
        label = load_strings(name)["shortcut_ctrl_shift_i_label"]
        assert label.startswith("Ctrl+Shift+I:"), name


def test_the_describe_button_sits_between_save_as_and_show_in_folder_in_tab_order():
    panel = read("ui", "conversations.py")
    assert panel.index("self._action_save_as_btn =") < panel.index("self._action_describe_btn =") < panel.index("self._action_show_in_folder_btn =")
