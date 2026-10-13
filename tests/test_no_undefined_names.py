"""No function in the client reads a name that nothing defines.

Reported live: Alt+Shift+T raised ``NameError: name 'can_insert' is not
defined``. ``TranscriptionResultDialog._build_ui`` read a parameter of
``__init__``. The dialog's own tests build a real window, so they only run in
CI (tests never open windows on a developer's machine), and the line had never
run before it reached a user.

A window builder is exactly the code a plain ``pytest`` cannot execute, so
this reads every module instead: a name a function loads must be a local, a
name of an enclosing function, a module-level name or a builtin. It is the one
check of a linter that would have caught it; the project runs no linter.
"""

import builtins
import symtable
from pathlib import Path

import pytest

CLIENT = Path(__file__).resolve().parent.parent / "client"
SKIPPED = ("venv", "api", "node", "__pycache__", "node_modules", "build", "dist")
# Names Python provides in every module without a binding of their own.
IMPLICIT = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__package__", "__spec__", "__loader__",
                                 "__builtins__", "__debug__", "__path__", "__class__", "__annotations__"}


def _modules():
    for path in sorted(CLIENT.rglob("*.py")):
        if not any(part in SKIPPED for part in path.relative_to(CLIENT).parts):
            yield path


def undefined_names(source, filename="<module>"):
    """(name, function, line) for every name a function reads that is neither
    its own, an enclosing function's, the module's nor a builtin."""
    top = symtable.symtable(source, filename, "exec")
    if "*" in source and any(line.strip().startswith("from ") and line.rstrip().endswith("import *")
                             for line in source.splitlines()):
        return []  # a star import defines names this cannot see
    module_names = {symbol.get_name() for symbol in top.get_symbols()
                    if symbol.is_assigned() or symbol.is_imported() or symbol.is_namespace()
                    or symbol.is_declared_global()}
    declared = set()

    def collect(table):
        for symbol in table.get_symbols():
            if symbol.is_declared_global() and symbol.is_assigned():
                declared.add(symbol.get_name())
        for child in table.get_children():
            collect(child)
    collect(top)
    known = module_names | declared | IMPLICIT
    found = []

    def walk(table):
        if table.get_type() == "function":
            for symbol in table.get_symbols():
                name = symbol.get_name()
                if (symbol.is_referenced() and symbol.is_global() and not symbol.is_assigned()
                        and name not in known):
                    found.append((name, table.get_name(), table.get_lineno()))
        for child in table.get_children():
            walk(child)
    walk(top)
    return found


def test_the_check_sees_the_bug_it_was_written_for():
    broken = (
        "class Dialog:\n"
        "    def __init__(self, can_insert=True):\n"
        "        self._build_ui()\n"
        "    def _build_ui(self):\n"
        "        self.button.Show(bool(can_insert))\n")
    assert undefined_names(broken) == [("can_insert", "_build_ui", 4)]
    fixed = broken.replace("self._build_ui()", "self._can_insert = can_insert\n        self._build_ui()") \
                  .replace("bool(can_insert))", "bool(self._can_insert))")
    assert undefined_names(fixed) == []


def test_names_defined_the_usual_ways_are_not_reported():
    source = (
        "import os\n"
        "from typing import Any as Alias\n"
        "LIMIT = 3\n"
        "def outer(parameter):\n"
        "    local = parameter + LIMIT\n"
        "    def inner():\n"
        "        return local, os.sep, Alias, len(__file__)\n"
        "    return [item for item in inner()]\n"
        "def later():\n"
        "    global COUNTER\n"
        "    COUNTER = 1\n"
        "def reader():\n"
        "    return COUNTER, helper()\n"
        "def helper():\n"
        "    return 1\n"
        "class Thing:\n"
        "    attribute = LIMIT\n"
        "    def method(self):\n"
        "        return __class__, Thing.attribute\n")
    assert undefined_names(source) == []


@pytest.mark.parametrize("path", list(_modules()), ids=lambda path: str(path.relative_to(CLIENT)).replace("\\", "/"))
def test_no_function_reads_a_name_nothing_defines(path):
    found = undefined_names(path.read_text(encoding="utf-8"), str(path))
    assert not found, "\n".join(f"{path.name}:{line} {function}() reads undefined name {name!r}"
                                for name, function, line in found)
