"""Every list and field gets its own label read by the screen reader.

Windows names an unlabelled control after the static text created just
before it. Five dialogs built the control first (as the argument of their
label helper) and the label after it, so each label was read on the next
control: in the catalog window the products list had no name and the empty
cart list was read as "Catalog products".
"""

import ast
import pathlib

import pytest
import wx

from tests.conftest import hidden_frame
from ui.dialogs.labelled_control import add_labelled

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_the_label_is_created_just_before_its_control(wx_app):
    frame = hidden_frame()
    try:
        sizer = wx.BoxSizer(wx.VERTICAL)
        first = add_labelled(frame, sizer, "Catalog &products", lambda: wx.ListBox(frame))
        second = add_labelled(frame, sizer, "&Cart", lambda: wx.ListBox(frame), 1)
        for control, label in ((first, "Catalog &products"), (second, "&Cart")):
            before = control.GetPrevSibling()
            assert isinstance(before, wx.StaticText) and before.GetLabel() == label
        assert first.GetName() == "Catalog products"
    finally:
        frame.Destroy()


def test_a_control_built_beforehand_is_refused(wx_app):
    frame = hidden_frame()
    try:
        with pytest.raises(TypeError):
            add_labelled(frame, wx.BoxSizer(wx.VERTICAL), "x", wx.ListBox(frame))
    finally:
        frame.Destroy()


def _creates_a_label(function):
    for node in ast.walk(function):
        if isinstance(node, ast.Call):
            target = node.func
            if (isinstance(target, ast.Attribute) and target.attr == "StaticText"
                    and isinstance(target.value, ast.Name) and target.value.id == "wx"):
                return True
            if isinstance(target, ast.Name) and target.id == "add_labelled":
                return True
    return False


def _builds_a_wx_window(node):
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name) and node.func.value.id == "wx")


def test_no_label_helper_is_handed_a_control_already_built():
    offenders = []
    for path in sorted((ROOT / "client" / "ui").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        helpers = {node.name for node in ast.walk(tree)
                   if isinstance(node, ast.FunctionDef) and _creates_a_label(node)}
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in helpers | {"add_labelled"}):
                continue
            arguments = list(node.args) + [keyword.value for keyword in node.keywords]
            if any(_builds_a_wx_window(argument) for argument in arguments):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert offenders == [], offenders
