"""Changing the language and pressing Apply in Settings must re-translate every
control the dialog labelled when it was built.

SettingsDialog does not rebuild itself on Apply: _refresh_dialog_labels() calls
SetLabel()/SetItemLabel()/... on an explicit list of controls. A control left
out of that list keeps the previous language until the dialog is closed and
opened again — and a control created inline, without a `self.` attribute, can
never be put on that list at all. Reported live: after switching from English
to Portuguese, "When opening a chat, show up to this many messages" and
"Messages to skip when pressing Page Up / Page Down" stayed in English while
the group right below them changed. An audit then found nine such controls,
plus two list column headers.

Compared per control, not per key: two controls may share one string (both
"custom path" labels do), and a key re-applied to one of them must not count
for the other.

This reads the dialog's source, so it runs everywhere without opening a
window.
"""

import ast
import inspect
from pathlib import Path

from core.sound_system import AlertPreviewController

SETTINGS_DIALOG = (
    Path(__file__).resolve().parent.parent / "client" / "ui" / "dialogs" / "settings_dialog.py"
)

LABELLED_CONTROLS = {"StaticText", "CheckBox", "RadioButton", "StaticBox", "Button", "RadioBox"}


def _self_attr(node):
    """'_foo' for `self._foo`, else None."""
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    ):
        return node.attr
    return None


def _i18n_keys(node):
    """Every literal key of an i18n.t("...") call below node."""
    return [
        sub.args[0].value
        for sub in ast.walk(node)
        if isinstance(sub, ast.Call)
        and isinstance(sub.func, ast.Attribute)
        and sub.func.attr == "t"
        and sub.args
        and isinstance(sub.args[0], ast.Constant)
        and isinstance(sub.args[0].value, str)
    ]


def _dialog_class():
    tree = ast.parse(SETTINGS_DIALOG.read_text(encoding="utf-8"))
    return next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "SettingsDialog")


def _built_and_refreshed():
    """built: [(line, what, target attr or None, key)] for every label the
    dialog puts on a control when building it.
    refreshed: {(target attr, key)} re-applied in _refresh_dialog_labels()."""
    cls = _dialog_class()
    refresh = next(
        n for n in cls.body
        if isinstance(n, ast.FunctionDef) and n.name == "_refresh_dialog_labels"
    )

    # The attribute each constructor call is assigned to, keyed by the Call
    # node itself — not by line, where an inline control nested in another
    # call on an assignment's line would inherit that assignment's target.
    targets = {}
    for func in cls.body:
        if not isinstance(func, ast.FunctionDef) or func is refresh:
            continue
        for node in ast.walk(func):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and _self_attr(node.targets[0])
                and isinstance(node.value, ast.Call)
            ):
                targets[id(node.value)] = _self_attr(node.targets[0])

    built = []
    preview_buttons = {}  # controller attr -> button attr
    for func in cls.body:
        if not isinstance(func, ast.FunctionDef) or func is refresh:
            continue
        for node in ast.walk(func):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "AlertPreviewController"
                and len(node.value.args) >= 2
            ):
                preview_buttons[_self_attr(node.targets[0])] = _self_attr(node.value.args[1])

            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr in LABELLED_CONTROLS:
                label = next((k.value for k in node.keywords if k.arg == "label"), None)
                if label is None:
                    continue
                target = targets.get(id(node))
                built.extend(
                    (node.lineno, f"wx.{node.func.attr}", target, key) for key in _i18n_keys(label)
                )
            elif node.func.attr == "InsertColumn" and len(node.args) >= 2:
                target = _self_attr(node.func.value)
                built.extend(
                    (node.lineno, "InsertColumn", target, key) for key in _i18n_keys(node.args[1])
                )

    refreshed = set()
    play_stop = {
        p.default
        for name, p in inspect.signature(AlertPreviewController.__init__).parameters.items()
        if name in ("play_label_key", "stop_label_key")
    }
    for node in ast.walk(refresh):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        # self._x.SetLabel(i18n.t(k)) / self._x.SetItemLabel(i, i18n.t(k)) / ...
        target = _self_attr(node.func.value)
        # self._helper(self._x, i18n.t(k))
        if target is None and node.args:
            target = _self_attr(node.args[0])
        if node.func.attr == "refresh_label" and target in preview_buttons:
            refreshed |= {(preview_buttons[target], key) for key in play_stop}
            continue
        if target is None:
            continue
        refreshed |= {(target, key) for key in _i18n_keys(node)}
    return built, refreshed


def test_the_parse_finds_the_dialog_labels():
    """Guards the guard: a rewrite of how labels are built must fail here,
    not make the real test pass on an empty list."""
    built, refreshed = _built_and_refreshed()
    assert len(built) >= 80
    assert len(refreshed) >= 80
    assert any(what == "InsertColumn" for _, what, _, _ in built)


def test_every_built_label_is_reapplied_to_its_own_control():
    built, refreshed = _built_and_refreshed()
    missing = sorted(
        f"line {line}: {what} on self.{target or '<inline, no attribute>'} "
        f"with i18n.t({key!r})"
        for line, what, target, key in built
        if (target, key) not in refreshed
    )
    assert missing == [], (
        "these keep the previous language after Apply — keep each control on a "
        "self._ attribute and re-apply its label to that same control in "
        "_refresh_dialog_labels():\n  " + "\n  ".join(missing)
    )


def test_main_language_change_repaints_dynamic_content_and_calls():
    main_source = (
        Path(__file__).resolve().parent.parent / "client" / "main.py"
    ).read_text(encoding="utf-8")
    start = main_source.index("    def apply_language_changes(self):")
    end = main_source.index("\n    def on_alt_1", start)
    body = main_source[start:end]

    assert "self._refresh_call_language_surfaces()" in body
    assert "cp.populate_messages(preserve_focus=True)" in body
    assert "self._chats_ui_fp = None" in body
    assert "self.add_chats_to_ui()" in body


def test_incoming_call_popup_has_live_language_refresh_hook():
    source = (
        Path(__file__).resolve().parent.parent
        / "client" / "ui" / "dialogs" / "incoming_call.py"
    ).read_text(encoding="utf-8")
    assert "def refresh_labels(self, message: str | None = None):" in source
    assert "self._apply_labels()" in source
    assert (
        "(self._answer_button, answer_key, self._on_answer)" in source
        and '"incoming_call_close_button", self._on_close' in source
    )
