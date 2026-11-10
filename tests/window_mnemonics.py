"""Which labelled controls share a screen, read from the sources.

Not a test module: tests/test_window_mnemonics_dont_collide.py uses it to check
that no two controls on one screen share an Alt+letter in any locale. A shared
letter only cycles the focus between the two controls, and no screen reader
says so; reported in the call participants dialog, where "Atualizar" and
"Adicionar participantes" were both Alt+A in pt-BR (and in three other
locales), and a hand-written key list per window had not caught it.

Nothing is listed by hand. A *unit* is a top-level function, a dialog class as
a whole, or one method of a class assembled from many (a mixin, MainWindow,
ConversationsPanel, StatusPanel), since those build a different window in each
method. Inside a unit a control's *screen* is its parent, the first argument
of the call that creates it: panels a window swaps (the status tab's viewer,
text post and media post panels) are parents of their own, and a helper
called without a parent (`button("key", handler)`) builds on the unit's own
window. A menu's items are a screen of their own (`menu.Append(...)`), and so
are the menu bar's titles. A label written as `a if x else b` is one control
showing either key, so its keys never collide with each other; a label set
later with SetLabel() replaces the one the control was created with and is
not counted.
"""

import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
CLIENT = ROOT / "client"
SKIPPED_DIRS = {"venv", "api", "node", "__pycache__"}
SPLIT_CLASS = re.compile(r"(Mixin$|^MainWindow$|^ConversationsPanel$|^StatusPanel$)")
#: wx calls whose label is a control on a screen
CONTROLS = {"Button", "StaticText", "CheckBox", "RadioButton", "RadioBox", "ToggleButton",
            "StaticBox", "CommandLinkButton", "HyperlinkCtrl", "CollapsiblePane"}
MENU_METHODS = {"Append", "AppendCheckItem", "AppendRadioItem", "AppendSubMenu", "Insert",
                "Prepend", "PrependCheckItem", "InsertCheckItem"}
#: the unit's own window, for a helper called without a parent
OWN = "self"


def _key(node):
    """The literal key of an `<x>.i18n.t("key")` / `i18n.t("key")` call."""
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "t" and node.args
            and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
        owner = node.func.value
        name = owner.attr if isinstance(owner, ast.Attribute) else getattr(owner, "id", "")
        if name in ("i18n", "_i18n"):
            return node.args[0].value
    return None


def _slots(node, keys=frozenset()):
    """The labels an expression can show: a list of alternative key sets.
    A bare string counts when it is a catalog key (*keys*): the key a helper
    such as `button("channels_open", handler)` looks up itself."""
    key = _key(node)
    if key:
        return [frozenset({key})]
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "t"
            and node.args and isinstance(node.args[0], ast.IfExp)):
        # i18n.t("a" if x else "b"): one control showing either key
        options = [n.value for n in (node.args[0].body, node.args[0].orelse)
                   if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        return [frozenset(options)] if len(options) == 2 else []
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in keys:
        return [frozenset({node.value})]
    if isinstance(node, ast.IfExp):
        body, orelse = _slots(node.body, keys), _slots(node.orelse, keys)
        if len(body) == 1 and len(orelse) == 1:
            return [body[0] | orelse[0]]
        return body + orelse
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in (
            "format", "replace", "strip"):
        return _slots(node.func.value, keys)
    if isinstance(node, (ast.List, ast.Tuple)):
        return [s for item in node.elts for s in _slots(item, keys)]
    return []


def _call_name(call):
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


#: a sizer is not a window: what is laid out in it is on its window's screen
_SIZER = re.compile(r"(sizer|row|col|grid|box)\w*$", re.IGNORECASE)


def _parent(call):
    if call.args and not _slots(call.args[0]) and not isinstance(call.args[0], ast.Constant):
        text = ast.unparse(call.args[0])
        if text in ("self", "parent_self") or _SIZER.search(text) or text.startswith("lambda"):
            return OWN
        return text
    return OWN


def _screens(unit, keys):
    """{screen: set of slots} for the calls in one unit."""
    screens = {}
    for call in ast.walk(unit):
        if not isinstance(call, ast.Call):
            continue
        name = _call_name(call)
        args = list(call.args) + [kw.value for kw in call.keywords if kw.arg in ("label", "choices", "text")]
        if name in MENU_METHODS and isinstance(call.func, ast.Attribute):
            screen = "menu:" + ast.unparse(call.func.value)
        elif name in CONTROLS:
            screen = _parent(call)
        elif name and not name[0].isupper() and _key(call) is None and name not in (
                "t", "format", "output", "speak_output", "SetLabel", "SetName", "SetTitle",
                "SetToolTip", "MessageBox", "replace", "split_mnemonic", "SetValue", "SetHint",
                "info", "warning", "error", "debug", "append", "get", "setdefault"):
            # a helper that builds a labelled control (button("k"), add_labelled(...))
            if not any(_slots(a, keys) for a in args):
                continue
            screen = _parent(call)
        else:
            continue
        helper = screen != "menu:" and name not in CONTROLS and name not in MENU_METHODS
        for arg in args:
            for slot in _slots(arg, keys if helper else frozenset()):
                screens.setdefault(screen, set()).add(slot)
    return screens


def units(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            if SPLIT_CLASS.search(node.name):
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        yield f"{node.name}.{item.name}", item
            else:
                yield node.name, node
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node.name, node


def source_files():
    for path in sorted(CLIENT.rglob("*.py")):
        if SKIPPED_DIRS & set(path.relative_to(CLIENT).parts):
            continue
        yield path


def all_screens(keys=frozenset()):
    """[(where, screen, slots)] for every screen with two or more labels.
    *keys* are the catalog's keys, for the helpers that take one bare."""
    out = []
    for path in source_files():
        try:
            found = list(units(path))
        except SyntaxError:
            continue
        rel = path.relative_to(ROOT).as_posix()
        for name, unit in found:
            for screen, slots in _screens(unit, keys).items():
                if len(slots) > 1:
                    out.append((f"{rel}::{name}", screen, slots))
    return out


def collisions(slots, strings, mnemonic):
    """{letter: [slot, ...]} where two different slots can show the same letter."""
    by_letter = {}
    for slot in slots:
        letters = {mnemonic(strings[k]) for k in slot if k in strings} - {None}
        for letter in letters:
            by_letter.setdefault(letter, []).append(slot)
    return {letter: found for letter, found in by_letter.items() if len(found) > 1}
