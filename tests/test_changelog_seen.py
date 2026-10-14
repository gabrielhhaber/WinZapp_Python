"""Help > "What's new since you last looked": the install-wide snapshot and
the window's text."""

import json

from core import changelog_seen
from main_window import whats_new_since
from main_window.whats_new_since import WhatsNewSinceMixin

OLD = "V1.0.0.0\n\nFirst.\n\nFIXES\n\n- One.\n"
NEW = "V1.0.0.1\n\nSecond.\n\nFIXES\n\n- Two.\n\nV1.0.0.0\n\nFirst.\n\nFIXES\n\n- One.\n"


def t(key):
    return {
        "whats_new_since_first": "First look; the notes of {version}:",
        "whats_new_since_nothing": "Nothing changed since {date}.",
        "whats_new_since_header": "Changed since {date}:",
        "changelog_diff_new_version": "New version {version}, in full:",
        "changelog_diff_version": "Version {version}",
    }.get(key, key)


class TestTheSnapshot:
    def test_it_round_trips_every_language(self, tmp_path):
        path = str(tmp_path / "global" / changelog_seen.FILE_NAME)
        assert changelog_seen.write(path, {"pt-BR": "a", "en-US": "b"}, "2.1.0.0", now=50.0)
        snapshot = changelog_seen.read(path)
        assert snapshot == {"texts": {"pt-BR": "a", "en-US": "b"}, "seen_at": 50.0,
                            "version": "2.1.0.0"}
        assert changelog_seen.previous_text(snapshot, "en-US") == "b"
        assert changelog_seen.previous_text(snapshot, "pl") is None
        assert not [p for p in (tmp_path / "global").iterdir() if p.name != changelog_seen.FILE_NAME]

    def test_a_missing_or_damaged_file_is_a_first_look(self, tmp_path):
        path = tmp_path / changelog_seen.FILE_NAME
        assert changelog_seen.read(str(path)) is None
        path.write_text("{not json", encoding="utf-8")
        assert changelog_seen.read(str(path)) is None
        path.write_text(json.dumps({"texts": "x"}), encoding="utf-8")
        assert changelog_seen.read(str(path)) is None


class TestTheText:
    def test_a_first_look_shows_the_version_in_use(self):
        text = changelog_seen.text_for(None, NEW, "", "1.0.0.1", t)
        assert text.startswith("First look; the notes of 1.0.0.1:")
        assert "Second." in text and "- Two." in text and "First." not in text

    def test_nothing_new_says_so_with_the_date(self):
        assert changelog_seen.text_for(NEW, NEW, "13/10/2026", "1", t) == "Nothing changed since 13/10/2026."

    def test_changes_come_under_the_date(self):
        text = changelog_seen.text_for(OLD, NEW, "13/10/2026", "1", t)
        assert text.startswith("Changed since 13/10/2026:\n\nNew version 1.0.0.1, in full:")


class _I18n:
    language = "pt-BR"

    @staticmethod
    def t(key):
        return t(key)


class _Dialog:
    shown = []

    def __init__(self, parent, text, title=""):
        _Dialog.shown.append((title, text))

    def ShowModal(self):
        return 0

    def Destroy(self):
        pass


class _Window(WhatsNewSinceMixin):
    def __init__(self):
        self.i18n = _I18n()


def test_the_menu_item_shows_then_remembers_for_every_account(monkeypatch, tmp_path):
    import updater
    _Dialog.shown = []
    current = {"text": OLD}
    monkeypatch.setattr(updater, "WhatsNewDialog", _Dialog)
    monkeypatch.setattr(updater, "load_changelog_text", lambda language: current["text"])
    monkeypatch.setattr(updater, "_read_changelog_file", lambda language: current["text"])
    monkeypatch.setattr(whats_new_since, "global_dir", lambda *parts: str(tmp_path.joinpath(*parts)))
    monkeypatch.setattr(WhatsNewSinceMixin, "_shipped_changelogs",
                        staticmethod(lambda: {"pt-BR": current["text"], "en-US": "english"}))
    monkeypatch.setattr(whats_new_since, "get_datetime_format", lambda fallback: "%d/%m/%Y")

    _Window()._on_whats_new_since()
    current["text"] = NEW
    _Window()._on_whats_new_since()
    _Window()._on_whats_new_since()

    first, changed, nothing = (text for _title, text in _Dialog.shown)
    assert first.startswith("First look")
    assert changed.startswith("Changed since ") and "New version 1.0.0.1" in changed
    assert nothing.startswith("Nothing changed since ")
    assert {title for title, _text in _Dialog.shown} == {"whats_new_since_title"}
    snapshot = changelog_seen.read(str(tmp_path / changelog_seen.FILE_NAME))
    assert snapshot["texts"] == {"pt-BR": NEW, "en-US": "english"}


def test_the_help_menu_offers_it():
    import pathlib
    source = (pathlib.Path(__file__).resolve().parents[1]
              / "client/main_window/window_chrome.py").read_text(encoding="utf-8")
    assert 'help_menu.Append(self._ID_WHATS_NEW_SINCE, self.i18n.t("menu_whats_new_since"))' in source
    assert "self._on_whats_new_since, id=self._ID_WHATS_NEW_SINCE)" in source
    assert "FindItemById(self._ID_WHATS_NEW_SINCE).SetItemLabel(" in source
