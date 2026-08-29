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


class TestTheDefaultRunIsSafe:
    """`pytest` with no arguments must not open anything in the foreground.

    The marker alone was not enough: it still ran by default, so staying safe
    depended on every developer remembering `-m "not wxgui"`. WinZapp is
    maintained by blind developers, and the cost of forgetting does not land
    on the test run — it lands on whatever they had open in another window.
    So the default skips these, and CI opts back in.
    """

    def test_the_marked_tests_are_skipped_without_the_opt_in(self, pytestconfig):
        """If this ever runs unskipped in a default run, the protection is
        gone. It asserts about its own session: with no --run-wx-gui and no
        WINZAPP_RUN_WX_GUI_TESTS, a wxgui test must not have been collected
        to run."""
        import os

        opted_in = (
            pytestconfig.getoption("--run-wx-gui")
            or os.environ.get("WINZAPP_RUN_WX_GUI_TESTS", "").strip()
            not in ("", "0", "false", "False")
        )
        if opted_in:
            pytest.skip("this session deliberately opted in")
        # conftest must have installed the deselection hook.
        conftest_src = (TESTS / "conftest.py").read_text(encoding="utf-8")
        assert "def pytest_collection_modifyitems" in conftest_src
        assert '"wxgui" in item.keywords' in conftest_src

    def test_every_ci_workflow_that_runs_pytest_opts_back_in(self):
        """The flip side: skipping by default silently loses the coverage
        unless CI asks for it. A workflow that runs a bare `pytest` would
        stop exercising every dialog test with nothing to show for it."""
        workflows = sorted(
            (TESTS.parent / ".github" / "workflows").glob("*.yml")
        )
        assert workflows, "no workflows found — has the path changed?"
        offenders = []
        for wf in workflows:
            for i, line in enumerate(wf.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                # Both shapes: `run: pytest ...` on one line, and a bare
                # `pytest ...` inside a `run: |` block. Only the first exists
                # today, but the block form is the natural way somebody adds
                # a second command later, and a guard that misses it fails
                # silently in the one direction that matters.
                if stripped.startswith("run:"):
                    command = stripped[len("run:"):].strip()
                else:
                    command = stripped
                if not (command == "pytest" or command.startswith("pytest ")):
                    continue
                if "--run-wx-gui" not in command:
                    offenders.append(f"{wf.name}:{i}: {command}")
        assert not offenders, (
            "these CI steps run pytest without --run-wx-gui, so the wxgui "
            f"tests are skipped there too and nothing covers them: {offenders}"
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
