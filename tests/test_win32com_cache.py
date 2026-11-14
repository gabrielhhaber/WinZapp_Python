"""tests/win32com_cache.py: each xdist worker keeps its own win32com cache
(the shared one's index was read half-written in CI, 2026-11-12)."""
import os
from types import ModuleType

from tests.win32com_cache import isolate_comtypes_gen, isolate_gen_py, worker_gen_path


def _win32com():
    win32com, gen_py = ModuleType("win32com"), ModuleType("win32com.gen_py")
    win32com.__gen_path__, gen_py.__path__ = "shared", ["shared"]
    win32com.gen_py = gen_py
    return {"win32com": win32com, "win32com.gen_py": gen_py}


def test_each_worker_gets_its_own_folder(tmp_path):
    modules = _win32com()
    path = isolate_gen_py("gw3", base=str(tmp_path), modules=modules)
    assert path == worker_gen_path("gw3", base=str(tmp_path)) and os.path.isdir(path)
    assert modules["win32com"].__gen_path__ == path
    assert modules["win32com.gen_py"].__path__ == [path]
    assert worker_gen_path("gw1", base=str(tmp_path)) != path


def test_a_run_without_workers_keeps_win32coms_own_folder(tmp_path):
    modules = _win32com()
    assert isolate_gen_py("", base=str(tmp_path), modules=modules) is None
    assert modules["win32com"].__gen_path__ == "shared"


def test_too_late_once_the_cache_was_read(tmp_path):
    modules = _win32com()
    modules["win32com.client.gencache"] = ModuleType("win32com.client.gencache")
    assert isolate_gen_py("gw0", base=str(tmp_path), modules=modules) is None
    assert modules["win32com"].__gen_path__ == "shared"


def test_comtypes_generates_into_the_workers_own_package(tmp_path):
    comtypes = ModuleType("comtypes")
    modules = {"comtypes": comtypes}
    path = isolate_comtypes_gen("gw2", base=str(tmp_path), modules=modules)
    assert path == worker_gen_path("gw2", base=str(tmp_path))
    assert os.path.isfile(os.path.join(path, "__init__.py"))
    assert modules["comtypes.gen"].__path__ == [path] and comtypes.gen is modules["comtypes.gen"]
    assert isolate_comtypes_gen("", base=str(tmp_path), modules={"comtypes": comtypes}) is None
    late = {"comtypes": ModuleType("comtypes"), "comtypes.client": ModuleType("comtypes.client")}
    assert isolate_comtypes_gen("gw2", base=str(tmp_path), modules=late) is None
