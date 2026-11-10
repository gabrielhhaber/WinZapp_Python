"""Which labelled controls share a window, in the settings dialog and the
windows opened from it, read from the sources.

Not a test module: tests/test_settings_mnemonics.py uses it to check that
every control has an Alt+letter and that no two controls a person can reach at
the same time share one. Nothing is written out by hand, so a control added
later is checked the day it is added.

A control is a wx.Button, CheckBox, RadioButton, ToggleButton or RadioBox
with its label, or a StaticText that names the field created right after it,
whose label is ``i18n.t(key)`` with a key the source states: a literal, a
loop over a literal table or over a module constant, or a builder's argument
(each call is followed on its own, so one builder used for two windows puts
its controls in both); or one of the small helpers that make one
(``self._check("key", ...)`` and the like). A group box (wx.StaticBox) is not
a tab stop and takes no mnemonic.

Its window is found by following the parent it was created with up to:

* a page of the settings' category list (a child of ``self._notebook``),
  on screen together with the dialog's own controls (search field, OK,
  Cancel, Apply);
* a SubPageDialog, a window of its own with its own OK button;
* ``self`` of any other class: a panel shown inside one of those (followed to
  where it is created) or a dialog of its own.

The settings dialog is assembled from mixins, so their ``self._x`` attributes
are one namespace.
"""

from __future__ import annotations

import ast
import pathlib
from collections import defaultdict

CLIENT = pathlib.Path(__file__).resolve().parents[1] / "client"
DIALOGS = CLIENT / "ui" / "dialogs"

#: The settings dialog and the windows it opens.
FILES = (
    "settings_dialog.py", "settings_pages.py", "shortcuts_tab.py", "shortcut_capture.py",
    "ai_settings_page.py", "status_audience_dialog.py", "common_groups_tab.py",
    "whatsapp_privacy_panel.py", "whatsapp_profile_panel.py",
    "transcription_background.py", "transcription_cache.py", "transcription_external.py",
    "transcription_model_marks.py", "transcription_options.py", "transcription_precision.py",
    "transcription_queue.py", "transcription_result_field.py", "transcription_reuse.py",
    "transcription_tab.py", "transcription_whisper_cpp.py", "transcription_windows.py",
    "advanced_tab.py", "ai_auto_options.py", "reading_position_options.py",
)

LABELLED = {"Button", "CheckBox", "RadioButton", "ToggleButton", "RadioBox", "StaticText",
            "CommandLinkButton"}
#: Fields named by the StaticText created just before them (settings_pages.py's
#: own search reads them the same way). A StaticText before anything else is a
#: note or a status line, not a label.
CAPTIONED = {"TextCtrl", "Choice", "ComboBox", "SpinCtrl", "SpinCtrlDouble", "ListBox",
             "ListCtrl", "Slider", "CheckListBox", "TreeCtrl", "_HotkeyCapture"}
CONTROLS = CAPTIONED | {"Button", "CheckBox", "RadioButton", "ToggleButton", "RadioBox",
                        "StaticText", "StaticBox", "BitmapButton", "Gauge", "HyperlinkCtrl"}
#: Helpers that make one labelled control from a literal key (ai_settings_page.py).
HELPERS = {"_caption", "_text", "_choice", "_button", "_label", "_check"}
SETTINGS = "SettingsDialog"
DIALOG_CONTROLS = f"window {SETTINGS}"
#: Shown with every page but set up through SettingsPages.set_search_texts().
DIALOG_EXTRA_KEYS = ("settings_search_label",)


def _module_constants():
    """NAME -> "text" for the string constants of every client module, for a
    key given as ``module.NAME`` (transcription_preferences.LANGUAGE_DETECT_I18N_KEY)."""
    found = {}
    for path in CLIENT.rglob("*.py"):
        if "venv" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in tree.body:
            if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        found.setdefault(target.id, set()).add(node.value.value)
    return {name: values.pop() for name, values in found.items() if len(values) == 1}


def _ctor(node):
    if isinstance(node, ast.Call):
        func = node.func
        return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    return ""


def _t_argument(node):
    """The argument of i18n.t(...) (any receiver), through .replace()/.format()."""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return None
    if node.func.attr in ("replace", "format", "strip"):
        return _t_argument(node.func.value)
    if node.func.attr in ("t", "_t") and node.args:
        return node.args[0]
    return None


def _strings(node, constants):
    """(dict keys or None, rows) of a table: a literal tuple/list/dict, or a
    module constant holding one; (None, None) for anything else."""
    if isinstance(node, ast.Name) and node.id in constants:
        node = constants[node.id]
    if isinstance(node, ast.Dict):
        return list(node.keys), list(node.values)
    if isinstance(node, (ast.Tuple, ast.List)):
        return None, list(node.elts)
    return None, None


def _label_node(call, ctor):
    for kw in call.keywords:
        if kw.arg == "label":
            return kw.value
    if len(call.args) >= 3:
        return call.args[2]
    if len(call.args) == 2 and ctor in ("StaticText", "Button", "CheckBox", "RadioButton",
                                       "ToggleButton"):
        return call.args[1]
    return None


class _Source:
    """What the files say: parents, constructors, calls and controls."""

    def __init__(self):
        self.parent_of = {}       # name -> parent name
        self.kind_of = {}         # name -> constructor, or "alias"
        self.controls = []        # (key spec, ctor, parent, where)
        self.params = {}          # "scope:param" -> (scope, index)
        self.returns = {}         # scope -> name it returns
        self.calls = defaultdict(list)   # callee scope -> [[argument, ...] per call]
        self.instances = {}       # panel class -> the name its instance is kept under

    def read(self):
        self.module_constants = _module_constants()
        trees = {f: ast.parse((DIALOGS / f).read_text(encoding="utf-8")) for f in FILES}
        panels = {n.name for t in trees.values() for n in t.body if isinstance(n, ast.ClassDef)
                  and not any("Dialog" in ast.unparse(b) for b in n.bases)}
        for filename, tree in trees.items():
            constants = {t.id: node.value for node in tree.body if isinstance(node, ast.Assign)
                         for t in node.targets if isinstance(t, ast.Name)}
            for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
                namespace = (SETTINGS if cls.name == SETTINGS or cls.name.endswith("Mixin")
                             else cls.name)
                for func in [n for n in cls.body if isinstance(n, ast.FunctionDef)]:
                    self._read_function(filename, namespace, func, constants, panels)

    def _read_function(self, filename, namespace, func, constants, panels):
        scope = f"{namespace}.{func.name}"
        for index, param in enumerate(a.arg for a in func.args.args[1:]):
            self.params[f"{scope}:{param}"] = (scope, index)

        def name(node):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                    and node.value.id == "self":
                return f"{namespace}:self.{node.attr}"
            if isinstance(node, ast.Name):
                return f"{namespace}:self" if node.id == "self" else f"{scope}:{node.id}"
            return None

        made = sorted((n.lineno, n.col_offset, _ctor(n)) for n in ast.walk(func)
                      if isinstance(n, ast.Call) and _ctor(n) in CONTROLS)

        def captions_a_field(call):
            later = [c for line, col, c in made if (line, col) > (call.lineno, call.col_offset)]
            return bool(later) and later[0] in CAPTIONED

        def loop_keys(variable):
            """("keys", [...]) for a loop over a table this file states, or
            ("param", "scope:param", element index) for one over an argument.
            A comprehension counts as a loop ({kind: self._check(label) for
            kind, label in KIND_TOGGLES}, ai_settings_page.py)."""
            for node in ast.walk(func):
                if not isinstance(node, (ast.For, ast.comprehension)):
                    continue
                target = node.target
                flat = [target] if isinstance(target, ast.Name) else (
                    target.elts if isinstance(target, ast.Tuple) else [])
                names = [getattr(e, "id", None) for e in flat]
                nested = {}     # for kind, (label_key, ...) in TABLE.items()
                for position, element in enumerate(flat):
                    if isinstance(element, ast.Tuple):
                        for inner, item in enumerate(element.elts):
                            nested[getattr(item, "id", None)] = (position, inner)
                if variable not in names and variable not in nested:
                    continue
                iterator = node.iter
                items_call = (isinstance(iterator, ast.Call) and isinstance(iterator.func, ast.Attribute)
                              and iterator.func.attr == "items")
                table = iterator.func.value if items_call else iterator
                if isinstance(table, ast.Name) and f"{scope}:{table.id}" in self.params:
                    return ("param", f"{scope}:{table.id}",
                            names.index(variable) if variable in names else None)
                dict_keys, rows = _strings(table, constants)
                if rows is None:
                    return ("keys", [])
                pairs = list(zip(dict_keys, rows)) if items_call and dict_keys else [(None, r) for r in rows]
                keys = []
                for key_node, row in pairs:
                    if variable in nested:
                        position, inner = nested[variable]
                        item = row.elts[inner] if isinstance(row, ast.Tuple) and inner < len(row.elts) else None
                    else:
                        index = names.index(variable)
                        if items_call:
                            item = key_node if index == 0 else row
                        elif isinstance(target, ast.Tuple) and isinstance(row, ast.Tuple):
                            item = row.elts[index] if index < len(row.elts) else None
                        else:
                            item = row
                    if isinstance(item, ast.Constant) and isinstance(item.value, str):
                        keys.append(item.value)
                return ("keys", keys)
            return ("keys", [])

        locals_ = {}
        for node in ast.walk(func):
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)):
                locals_[node.targets[0].id] = node.value

        def key_spec(argument):
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                return ("keys", [argument.value])
            if isinstance(argument, ast.Attribute) and isinstance(argument.value, ast.Name)                     and argument.attr in self.module_constants:
                return ("keys", [self.module_constants[argument.attr]])
            if isinstance(argument, ast.Name) and isinstance(locals_.get(argument.id), ast.Call):
                inner = _t_argument(locals_[argument.id])
                if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                    return ("keys", [inner.value])
            if isinstance(argument, ast.Name):
                if f"{scope}:{argument.id}" in self.params:
                    return ("param", f"{scope}:{argument.id}", None)
                value = constants.get(argument.id)
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    return ("keys", [value.value])
                return loop_keys(argument.id)
            return ("keys", [])

        for node in ast.walk(func):
            if isinstance(node, ast.Return) and node.value is not None and name(node.value):
                self.returns[scope] = name(node.value)
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "self"):
                self.calls[f"{namespace}.{node.func.attr}"].append(
                    [(name(a), a, _strings(a, constants)[1]) for a in node.args])
            targets, value = [], None
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            if isinstance(value, (ast.Attribute, ast.Name)) and name(value):
                for target in targets:
                    if name(target):
                        self.parent_of[name(target)], self.kind_of[name(target)] = name(value), "alias"
            if isinstance(value, ast.Call):
                ctor = _ctor(value)
                builder = (f"{namespace}.{value.func.attr}" if isinstance(value.func, ast.Attribute)
                           and isinstance(value.func.value, ast.Name)
                           and value.func.value.id == "self" else None)
                parent = name(value.args[0]) if value.args else None
                for target in targets:
                    for single in (target.elts if isinstance(target, ast.Tuple) else [target]):
                        child = name(single)
                        if not child:
                            continue
                        if builder:
                            self.parent_of[child], self.kind_of[child] = f"returns {builder}", "alias"
                        elif parent:
                            self.parent_of[child], self.kind_of[child] = parent, ctor
                        if ctor in panels:
                            self.instances[ctor] = child
                        if ctor == "SubPageDialog" and len(value.args) >= 3:
                            # Its own OK button, labelled by the caller.
                            label = value.args[2]
                            self.controls.append((key_spec(_t_argument(label) if isinstance(label, ast.Call)
                                                           else label), "Button",
                                                  child, f"{filename}:{value.lineno}"))
            if not isinstance(node, ast.Call):
                continue
            ctor = _ctor(node)
            where = f"{filename}:{node.lineno}"
            if ctor == "StaticText" and not captions_a_field(node):
                continue
            if ctor in LABELLED and node.args:
                argument = _t_argument(_label_node(node, ctor))
                if argument is not None:
                    self.controls.append((key_spec(argument), ctor, name(node.args[0]), where))
            elif ctor in HELPERS:
                literal = [a for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
                if not literal and node.args and isinstance(node.args[0], ast.Name):
                    # self._check(label, ...) with the key from a loop variable.
                    spec = key_spec(node.args[0])
                    if spec[0] == "keys" and spec[1]:
                        self.controls.append((spec, ctor, f"{namespace}:self", where))
                elif literal:
                    body = (node.args[0] if ctor in ("_caption", "_text", "_choice", "_button")
                            and len(node.args) > 1 and not isinstance(node.args[0], ast.Constant) else None)
                    parent = name(body) if body is not None else f"{namespace}:self"
                    self.controls.append((("keys", [literal[0].value]), ctor, parent, where))

    # ── Resolution ───────────────────────────────────────────────────────────

    def _keys_from(self, spec, binding):
        """The keys of *spec* under *binding* (callee scope -> call arguments)."""
        if spec[0] == "keys":
            return spec[1]
        _kind, param, element = spec
        scope, index = self.params[param]
        arguments = binding.get(scope)
        if arguments is None or index >= len(arguments):
            return []
        _name, node, rows = arguments[index]
        if element is None:
            return [node.value] if isinstance(node, ast.Constant) and isinstance(node.value, str) else []
        keys = []
        for row in rows or ():
            item = row.elts[element] if isinstance(row, ast.Tuple) and element < len(row.elts) else None
            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                keys.append(item.value)
        return keys

    def _settled(self, node, binding):
        """*node* through aliases and arguments: [(name, binding)]."""
        seen = set()
        while node and node not in seen:
            seen.add(node)
            if self.kind_of.get(node) == "alias":
                node = self.parent_of[node]
            elif node.startswith("returns "):
                node = self.returns.get(node[len("returns "):])
            elif node in self.params:
                scope, index = self.params[node]
                if scope in binding:
                    node = binding[scope][index][0]
                    continue
                found = []
                for arguments in self.calls.get(scope, ()):
                    if index < len(arguments):
                        found += self._settled(arguments[index][0], {**binding, scope: arguments})
                return found
            else:
                return [(node, binding)]
        return [(node, binding)]

    def windows(self, node, spec):
        """[(window, keys)] for a control created under *node*."""
        found = []
        frontier = self._settled(node, {})
        for _ in range(30):
            if not frontier:
                break
            following = []
            for current, binding in frontier:
                owner = current.split(":", 1)[0] if current else ""
                keys = self._keys_from(spec, binding)
                if current and current.endswith(":self") and owner in self.instances:
                    following += self._settled(self.instances[owner], binding)
                    continue
                parent = self.parent_of.get(current)
                parents = self._settled(parent, binding) if parent else []
                if any(p == f"{SETTINGS}:self._notebook" for p, _b in parents):
                    local = current.split(":", 1)[1]
                    found.append((f"page {local if local.startswith('self.') else current}", keys))
                elif self.kind_of.get(current) in ("SubPageDialog", "Dialog"):
                    found.append((f"window {current.split(':', 1)[1]}", keys))
                elif current and current.endswith(":self"):
                    found.append((f"window {owner}", keys))
                elif not parents:
                    found.append((f"window {current}", keys))
                else:
                    following += parents
            frontier = following
        return found


def _labels_from_data():
    """{window: [(key, ctor, where)]} for labels a window reads from data
    rather than from its own source: Privacy's dropdowns, one per
    core.whatsapp_privacy.PRIVACY_FIELDS entry."""
    from core import whatsapp_privacy
    return {"window self._whatsapp_privacy_dialog": [
        (field.label_key, "StaticText", "core/whatsapp_privacy.py:PRIVACY_FIELDS")
        for field in whatsapp_privacy.PRIVACY_FIELDS]}


def collect():
    """{window: [(key, ctor, "file:line"), ...]} for every labelled control."""
    source = _Source()
    source.read()
    windows = defaultdict(list)
    for spec, ctor, parent, where in source.controls:
        for window, keys in source.windows(parent, spec):
            for key in keys:
                windows[window].append((key, ctor, where))
    for window, items in _labels_from_data().items():
        windows[window].extend(items)
    return dict(windows)


def page_windows(windows):
    """The settings pages: on screen together with the dialog's own controls."""
    return {name: items for name, items in windows.items() if name.startswith("page ")}
