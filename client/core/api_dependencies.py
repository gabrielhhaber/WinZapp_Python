"""End-user npm installs need runtime libraries and, when building, compilers.

Keep the upstream version/ranges. Unknown build or generation scripts retain
their full development environment instead of guessing their requirements.
"""

import json
from pathlib import Path
import re


BUILD_TOOLS = {
    "typescript", "rimraf", "@babel/cli", "@babel/core",
    "@babel/plugin-transform-runtime", "@babel/preset-env", "@babel/preset-typescript",
}
_IMPORT = re.compile(r'''(?:require\s*\(\s*|from\s+|import\s+)["']([^"']+)["']''')


def npm_cached_metadata_is_stale(stderr: str) -> bool:
    """True when npm found no version a range asks for (ETARGET/notarget).

    With --prefer-offline that usually means the cached registry metadata
    predates a release upstream already depends on, so one online retry fixes it.
    """
    text = stderr or ""
    return "ETARGET" in text or "notarget" in text


def _babel_tools(root: Path, declared: dict) -> set[str] | None:
    """Only trim a JSON Babel configuration whose module names are explicit."""
    if any(root.glob("babel.config.*")) or any(root.glob(".babelrc.*")):
        return None
    path = root / ".babelrc"
    if not path.is_file():
        return None
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
        names = set()
        def visit(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in ("plugins", "presets"):
                        for entry in item:
                            name = entry[0] if isinstance(entry, list) else entry
                            if not isinstance(name, str) or name not in declared:
                                raise ValueError("unknown Babel module")
                            names.add(name)
                    else:
                        visit(item)
        visit(config)
        return names
    except (ValueError, TypeError, OSError):
        return None


def _imports(root: Path, *, runtime: bool) -> set[str]:
    packages = set()
    if not root.is_dir():
        return packages
    for path in root.rglob("*"):
        if path.suffix not in (".js", ".ts", ".tsx"):
            continue
        if runtime and ("tests" in path.parts or ".test." in path.name or ".spec." in path.name):
            continue
        for name in _IMPORT.findall(path.read_text(encoding="utf-8", errors="replace")):
            if not name.startswith((".", "/", "node:")):
                packages.add("/".join(name.split("/")[:2]) if name.startswith("@") else name.split("/")[0])
    return packages


def prepare_api_dependencies(api_dir: str, *, building: bool) -> list[str]:
    """Prepare this installation's manifest and return explicit npm flags."""
    root = Path(api_dir)
    manifest = root / "package.json"
    if not manifest.is_file():
        return ["--include=dev"]  # npm itself reports a missing manifest.
    package = json.loads(manifest.read_text(encoding="utf-8"))
    dev = package.get("devDependencies", {})
    deps = package.setdefault("dependencies", {})
    # Upstream peers are used by optional storage providers; --legacy-peer-deps
    # does not install them automatically. Babel's helpers run in dist/ too.
    runtime = set(package.get("peerDependencies", {})) | {"@babel/runtime"}
    runtime |= _imports(root / "dist", runtime=True)
    for name in runtime:
        if name not in deps:
            version = dev.get(name) or package.get("peerDependencies", {}).get(name)
            if version:
                deps[name] = version
    scripts = package.get("scripts", {})
    known_build = (scripts.get("build") == "npm run build:types && npm run build:js"
                   and scripts.get("build:types") in ("tsc", "tsc --noEmit")
                   and scripts.get("build:js", "").startswith("rimraf dist && babel src "))
    lifecycle = any(key in scripts for key in ("preinstall", "install", "postinstall"))
    lifecycle |= bool(scripts.get("prepare") and scripts["prepare"].strip() not in ("husky", "husky install"))
    needs_dev = building or "db:generate" in scripts or lifecycle
    build_hooks = any(key in scripts for key in (
        "prebuild", "postbuild", "prebuild:types", "postbuild:types", "prebuild:js", "postbuild:js"))
    babel = _babel_tools(root, {**dev, **deps}) if building else None
    if building and known_build and babel is not None and "db:generate" not in scripts and not lifecycle and not build_hooks:
        # Babel clears dist before emitting JavaScript, discarding tsc's
        # declarations. Keep type checking without writing those files.
        scripts["build:types"] = "tsc --noEmit"
        keep = BUILD_TOOLS | babel | _imports(root / "src", runtime=False)
        package["devDependencies"] = {
            name: version for name, version in dev.items()
            if name in keep or name.startswith("@types/")
        }
    # Husky's root prepare hook otherwise invokes a development-only executable
    # even during --omit=dev. Dependency lifecycle scripts remain enabled.
    if scripts.get("prepare", "").strip() in ("husky", "husky install"):
        del scripts["prepare"]
    manifest.write_text(json.dumps(package, indent=2) + "\n", encoding="utf-8")
    return ["--include=dev" if needs_dev else "--omit=dev"]
