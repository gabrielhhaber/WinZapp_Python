"""The uninstaller follows the Windows display language, like the installer.

Issue #352: uninstaller.c hardcoded Portuguese. Both stubs now pick their
language through the one helper in installer/lang.h (Portuguese -> pt-BR,
Spanish -> es-ES, anything else -> English) and keep their own string tables.
These are source-contract tests: the C is never executed here.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER_DIR = ROOT / "installer"


def _read(name):
    return (INSTALLER_DIR / name).read_text(encoding="utf-8")


def _strip_comments(src):
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"//[^\n]*", "", src)


def _table_body(src, name):
    m = re.search(r"static const \w+ " + name + r" = \{(.*?)\n\};", src, re.S)
    assert m, f"{name} table missing"
    return m.group(1)


def _table_fields(src, name):
    """One entry per field; adjacent literals (L"a" L"b") are one field."""
    body = _table_body(src, name)
    fields = []
    for chunk in re.split(r",\s*\n", body.strip().rstrip(",")):
        fields.append("".join(re.findall(r'L"((?:[^"\\]|\\.)*)"', chunk)))
    return fields


def _format_specs(text):
    return re.findall(r"%[-+ #0]*\d*(?:\.\d+)?[a-zA-Z]", text)


def test_helper_defaults_to_english_and_maps_only_pt_and_es():
    lang = _read("lang.h")
    assert "winzapp_ui_lang" in lang
    assert "GetUserDefaultUILanguage" in lang
    assert re.search(r"case LANG_PORTUGUESE:\s*return WINZAPP_LANG_PT", lang)
    assert re.search(r"case LANG_SPANISH:\s*return WINZAPP_LANG_ES", lang)
    assert re.search(r"default:\s*return WINZAPP_LANG_EN", lang)


@pytest.mark.parametrize("name", ["installer.c", "uninstaller.c"])
def test_both_stubs_share_the_language_helper(name):
    src = _strip_comments(_read(name))
    assert '#include "lang.h"' in src
    assert "winzapp_ui_lang()" in src
    # The rule lives in lang.h only; a second copy is how the two drifted.
    assert "GetUserDefaultUILanguage" not in src
    assert "LANG_PORTUGUESE" not in src
    # English is the pointer's initial value, so any path that skips
    # select_language() still shows English.
    assert re.search(r"g_str\s*=\s*&STR_EN;", src)
    assert "select_language();" in src


def test_uninstaller_has_three_tables_of_the_same_shape():
    src = _read("uninstaller.c")
    pt, es, en = (_table_fields(src, n) for n in ("STR_PT", "STR_ES", "STR_EN"))
    struct = re.search(r"typedef struct \{(.*?)\} UninstallStrings;", src, re.S)
    declared = len(re.findall(r"const wchar_t \*", struct.group(1)))
    assert len(pt) == len(es) == len(en) == declared
    assert all(pt) and all(es) and all(en)
    for p, e, n in zip(pt, es, en):
        assert _format_specs(p) == _format_specs(e) == _format_specs(n)


def test_installer_tables_still_agree():
    src = _read("installer.c")
    pt, es, en = (_table_fields(src, n) for n in ("STR_PT", "STR_ES", "STR_EN"))
    assert len(pt) == len(es) == len(en)
    for p, e, n in zip(pt, es, en):
        assert _format_specs(p) == _format_specs(e) == _format_specs(n)


def test_english_is_not_portuguese():
    en = _table_fields(_read("uninstaller.c"), "STR_EN")
    pt = _table_fields(_read("uninstaller.c"), "STR_PT")
    assert not set(en) & set(pt)
    assert not re.search(r"[^\x00-\x7f]", "".join(en))


def test_no_literal_user_text_outside_the_tables():
    src = _strip_comments(_read("uninstaller.c"))
    for name in ("STR_PT", "STR_ES", "STR_EN"):
        src = src.replace(_table_body(src, name), "")
    # Wide literals left are paths, registry names and file names, never
    # words a person reads: none may carry non-ASCII text or a space-separated
    # sentence.
    for lit in re.findall(r'L"((?:[^"\\]|\\.)*)"', src):
        assert not re.search(r"[^\x00-\x7f]", lit), lit
        assert not re.search(r"[A-Za-z]{3,} [A-Za-z]{3,}", lit), lit


def test_every_message_box_takes_text_and_title_from_the_table():
    src = _strip_comments(_read("uninstaller.c"))
    calls = re.findall(r"MessageBoxW\((.*?)\);", src, re.S)
    assert len(calls) == 2
    for call in calls:
        args = [a.strip() for a in call.split(",")]
        assert args[1].startswith("g_str->"), call
        assert args[2].startswith("g_str->"), call


def test_dialog_text_is_set_from_the_table():
    src = _strip_comments(_read("uninstaller.c"))
    for field in ("title", "prompt", "uninstall", "cancel"):
        assert f"g_str->{field}" in src


def test_resource_script_carries_no_portuguese_fallback():
    rc = (INSTALLER_DIR / "uninstaller.rc").read_text(encoding="utf-8")
    assert "Desinstalar" not in rc and "Tem certeza" not in rc
    assert "IDC_UNINSTALL_PROMPT" in rc


def test_batch_script_stays_free_of_localised_text():
    # The .bat is read back by cmd.exe in the OEM code page: translated text
    # (accents) must never be written into it.
    src = _read("uninstaller.c")
    m = re.search(r'int len = snprintf\(script.*?uninstall_a, uninstall_a, install_a\);',
                  src, re.S)
    assert m
    assert "g_str" not in m.group(0)
    assert not re.search(r"[^\x00-\x7f]", m.group(0))


@pytest.mark.skipif(shutil.which("gcc") is None, reason="gcc not installed")
@pytest.mark.parametrize("name", ["installer.c", "uninstaller.c"])
def test_stub_compiles_syntax_only(name):
    # Same flags build.py passes to gcc; -fsyntax-only never links or runs.
    result = subprocess.run(
        ["gcc", "-fsyntax-only", "-finput-charset=UTF-8",
         "-fwide-exec-charset=UTF-16LE", "-I", str(INSTALLER_DIR),
         str(INSTALLER_DIR / name)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
