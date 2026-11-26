"""Browser reuse without network access, wx windows or browser processes."""

import json
from pathlib import Path
import subprocess

import pytest

from core.browser_cache import prepare_browser_cache

ROOT = Path(__file__).resolve().parents[1]


def browser(cache, version="148.0.0.1", product="chrome", platform="win64", complete=True):
    build = cache / product / f"{platform}-{version}"
    folder = build / f"{product}-{'linux64' if platform == 'linux' else platform}"
    folder.mkdir(parents=True)
    binary = folder / (product + (".exe" if platform.startswith("win") else ""))
    binary.write_bytes(b"executable")
    if complete:
        (folder / "icudtl.dat").write_bytes(b"data")
    return build, binary


@pytest.mark.parametrize("product,platform", [("chrome", "win64"), ("chrome-headless-shell", "linux")])
def test_staging_reuses_an_independent_complete_build(tmp_path, product, platform):
    source, target = tmp_path / "api" / ".cache", tmp_path / "api_staging" / ".cache"
    build, binary = browser(source, product=product, platform=platform)
    prepare_browser_cache(str(target), product, str(source))
    copied = target / binary.relative_to(source)
    assert copied.read_bytes() == binary.read_bytes()
    copied.write_bytes(b"changed staging")
    assert binary.read_bytes() == b"executable"
    assert (target / build.relative_to(source)).is_dir()


def test_in_place_preserves_complete_versions_and_removes_incomplete_ones(tmp_path):
    good, _ = browser(tmp_path)
    broken, _ = browser(tmp_path, "149.0.0.1", complete=False)
    empty = tmp_path / "chrome" / "win64-150.0.0.1"
    empty.mkdir()
    prepare_browser_cache(str(tmp_path), "chrome")
    assert good.is_dir()
    assert not broken.exists()
    assert not empty.exists()


@pytest.mark.parametrize("filename", ["chrome.exe", "icudtl.dat"])
def test_quarantine_stubs_do_not_count_as_complete_builds(tmp_path, filename):
    build, binary = browser(tmp_path)
    (binary.parent / filename).write_bytes(b"")
    prepare_browser_cache(str(tmp_path), "chrome")
    assert not build.exists()


def test_legacy_cache_is_reused_but_broken_source_is_untouched(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    good, _ = browser(source / "puppeteer")
    broken, _ = browser(source, "149.0.0.1", complete=False)
    prepare_browser_cache(str(target), "chrome", str(source))
    assert (target / "chrome" / good.name).is_dir()
    assert not (target / "chrome" / broken.name).exists()
    assert broken.is_dir()


def test_a_failed_copy_does_not_leave_a_partial_build(tmp_path, monkeypatch):
    source, target = tmp_path / "source", tmp_path / "target"
    build, binary = browser(source)

    def fail(source, destination):
        Path(destination).mkdir(parents=True)
        (Path(destination) / "partial").write_bytes(b"partial")
        raise OSError("disk full")

    monkeypatch.setattr("core.browser_cache.shutil.copytree", fail)
    prepare_browser_cache(str(target), "chrome", str(source))
    assert not (target / "chrome" / build.name).exists()
    assert binary.read_bytes() == b"executable"


def test_a_different_version_is_not_renamed_to_the_required_one(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    browser(source, "148.0.0.1")
    prepare_browser_cache(str(target), "chrome", str(source))
    assert (target / "chrome" / "win64-148.0.0.1").is_dir()
    assert not (target / "chrome" / "win64-149.0.0.1").exists()


#: start.js's Chrome selection run in a VM. Puppeteer is what version 25 is:
#: executablePath() answers a Promise (issue #912: called synchronously it
#: made every lookup fail), PUPPETEER_REVISIONS names the build it wants, and
#: @puppeteer/browsers lays the cache out as cache/<browser>/win64-<build>/.
_SELECTION_SCRIPT = r"""
const vm = require('vm');
const path = require('path');
const input = JSON.parse(process.argv[1]);
const fakes = {
  puppeteer: { executablePath: async () => input.required },
  'puppeteer-core': { PUPPETEER_REVISIONS: { chrome: input.build, 'chrome-headless-shell': input.build } },
  '@puppeteer/browsers': { computeExecutablePath: ({ cacheDir, browser, buildId }) =>
    path.join(cacheDir, browser, `win64-${buildId}`, `${browser}-win64`, `${browser}.exe`) },
};
const context = { __dirname: input.dir, console,
  process: { platform: 'win32', env: input.env, hrtime: process.hrtime },
  require: name => fakes[name] || require(name) };
vm.runInNewContext(input.source + '\nresult = findPreferredChrome();', context);
console.log(JSON.stringify(context.result));
"""


def _preferred_chrome(tmp_path, build, env=None):
    node = ROOT / "client" / "node" / "node.exe"
    if not node.is_file():
        pytest.skip("portable Node not installed")
    source = (ROOT / "client" / "api_patches" / "start.js").read_text(encoding="utf-8")
    # Only the real selection functions, stopping before any installer/server.
    functions = source[:source.index("if (!findPreferredChrome())")]
    result = subprocess.run([str(node), "-e", _SELECTION_SCRIPT, json.dumps({
        "dir": str(tmp_path), "required": "unused", "build": build,
        "env": env or {}, "source": functions,
    })], capture_output=True, text=True, timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 0, result.stderr
    # Startup timing emits diagnostic lines before the JSON result.
    return json.loads(result.stdout.splitlines()[-1])


def test_launcher_prefers_puppeteers_required_version_over_an_older_cache(tmp_path):
    browser(tmp_path / ".cache", "148.0.0.1")
    _, required = browser(tmp_path / ".cache", "149.0.0.1")
    assert _preferred_chrome(tmp_path, "149.0.0.1") == str(required)


def test_without_the_required_build_the_cache_walk_still_finds_one(tmp_path):
    _, older = browser(tmp_path / ".cache", "148.0.0.1")
    assert _preferred_chrome(tmp_path, "157.0.0.1") == str(older)


def test_puppeteer_executable_path_is_honoured(tmp_path):
    """Issue #912: a native ARM64 Chrome named in PUPPETEER_EXECUTABLE_PATH
    was never used, because the lookup always failed."""
    browser(tmp_path / ".cache", "149.0.0.1")
    native = tmp_path / "arm64" / "chrome.exe"
    native.parent.mkdir()
    native.write_bytes(b"executable")
    env = {"PUPPETEER_EXECUTABLE_PATH": str(native)}
    assert _preferred_chrome(tmp_path, "149.0.0.1", env) == str(native)


def test_a_missing_configured_browser_falls_back_to_the_cache(tmp_path):
    _, required = browser(tmp_path / ".cache", "149.0.0.1")
    env = {"PUPPETEER_EXECUTABLE_PATH": str(tmp_path / "missing" / "chrome.exe")}
    assert _preferred_chrome(tmp_path, "149.0.0.1", env) == str(required)


def test_the_real_puppeteer_lays_the_cache_out_as_the_fake_does(tmp_path):
    """The fakes above stand in for @puppeteer/browsers; check them against
    the installed one when client/api exists (it is git-ignored)."""
    node = ROOT / "client" / "node" / "node.exe"
    api = ROOT / "client" / "api"
    if not node.is_file() or not (api / "node_modules" / "@puppeteer" / "browsers").is_dir():
        pytest.skip("portable Node or client/api not installed")
    script = r"""
const { computeExecutablePath } = require('@puppeteer/browsers');
const { PUPPETEER_REVISIONS } = require('puppeteer-core');
const build = PUPPETEER_REVISIONS.chrome;
console.log(JSON.stringify({ build, path: computeExecutablePath({ cacheDir: process.argv[1], browser: 'chrome', buildId: build }) }));
"""
    result = subprocess.run([str(node), "-e", script, str(tmp_path / ".cache")], cwd=str(api),
                            capture_output=True, text=True, timeout=30,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 0, result.stderr
    found = json.loads(result.stdout.splitlines()[-1])
    _, expected = browser(tmp_path / ".cache", found["build"])
    assert Path(found["path"]) == expected
