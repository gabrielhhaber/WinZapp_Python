"""The AI media feature has one set of translation keys: none unused, none twice.

Two features were merged into this one (photo descriptions, and transcriptions
with descriptions). Nothing here may fall back to a key of either original:
every string is declared once, in every locale, and used by the code.
"""
import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "client"
from tests.locales import load_strings, registered_locale_codes

LOCALES = registered_locale_codes()

#: The modules that own this feature's text; any key in a string literal of
#: these files counts as used.
SOURCES = (
    *sorted((CLIENT / "core" / "ai_media").glob("*.py")),
    *sorted((CLIENT / "ui" / "dialogs").glob("ai_*.py")),
    CLIENT / "ui" / "conversation_panel" / "ai_actions.py",
    CLIENT / "ui" / "conversation_panel" / "message_menu.py",
    CLIENT / "ui" / "dialogs" / "shortcuts_dialog.py",
    CLIENT / "ui" / "ai_media_demo.py",
    CLIENT / "core" / "sound_system.py",
)
KEY = re.compile(r"^(?:ai_[a-z0-9_]*[a-z0-9]|tab_ai_[a-z0-9_]*[a-z0-9]|sound_event_ai_[a-z0-9_]*[a-z0-9])$")
#: Dialog and tab texts that reach the screen through a generic key, not an ai_ one.
OWNED = re.compile(r"^(?:ai_|tab_ai_|sound_event_ai_)")


def strings(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)}


def error_categories():
    """The ``ai_error_<category>`` keys: one per category a DescriptionError can carry."""
    found = set()
    for path in sorted((CLIENT / "core" / "ai_media").glob("*.py")) + sorted(
            (CLIENT / "ui").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "DescriptionError"
                    and node.args and isinstance(node.args[0], ast.Constant)):
                found.add(f"ai_error_{node.args[0].value}")
    return found


def used_keys():
    keys = set()
    for path in SOURCES:
        keys |= {s for s in strings(path) if KEY.match(s)}
    keys |= error_categories()
    # sound_system lists the event as "ai_processing"; its label is derived.
    if "ai_processing" in strings(CLIENT / "core" / "sound_system.py"):
        keys.add("sound_event_ai_processing")
    keys.discard("ai_processing")
    keys.discard("ai_media")  # the settings block and package name, not a string shown to anyone
    return keys


def locale(name):
    return load_strings(name)


@pytest.fixture(scope="module")
def used():
    return used_keys()


def test_the_scan_finds_the_feature(used):
    assert len(used) > 80 and "ai_consent" in used and "ai_error_media_size" in used


@pytest.mark.parametrize("name", LOCALES)
def test_every_key_the_code_uses_exists_and_is_not_blank(used, name):
    strings_ = locale(name)
    assert [k for k in sorted(used) if not str(strings_.get(k, "")).strip()] == []


def test_no_key_is_declared_without_being_used(used):
    declared = {k for k in locale("en-US") if OWNED.match(k)}
    assert sorted(declared - used) == []


def test_the_keys_of_the_two_original_features_are_gone():
    """Names that existed only in one of the merged pull requests; a second
    copy of a string comes back under one of these."""
    gone = {
        "ai_title", "ai_enabled", "ai_provider", "ai_status", "ai_describe_photo", "ai_describe_prompt",
        "ai_description_loading", "ai_send_photo", "ai_error_image_size", "ai_error_image_format",
        "ai_privacy_notice", "ai_privacy_notice_title", "ai_not_configured_msg", "ai_pdf_only_msg",
        "ai_media_download_error_msg", "ai_unexpected_error_msg", "ai_still_processing_msg",
        "ai_result_ask_btn", "ai_result_copy_all_btn", "ai_result_ask_label", "ai_result_blocks_label",
        "ai_result_sticker_title", "ai_transcribe_sticker_menu", "ai_transcribe_stickers_label",
        "sound_event_photo_describing",
        *(f"{p}_{s}" for p in ("gemini", "openai", "claude", "groq", "openrouter")
          for s in ("api_key_label", "api_key_help_label", "model_label", "model_help_label")),
    }
    for name in LOCALES:
        assert sorted(gone & set(locale(name))) == [], name


def test_no_two_keys_say_the_same_thing():
    texts = {}
    for key, value in locale("en-US").items():
        if OWNED.match(key):
            texts.setdefault(value.strip().lower().rstrip(".:…"), []).append(key)
    same = [sorted(keys) for keys in texts.values() if len(keys) > 1]
    # Two media kinds keep their own menu entry so each can be reworded alone.
    assert same == [["ai_describe_image_menu", "ai_describe_video_menu"]]


#: The controls that carry a mnemonic, grouped by the window they live in; a
#: letter may appear once per window. Every other ai_ label (the result window,
#: menus) carries none, and no label writes a shortcut such as Ctrl+Enter.
CONSENT_WINDOW = ("ai_remember_consent", "ai_send_media")
SETTINGS_PAGE = (
    "ai_accessibility_enabled_label", "ai_provider_list_label", "ai_provider_configure_button",
    "ai_provider_move_up_button", "ai_provider_move_down_button", "ai_kinds_button",
    "ai_profile", "ai_read_answers", "ai_hide_action_button", "ai_auto_button", "ai_settings_help",
    "ai_technical_info", "translation_settings", "ai_reset_keys", "ai_status_label",
)
#: The window of the kinds of media, opened from the page (ai_kinds_options.py).
KINDS_WINDOW = (
    "ai_describe_images_label", "ai_describe_stickers_label", "ai_describe_videos_label",
    "ai_transcribe_audio_label", "ai_pdf_accessible_label",
)
#: The window of the automatic options, opened from the page (ai_auto_options.py).
AUTO_WINDOW = (
    "ai_auto_transcribe", "ai_auto_describe", "ai_auto_scope", "ai_auto_max_age",
    "ai_auto_daily_limit", "ai_auto_used_today",
)
PROVIDER_WINDOW = (
    "ai_provider_enabled_checkbox", "ai_api_key", "ai_key_readable", "ai_show_key", "ai_delete_key",
    "ai_get_key", "ai_get_models", "ai_voice_model", "ai_model_automatic", "ai_model_choice", "ai_model",
    "ai_test_connection", "ai_billing", "ai_privacy_link", "ai_status_label",
)
MNEMONIC = re.compile(r"&([^&])")


def mnemonic(text):
    found = MNEMONIC.findall(text.replace("&&", ""))
    return found[0].lower() if len(found) == 1 else None


@pytest.mark.parametrize("name", LOCALES)
def test_only_the_keyboard_reachable_windows_carry_mnemonics_and_never_a_shortcut(name):
    """Shortcuts are announced by ui/accessible.py objects. Mnemonics belong to
    the consent window, the settings page, its automatic options and kinds of
    media windows and the provider window only; the
    result window and the menus stay free of them."""
    allowed = {*CONSENT_WINDOW, *SETTINGS_PAGE, *PROVIDER_WINDOW, *AUTO_WINDOW, *KINDS_WINDOW}
    for key, value in locale(name).items():
        if OWNED.match(key) and key != "ai_demo_notice":
            if key not in allowed:
                assert "&" not in value.replace("&&", ""), key
            assert not re.search(r"\b(?:Ctrl|Alt|Shift)\+", value), key


@pytest.mark.parametrize("name", LOCALES)
@pytest.mark.parametrize("keys,fixed", [
    (CONSENT_WINDOW, ("cancel",)),
    (SETTINGS_PAGE, ("ok", "apply", "cancel", "settings_search_label")),
    (AUTO_WINDOW, ("ok",)),
    (KINDS_WINDOW, ("ok",)),
    (PROVIDER_WINDOW, ("ok", "cancel")),
])
def test_every_control_of_a_window_has_its_own_mnemonic_letter(name, keys, fixed):
    """The Alt shortcuts of one window must not collide with each other or with
    the OK / Apply / Cancel buttons that share it."""
    strings_ = locale(name)
    letters = {key: mnemonic(strings_[key]) for key in keys}
    assert all(letters.values()), [key for key, letter in letters.items() if not letter]
    # The shared OK / Apply / Cancel letters may already coincide in a locale
    # (ro: Apply and Cancel); that is not this feature's to fix.
    taken = {mnemonic(strings_[key]) for key in fixed}
    everything = list(letters.values())
    assert len(everything) == len(set(everything)), letters
    assert not taken & set(everything), (taken & set(everything), letters)


def test_the_menu_and_window_titles_are_declared_keys(used):
    from core.ai_media import config
    assert set(config.MENU_KEY.values()) <= used and set(config.TITLE_KEY.values()) <= used


@pytest.mark.parametrize("name", LOCALES)
def test_the_provider_and_kind_names_fill_their_placeholders(name):
    strings_ = locale(name)
    assert strings_["ai_provider_button"].format(provider="X")
    assert "X" in strings_["ai_consent"].format(providers="X")
    assert "X" in strings_["ai_provider_supports"].format(kinds="X")
    assert "X" in strings_["ai_answered_by"].format(provider="X")
    assert strings_["ai_attempt_line"].format(provider="A", reason="B") == "A: B"
