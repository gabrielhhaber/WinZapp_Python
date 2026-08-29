"""Which folder a Save As dialog opens on.

Only the dialog's starting folder — nothing here saves without asking. The
behaviour was argued over repeatedly without landing anywhere, because both
answers are right for different people, so it became a setting
(Configurações > Arquivos e salvamento) with "remember the last folder" as the
default. That default is a change: every save dialog used to open on Downloads
unconditionally.

core/save_location.py is deliberately free of wx and of MainWindow so the
resolution order can be tested on its own — the four call sites that use it
live in three different files.
"""

import os

import pytest

from core import save_location


@pytest.fixture
def downloads(tmp_path, monkeypatch):
    """Stand in for the real Windows Downloads folder."""
    folder = tmp_path / "Downloads"
    folder.mkdir()
    monkeypatch.setattr(save_location, "get_downloads_folder", lambda: str(folder))
    return folder


def _settings(**files):
    return {"files": files}


class TestModeIndexRoundTrip:
    """The radio button stores an index into MODES; both directions have to
    survive a settings file written by another version."""

    def test_every_mode_round_trips(self):
        for mode in save_location.MODES:
            assert save_location.mode_from_index(
                save_location.mode_index(mode)) == mode

    def test_an_unknown_mode_reads_as_the_default(self):
        assert save_location.mode_index("something-else") == \
            save_location.mode_index(save_location.DEFAULT_MODE)

    @pytest.mark.parametrize("index", [-1, 99, None, "x", 1.5])
    def test_an_impossible_index_reads_as_the_default(self, index):
        assert save_location.mode_from_index(index) == save_location.DEFAULT_MODE

    def test_the_default_is_remember_last(self):
        """Stated explicitly: this is the behaviour change, and reordering
        MODES would silently change what existing installs mean."""
        assert save_location.DEFAULT_MODE == save_location.MODE_REMEMBER_LAST
        assert save_location.MODES[0] == save_location.MODE_REMEMBER_LAST


class TestDownloadsMode:
    def test_it_returns_downloads(self, tmp_path, downloads):
        settings = _settings(
            save_dialog_folder_mode="downloads",
            save_dialog_last_folder=str(tmp_path),
        )
        assert save_location.resolve_save_dialog_folder(settings) == str(downloads)


class TestRememberLastMode:
    def test_the_remembered_folder_is_used(self, tmp_path, downloads):
        projeto = tmp_path / "projeto"
        projeto.mkdir()
        settings = _settings(
            save_dialog_folder_mode="last",
            save_dialog_last_folder=str(projeto),
        )
        assert save_location.resolve_save_dialog_folder(settings) == str(projeto)

    def test_the_first_save_of_a_fresh_install_falls_back_to_downloads(self, downloads):
        """Nothing has been saved yet. This is what makes the new default safe
        to ship without anyone opening the settings."""
        settings = _settings(save_dialog_folder_mode="last", save_dialog_last_folder="")
        assert save_location.resolve_save_dialog_folder(settings) == str(downloads)

    def test_a_remembered_folder_that_no_longer_exists_falls_back(self, tmp_path, downloads):
        """Deleted, renamed, or on a drive that is not plugged in. wx would
        pick somewhere of its own choosing for a missing defaultDir, which is
        worse than choosing deliberately."""
        settings = _settings(
            save_dialog_folder_mode="last",
            save_dialog_last_folder=str(tmp_path / "sumiu"),
        )
        assert save_location.resolve_save_dialog_folder(settings) == str(downloads)

    def test_a_file_path_is_not_a_folder(self, tmp_path, downloads):
        arquivo = tmp_path / "nao_e_pasta.txt"
        arquivo.write_text("x", encoding="utf-8")
        settings = _settings(
            save_dialog_folder_mode="last", save_dialog_last_folder=str(arquivo)
        )
        assert save_location.resolve_save_dialog_folder(settings) == str(downloads)


class TestCustomMode:
    def test_the_configured_folder_is_used(self, tmp_path, downloads):
        alvo = tmp_path / "Documentos" / "WinZapp"
        alvo.mkdir(parents=True)
        settings = _settings(
            save_dialog_folder_mode="custom", save_dialog_custom_folder=str(alvo)
        )
        assert save_location.resolve_save_dialog_folder(settings) == str(alvo)

    def test_a_folder_that_disappeared_falls_back_to_downloads(self, tmp_path, downloads):
        """The settings dialog refuses a non-existent folder, but it can stop
        existing afterwards — a removable drive is the ordinary case."""
        settings = _settings(
            save_dialog_folder_mode="custom",
            save_dialog_custom_folder=str(tmp_path / "unidade_removida"),
        )
        assert save_location.resolve_save_dialog_folder(settings) == str(downloads)

    def test_the_remembered_folder_is_ignored_in_this_mode(self, tmp_path, downloads):
        alvo = tmp_path / "fixa"
        alvo.mkdir()
        outra = tmp_path / "outra"
        outra.mkdir()
        settings = _settings(
            save_dialog_folder_mode="custom",
            save_dialog_custom_folder=str(alvo),
            save_dialog_last_folder=str(outra),
        )
        assert save_location.resolve_save_dialog_folder(settings) == str(alvo)


class TestMalformedSettings:
    """A resolver that raises leaves the user with no save dialog at all."""

    @pytest.mark.parametrize("settings", [
        {}, None, "nao e um dict", {"files": None}, {"files": "texto"},
        {"files": {}}, {"files": {"save_dialog_folder_mode": None}},
    ])
    def test_it_still_returns_downloads(self, settings, downloads):
        assert save_location.resolve_save_dialog_folder(settings) == str(downloads)

    def test_an_unrecognised_mode_behaves_like_the_default(self, tmp_path, downloads):
        """A settings file written by a newer build must not leave the dialog
        with nowhere to open."""
        projeto = tmp_path / "projeto"
        projeto.mkdir()
        settings = _settings(
            save_dialog_folder_mode="modo_do_futuro",
            save_dialog_last_folder=str(projeto),
        )
        assert save_location.resolve_save_dialog_folder(settings) == str(projeto)


class TestRememberingAFolder:
    def test_the_folder_of_the_saved_file_is_stored(self, tmp_path):
        settings = {}
        assert save_location.remember_save_dialog_folder(
            settings, str(tmp_path / "pasta" / "arquivo.pdf")) is True
        assert settings["files"]["save_dialog_last_folder"] == \
            str(tmp_path / "pasta")

    def test_saving_again_into_the_same_folder_changes_nothing(self, tmp_path):
        """Reported as a change so callers can skip a settings write — saving
        a run of files into one folder is the common case."""
        settings = {}
        path = str(tmp_path / "pasta" / "a.pdf")
        assert save_location.remember_save_dialog_folder(settings, path) is True
        assert save_location.remember_save_dialog_folder(
            settings, str(tmp_path / "pasta" / "b.pdf")) is False

    def test_it_is_recorded_whatever_the_active_mode_is(self, tmp_path):
        """Switching to "última pasta" later should not find it empty."""
        settings = _settings(save_dialog_folder_mode="downloads")
        assert save_location.remember_save_dialog_folder(
            settings, str(tmp_path / "x" / "a.pdf")) is True
        assert settings["files"]["save_dialog_last_folder"] == str(tmp_path / "x")

    def test_an_existing_section_is_not_replaced(self, tmp_path):
        settings = _settings(
            save_dialog_folder_mode="custom", save_dialog_custom_folder="C:/algo"
        )
        save_location.remember_save_dialog_folder(settings, str(tmp_path / "a.pdf"))
        assert settings["files"]["save_dialog_folder_mode"] == "custom"
        assert settings["files"]["save_dialog_custom_folder"] == "C:/algo"

    @pytest.mark.parametrize("bad", ["", None])
    def test_nothing_to_record_is_not_a_change(self, bad):
        settings = {}
        assert save_location.remember_save_dialog_folder(settings, bad) is False
        assert settings == {}

    def test_a_non_dict_settings_object_is_refused_quietly(self, tmp_path):
        assert save_location.remember_save_dialog_folder(
            None, str(tmp_path / "a.pdf")) is False


class TestTheDefaultsAreDeclared:
    """The section has to exist in both places a default can come from, or the
    tab loads a mode nobody chose."""

    def test_default_settings_carries_the_section(self):
        from core.utils import DEFAULT_SETTINGS
        section = DEFAULT_SETTINGS[save_location.SECTION]
        assert section[save_location.MODE_KEY] == save_location.DEFAULT_MODE
        assert section[save_location.CUSTOM_KEY] == ""
        assert section[save_location.LAST_KEY] == ""

    def test_the_shipped_json_agrees(self):
        """tests/test_settings_sync.py compares the two whole files; this names
        the section so a failure here says which one moved."""
        import json
        from app_paths import resource_path
        with open(resource_path("data", "settings_default.json"),
                  encoding="utf-8") as fh:
            shipped = json.load(fh)
        from core.utils import DEFAULT_SETTINGS
        assert shipped[save_location.SECTION] == \
            DEFAULT_SETTINGS[save_location.SECTION]
