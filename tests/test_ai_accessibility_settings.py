"""AI transcription / description: settings, export, menu rules and wiring.

A plain pytest never opens a window (CLAUDE.md), so everything that needs wx is
checked from the source text; everything else is real logic.
"""

import copy
import json
import re
from pathlib import Path

import pytest

from core import ai_providers
from core import settings_transfer as transfer
from core.utils import DEFAULT_SETTINGS

ROOT = Path(__file__).resolve().parent.parent / "client"
LOCALES = sorted(
    p.stem for p in (ROOT / "languages").glob("*.json") if p.stem != "language_map"
)
IDS = [p.id for p in ai_providers.PROVIDERS]
KEYS = [f"{pid}_api_key" for pid in IDS]


def _read(*parts):
    return ROOT.joinpath(*parts).read_text(encoding="utf-8")


def _configured(**overrides):
    settings = {"enabled": True, "gemini_api_key": "AIza-test"}
    settings.update(overrides)
    return settings


class TestDefaults:
    def test_the_feature_starts_off(self):
        """It sends chat media to third parties: it must be a choice."""
        assert DEFAULT_SETTINGS["ai_accessibility"]["enabled"] is False

    def test_the_json_defaults_match_the_python_defaults(self):
        shipped = json.loads(_read("data", "settings_default.json"))
        assert shipped["ai_accessibility"] == DEFAULT_SETTINGS["ai_accessibility"]

    def test_the_default_order_is_the_fallback_order(self):
        assert DEFAULT_SETTINGS["ai_accessibility"]["provider_order"] == IDS

    def test_no_api_key_is_a_shipped_default(self):
        """A key declared in DEFAULT_SETTINGS would start being exported and
        imported (core/settings_transfer.py only moves declared keys)."""
        assert not set(KEYS) & set(DEFAULT_SETTINGS["ai_accessibility"])


class TestApiKeysNeverTravel:
    def _settings(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings["ai_accessibility"].update(
            {f"{pid}_api_key": f"secret-{pid}" for pid in IDS}
        )
        settings["ai_accessibility"]["enabled"] = True
        return settings

    @pytest.mark.parametrize("key", KEYS)
    def test_the_key_is_excluded(self, key):
        assert transfer.is_excluded("ai_accessibility", key)
        assert ("ai_accessibility", key) in transfer.EXCLUDED_KEYS

    def test_an_export_carries_no_key_and_keeps_the_preferences(self):
        exported = transfer.build_export(self._settings())
        assert "secret-" not in repr(exported)
        section = exported["settings"]["ai_accessibility"] if "settings" in exported \
            else exported["ai_accessibility"]
        assert section["enabled"] is True
        assert section["provider_order"] == IDS

    def test_an_import_never_overwrites_the_local_keys(self):
        current = self._settings()
        incoming = copy.deepcopy(DEFAULT_SETTINGS)
        incoming["ai_accessibility"]["gemini_api_key"] = "from-the-file"
        merged, _applied, _ignored = transfer.merge_settings(current, incoming)
        assert merged["ai_accessibility"]["gemini_api_key"] == "secret-gemini"


class TestMenuRules:
    @pytest.mark.parametrize(
        "msg_type, label_key",
        [
            ("audioMessage", "ai_transcribe_audio_menu"),
            ("imageMessage", "ai_describe_image_menu"),
            ("videoMessage", "ai_describe_video_menu"),
            ("stickerMessage", "ai_transcribe_sticker_menu"),
            ("documentMessage", "ai_pdf_accessible_menu"),
        ],
    )
    def test_each_kind_of_media_gets_its_label(self, msg_type, label_key):
        assert ai_providers.action_label_key(msg_type, _configured()) == label_key

    def test_other_message_types_get_nothing(self):
        for msg_type in ("conversation", "locationMessage", "contactMessage"):
            assert ai_providers.action_label_key(msg_type, _configured()) == ""

    def test_nothing_is_offered_while_the_feature_is_off(self):
        assert ai_providers.action_label_key(
            "audioMessage", _configured(enabled=False)
        ) == ""

    def test_nothing_is_offered_without_a_key(self):
        assert ai_providers.action_label_key(
            "audioMessage", {"enabled": True}
        ) == ""

    def test_a_blank_key_is_not_a_key(self):
        assert ai_providers.usable_settings(
            {"enabled": True, "gemini_api_key": "   "}
        ) is None

    @pytest.mark.parametrize(
        "msg_type, toggle",
        [
            ("audioMessage", "transcribe_audio"),
            ("imageMessage", "describe_images"),
            ("videoMessage", "describe_videos"),
            ("stickerMessage", "transcribe_stickers"),
            ("documentMessage", "pdf_to_accessible_text"),
        ],
    )
    def test_each_toggle_hides_only_its_own_kind(self, msg_type, toggle):
        settings = _configured(**{toggle: False})
        assert ai_providers.action_label_key(msg_type, settings) == ""
        others = [t for t in ai_providers._ACTION_BY_MESSAGE_TYPE if t != msg_type]
        assert all(ai_providers.action_label_key(t, settings) for t in others)

    def test_a_missing_or_broken_settings_value_is_just_off(self):
        assert ai_providers.usable_settings(None) is None
        assert ai_providers.usable_settings("nope") is None


class TestPdfOnly:
    def _doc(self, mimetype):
        return {"message": {"documentMessage": {"mimetype": mimetype}}}

    def test_a_pdf(self):
        assert ai_providers.is_pdf_message(self._doc("application/pdf"))

    def test_a_pdf_with_parameters_or_odd_case(self):
        assert ai_providers.is_pdf_message(self._doc("Application/PDF; charset=x"))

    @pytest.mark.parametrize(
        "mimetype",
        ["application/vnd.android.package-archive", "text/plain", "", None],
    )
    def test_anything_else_is_refused(self, mimetype):
        assert not ai_providers.is_pdf_message(self._doc(mimetype))

    def test_a_message_without_the_document_body(self):
        assert not ai_providers.is_pdf_message({})


class TestStringsInEveryLocale:
    NEEDED = (
        [
            "tab_ai_accessibility", "ai_accessibility_enabled_label",
            "ai_privacy_notice", "ai_privacy_notice_title",
            "ai_provider_button", "ai_provider_list_label",
            "ai_provider_state_enabled", "ai_provider_state_disabled",
            "ai_transcribe_audio_menu", "ai_describe_image_menu",
            "ai_describe_video_menu", "ai_transcribe_sticker_menu",
            "ai_pdf_accessible_menu", "ai_not_configured_msg", "ai_pdf_only_msg",
            "ai_result_copy_all_btn", "ai_result_ask_btn",
        ]
        + [
            f"{pid}_{suffix}"
            for pid in IDS
            for suffix in (
                "api_key_label", "api_key_help_label", "model_label", "model_help_label",
            )
        ]
    )

    @pytest.mark.parametrize("locale", LOCALES)
    def test_every_string_exists_and_is_not_empty(self, locale):
        strings = json.loads(_read("languages", f"{locale}.json"))
        missing = [k for k in self.NEEDED if not str(strings.get(k, "")).strip()]
        assert missing == []

    @pytest.mark.parametrize("locale", LOCALES)
    def test_the_provider_placeholder_survives_translation(self, locale):
        strings = json.loads(_read("languages", f"{locale}.json"))
        assert "{provider}" in strings["ai_provider_button"]

    def test_every_key_the_code_asks_for_is_declared(self):
        """A key typed in code but missing from the language files shows the
        raw key to the user (and to the screen reader)."""
        pt = json.loads(_read("languages", "pt-BR.json"))
        used = set()
        for path in (
            ("ui", "dialogs", "ai_settings_page.py"),
            ("ui", "conversation_panel", "ai_actions.py"),
            ("ui", "dialogs", "ai_result_dialog.py"),
        ):
            used |= set(re.findall(r'i18n\.t\(\s*(?:f)?"([a-z0-9_{}]+)"', _read(*path)))
        used = {
            k.replace("{provider_id}", pid) for k in used for pid in IDS
        } if any("{" in k for k in used) else used
        assert [k for k in sorted(used) if "{" not in k and k not in pt] == []


class TestSettingsPageWiring:
    SOURCE = _read("ui", "dialogs", "ai_settings_page.py")
    DIALOG = _read("ui", "dialogs", "settings_dialog.py")

    def test_the_page_lists_every_provider_in_fallback_order(self):
        block = self.SOURCE[self.SOURCE.index("AI_PROVIDER_UI = ("):]
        block = block[: block.index("\n)\n")]
        assert re.findall(r'\("(\w+)", "\w+", \w+_RECOMMENDED_MODELS\)', block) == IDS

    @pytest.mark.parametrize("spec", ai_providers.PROVIDERS, ids=IDS)
    def test_setting_names_follow_the_pattern_the_page_saves_under(self, spec):
        assert spec.key_setting == f"{spec.id}_api_key"
        assert spec.model_setting == f"{spec.id}_model"

    def test_the_page_saves_and_loads_by_that_same_pattern(self):
        assert 'values[f"{pid}_api_key"]' in self.SOURCE
        assert 'values[f"{pid}_model"]' in self.SOURCE
        assert 'ai_settings.get(f"{pid}_api_key")' in self.SOURCE
        assert 'ai_settings.get(f"{pid}_model")' in self.SOURCE

    def test_the_tab_sits_after_reactions_and_before_the_optional_locked_chats(self):
        """settings_dialog.py addresses tabs by number; the locked-chats tab is
        optional and must stay last (tests/test_settings_files_saving_tab.py)."""
        reactions = self.DIALOG.index('i18n.t("tab_reactions"))\n        self._fixed_quick')
        ai = self.DIALOG.index('i18n.t("tab_ai_accessibility"))')
        locked = self.DIALOG.index("# ── Locked chats tab")
        assert reactions < ai < locked
        assert 'SetPageText(14, i18n.t("tab_ai_accessibility"))' in self.DIALOG
        assert 'SetPageText(15, i18n.t("locked_chats"))' in self.DIALOG

    def test_the_dialog_only_delegates(self):
        """Feature logic stays in ai_settings_page.py; settings_dialog.py is a
        god file and only builds, loads, collects and relabels the page."""
        assert "AISettingsPage(" in self.DIALOG
        assert "self._ai_page.load(" in self.DIALOG
        assert "self._ai_page.collect()" in self.DIALOG
        assert "self._ai_page.refresh_labels(" in self.DIALOG
        assert "_AI_PROVIDER_UI" not in self.DIALOG

    def test_turning_the_feature_on_asks_first_and_defaults_to_no(self):
        assert "ai_privacy_notice" in self.SOURCE
        assert "wx.NO_DEFAULT" in self.SOURCE

    def test_the_provider_list_is_not_a_checklistbox(self):
        """NVDA does not reliably announce a CheckListBox item's state; the
        state is part of the item text instead."""
        assert "wx.CheckListBox" not in self.SOURCE

    def test_the_menu_is_wired_in_the_message_menu(self):
        assert "_ai_menu_label_for_type" in _read("ui", "conversation_panel", "message_menu.py")

    def test_the_mixin_is_part_of_the_panel(self):
        panel = _read("ui", "conversations.py")
        assert "AIActionsMixin" in panel


class TestDecryptedMediaDoesNotLinger:
    """The media cache is encrypted; the providers need a plain file. That file
    must not outlive the request (nothing readable left in %TEMP%)."""

    SOURCE = _read("ui", "conversation_panel", "ai_actions.py")

    def test_the_temporary_folder_is_removed(self):
        assert "shutil.rmtree(tmp_dir" in self.SOURCE

    def test_it_is_removed_both_after_the_dialog_and_on_failure(self):
        assert self.SOURCE.count("shutil.rmtree(tmp_dir") >= 2

    def test_the_senders_file_name_never_reaches_the_temporary_path(self):
        assert '"media" + os.path.splitext(file_name)[1]' in self.SOURCE

    def test_an_unexpected_error_logs_only_its_type(self):
        """An SDK message can carry the file name or a contact's number."""
        assert "type(exc).__name__" in self.SOURCE
        assert "logging.exception" not in self.SOURCE
