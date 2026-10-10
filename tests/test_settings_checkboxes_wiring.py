"""Configurações, every tab: each on/off checkbox is wired to one settings key,
the same way, end to end.

Each checkbox is hand-wired in separate places of settings_dialog.py — loaded
from settings[section][key] with a fallback default in _load_values(), and
written back in _apply_values(). Nothing ties them together, so a copy-paste
slip in either fails silently:

- loaded from one key and saved to another (or another section) — the option
  looks like it works and resets itself every time Settings is saved;
- a fallback default different from DEFAULT_SETTINGS — the box shows a state
  the app is not actually in, on any install missing the key.

Relabelling after a language change is covered, for every control in the
dialog, by tests/test_settings_dialog_relabels_everything.py.

The dialog writes these in several shapes, and all of them are understood
here, so a checkbox on any tab written in any of them is covered:

    load   x = settings.get("sec", {}).get("key", default); cb.SetValue(x)
           sec = settings.get("sec", {}); cb.SetValue(sec.get("key", default))
           sec = settings.get("sec", {}); x = sec.get("key", default); cb.SetValue(x)
    save   settings.setdefault("sec", {})["key"] = cb.GetValue()
           sec = settings.setdefault("sec", {}); sec["key"] = cb.GetValue()
           settings.setdefault("sec", {}).update({"key": cb.GetValue(), ...})
           x = cb.GetValue(); <any of the above>["key"] = x

This reads the dialog's source, so it runs everywhere without opening a
window. tests/test_settings_checkboxes_roundtrip.py drives the real dialog
through the same checkboxes, in CI only.
"""

import ast
from pathlib import Path

import pytest

from app_settings import _CONNECTION_GLOBAL
from core.transcription import preferences as transcription_preferences
from core.utils import DEFAULT_SETTINGS

SETTINGS_DIALOG = (
    Path(__file__).resolve().parent.parent / "client" / "ui" / "dialogs" / "settings_dialog.py"
)

TRANSCRIPTION_TAB = SETTINGS_DIALOG.with_name("transcription_tab.py")


def _dialog_class():
    """SettingsDialog with TranscriptionTabMixin's methods merged in: that tab
    lives in transcription_tab.py but is loaded and applied as part of the
    dialog."""
    tree = ast.parse(SETTINGS_DIALOG.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "SettingsDialog")
    tab = ast.parse(TRANSCRIPTION_TAB.read_text(encoding="utf-8"))
    mixin = next(
        n for n in tab.body if isinstance(n, ast.ClassDef) and n.name == "TranscriptionTabMixin"
    )
    cls.body = cls.body + mixin.body
    return cls


#: Checkboxes that deliberately do not mirror a settings.json key, and why.
#: Each one must still exist, so a stale entry here fails rather than hiding.
NOT_BACKED_BY_SETTINGS = {
    "_autostart_check": (
        "shows the Windows Run registry entry (autostart.is_autostart_enabled()) "
        "and is applied through MainWindow._apply_autostart(), which rolls the "
        "box back if Windows refuses"
    ),
    "_chat_lock_show_navigation_check": (
        "shows the authenticated encrypted-vault policy rather than a "
        "settings.json value, and is applied through "
        "MainWindow.set_chat_lock_navigation_hidden()"
    ),
}

#: Checkboxes that DO mirror a settings.json key, but through a module that
#: owns the section instead of a literal settings.get("sec", {}).get("key")
#: here — a shape this parse cannot follow. Each entry declares the
#: (section, key) it writes, taken from the owning module's own names rather
#: than retyped, so checkbox_keys() can still hand it to the real-dialog round
#: trip; without the pair the box dropped out of that test entirely and was
#: covered only by a stub. Each must still be a checkbox this file sees.
WIRED_THROUGH_A_MODULE = {
    "_transcription_detect_language_check": (
        (transcription_preferences.SECTION,
         transcription_preferences.SETTING_AUTO_DETECT_LANGUAGE),
        "the Local Transcription tab reads its section through "
        "core.transcription.preferences.read_section(), which validates every "
        "value and owns the key names; the stub-level round trip is "
        "tests/test_transcription_settings_tab.py::"
        "TestEverySettingIsReadBackAndWritten"
    ),
}


def _self_attr(node):
    """'_foo' for `self._foo`, else None."""
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    ):
        return node.attr
    return None


def _section_of(node, section_names):
    """The section name for `<...>.get("sec", {})` / `.setdefault("sec", {})`,
    or for a local name bound to one of those; else None."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("get", "setdefault")
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        return node.args[0].value
    if isinstance(node, ast.Name):
        return section_names.get(node.id)
    return None


def _key_lookup(node, section_names):
    """(section, key, default) for `<section>.get("key", default)`, else None."""
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and len(node.args) == 2
        and all(isinstance(a, ast.Constant) for a in node.args)
    ):
        return None
    section = _section_of(node.func.value, section_names)
    if section is None:
        return None
    return section, node.args[0].value, node.args[1].value


#: One-argument functions a load may pass the stored value through before
#: SetValue(): bool() itself, and a feature's own value guard (so the box
#: shows what the feature actually does with an odd stored value).
_VALUE_GUARDS = ("bool", "emoticon_setting_enabled")


def _unwrap_bool(node):
    # A strict opt-in guard has the same settings source as bool(value),
    # but rejects malformed stored values such as the string "false".
    if (isinstance(node, ast.Compare) and len(node.ops) == 1
            and isinstance(node.ops[0], ast.Is) and len(node.comparators) == 1
            and isinstance(node.comparators[0], ast.Constant)
            and node.comparators[0].value is True):
        return node.left
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _VALUE_GUARDS
        and node.args
    ):
        return node.args[0]
    return node


def _chosen_section(target):
    """Section of `choices["key"] = ...`: an install-wide key the dialog hands
    to MainWindow.choose_global_settings(), which stores it under the section
    app_settings files it in -- still a save of that settings key."""
    if isinstance(target.value, ast.Name) and target.value.id == "choices":
        return "connection" if target.slice.value in _CONNECTION_GLOBAL else "general"
    return None


def _parse():
    """{attr: {"page", "line", "loads": [(section, key, default)],
    "saves": [(section, key)]}} for every wx.CheckBox in SettingsDialog."""
    cls = _dialog_class()

    boxes = {}
    for node in ast.walk(cls):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and _self_attr(node.targets[0])
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "CheckBox"
        ):
            page = _self_attr(node.value.args[0]) if node.value.args else None
            boxes[_self_attr(node.targets[0])] = {
                "page": page, "line": node.lineno, "loads": [], "saves": [],
            }

    def is_getvalue(node):
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "GetValue"
            and _self_attr(node.func.value) in boxes
        )

    for func in (n for n in cls.body if isinstance(n, ast.FunctionDef)):
        # One pass in source order, bindings and uses together, so every use
        # resolves a local name to the assignment in force AT THAT LINE — a
        # separate pass after all assignments would read the last one, and a
        # reused name could make a real load/save mismatch look consistent.
        events = sorted(
            (
                n for n in ast.walk(func)
                if (isinstance(n, ast.Assign) and len(n.targets) == 1)
                or (
                    isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and n.func.attr in ("SetValue", "update")
                )
            ),
            key=lambda n: (n.lineno, n.col_offset),
        )
        section_names = {}   # local -> section, for sec = settings.get/setdefault("sec", {})
        loaded = {}          # local -> (section, key, default)
        value_of = {}        # local -> checkbox attr, for x = self._cb.GetValue()
        for node in events:
            if isinstance(node, ast.Assign):
                target, value = node.targets[0], node.value
                if isinstance(target, ast.Name):
                    # A rebinding forgets whatever the name meant before.
                    for table in (section_names, loaded, value_of):
                        table.pop(target.id, None)
                    lookup = _key_lookup(value, section_names)
                    if lookup is not None:
                        loaded[target.id] = lookup
                    elif (
                        isinstance(value, ast.Call)
                        and isinstance(value.func, ast.Attribute)
                        and value.func.attr in ("get", "setdefault")
                        and len(value.args) == 2
                        and isinstance(value.args[1], ast.Dict)
                        and _section_of(value, section_names) is not None
                    ):
                        section_names[target.id] = _section_of(value, section_names)
                    elif is_getvalue(value):
                        value_of[target.id] = _self_attr(value.func.value)
                    continue

                # <section>["key"] = self._cb.GetValue() / = x
                if isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant):
                    section = _section_of(target.value, section_names)
                    if section is None:
                        section = _chosen_section(target)
                    if section is None:
                        continue
                    if is_getvalue(value):
                        boxes[_self_attr(value.func.value)]["saves"].append((section, target.slice.value))
                    elif isinstance(value, ast.Name) and value.id in value_of:
                        boxes[value_of[value.id]]["saves"].append((section, target.slice.value))
                continue

            # self._cb.SetValue(<load>)
            if node.func.attr == "SetValue":
                if _self_attr(node.func.value) not in boxes or not node.args:
                    continue
                arg = _unwrap_bool(node.args[0])
                lookup = _key_lookup(arg, section_names)
                if lookup is None and isinstance(arg, ast.Name):
                    lookup = loaded.get(arg.id)
                if lookup is not None:
                    boxes[_self_attr(node.func.value)]["loads"].append(lookup)
            # <section>.update({"key": self._cb.GetValue(), ...})
            elif node.args and isinstance(node.args[0], ast.Dict):
                section = _section_of(node.func.value, section_names)
                if section is None:
                    continue
                for key, value in zip(node.args[0].keys, node.args[0].values):
                    if isinstance(key, ast.Constant) and is_getvalue(value):
                        boxes[_self_attr(value.func.value)]["saves"].append((section, key.value))
    return boxes


def _checkbox_constructions():
    """Every wx.CheckBox(...) call in SettingsDialog, however it is stored."""
    cls = _dialog_class()
    return [
        n.lineno for n in ast.walk(cls)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "CheckBox"
    ]


BOXES = _parse()
WIRED = sorted(
    attr for attr in BOXES
    if attr not in NOT_BACKED_BY_SETTINGS and attr not in WIRED_THROUGH_A_MODULE
)


def checkbox_keys():
    """[(attr, section, key)] for the round-trip test, from the same parse plus
    the pairs WIRED_THROUGH_A_MODULE declares."""
    out = []
    for attr in WIRED:
        saves = BOXES[attr]["saves"]
        if len(saves) == 1:
            out.append((attr, saves[0][0], saves[0][1]))
    for attr, ((section, key), _reason) in WIRED_THROUGH_A_MODULE.items():
        out.append((attr, section, key))
    return out


def _only(items, attr, what, fix):
    assert len(items) == 1, f"{attr}: expected exactly one {what}, found {items!r} — {fix}"
    return items[0]


def test_the_parse_finds_the_checkboxes_on_every_tab():
    """Guards the guard: a refactor that changes the wiring's shape must fail
    here loudly, not make every test below pass on an empty list."""
    assert len(BOXES) >= 34
    # Every CheckBox the dialog constructs must be one this file analyses. A
    # new one kept in a local, built in a loop or stored in a dict would
    # otherwise be tested by nothing while every test here stayed green.
    constructed = _checkbox_constructions()
    analysed = {info["line"] for info in BOXES.values()}
    unanalysed = sorted(line for line in constructed if line not in analysed)
    assert unanalysed == [], (
        f"wx.CheckBox built at line(s) {unanalysed} of settings_dialog.py is not "
        f"assigned to a self._ attribute, so its wiring is never checked; keep it on "
        f"self._name = wx.CheckBox(...) or extend _parse()"
    )
    assert len(constructed) == len(BOXES)
    pages = {info["page"] for info in BOXES.values()}
    for page in (
        "_general_page", "_ui_page", "_accessibility_page", "_speech_page",
        "_conn_page", "_audio_devices_page", "_storage_page", "_audio_page",
        "_calls_page", "_profile_backup_page", "_reactions_page",
        "_chat_lock_page",
    ):
        assert page in pages, f"no checkbox found on {page}"
    assert len(checkbox_keys()) == len(WIRED) + len(WIRED_THROUGH_A_MODULE)


def test_every_exception_still_exists_and_is_really_unwired():
    for attr, reason in NOT_BACKED_BY_SETTINGS.items():
        assert attr in BOXES, f"{attr} is no longer a checkbox; drop it from NOT_BACKED_BY_SETTINGS"
        assert BOXES[attr]["loads"] == [], (
            f"{attr} now loads from settings ({BOXES[attr]['loads']!r}); it is no longer "
            f"an exception ({reason}) — remove it from NOT_BACKED_BY_SETTINGS"
        )
        assert BOXES[attr]["saves"] == [], (
            f"{attr} now saves to settings ({BOXES[attr]['saves']!r}) without loading "
            f"from there — either wire both halves and remove it from "
            f"NOT_BACKED_BY_SETTINGS, or stop writing it ({reason})"
        )


def test_every_module_wired_checkbox_still_exists_and_is_not_wired_here():
    for attr, ((section, key), reason) in WIRED_THROUGH_A_MODULE.items():
        assert attr in BOXES, f"{attr} is no longer a checkbox; drop it from WIRED_THROUGH_A_MODULE"
        assert BOXES[attr]["loads"] == [] and BOXES[attr]["saves"] == [], (
            f"{attr} is now wired in the shape this file checks "
            f"({BOXES[attr]['loads']!r} / {BOXES[attr]['saves']!r}); remove it from "
            f"WIRED_THROUGH_A_MODULE so it is checked like every other ({reason})"
        )
        # The round trip reads the expected state off DEFAULT_SETTINGS, so a
        # declared pair that is not a shipped default would only surface there
        # as a KeyError, in CI.
        assert key in DEFAULT_SETTINGS.get(section, {}), (
            f"{attr} declares {section}.{key}, which is not in core/utils.py DEFAULT_SETTINGS"
        )


def test_the_transcription_detection_box_is_handed_to_the_real_dialog_round_trip():
    """Pinned by the settings.json spelling, not the module's constants: the
    round trip exercises exactly the tuples checkbox_keys() returns, and this
    box once fell out of that list without any test noticing."""
    assert (
        "_transcription_detect_language_check", "transcription", "auto_detect_language"
    ) in checkbox_keys()


@pytest.mark.parametrize("attr", WIRED)
class TestEachCheckbox:
    def test_loads_and_saves_the_same_key(self, attr):
        info = BOXES[attr]
        loaded_section, loaded_key, _ = _only(
            info["loads"], attr, "load in _load_values()",
            "read it with settings.get(section, {}).get(key, default) and SetValue() it; "
            "if it genuinely is not a settings.json value, add it to NOT_BACKED_BY_SETTINGS "
            "with the reason",
        )
        saved = _only(
            info["saves"], attr, "save in _apply_values()",
            f"write settings.setdefault(section, {{}})[key] = self.{attr}.GetValue()",
        )
        assert (loaded_section, loaded_key) == saved, (
            f"{attr} is loaded from {loaded_section}.{loaded_key} but saved to "
            f"{saved[0]}.{saved[1]}: the option would reset itself every time Settings is saved"
        )

    def test_its_fallback_matches_the_shipped_default(self, attr):
        info = BOXES[attr]
        if len(info["loads"]) != 1:
            pytest.skip("covered by test_loads_and_saves_the_same_key")
        (section, key, fallback), = info["loads"]
        assert key in DEFAULT_SETTINGS.get(section, {}), (
            f"{attr} uses {section}.{key}, which is not in core/utils.py DEFAULT_SETTINGS "
            f"(nor, then, in data/settings_default.json)"
        )
        assert fallback == DEFAULT_SETTINGS[section][key], (
            f"{attr} falls back to {fallback!r} in _load_values(), but DEFAULT_SETTINGS "
            f"ships {section}.{key} = {DEFAULT_SETTINGS[section][key]!r}; make them agree"
        )
