import json
import pytest

from core.api_dependencies import prepare_api_dependencies


def manifest(tmp_path, **overrides):
    package = {
        "version": "2.10.37",
        "dependencies": {"@wppconnect-team/wppconnect": "2.3.4"},
        "devDependencies": {"@babel/runtime": "^7.29.2", "jest": "^29", "husky": "^9",
                            "typescript": "^5", "rimraf": "^6", "@babel/cli": "^7",
                            "@types/jest": "^29", "mongoose": "^8", "redis": "^4",
                            "socket.io-client": "^4"},
        "peerDependencies": {"mongoose": "^8", "redis": "^4", "crypto-js": "^4"},
        "scripts": {"prepare": "husky install", "build": "npm run build:types && npm run build:js",
                    "build:types": "tsc", "build:js": "rimraf dist && babel src --out-dir dist"},
    }
    package.update(overrides)
    path = tmp_path / "package.json"
    path.write_text(json.dumps(package), encoding="utf-8")
    (tmp_path / ".babelrc").write_text('{"plugins": [], "presets": []}')
    return path


def test_precompiled_api_omits_tools_but_keeps_runtime_and_peers(tmp_path):
    path = manifest(tmp_path)
    assert prepare_api_dependencies(str(tmp_path), building=False) == ["--omit=dev"]
    package = json.loads(path.read_text())
    assert package["version"] == "2.10.37"
    assert package["dependencies"]["@babel/runtime"] == "^7.29.2"
    assert package["dependencies"]["mongoose"] == "^8"
    assert package["dependencies"]["crypto-js"] == "^4"
    assert "prepare" not in package["scripts"]


def test_known_build_keeps_compilers_types_and_imported_dev_packages(tmp_path):
    path = manifest(tmp_path)
    source = tmp_path / "src"
    source.mkdir()
    (source / "manager.test.ts").write_text("import { io } from 'socket.io-client';")
    assert prepare_api_dependencies(str(tmp_path), building=True) == ["--include=dev"]
    dev = json.loads(path.read_text())["devDependencies"]
    assert {"typescript", "rimraf", "@babel/cli", "@types/jest", "socket.io-client"} <= dev.keys()
    assert "jest" not in dev and "husky" not in dev
    assert json.loads(path.read_text())["scripts"]["build:types"] == "tsc --noEmit"
    prepare_api_dependencies(str(tmp_path), building=True)
    assert json.loads(path.read_text())["devDependencies"] == dev


@pytest.mark.parametrize("extra", [
    {"postbuild:types": "consume-declarations"},
    {"db:generate": "generate"},
    {"postinstall": "custom-install"},
])
def test_custom_hooks_keep_original_type_build(tmp_path, extra):
    path = manifest(tmp_path)
    package = json.loads(path.read_text())
    package["scripts"].update(extra)
    path.write_text(json.dumps(package))
    prepare_api_dependencies(str(tmp_path), building=True)
    assert json.loads(path.read_text())["scripts"]["build:types"] == "tsc"


def test_compiled_runtime_import_is_promoted_without_adding_test_imports(tmp_path):
    path = manifest(tmp_path, devDependencies={"runtime-lib": "1", "test-lib": "2"})
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "server.js").write_text("require('runtime-lib/subpath');")
    (dist / "server.test.js").write_text("require('test-lib');")
    prepare_api_dependencies(str(tmp_path), building=False)
    deps = json.loads(path.read_text())["dependencies"]
    assert deps["runtime-lib"] == "1"
    assert "test-lib" not in deps


def test_unknown_build_or_generation_script_keeps_development_environment(tmp_path):
    path = manifest(tmp_path, scripts={"build": "custom-compiler", "prepare": "custom-prepare"})
    prepare_api_dependencies(str(tmp_path), building=True)
    assert "jest" in json.loads(path.read_text())["devDependencies"]
    assert json.loads(path.read_text())["scripts"]["prepare"] == "custom-prepare"
    manifest(tmp_path, scripts={"db:generate": "prisma generate"})
    assert prepare_api_dependencies(str(tmp_path), building=False) == ["--include=dev"]


def test_extra_babel_plugin_is_kept_and_unknown_config_falls_back(tmp_path):
    path = manifest(tmp_path, devDependencies={"babel-plugin-custom": "1", "jest": "2"})
    (tmp_path / ".babelrc").write_text('{"env": {"production": {"plugins": ["babel-plugin-custom"]}}}')
    prepare_api_dependencies(str(tmp_path), building=True)
    assert json.loads(path.read_text())["devDependencies"] == {"babel-plugin-custom": "1"}
    path = manifest(tmp_path)
    (tmp_path / "babel.config.js").write_text("module.exports = {};")
    prepare_api_dependencies(str(tmp_path), building=True)
    assert "jest" in json.loads(path.read_text())["devDependencies"]


def test_etarget_from_stale_cache_is_recognised_for_an_online_retry():
    from core.api_dependencies import npm_cached_metadata_is_stale

    assert npm_cached_metadata_is_stale(
        "npm error code ETARGET\nnpm error notarget No matching version found for @babel/runtime@^7.29.10.")
    assert not npm_cached_metadata_is_stale("npm error code ENOTFOUND")
    assert not npm_cached_metadata_is_stale("")
