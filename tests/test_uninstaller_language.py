"""The installer and uninstaller speak every language of the app.

Issue #352: uninstaller.c hardcoded Portuguese. Both stubs now pick their
language through the one helper in installer/lang.h and keep their own string
tables, one per language in client/languages/language_map.json (pt-BR, pt-PT,
es-ES, pl, ro, tr-TR, en-US). The language follows the Windows display
language; en-US is the fallback for any other.

Adding an 8th app language to language_map.json fails
test_stub_locales_match_the_apps_languages until lang.h, installer.c and
uninstaller.c each get the new language (a LANG_* case, an enumerator and a
table) -- that is the point of the test.

The table tests are source-contract tests (the stubs are never executed here;
the installer opens a window). The LANGID mapping is real: gcc builds the
console harness tests/c/lang_cli.c around the pure function in lang.h.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER_DIR = ROOT / "installer"
LANG_CLI_SOURCE = ROOT / "tests" / "c" / "lang_cli.c"

# Table-name suffix (STR_<suffix>) -> locale code in language_map.json.
SUFFIX_TO_LOCALE = {
    "PT_BR": "pt-BR",
    "PT_PT": "pt-PT",
    "ES": "es-ES",
    "PL": "pl",
    "RO": "ro",
    "TR": "tr-TR",
    "EN": "en-US",
}
NON_ENGLISH = [s for s in SUFFIX_TO_LOCALE if s != "EN"]

# Fields whose text may legitimately equal the English one. None today: every
# translated word differs from English, brand name included in a sentence.
SAME_AS_ENGLISH_OK = {"installer.c": set(), "uninstaller.c": set()}

INSTALLER_FIELDS = [
    "title", "path_label", "browse", "desktop_sc", "startmenu_sc", "install",
    "cancel", "browse_title", "err_no_folder", "extract_failed", "done_msg",
    "done_title", "err_fmt", "err_title",
]
UNINSTALLER_FIELDS = [
    "title", "prompt", "uninstall", "cancel", "not_found", "done_msg",
    "done_title",
]


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


def _tables(name):
    src = _read(name)
    return {s: _table_fields(src, "STR_" + s) for s in SUFFIX_TO_LOCALE}


def _format_specs(text):
    return sorted(re.findall(r"%[-+ #0]*\d*(?:\.\d+)?[a-zA-Z]", text))


def _declared_fields(name, struct):
    m = re.search(r"typedef struct \{(.*?)\} " + struct + ";", _read(name), re.S)
    return re.findall(r"const wchar_t \*(\w+);", m.group(1))


def _field(name, struct, suffix, field):
    declared = _declared_fields(name, struct)
    return _tables(name)[suffix][declared.index(field)]


# ── the helper ───────────────────────────────────────────────────────────────

def test_helper_has_a_pure_function_and_a_thin_wrapper():
    lang = _strip_comments(_read("lang.h"))
    assert "winzapp_lang_from_langid(LANGID" in lang
    assert "winzapp_ui_lang(void)" in lang
    pure = re.search(r"winzapp_lang_from_langid\(LANGID id\)\s*\{(.*?)\n\}", lang, re.S)
    assert pure and "GetUserDefaultUILanguage" not in pure.group(1)
    wrapper = re.search(r"winzapp_ui_lang\(void\)\s*\{(.*?)\n\}", lang, re.S)
    assert wrapper.group(1).strip() == \
        "return winzapp_lang_from_langid(GetUserDefaultUILanguage());"


def test_helper_maps_each_language_and_defaults_to_english():
    lang = _strip_comments(_read("lang.h"))
    assert re.search(r"case LANG_SPANISH:\s*return WINZAPP_LANG_ES", lang)
    assert re.search(r"case LANG_POLISH:\s*return WINZAPP_LANG_PL", lang)
    assert re.search(r"case LANG_ROMANIAN:\s*return WINZAPP_LANG_RO", lang)
    assert re.search(r"case LANG_TURKISH:\s*return WINZAPP_LANG_TR", lang)
    assert "SUBLANG_PORTUGUESE" in lang
    assert re.search(r"default:\s*return WINZAPP_LANG_EN", lang)


@pytest.fixture(scope="module")
def lang_cli(tmp_path_factory):
    if shutil.which("gcc") is None:
        pytest.skip("gcc not installed")
    exe = tmp_path_factory.mktemp("langcli") / "lang_cli.exe"
    result = subprocess.run(
        ["gcc", "-Wall", "-mconsole", "-I", str(INSTALLER_DIR),
         str(LANG_CLI_SOURCE), "-o", str(exe)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return exe


def _makelangid(primary, sub):
    return (sub << 10) | primary


# (LANGID, expected locale). Primary ids: pt 0x16, es 0x0A, pl 0x15,
# ro 0x18, tr 0x1F, en 0x09, ja 0x11; Portuguese sublangs: 1 Brazil, 2 Portugal.
LANGID_CASES = [
    (0x0416, "pt-BR"),
    (0x0816, "pt-PT"),
    (_makelangid(0x16, 1), "pt-BR"),
    (_makelangid(0x16, 2), "pt-PT"),
    (_makelangid(0x16, 0), "pt-BR"),   # neutral Portuguese
    (_makelangid(0x16, 3), "pt-BR"),   # any other Portuguese sublanguage
    (0x0C0A, "es-ES"),                 # Spanish, modern sort
    (0x040A, "es-ES"),                 # Spanish, traditional sort
    (0x2C0A, "es-ES"),                 # Spanish, Argentina
    (0x080A, "es-ES"),                 # Spanish, Mexico
    (0x0415, "pl"),
    (0x0418, "ro"),
    (0x0818, "ro"),                    # Romanian, Moldova
    (0x041F, "tr-TR"),
    (0x0409, "en-US"),
    (0x0809, "en-US"),                 # English, UK
    (0x0411, "en-US"),                 # Japanese -> default
    (0x0407, "en-US"),                 # German -> default
    (0x0000, "en-US"),
]


def test_langid_mapping_through_the_real_helper(lang_cli):
    args = [format(i, "x") for i, _ in LANGID_CASES]
    out = subprocess.run([str(lang_cli), *args], capture_output=True,
                         text=True, timeout=60).stdout.split()
    assert out == [exp for _, exp in LANGID_CASES]


# ── both stubs ───────────────────────────────────────────────────────────────

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


@pytest.mark.parametrize("name", ["installer.c", "uninstaller.c"])
def test_select_language_covers_every_enumerator(name):
    src = _strip_comments(_read(name))
    body = re.search(r"static void select_language\(void\)\s*\{(.*?)\n\}", src, re.S).group(1)
    for suffix in NON_ENGLISH:
        assert re.search(
            rf"case WINZAPP_LANG_{suffix}:\s*g_str = &STR_{suffix};", body), suffix
    assert "default:" in body


@pytest.mark.parametrize("name", ["installer.c", "uninstaller.c"])
def test_stub_locales_match_the_apps_languages(name):
    """Adding an app language fails here until the installer follows."""
    app = set(json.loads((ROOT / "client" / "languages" / "language_map.json")
                         .read_text(encoding="utf-8")))
    in_stub = {SUFFIX_TO_LOCALE[s]
               for s in re.findall(r"static const \w+ STR_(\w+) = \{", _read(name))}
    assert in_stub == app
    # lang.h has one enumerator per language too.
    enum = set(re.findall(r"WINZAPP_LANG_(\w+)\s*[,=\n}]", _read("lang.h").split("typedef enum")[1]
                          .split("} WinzappLang")[0]))
    assert {SUFFIX_TO_LOCALE[s] for s in enum} == app


@pytest.mark.parametrize("name,struct,fields", [
    ("installer.c", "UiStrings", INSTALLER_FIELDS),
    ("uninstaller.c", "UninstallStrings", UNINSTALLER_FIELDS),
])
def test_tables_have_the_declared_shape_and_the_same_format_specifiers(name, struct, fields):
    assert _declared_fields(name, struct) == fields
    tables = _tables(name)
    for suffix, table in tables.items():
        assert len(table) == len(fields), suffix
        assert all(table), f"{suffix} has an empty entry"
    for i, field in enumerate(fields):
        en = _format_specs(tables["EN"][i])
        for suffix in NON_ENGLISH:
            assert _format_specs(tables[suffix][i]) == en, (suffix, field)


@pytest.mark.parametrize("name,fields", [
    ("installer.c", INSTALLER_FIELDS),
    ("uninstaller.c", UNINSTALLER_FIELDS),
])
def test_translations_are_not_the_english_text(name, fields):
    tables = _tables(name)
    for suffix in NON_ENGLISH:
        for i, field in enumerate(fields):
            if field in SAME_AS_ENGLISH_OK[name]:
                continue
            assert tables[suffix][i] != tables["EN"][i], (suffix, field)
    assert not re.search(r"[^\x00-\x7f]", "".join(tables["EN"]))


@pytest.mark.parametrize("name", ["installer.c", "uninstaller.c"])
def test_non_english_tables_use_their_own_script(name):
    tables = _tables(name)
    for suffix in ("PL", "RO", "TR"):
        assert re.search(r"[^\x00-\x7f]", "".join(tables[suffix])), suffix


@pytest.mark.parametrize("name", ["installer.c", "uninstaller.c"])
def test_romanian_uses_comma_below_like_ro_json(name):
    # ro.json writes s/t with comma below (U+0219/U+021B); the cedilla forms
    # (U+015F/U+0163, also U+015E/U+0162) render but are the wrong letters.
    ro_json = (ROOT / "client" / "languages" / "ro.json").read_text(encoding="utf-8")
    assert not re.search("[ŞşŢţ]", ro_json)
    ro = "".join(_tables(name)["RO"])
    assert not re.search("[ŞşŢţ]", ro)
    assert re.search("[ȘșȚț]", ro)


def test_pt_pt_differs_from_pt_br_where_the_languages_differ():
    for name, struct, fields in (("installer.c", "UiStrings", INSTALLER_FIELDS),
                                 ("uninstaller.c", "UninstallStrings", UNINSTALLER_FIELDS)):
        tables = _tables(name)
        differing = [f for i, f in enumerate(fields)
                     if tables["PT_PT"][i] != tables["PT_BR"][i]]
        assert differing, name
    # The words a Portuguese reader would trip over.
    assert "ambiente de trabalho" in _field("installer.c", "UiStrings", "PT_PT", "desktop_sc")
    assert "área de trabalho" in _field("installer.c", "UiStrings", "PT_BR", "desktop_sc")
    assert _field("installer.c", "UiStrings", "PT_PT", "extract_failed") != \
        _field("installer.c", "UiStrings", "PT_BR", "extract_failed")
    for field in ("prompt", "not_found", "done_msg"):
        assert _field("uninstaller.c", "UninstallStrings", "PT_PT", field) != \
            _field("uninstaller.c", "UninstallStrings", "PT_BR", field)
    pt_pt = "".join("".join(t) for t in (_tables("installer.c")["PT_PT"],
                                         _tables("uninstaller.c")["PT_PT"]))
    assert not re.search(r"\b(tela|arquivo|diretório|você)\b", pt_pt)


def test_uninstaller_keeps_the_original_pt_es_en_wording():
    # The pre-existing three tables are unchanged: PT became pt-BR as it was.
    pt = _tables("uninstaller.c")["PT_BR"]
    assert pt[1] == "Tem certeza que deseja desinstalar o WinZapp?"
    assert _tables("uninstaller.c")["ES"][1] == "¿Está seguro de que desea desinstalar WinZapp?"
    assert _tables("uninstaller.c")["EN"][1] == "Are you sure you want to uninstall WinZapp?"


def test_no_literal_user_text_outside_the_tables():
    src = _strip_comments(_read("uninstaller.c"))
    for suffix in SUFFIX_TO_LOCALE:
        src = src.replace(_table_body(src, "STR_" + suffix), "")
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


@pytest.mark.parametrize("rc", ["installer.rc", "uninstaller.rc"])
def test_resource_scripts_carry_only_english_fallback_text(rc):
    # The stubs overwrite every caption at WM_INITDIALOG; whatever the .rc
    # holds is only what shows if that ever fails, so it is the default
    # language, never a translation.
    text = _read(rc)
    assert not re.search(r"[^\x00-\x7f]", text)
    for word in ("Desinstalar", "Tem certeza", "Instalador", "Procurar", "Cancelar"):
        assert word not in text
    assert "IDC_UNINSTALL_PROMPT" in _read("uninstaller.rc")


def _rc_control_size(rc_text, ident):
    m = re.search(ident + r",\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+)", rc_text)
    assert m, ident
    return tuple(int(g) for g in m.groups())


# Rough Segoe UI 9pt: one dialog unit is 1.5 px, an average letter ~6 px.
_PX_PER_CHAR = 6.0
_PX_PER_DU = 1.5


def test_longest_strings_fit_their_controls():
    inst_rc = _read("installer.rc")
    for ident, field in (("IDC_DESKTOP_SC", "desktop_sc"),
                         ("IDC_STARTMENU_SC", "startmenu_sc")):
        _, _, w, _ = _rc_control_size(inst_rc, ident)
        room = w * _PX_PER_DU - 18          # minus the check box glyph
        for suffix in SUFFIX_TO_LOCALE:
            text = _field("installer.c", "UiStrings", suffix, field)
            assert len(text) * _PX_PER_CHAR <= room, (suffix, field, len(text))
    _, _, w, _ = _rc_control_size(inst_rc, "IDC_PATH_LABEL")
    for suffix in SUFFIX_TO_LOCALE:
        text = _field("installer.c", "UiStrings", suffix, "path_label")
        assert len(text) * _PX_PER_CHAR <= w * _PX_PER_DU, suffix
    for ident, field in (("IDC_BROWSE", "browse"), ("IDC_INSTALL", "install"),
                         ("IDC_CANCEL", "cancel")):
        _, _, w, _ = _rc_control_size(inst_rc, ident)
        for suffix in SUFFIX_TO_LOCALE:
            text = _field("installer.c", "UiStrings", suffix, field)
            assert len(text) * _PX_PER_CHAR <= w * _PX_PER_DU - 16, (suffix, field)

    # The uninstall prompt may wrap onto a second line, which the static must
    # be tall enough for (a line is ~15 px = 10 dialog units).
    un_rc = _read("uninstaller.rc")
    _, _, w, h = _rc_control_size(un_rc, "IDC_UNINSTALL_PROMPT")
    assert h >= 24
    for suffix in SUFFIX_TO_LOCALE:
        text = _field("uninstaller.c", "UninstallStrings", suffix, "prompt")
        assert len(text) * _PX_PER_CHAR <= 2 * w * _PX_PER_DU, suffix
    for ident, field in (("IDC_INSTALL", "uninstall"), ("IDC_CANCEL", "cancel")):
        _, _, bw, _ = _rc_control_size(un_rc, ident)
        for suffix in SUFFIX_TO_LOCALE:
            text = _field("uninstaller.c", "UninstallStrings", suffix, field)
            assert len(text) * _PX_PER_CHAR <= bw * _PX_PER_DU - 16, (suffix, field)


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
