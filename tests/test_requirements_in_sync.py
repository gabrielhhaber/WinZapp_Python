"""pip/venv and uv must install the same Python dependencies.

Both workflows are supported: uv reads pyproject.toml (resolved into uv.lock),
while ``pip install -r requirements.txt -r requirements-dev.txt`` is what every
existing checkout, fork and script already runs. Two hand-maintained lists of
the same pins drift the first time somebody bumps one of them, and the
failure it produces is the worst kind — a bug that reproduces on one
contributor's machine and not on CI, because they installed a different
version of the same package. So the two are compared here, including the
version markers (PyAudio has no wheel on Python 3.14, and a marker lost in one
list is an install that fails outright there).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]


def _read_requirements(name: str) -> dict:
    reqs = {}
    for raw in (ROOT / name).read_text(encoding="utf-8").splitlines():
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        assert not line.startswith("-"), (
            f"{name}: pip options and includes ({line!r}) are not understood "
            "by this check — extend it before using one"
        )
        req = Requirement(line)
        key = canonicalize_name(req.name)
        assert key not in reqs, f"{name} lists {req.name} twice"
        reqs[key] = req
    return reqs


def _pyproject() -> dict:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = list(data["project"]["dependencies"])
    for group in data.get("dependency-groups", {}).values():
        declared.extend(group)
    reqs = {}
    for spec in declared:
        req = Requirement(spec)
        key = canonicalize_name(req.name)
        assert key not in reqs, f"pyproject.toml declares {req.name} twice"
        reqs[key] = req
    return reqs


def _pin(req: Requirement) -> str:
    specs = list(req.specifier)
    assert len(specs) == 1 and specs[0].operator == "==", (
        f"{req} is not an exact pin; pyproject.toml pins every dependency "
        "exactly so both workflows install the same build"
    )
    return specs[0].version


def _marker(req: Requirement) -> str:
    return str(req.marker) if req.marker else ""


def test_pyproject_pins_every_dependency_exactly():
    for req in _pyproject().values():
        _pin(req)


def test_requirements_txt_pins_what_pyproject_pins():
    declared = _pyproject()
    mismatches = []
    for key, req in _read_requirements("requirements.txt").items():
        if key not in declared:
            mismatches.append(f"{req.name}: in requirements.txt, missing from pyproject.toml")
            continue
        if _pin(req) != _pin(declared[key]):
            mismatches.append(
                f"{req.name}: requirements.txt {_pin(req)} != pyproject.toml {_pin(declared[key])}"
            )
        if _marker(req) != _marker(declared[key]):
            mismatches.append(
                f"{req.name}: marker {_marker(req)!r} != pyproject.toml {_marker(declared[key])!r}"
            )
    assert not mismatches, mismatches


def test_requirements_dev_ranges_accept_the_pyproject_pins():
    declared = _pyproject()
    problems = []
    for key, req in _read_requirements("requirements-dev.txt").items():
        if key not in declared:
            problems.append(f"{req.name}: in requirements-dev.txt, missing from pyproject.toml")
        elif not req.specifier.contains(_pin(declared[key]), prereleases=True):
            problems.append(
                f"{req.name}: requirements-dev.txt {req.specifier} rejects "
                f"pyproject.toml's {_pin(declared[key])}"
            )
    assert not problems, problems


def test_nothing_uv_installs_is_left_out_of_the_pip_workflow():
    pip_side = set(_read_requirements("requirements.txt")) | set(
        _read_requirements("requirements-dev.txt")
    )
    missing = sorted(set(_pyproject()) - pip_side)
    assert not missing, (
        f"declared in pyproject.toml but not installable with pip -r: {missing}"
    )


# The environment WinZapp is built and run in. Lock edges carry markers
# (appscript and macholib only on macOS), so "what pip would install" is the
# part of uv.lock reachable under this environment, not the whole file.
_WINDOWS_CPYTHON_313 = {
    "sys_platform": "win32",
    "platform_system": "Windows",
    "os_name": "nt",
    "implementation_name": "cpython",
    "platform_python_implementation": "CPython",
    "implementation_version": "3.13.7",
    "python_version": "3.13",
    "python_full_version": "3.13.7",
    "platform_machine": "AMD64",
    "platform_release": "",
    "platform_version": "",
    "extra": "",
}


def _windows_lock_closure() -> dict:
    """{package: version} uv installs on Windows, from the project root down."""
    from packaging.markers import Marker

    entries = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8")).get("package", [])
    packages = {canonicalize_name(pkg["name"]): pkg for pkg in entries}
    # The walk below keys packages by name. If the lock ever records one
    # package at several versions, say so instead of quietly comparing against
    # the wrong one. Extras ARE followed (google-genai asks for
    # google-auth[requests]): an edge with an "extra" also walks that extra's
    # optional-dependencies of the package it points to.
    assert len(packages) == len(entries), (
        "uv.lock now records one package at several versions; teach "
        "_windows_lock_closure() to pick by resolution marker"
    )
    root = next(p for p in packages.values() if p.get("source", {}).get("editable") == ".")

    def edges(pkg, extras=()):
        yield from pkg.get("dependencies", [])
        for group in pkg.get("dev-dependencies", {}).values():
            yield from group
        for extra in extras:
            yield from pkg.get("optional-dependencies", {}).get(extra, [])

    seen = {}
    visited = set()
    pending = [(root, ())]
    while pending:
        pkg, extras = pending.pop()
        for dep in edges(pkg, extras):
            marker = dep.get("marker")
            if marker and not Marker(marker).evaluate(_WINDOWS_CPYTHON_313):
                continue
            key = canonicalize_name(dep["name"])
            dep_extras = tuple(sorted(dep.get("extra", [])))
            seen[key] = str(Version(packages[key]["version"]))
            if (key, dep_extras) in visited:
                continue
            visited.add((key, dep_extras))
            pending.append((packages[key], dep_extras))
    return seen


def test_pip_pins_every_package_uv_installs_on_windows():
    """Declared pins agreeing is not enough: a bump that pulls in a new
    transitive dependency gets it pinned by `uv lock` and nowhere else, and
    `pip install -r` then quietly takes whatever is newest on PyPI."""
    if not (ROOT / "uv.lock").is_file():
        pytest.skip("no uv.lock in this checkout")
    pip_side = {
        key: str(Version(_pin(req)))
        for key, req in _read_requirements("requirements.txt").items()
    }
    problems = []
    for key, version in sorted(_windows_lock_closure().items()):
        if key not in pip_side:
            problems.append(f"{key}=={version}: in uv.lock, not pinned in requirements.txt")
        elif pip_side[key] != version:
            problems.append(f"{key}: uv.lock {version} != requirements.txt {pip_side[key]}")
    assert not problems, problems


def test_uv_lock_resolves_the_same_pins():
    """A pip-only contributor who bumps a pin in both lists but has no uv to
    run `uv lock` would otherwise learn about it from `uv sync --locked`
    failing in CI with a far less specific message."""
    lock_path = ROOT / "uv.lock"
    if not lock_path.is_file():
        pytest.skip("no uv.lock in this checkout")
    locked = {}
    for pkg in tomllib.loads(lock_path.read_text(encoding="utf-8")).get("package", []):
        locked.setdefault(canonicalize_name(pkg["name"]), set()).add(
            str(Version(pkg["version"])) if "version" in pkg else ""
        )
    stale = []
    for key, req in _pyproject().items():
        wanted = str(Version(_pin(req)))
        if wanted not in locked.get(key, set()):
            stale.append(f"{req.name}=={wanted} (uv.lock has {sorted(locked.get(key, []))})")
    assert not stale, f"uv.lock is stale — run `uv lock`: {stale}"
