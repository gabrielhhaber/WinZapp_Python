"""Helpers for tests that reach MainWindow / ConversationsPanel as a whole.

``MainWindow`` (client/main.py) and ``ConversationsPanel``
(client/ui/conversations.py) used to be single files of 35,000 and 18,000
lines. They are now assembled from mixins, one module per responsibility:

- ``client/main.py`` + ``client/main_window/*.py``
- ``client/ui/conversations.py`` + ``client/ui/conversation_panel/*.py``

Two kinds of test depended on the old single-file layout, and each gets one
helper here instead of every test learning where a method now lives:

* **Source-text checks** ("somewhere in MainWindow, X is called under Y")
  read the whole class. ``main_window_source()`` /
  ``conversations_source()`` return the concatenated text of every file the
  class is built from, in a stable order, so ``in`` checks and ``ast.parse``
  keep working without naming a module.

* **Patching a module global** ("make api_post fail for this test"). A
  method looks a global up in the module it is *defined* in, so patching
  ``main.api_post`` no longer reaches a method that moved to
  ``main_window/sending.py``. ``patch_main_global()`` patches the name in
  every module MainWindow is built from that binds it — the same thing
  ``monkeypatch.setattr(main, name, value)`` meant before the split.
"""

import importlib
import pkgutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "client"


def _source_files(entry: Path, package_dir: Path) -> list:
    return [entry] + sorted(package_dir.glob("*.py"))


def main_window_source_files() -> list:
    """client/main.py followed by every client/main_window/*.py."""
    return _source_files(CLIENT / "main.py", CLIENT / "main_window")


def conversations_source_files() -> list:
    """client/ui/conversations.py followed by every conversation_panel/*.py."""
    return _source_files(CLIENT / "ui" / "conversations.py",
                         CLIENT / "ui" / "conversation_panel")


def _joined(files) -> str:
    return "\n\n".join(f.read_text(encoding="utf-8") for f in files)


def main_window_source() -> str:
    """The whole text MainWindow is built from (valid Python on its own)."""
    return _joined(main_window_source_files())


def conversations_source() -> str:
    """The whole text ConversationsPanel is built from."""
    return _joined(conversations_source_files())


def _method_source(name: str, files) -> str:
    import ast
    for f in files:
        text = f.read_text(encoding="utf-8")
        tree = ast.parse(text)
        lines = text.splitlines()
        for cls in tree.body:
            if not isinstance(cls, ast.ClassDef):
                continue
            for node in cls.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                    start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                    return "\n".join(lines[start - 1:node.end_lineno]) + "\n"
    raise AssertionError(f"no method {name}() in {[f.name for f in files]}")


def main_window_method_source(name: str) -> str:
    """Source of one MainWindow method, wherever its mixin lives.

    Replaces slicing the old single file from ``def name(`` to the *next*
    method's ``def``: after the split the next method can live in another
    module, and the slice would then run on into unrelated code."""
    return _method_source(name, main_window_source_files())


def conversations_method_source(name: str) -> str:
    """Source of one ConversationsPanel method, wherever its mixin lives."""
    return _method_source(name, conversations_source_files())


def _package_modules(entry_name: str, package_name: str) -> list:
    mods = [importlib.import_module(entry_name)]
    package = importlib.import_module(package_name)
    for info in pkgutil.iter_modules(package.__path__):
        mods.append(importlib.import_module(f"{package_name}.{info.name}"))
    return mods


def main_window_modules() -> list:
    """The ``main`` module and every ``main_window.*`` module."""
    return _package_modules("main", "main_window")


def conversations_modules() -> list:
    """The ``ui.conversations`` module and every ``ui.conversation_panel.*``."""
    return _package_modules("ui.conversations", "ui.conversation_panel")


def _patch_global(modules, monkeypatch, name, value):
    patched = False
    for mod in modules:
        if name in vars(mod):
            monkeypatch.setattr(mod, name, value)
            patched = True
    if not patched:
        raise AttributeError(f"no module binds {name!r}")


def patch_main_global(monkeypatch, name: str, value) -> None:
    """``monkeypatch.setattr(main, name, value)`` for the split MainWindow:
    patch *name* in every module MainWindow's code looks it up from."""
    _patch_global(main_window_modules(), monkeypatch, name, value)


def patch_conversations_global(monkeypatch, name: str, value) -> None:
    """Same as patch_main_global() for ConversationsPanel's modules."""
    _patch_global(conversations_modules(), monkeypatch, name, value)
