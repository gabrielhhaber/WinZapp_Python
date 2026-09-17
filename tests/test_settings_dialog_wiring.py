"""Configurações, every control: each settings key the dialog saves is loaded
back, each key it loads is saved, and every fallback agrees with
DEFAULT_SETTINGS.

tests/test_settings_checkboxes_wiring.py checks checkboxes control by control.
Every other control — radio groups, RadioBoxes, text fields, combos, the media
type lists, the hotkey field — goes through a conversion (text to int, two
radios to one string, a combo index to a language code), so it cannot be
matched control-to-key from source alone. This file works one level up, on the
keys themselves, where nothing can hide:

- a key saved in _apply_values() but never loaded in _load_values() opens on
  a stale or empty control. The default audio speed shipped exactly like that:
  its load sat inside _on_custom_api_toggle(), so Settings opened with the
  speed combo empty whatever speed was saved;
- a key loaded but never saved is a control whose changes are thrown away;
- a fallback different from DEFAULT_SETTINGS shows a state the app is not in
  (this found voice_record_focus defaulting to an unused "send_button",
  page_up_down_step to 10 against the 15 written with it, and the API key
  falling back to a placeholder that OK then saved);
- a saved key covered by no round-trip scenario is a control nothing drives.

Anything that is deliberately different is listed below with its reason, and a
stale entry fails. tests/test_settings_dialog_roundtrip.py drives the real
dialog through every scenario, in CI only.
"""

import ast
from pathlib import Path

import pytest

from core import save_location
from core.utils import DEFAULT_SETTINGS
from tests.test_settings_checkboxes_wiring import checkbox_keys
from tests.test_settings_dialog_roundtrip import ROUNDTRIP_KEYS

SETTINGS_DIALOG = (
    Path(__file__).resolve().parent.parent / "client" / "ui" / "dialogs" / "settings_dialog.py"
)

#: Saved in _apply_values() but loaded somewhere other than _load_values().
READ_ELSEWHERE = {
    ("general", "spell_check_mode"): "loaded through spell_check_mode(general) in _apply_spell_check_mode()",
    ("connection", "wpp_port"): "the field shows main_window.wpp_port, the port this account's API is actually on",
    (None, "sound_events"): "per-pack event settings, loaded by _reload_sound_pack_choices()",
    (None, "active_sound_pack"): "loaded by _reload_sound_pack_choices()",
}

#: Loaded in _load_values() but saved somewhere other than _apply_values().
WRITTEN_ELSEWHERE = {
    ("general", "global_hotkey"): "saved by MainWindow.set_global_hotkey(), which also registers it",
}

#: A constant fallback that deliberately differs from DEFAULT_SETTINGS.
FALLBACK_EXCEPTIONS = {
    ("general", "language"): (
        'DEFAULT_SETTINGS ships "" meaning "not chosen yet" (it triggers the '
        "first-run language dialog); the combo needs a real language to show"
    ),
}

#: Saved keys no round-trip scenario drives, and why.
ROUNDTRIP_EXEMPT = {
    ("audio_devices", "output_device_name"): "device names come from the machine; the test sound system lists none",
    ("audio_devices", "effects_output_device_name"): "device names come from the machine; the test sound system lists none",
    ("audio_devices", "input_device_name"): "device names come from the machine; the test sound system lists none",
    (None, "sound_events"): "sound-pack machinery with its own file checks",
    (None, "active_sound_pack"): "sound-pack machinery with its own file checks",
}


def _class():
    tree = ast.parse(SETTINGS_DIALOG.read_text(encoding="utf-8"))
    return next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "SettingsDialog")


def _method(cls, name):
    return next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name)


def _constant(node):
    """A literal str/number/bool, or a save_location.NAME constant; else raises."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "save_location":
        return getattr(save_location, node.attr)
    raise ValueError(ast.unparse(node))


def _is_settings(node):
    """`self.main_window.settings`."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "settings"
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "main_window"
    )


def _section(node, names):
    """Section for settings.get/setdefault(SEC, {}) or a name bound to one."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("get", "setdefault")
        and _is_settings(node.func.value)
        and node.args
    ):
        try:
            return _constant(node.args[0])
        except ValueError:
            return None
    if isinstance(node, ast.Name):
        return names.get(node.id)
    return None


def _section_names(func):
    names = {}
    for node in ast.walk(func):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            section = _section(node.value, {})
            if section is not None:
                names[node.targets[0].id] = section
    return names


def _reads(cls):
    """{(section, key): [fallback node or None, ...]} loaded in _load_values()."""
    func = _method(cls, "_load_values")
    names = _section_names(func)
    reads = {}
    for node in ast.walk(func):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"):
            continue
        section = _section(node.func.value, names)
        if section is None or not node.args:
            continue
        try:
            key = _constant(node.args[0])
        except ValueError:
            continue
        reads.setdefault((section, key), []).append(node.args[1] if len(node.args) > 1 else None)
    return reads


def _writes(cls):
    """{(section, key)} saved in _apply_values(); section None = top level."""
    func = _method(cls, "_apply_values")
    names = _section_names(func)
    writes = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Subscript):
            target = node.targets[0]
            try:
                key = _constant(target.slice)
            except ValueError:
                continue
            section = _section(target.value, names)
            if section is not None:
                writes.add((section, key))
            elif _is_settings(target.value):
                if isinstance(node.value, ast.Dict):
                    for k in node.value.keys:
                        writes.add((key, _constant(k)))
                else:
                    writes.add((None, key))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "update"
            and node.args
            and isinstance(node.args[0], ast.Dict)
        ):
            section = _section(node.func.value, names)
            if section is not None:
                for k in node.args[0].keys:
                    writes.add((section, _constant(k)))
    return writes


CLASS = _class()
READS = _reads(CLASS)
WRITES = _writes(CLASS)


def _label(pair):
    section, key = pair
    return key if section is None else f"{section}.{key}"


def _default(pair):
    section, key = pair
    return DEFAULT_SETTINGS[key] if section is None else DEFAULT_SETTINGS[section][key]


def test_the_parse_finds_the_whole_dialog():
    """Guards the guard: a refactor of how settings are read or written must
    fail here, not leave every test below passing on a short list."""
    assert len(WRITES) >= 60
    assert len(READS) >= 55
    for pair in (
        ("audio_playback", "audio_default_speed"),
        ("files", "save_dialog_folder_mode"),
        ("alert_tones", "private_custom_path"),
        ("storage", "auto_download_media_types"),
        (None, "sound_events"),
    ):
        assert pair in WRITES, _label(pair)


def test_every_saved_key_is_loaded_when_the_dialog_opens():
    missing = sorted(
        _label(pair) for pair in WRITES
        if pair not in READS and pair not in READ_ELSEWHERE
    )
    assert missing == [], (
        f"saved in _apply_values() but never loaded in _load_values(), so the control "
        f"opens without the stored value: {missing}"
    )


def test_every_loaded_key_is_saved():
    missing = sorted(
        _label(pair) for pair in READS
        if pair not in WRITES and pair not in WRITTEN_ELSEWHERE
    )
    assert missing == [], (
        f"loaded in _load_values() but never saved in _apply_values(), so changing the "
        f"control does nothing: {missing}"
    )


def test_every_key_is_a_shipped_default():
    unknown = []
    for pair in WRITES | set(READS):
        try:
            _default(pair)
        except KeyError:
            unknown.append(_label(pair))
    assert sorted(unknown) == [], (
        f"not in core/utils.py DEFAULT_SETTINGS (nor data/settings_default.json): {sorted(unknown)}"
    )


@pytest.mark.parametrize("pair", sorted(READS, key=_label), ids=_label)
def test_every_constant_fallback_matches_the_shipped_default(pair):
    for fallback in READS[pair]:
        if fallback is None or pair in FALLBACK_EXCEPTIONS:
            continue
        # settings.get(..., DEFAULT_SETTINGS["sec"]["key"]) agrees by construction.
        if isinstance(fallback, ast.Subscript) and "DEFAULT_SETTINGS" in ast.unparse(fallback):
            assert ast.unparse(fallback) == (
                f"DEFAULT_SETTINGS[{pair[0]!r}][{pair[1]!r}]"
            ), f"{_label(pair)} falls back to {ast.unparse(fallback)}, another key's default"
            continue
        try:
            value = _constant(fallback)
        except ValueError:
            continue  # e.g. a nested .get() — its own inner fallback is checked separately
        assert value == _default(pair), (
            f"{_label(pair)} falls back to {value!r} in _load_values(), but DEFAULT_SETTINGS "
            f"ships {_default(pair)!r}; make them agree or list it in FALLBACK_EXCEPTIONS with the reason"
        )


def test_every_saved_key_is_driven_by_a_roundtrip():
    by_checkbox = {(section, key) for _, section, key in checkbox_keys()}
    uncovered = sorted(
        _label(pair) for pair in WRITES
        if pair not in by_checkbox and pair not in ROUNDTRIP_KEYS and pair not in ROUNDTRIP_EXEMPT
    )
    assert uncovered == [], (
        f"saved by a control no round-trip scenario drives — add one to SCENARIOS in "
        f"tests/test_settings_dialog_roundtrip.py, or to ROUNDTRIP_EXEMPT with the reason: {uncovered}"
    )


@pytest.mark.parametrize("table", ["READ_ELSEWHERE", "WRITTEN_ELSEWHERE", "FALLBACK_EXCEPTIONS", "ROUNDTRIP_EXEMPT"])
def test_no_exception_is_stale(table):
    entries = globals()[table]
    if table == "READ_ELSEWHERE":
        stale = [p for p in entries if p not in WRITES or p in READS]
    elif table == "WRITTEN_ELSEWHERE":
        stale = [p for p in entries if p not in READS or p in WRITES]
    elif table == "FALLBACK_EXCEPTIONS":
        stale = [p for p in entries if p not in READS]
    else:
        stale = [p for p in entries if p not in WRITES]
    assert stale == [], f"{table} lists entries that no longer apply: {[_label(p) for p in stale]}"


def test_no_roundtrip_scenario_drives_a_key_that_is_not_saved():
    stale = sorted(
        _label(p) for p in ROUNDTRIP_KEYS
        if p not in WRITES and p not in WRITTEN_ELSEWHERE
    )
    assert stale == [], f"round-trip scenarios for keys _apply_values() no longer saves: {stale}"
