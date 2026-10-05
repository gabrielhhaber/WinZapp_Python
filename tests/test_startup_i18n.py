"""Tests for client/startup_i18n.py — translations for the messages shown
before MainWindow exists, in the install-wide language from global/app.json.

Before: those messages were hardcoded — Polish for an invalid --account and
for "no accounts to start", Portuguese for the settings.json-unreadable box
and the startup-crash fallback — whatever language the user had chosen.
"""

import json

from startup_i18n import DEFAULT_STARTUP_LANGUAGE, resolve_startup_language, startup_i18n as build


def _write_language(global_dir, language):
    (global_dir / "app.json").write_text(json.dumps({"language": language}), encoding="utf-8")


def test_registered_language_is_kept():
    assert resolve_startup_language("pl") == "pl"


def test_unset_or_unknown_language_falls_back_to_english():
    assert DEFAULT_STARTUP_LANGUAGE == "en-US"
    assert resolve_startup_language("") == "en-US"
    assert resolve_startup_language(None) == "en-US"
    assert resolve_startup_language("xx-XX") == "en-US"


def test_translates_in_the_global_language(tmp_path):
    _write_language(tmp_path, "pt-BR")

    i18n = build(str(tmp_path))

    assert i18n.language == "pt-BR"
    assert i18n.t("startup_critical_title") == "WinZapp — Erro de inicialização"


def test_first_run_without_app_json_uses_english(tmp_path):
    i18n = build(str(tmp_path))

    assert i18n.t("startup_critical_title") == "WinZapp — Startup error"


def test_unreadable_global_settings_never_raise(tmp_path, monkeypatch):
    def _boom(*_a, **_k):
        raise OSError("disk gone")

    monkeypatch.setattr("app_settings.AppSettings.get", _boom)

    i18n = build(str(tmp_path))

    assert i18n.language == "en-US"


def test_invalid_account_message_carries_the_account_id(tmp_path):
    _write_language(tmp_path, "en-US")

    text = build(str(tmp_path)).t("startup_invalid_account").format(account="acc-123")

    assert "acc-123" in text
