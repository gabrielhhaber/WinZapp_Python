"""The suite must not put focusable windows on the desktop.

Running the tests on a machine with NVDA active crashed NVDA repeatedly. Its
own traceback named the mechanism precisely:

    event_gainFocus -> reportFocus -> getObjectPropertiesSpeech
      -> behaviors.getDialogText -> IAccessible._get_children
      -> oleacc.AccessibleObjectFromEvent

NVDA took focus on one of the suite's throwaway wx windows and was still
enumerating its children over COM when the test destroyed it, so the object it
was reading vanished mid-call. A single run creates and destroys dozens of
these windows, which makes that race very easy to lose — and it hits a user
who is not even running the tests, since focus is global.

Frames now go through conftest.hidden_frame(): real windows, fully functional
as parents, but off-screen tool windows with no taskbar button, so they never
become foreground and no focus event is raised for a screen reader to chase.
This test keeps them that way.

Dialog subclasses own their own construction and cannot be positioned from the
outside, so the handful of modules that build one carry the `wxgui` marker
instead — deselect them (`-m "not wxgui"`) when running this suite on a
machine somebody is actually using.
"""

import ast
import pathlib

import pytest

TESTS = pathlib.Path(__file__).resolve().parent

# This file's own docstring quotes the call, and conftest explains it.
_EXEMPT = {"test_no_desktop_visible_windows.py", "conftest.py"}


def _test_modules():
    return sorted(p for p in TESTS.glob("test_*.py") if p.name not in _EXEMPT)


@pytest.mark.parametrize("path", _test_modules(), ids=lambda p: p.name)
def test_no_module_builds_a_bare_top_level_frame(path):
    """`wx.Frame(None)` is a normal top-level window — taskbar button, real
    foreground transitions, and a focus event a screen reader will chase.
    Use conftest.hidden_frame() instead."""
    source = path.read_text(encoding="utf-8")
    assert "wx.Frame(None)" not in source, (
        f"{path.name} constructs a bare top-level wx.Frame. Use "
        f"hidden_frame() from tests.conftest — a plain wx.Frame(None) steals "
        f"focus from whoever is using the machine and can crash their screen "
        f"reader when the test destroys it."
    )


class TestTheMarkerIsRegisteredAndUsed:
    def test_pytest_ini_declares_the_marker(self):
        ini = (TESTS.parent / "pytest.ini").read_text(encoding="utf-8")
        assert "wxgui:" in ini, "the wxgui marker must be declared in pytest.ini"

    def test_every_module_building_a_real_dialog_carries_it(self):
        """A new module that constructs a real Dialog subclass without the
        marker silently reintroduces the crash for anyone running the full
        suite locally."""
        unmarked = []
        for path in _test_modules():
            source = path.read_text(encoding="utf-8")
            if "pytest.mark.wxgui" in source:
                continue
            try:
                tree = ast.parse(source)
            except SyntaxError:  # pragma: no cover - caught elsewhere
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = getattr(func, "id", None) or getattr(func, "attr", None)
                if not name or not name.endswith("Dialog"):
                    continue
                # A local stub/fake is not a real window.
                if f"class {name}" in source:
                    continue
                unmarked.append(f"{path.name}: {name}(...)")
                break
        assert not unmarked, (
            "these modules construct a real wx dialog but are not marked "
            f"`wxgui`: {unmarked}"
        )
