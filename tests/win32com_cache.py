"""One win32com type-library cache per pytest-xdist worker.

win32com keeps the wrappers it generates for COM type libraries (SAPI,
which accessible_output2 loads when client/main.py is imported) in one
folder shared by every Python process: %TEMP%\\gen_py\\<version>, with an
index file, dicts.dat. Importing win32com.client reads that index; a worker
that generates a wrapper writes it. Under `-n auto` one worker read it while
another was rewriting it, so it was empty: "EOFError: Ran out of input" while
collecting a test module (test-build run 43214971811, 2026-11-12), a failure
that has nothing to do with the code under test.

So each worker gets its own folder, kept between runs (the wrappers are
generated once per worker name), before anything imports win32com.client.
A run without xdist keeps win32com's own folder: one process cannot race
itself. tests/conftest.py calls `isolate_gen_py()` first thing.

comtypes (core/spell_checker.py) has the same kind of cache: on the next
run two workers created its %APPDATA% comtypes_cache folder at once and one
got FileExistsError (run 43217824476). `isolate_comtypes_gen()` gives each
worker its own `comtypes.gen` package folder the same way.
"""

import os
import sys
import tempfile


def worker_gen_path(worker: str, version=None, base=None) -> str:
    """The cache folder of `worker` (gw0, gw1, ...) for this Python version."""
    version = version or "%d.%d" % sys.version_info[:2]
    base = base or os.path.join(tempfile.gettempdir(), "winzapp-tests-gen_py")
    return os.path.join(base, worker, version)


def isolate_gen_py(worker=None, base=None, modules=None):
    """Point win32com's cache at the worker's own folder. Returns the folder,
    or None when nothing was changed (no xdist worker, no win32com, or
    win32com.client already imported, when it would be too late)."""
    modules = sys.modules if modules is None else modules
    worker = worker if worker is not None else os.environ.get("PYTEST_XDIST_WORKER", "")
    if not worker or "win32com.client.gencache" in modules:
        return None
    win32com = modules.get("win32com")
    if win32com is None:
        try:
            import win32com  # noqa: F811 - only on Windows
        except ImportError:
            return None
    path = worker_gen_path(worker, base=base)
    os.makedirs(path, exist_ok=True)
    win32com.__gen_path__ = path
    gen_py = modules.get("win32com.gen_py") or getattr(win32com, "gen_py", None)
    if gen_py is not None:
        gen_py.__path__ = [path]
    return path


def isolate_comtypes_gen(worker=None, base=None, modules=None):
    """Give comtypes' generated modules (`comtypes.gen`) the worker's own
    folder. Returns it, or None when nothing was changed (no xdist worker,
    no comtypes, or comtypes.client already imported)."""
    modules = sys.modules if modules is None else modules
    worker = worker if worker is not None else os.environ.get("PYTEST_XDIST_WORKER", "")
    if not worker or "comtypes.client" in modules:
        return None
    comtypes = modules.get("comtypes")
    if comtypes is None:
        try:
            import comtypes  # noqa: F811 - only on Windows
        except ImportError:
            return None
    path = worker_gen_path(worker, base=base or os.path.join(
        tempfile.gettempdir(), "winzapp-tests-comtypes"))
    os.makedirs(path, exist_ok=True)
    init = os.path.join(path, "__init__.py")
    if not os.path.exists(init):
        with open(init, "w", encoding="utf-8") as f:
            f.write("# comtypes.gen of one pytest-xdist worker" + chr(10))
    import types
    gen = types.ModuleType("comtypes.gen")
    gen.__path__ = [path]
    gen.__file__ = init
    modules["comtypes.gen"] = gen
    comtypes.gen = gen
    return path
