"""The usage guide must keep up with the changelog.

Every commit on main becomes an alpha, and the alpha workflow runs the whole
suite first, so this test is what makes updating the guide a prerequisite of
a complete alpha. How to fix a failure: docs/user-guide/PROCESS.md.
"""

import shutil
import sys
from pathlib import Path

import pytest

from tests.locales import registered_locale_codes

GUIDE_DIR = Path(__file__).resolve().parents[1] / "docs" / "user-guide"
REPO = GUIDE_DIR.parents[1]

sys.path.insert(0, str(GUIDE_DIR))
import verify_guide  # noqa: E402


@pytest.fixture(autouse=True)
def _quiet(capsys):
    yield
    capsys.readouterr()


def test_every_changelog_item_of_the_current_version_is_in_the_guide():
    missing = verify_guide.itens_sem_registro(REPO)
    assert not missing, (
        "The changelog has items the usage guide does not cover yet. Describe "
        "each one in docs/user-guide/sections/<every language>/, then run "
        "`python docs/user-guide/verify_guide.py . --registrar` and "
        "`python docs/user-guide/generate_guide.py .`:\n  - " + "\n  - ".join(missing)
    )


def test_the_shipped_guides_are_what_the_generator_produces():
    stale = verify_guide.guias_desatualizados(REPO)
    assert not stale, (
        f"client/data/help_guide is out of date for {stale}. "
        "Run `python docs/user-guide/generate_guide.py .` and commit the result."
    )


def test_all_languages_have_the_same_sections():
    assert verify_guide.divergencias_entre_idiomas() == []


def test_every_registered_language_goes_through_the_guide_checks():
    assert set(verify_guide.generate_guide.IDIOMAS) == set(registered_locale_codes())


def test_a_subtitle_missing_from_the_arabic_guide_is_reported(tmp_path, monkeypatch):
    for locale in ("pt-BR", "ar"):
        shutil.copytree(GUIDE_DIR / "sections" / locale, tmp_path / "sections" / locale)
    section = tmp_path / "sections" / "ar" / "13-accounts.html"
    section.write_text(
        section.read_text(encoding="utf-8").replace("<h3>", "<h4>", 1), encoding="utf-8"
    )
    monkeypatch.setattr(verify_guide, "AQUI", tmp_path)
    monkeypatch.setattr(verify_guide.generate_guide, "IDIOMAS", ["pt-BR", "ar"])

    assert any(
        found.startswith("ar/13-accounts.html")
        for found in verify_guide.divergencias_entre_idiomas()
    )
