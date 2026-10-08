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
instead. The default run skips them; only CI enables `--run-wx-gui`.
"""

import ast
import pathlib
import re

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
                command = _pytest_command_in_workflow_line(line)
                if command is None:
                    continue
                if "--run-wx-gui" not in command:
                    offenders.append(f"{wf.name}:{i}: {command}")
        assert not offenders, (
            "these CI steps run pytest without --run-wx-gui, so the wxgui "
            f"tests are skipped there too and nothing covers them: {offenders}"
        )

    def test_launcher_prefixes_do_not_blind_the_guard(self):
        """`uv run pytest` used to read as "not pytest", which silently turned
        the check above off the day CI moved to uv."""
        for line in (
            "pytest",
            "pytest -q",
            "uv run pytest -q",
            "uv run test -q",
            "uv run --locked pytest",
            "uv run --frozen test",
            "uv run python -m pytest",
            "uv run -m pytest",
            r"venv\Scripts\python.exe -m pytest -q",
            r"venv\Scripts\pytest.exe -q",
            "python -X utf8 -m pytest",
            "py -m pytest",
        ):
            assert _as_pytest_invocation(line) is not None, line
        # The shell builtin must not be mistaken for the `uv run test` shortcut.
        for line in (
            'test -s "$f" || exit 1',
            "pip install pytest_asyncio",
            "uv sync --locked",
            'python -c "import wx, pytest_asyncio"',
        ):
            assert _as_pytest_invocation(line) is None, line

    def test_workflow_line_shapes(self):
        for line in (
            "        run: uv run pytest",
            "      - run: uv run pytest",
            "      - run: pytest -q",
            "          uv run python -m pytest",
        ):
            assert _pytest_command_in_workflow_line(line) is not None, line
        for line in (
            "      - name: Run pytest",
            "        name: Run pytest",
            "      # run: pytest",
        ):
            assert _pytest_command_in_workflow_line(line) is None, line


def _pytest_command_in_workflow_line(line):
    """The pytest command a workflow line runs, or None.

    Both shapes: `run: pytest ...` on one line, and a bare `pytest ...` inside
    a `run: |` block — the block form is the natural way somebody adds a
    second command later, and a guard that misses it fails silently in the one
    direction that matters.
    """
    stripped = line.strip()
    if stripped.startswith("#"):
        return None
    # `- run: pytest` is a step with no name; the list marker must not make it
    # read as "some other YAML key" below.
    if stripped.startswith("- "):
        stripped = stripped[2:].lstrip()
    if stripped.startswith("run:"):
        command = stripped[len("run:"):].strip()
    elif re.match(r"^[A-Za-z_][\w-]*:(?:\s|$)", stripped):
        # Any other YAML key: `name: Run pytest` names a step, it runs nothing.
        return None
    else:
        command = stripped
    return _as_pytest_invocation(command)


def _as_pytest_invocation(command):
    """`command` if it runs the test suite, however it is launched, else None.

    Matches `pytest` as a whole command token anywhere in the line rather than
    a list of launcher prefixes: every prefix list so far (`uv run`,
    `python -m`) missed the next idiom somebody wrote.
    """
    if re.search(r"(?:^|[\s\\/])pytest(?:\.exe)?(?:\s|$)", command):
        return command
    if re.match(r"^uv run(?:\s+--?\S+)*\s+test(?:\s|$)", command):
        return command
    return None


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


def _is_wx(node):
    """`wx`, `wx.adv`, ... or `sys.modules["wx..."]`."""
    while isinstance(node, ast.Attribute):
        node = node.value
    if isinstance(node, ast.Name):
        return node.id == "wx"
    return (
        isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, str)
        and node.slice.value.split(".")[0] == "wx"
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "modules"
    )


def _import_time_wx_assignments(body):
    """Lines that assign onto wx while the module is imported. Function and
    class bodies run later (and can use monkeypatch); an `except ImportError`
    handler only runs when wx is not installed, so it stubs a fake module, not
    the real one."""
    lines = []
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Attribute) and _is_wx(t.value) for t in targets):
                lines.append(node.lineno)
        for field in ("body", "orelse", "finalbody"):
            lines += _import_time_wx_assignments(getattr(node, field, None) or [])
        for handler in getattr(node, "handlers", None) or []:
            name = handler.type
            if not (isinstance(name, ast.Name) and name.id in ("ImportError", "ModuleNotFoundError")):
                lines += _import_time_wx_assignments(handler.body)
    return lines


@pytest.mark.parametrize("path", _test_modules(), ids=lambda p: p.name)
def test_no_module_changes_the_real_wx_for_the_tests_after_it(path):
    """`wx.CallAfter = <run now>` at module level stays in force for the rest
    of the run. test_group_presence_isgroup_fallback.py did exactly that, and a
    later pairing test's background thread then ran connect.py's error handler
    on the spot and showed a real wx.MessageBox on the user's desktop. Patch wx
    inside a test or fixture with monkeypatch, which undoes it afterwards."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    lines = _import_time_wx_assignments(tree.body)
    assert not lines, (
        f"{path.name} line(s) {lines} change wx when the module is imported, "
        f"for every test that runs after it. Use monkeypatch in a fixture."
    )
