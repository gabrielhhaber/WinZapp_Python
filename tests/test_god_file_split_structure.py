"""Guards for the split of the two god classes into mixin packages.

MainWindow (client/main.py) reached 35,600 lines and ConversationsPanel
(client/ui/conversations.py) 18,100 in about four months, one feature at a
time, without anyone deciding it should: every change appended to the file
that was already open. They are now assembled from one module per
responsibility (client/main_window/, client/ui/conversation_panel/). These
tests are what stops the same slide from happening again — they fail at the
moment a change would make it worse, which is the only moment anyone is
looking.

When a size budget fails, do not raise the number. Move the new code (or the
oldest unrelated part of the module) into the module that owns it, or a new
one. Raising a budget is a team decision, made in its own PR.
"""

import ast
import inspect
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "client"

# (entry file, mixin package dir, god class, entry-file line budget)
SPLITS = [
    ("main.py", "main_window", "MainWindow", 2_500),
]

#: No single module of a split package may grow past this. The largest one
#: at the time of the split was ~2,650 lines.
MODULE_LINE_BUDGET = 3_000


def _lines(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def _import_class(entry: str, cls: str):
    module = __import__(entry[:-3].replace("/", "."), fromlist=[cls])
    return getattr(module, cls)


@pytest.mark.parametrize("entry,package,cls,budget", SPLITS)
def test_the_entry_file_stays_thin(entry, package, cls, budget):
    lines = _lines(CLIENT / entry)
    assert lines <= budget, (
        f"client/{entry} has {lines} lines (budget {budget}). New {cls} code "
        f"belongs in the client/{package}/ module that owns its responsibility "
        f"(the map is client/{package}/__init__.py), or in a new module there."
    )


@pytest.mark.parametrize("entry,package,cls,budget", SPLITS)
def test_no_split_module_grows_into_a_new_god_file(entry, package, cls, budget):
    too_big = {
        p.name: _lines(p) for p in sorted((CLIENT / package).glob("*.py"))
        if _lines(p) > MODULE_LINE_BUDGET
    }
    assert too_big == {}, (
        f"these client/{package}/ modules passed {MODULE_LINE_BUDGET} lines: {too_big}. "
        "Split the module by responsibility instead of raising the budget."
    )


@pytest.mark.parametrize("entry,package,cls,budget", SPLITS)
def test_no_member_is_defined_by_two_mixins(entry, package, cls, budget):
    """Two mixins defining the same name do not fail — the one earlier in the
    MRO silently wins and the other is dead code that still looks live. In the
    single file the same mistake was at least visible as a duplicate def."""
    klass = _import_class(entry, cls)
    owners = {}
    for base in klass.__mro__:
        if base is klass or base.__module__.split(".")[0] != package:
            continue
        for name in vars(base):
            if name.startswith("__") and name.endswith("__"):
                continue
            owners.setdefault(name, []).append(base.__name__)
    for name in vars(klass):
        if name in owners:
            owners[name].append(cls)
    shadowed = {n: o for n, o in owners.items() if len(o) > 1}
    assert shadowed == {}, f"members defined in more than one place: {shadowed}"


@pytest.mark.parametrize("entry,package,cls,budget", SPLITS)
def test_every_mixin_in_the_package_is_actually_mixed_in(entry, package, cls, budget):
    """A mixin module nobody lists in the class bases is code that never runs."""
    klass = _import_class(entry, cls)
    in_mro = {b.__name__ for b in klass.__mro__}
    defined = set()
    for p in (CLIENT / package).glob("*.py"):
        for node in ast.parse(p.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.ClassDef) and node.name.endswith("Mixin"):
                defined.add(node.name)
    assert defined - in_mro == set()


def _imports_of(path: Path):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module


def test_nothing_under_client_imports_main():
    """`main` is the entry script. Running the app, it is `__main__`, so an
    `import main` anywhere loads main.py a SECOND time as a new module —
    re-running its import-time side effects (the requests.get/post patch, the
    DLL path) and handing back a different MainWindow class. The helpers that
    used to be reached that way live in client/main_window/ now."""
    offenders = []
    for path in CLIENT.rglob("*.py"):
        rel = path.relative_to(CLIENT).as_posix()
        if rel.startswith(("api/", "node/", "lib/")) or "__pycache__" in rel:
            continue
        if any(name == "main" for name in _imports_of(path)):
            offenders.append(rel)
    assert offenders == []


def test_mixin_methods_still_resolve_on_the_class():
    """Stub tests call MainWindow.<method>(stub, ...). That keeps working
    through the MRO; this pins that a sample from every mixin resolves."""
    klass = _import_class("main.py", "MainWindow")
    for base in klass.__mro__:
        if base.__module__.startswith("main_window."):
            for name, member in vars(base).items():
                if inspect.isfunction(member):
                    assert inspect.getattr_static(klass, name) is member
                    break
