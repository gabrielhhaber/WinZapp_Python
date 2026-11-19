"""A missing shared COM index must not break speech-driver imports.

Run in a subprocess so no imported COM modules or paths leak into other tests.
No screen reader, speech object, or window is instantiated.
"""

import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="Windows COM cache")
def test_speech_import_ignores_shared_wrappers_without_index(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    wrapper = shared / "C866CA3A-32F7-11D2-9602-00C04F8EE628x0x5x4.py"
    wrapper.write_text("from win32com.client import _get_good_object_\n")
    script = textwrap.dedent('''
        import pathlib
        import sys
        import win32com
        shared = pathlib.Path(sys.argv[1])
        win32com.__gen_path__ = str(shared)
        win32com.gen_py.__path__ = [str(shared)]
        from core.speech_backend import outputs, _com_cache
        from win32com.client import gencache
        assert pathlib.Path(win32com.__gen_path__) != shared
        assert win32com.gen_py.__path__ == [_com_cache.name]
        assert pathlib.Path(_com_cache.name, "dicts.dat").is_file()
        assert not (shared / "dicts.dat").exists()
        # Exercise real wrapper generation, without creating a voice or speaking.
        module = gencache.EnsureModule(
            "{C866CA3A-32F7-11D2-9602-00C04F8EE628}", 0, 5, 4)
        assert module is not None
        assert pathlib.Path(module.__file__).is_relative_to(_com_cache.name)
        assert outputs.auto.Auto is not None
        print(_com_cache.name)
    ''')
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "client"))
    result = subprocess.run(
        [sys.executable, "-c", script, str(shared)],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stderr == ""
    assert "Could not add module" not in result.stdout
    assert "Rebuilding cache" not in result.stdout
    assert wrapper.read_text() == "from win32com.client import _get_good_object_\n"
    assert not Path(result.stdout.strip()).exists()  # cleaned on process exit
