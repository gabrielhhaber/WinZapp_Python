"""NumPy stays below 2.4: its Windows wheels need an x86-64-v2 processor.

On a machine without it, importing NumPy raises ``RuntimeError: NumPy was
built with baseline optimizations``, the failed C extension can never be
loaded again in the same process, and every later call attempt then dies with
``ImportError: cannot load module more than once per process``. NumPy 2.3
needs nothing beyond SSE2 and still supports Python 3.13.
"""

import tomllib
from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]


def _numpy_requirement(lines):
    for line in lines:
        req = Requirement(line.split("#")[0].strip())
        if req.name.lower() == "numpy":
            return req
    raise AssertionError("numpy is not pinned")


def _allows_2_4(req):
    return any(v in req.specifier for v in ("2.4.0", "2.4.6", "2.5.0"))


def test_pyproject_numpy_stays_below_2_4():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    req = _numpy_requirement(data["project"]["dependencies"])
    assert not _allows_2_4(req), req


def test_requirements_numpy_stays_below_2_4():
    lines = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    lines = [l for l in lines if l.strip() and not l.startswith(("#", "-"))]
    assert not _allows_2_4(_numpy_requirement(lines))
