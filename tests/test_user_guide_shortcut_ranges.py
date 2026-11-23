"""Keep translated shortcut ranges correct without changing other locales."""

import importlib.util
import json
from pathlib import Path


GUIDE_DIR = Path(__file__).resolve().parents[1] / "docs" / "user-guide"
_spec = importlib.util.spec_from_file_location("guide_shortcut_ranges", GUIDE_DIR / "generate_shortcuts.py")
_guide = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_guide)


def test_polish_account_range_uses_the_guide_translation():
    labels = json.loads((GUIDE_DIR / "i18n" / "pl.json").read_text(encoding="utf-8"))
    rows = [(f"Ctrl+Alt+{n}", f"Przełącz na konto ({n})") for n in range(1, 10)]
    assert _guide.juntar_numerados(rows, labels["number_range"]) == [
        ("Ctrl+Alt+1 do 9", "Przełącz na konto")
    ]


def test_unconfigured_locale_retains_the_existing_range_format():
    rows = [(f"Ctrl+{n}", f"Bookmark ({n})") for n in range(10)]
    assert _guide.juntar_numerados(rows) == [("Ctrl+0 a 9", "Bookmark")]


def test_nonconsecutive_digits_are_listed_without_implying_missing_shortcuts():
    rows = [("Ctrl+1", "Zakładka (1)"), ("Ctrl+3", "Zakładka (3)")]
    assert _guide.juntar_numerados(rows, "{first} do {last}") == [
        ("Ctrl+1, 3", "Zakładka")
    ]


def test_grouping_keeps_unrelated_rows_and_a_single_numbered_action():
    rows = [
        ("F1", "Pomoc"),
        ("Ctrl+1", "Zakładka (1)"),
        ("Esc", "Zamknij"),
        ("Ctrl+2", "Zakładka (2)"),
        ("Alt+7", "Panel (7)"),
    ]
    assert _guide.juntar_numerados(rows, "{first} do {last}") == [
        ("F1", "Pomoc"),
        ("Ctrl+1 do 2", "Zakładka"),
        ("Esc", "Zamknij"),
        ("Alt+7", "Panel (7)"),
    ]
