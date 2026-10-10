"""WinZapp's npm keeps its own cache and config, never the user's.

Reported from Windows: after a WinZapp update, WinZapp worked and the user's
own npm did not. The update relaunched WinZapp as Administrator, and every
npm under it wrote into the user's shared %LOCALAPPDATA%\\npm-cache as
Administrator. See core/npm_environment.py; the relaunch half is
tests/test_update_relaunch.py.
"""

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from core.npm_environment import forwarded_settings, npm_environment, parse_npmrc

ROOT = Path(__file__).resolve().parents[1]


def _home(tmp_path, npmrc=None):
    home = tmp_path / "home"
    home.mkdir()
    if npmrc is not None:
        (home / ".npmrc").write_text(npmrc, encoding="utf-8")
    key = "USERPROFILE" if sys.platform == "win32" else "HOME"
    return {key: str(home), "PATH": "node"}


class TestWinZappsNpmIsItsOwn:
    def test_cache_config_and_update_notice_are_winzapps(self, tmp_path):
        parent = _home(tmp_path)
        npm_dir = tmp_path / "data" / "global" / "npm"
        child = npm_environment(parent, str(npm_dir))
        assert child["npm_config_cache"] == str(npm_dir / "cache")
        assert child["npm_config_userconfig"] == str(npm_dir / "npmrc")
        assert child["npm_config_update_notifier"] == "false"
        assert child["PATH"] == "node"
        assert "npm_config_cache" not in parent, "the caller's mapping is never modified"

    def test_building_the_environment_writes_nothing(self, tmp_path):
        """npm creates its cache itself and reads a missing userconfig as
        empty, so tests and call sites never create directories."""
        npm_environment(_home(tmp_path), str(tmp_path / "npm"))
        assert not (tmp_path / "npm").exists()

    def test_a_user_cache_in_any_spelling_is_replaced_not_duplicated(self, tmp_path):
        """os.environ on Windows upper-cases names; two spellings of one
        variable in a child's environment leave the winner to chance."""
        parent = {**_home(tmp_path), "NPM_CONFIG_CACHE": r"C:\Users\me\AppData\Local\npm-cache",
                  "Npm_Config_Userconfig": "elsewhere"}
        child = npm_environment(parent, str(tmp_path / "npm"))
        names = [k for k in child if k.lower() in ("npm_config_cache", "npm_config_userconfig")]
        assert sorted(names) == ["npm_config_cache", "npm_config_userconfig"]
        assert child["npm_config_cache"] == str(tmp_path / "npm" / "cache")


class TestTheUsersNpmrcStillApplies:
    def test_every_plain_setting_is_forwarded_as_npm_would_export_it(self, tmp_path):
        """npm hands every npmrc key to install scripts as npm_config_*
        (lowercase, - as _); for a key of a-z, 0-9 and - the variable is
        read back as exactly that key."""
        parent = _home(tmp_path, npmrc=(
            "; my settings\n"
            "https-proxy=http://proxy.corp:8080\n"
            "registry=\"https://mirror.corp/npm/\"\n"
            "strict-ssl=false\n"
            "ignore-scripts=true ; inline comment\n"
            "fetch-retries=5\n"
            "proxy-from=${CORP_PROXY}\n"
        ))
        child = npm_environment(parent, str(tmp_path / "npm"))
        assert child["npm_config_https_proxy"] == "http://proxy.corp:8080"
        assert child["npm_config_registry"] == "https://mirror.corp/npm/"
        assert child["npm_config_strict_ssl"] == "false"
        assert child["npm_config_ignore_scripts"] == "true"
        assert child["npm_config_fetch_retries"] == "5"
        # npm expands ${VAR} in values from the environment too (parse-field.js).
        assert child["npm_config_proxy_from"] == "${CORP_PROXY}"
        assert child["npm_config_userconfig"] == str(tmp_path / "npm" / "npmrc")

    @pytest.mark.parametrize("line", [
        # npm reads these literally from a file and they did nothing to npm
        # itself; as npm_config_* they would turn into strict-ssl/registry.
        "strict_ssl=false",
        "Registry=https://evil.example/",
        "STRICT-SSL=false",
        # Underscore keys npm still exported to scripts: only the file keeps
        # that exactly as it was.
        "puppeteer_download_base_url=https://mirror.corp/chrome",
        "msvs_version=2022",
        "CACHE=C:\\elsewhere",
    ])
    def test_a_key_npm_would_read_differently_keeps_the_file(self, tmp_path, line):
        parent = _home(tmp_path, npmrc="registry=https://r/\n" + line + "\n")
        child = npm_environment(parent, str(tmp_path / "npm"))
        assert "npm_config_userconfig" not in child
        assert not [k for k in child if k.lower() in ("npm_config_strict_ssl",
                                                      "npm_config_registry",
                                                      "npm_config_puppeteer_download_base_url")]
        assert child["npm_config_cache"] == str(tmp_path / "npm" / "cache")

    def test_winzapps_own_settings_are_not_taken_from_the_npmrc(self, tmp_path):
        parent = _home(tmp_path, npmrc=(
            "cache=C:\\Users\\me\\npm-cache\nlogs-dir=C:\\logs\n"
            "update-notifier=true\nuserconfig=C:\\other\n"))
        child = npm_environment(parent, str(tmp_path / "npm"))
        assert child["npm_config_cache"] == str(tmp_path / "npm" / "cache")
        assert child["npm_config_update_notifier"] == "false"
        assert child["npm_config_userconfig"] == str(tmp_path / "npm" / "npmrc")
        assert "npm_config_logs_dir" not in child

    def test_a_section_keeps_the_file(self, tmp_path):
        """A section becomes an object setting of that name in npm; only
        the file reproduces that."""
        parent = _home(tmp_path, npmrc="registry=https://r/\n[project]\npython=x\n")
        child = npm_environment(parent, str(tmp_path / "npm"))
        assert "npm_config_userconfig" not in child and "npm_config_registry" not in child

    def test_a_key_the_apis_own_npmrc_sets_is_not_forwarded(self, tmp_path):
        """wppconnect-server ships legacy-peer-deps=true; it outranked the
        user's file, but would lose to an npm_config_* variable."""
        api = tmp_path / "api"
        api.mkdir()
        (api / ".npmrc").write_text("legacy-peer-deps=true", encoding="utf-8")
        parent = _home(tmp_path, npmrc="legacy-peer-deps=false\nregistry=https://r/\n")
        child = npm_environment(parent, str(tmp_path / "npm"), str(api))
        assert "npm_config_legacy_peer_deps" not in child
        assert child["npm_config_registry"] == "https://r/"


class TestAnNpmrcThatIsNotCleanText:
    """Reported in review: Windows PowerShell 5.1's `echo x > ~/.npmrc`
    writes UTF-16 with a BOM. Read as UTF-8 it gave names and values with
    NUL bytes, every Popen raised "embedded null byte", and Node never
    started. npm reads it as UTF-8 too and gets nothing usable from it, so
    leaving it to npm changes nothing."""

    def test_a_utf16_file_is_left_to_npm(self, tmp_path):
        parent = _home(tmp_path)
        npmrc = os.path.join(next(iter(v for k, v in parent.items() if k != "PATH")), ".npmrc")
        with open(npmrc, "wb") as fh:
            fh.write("registry=https://r/\r\n".encode("utf-16"))
        child = npm_environment(parent, str(tmp_path / "npm"))
        assert "npm_config_userconfig" not in child
        assert not any("\x00" in k or "\x00" in v for k, v in child.items())

    def test_a_utf8_bom_is_dropped_from_the_first_key_as_npm_does(self, tmp_path):
        """npm's ini trims with JavaScript's trim(), which removes U+FEFF:
        ini.decode('\\ufeffregistry=x') gives {registry: 'x'}."""
        parent = _home(tmp_path)
        npmrc = os.path.join(next(iter(v for k, v in parent.items() if k != "PATH")), ".npmrc")
        with open(npmrc, "wb") as fh:
            fh.write(b"\xef\xbb\xbfregistry=https://r/\nstrict-ssl=false\n")
        child = npm_environment(parent, str(tmp_path / "npm"))
        assert child["npm_config_registry"] == "https://r/"
        assert child["npm_config_strict_ssl"] == "false"

    def test_a_utf8_bom_before_a_comment_or_section_is_still_npms(self):
        assert parse_npmrc("\ufeff; comment\nregistry=y\n") == [(None, "registry", "y", False)]
        # Not a section for npm either: the BOM is before the [.
        assert parse_npmrc("\ufeff[sec]\nk=v\n") == [(None, "[sec]", True, False),
                                                    (None, "k", "v", False)]

    @pytest.mark.parametrize("text", ["registry=https://r/\x00\n", "proxy=a\x1bb\n",
                                      "re\x00gistry=x\n"])
    def test_a_control_character_is_never_forwarded(self, text):
        assert forwarded_settings(text) == ({}, True)

    def test_invalid_utf8_is_left_to_npm(self, tmp_path):
        parent = _home(tmp_path)
        npmrc = os.path.join(next(iter(v for k, v in parent.items() if k != "PATH")), ".npmrc")
        with open(npmrc, "wb") as fh:
            fh.write(b"registry=https://r/\xff\n")
        child = npm_environment(parent, str(tmp_path / "npm"))
        assert "npm_config_userconfig" not in child and "npm_config_registry" not in child


class TestTheEnvironmentStillRanksFirst:
    def test_proxy_variables_in_the_environment_pass_through(self, tmp_path):
        parent = {**_home(tmp_path), "HTTPS_PROXY": "http://p:3128", "NO_PROXY": "localhost"}
        child = npm_environment(parent, str(tmp_path / "npm"))
        assert child["HTTPS_PROXY"] == "http://p:3128" and child["NO_PROXY"] == "localhost"

    def test_a_setting_already_in_the_environment_wins_over_the_npmrc(self, tmp_path):
        """As it did before: npm ranks the environment above the user config."""
        parent = {**_home(tmp_path, npmrc="https-proxy=http://from-file:1\n"),
                  "NPM_CONFIG_HTTPS_PROXY": "http://from-env:2"}
        child = npm_environment(parent, str(tmp_path / "npm"))
        assert child["NPM_CONFIG_HTTPS_PROXY"] == "http://from-env:2"
        assert "npm_config_https_proxy" not in child

    def test_an_explicit_userconfig_is_the_file_that_is_read(self, tmp_path):
        custom = tmp_path / "custom.npmrc"
        custom.write_text("registry=https://mirror.example/\n", encoding="utf-8")
        parent = {**_home(tmp_path), "npm_config_userconfig": str(custom)}
        child = npm_environment(parent, str(tmp_path / "npm"))
        assert child["npm_config_registry"] == "https://mirror.example/"

    @pytest.mark.parametrize("line", [
        "//mirror.corp/npm/:_authToken=secret",
        "_auth=dXNlcjpwYXNz",
        "_authToken=abc",
        "@corp:registry=https://mirror.corp/",
        "ca[]=-----BEGIN CERTIFICATE-----",
        "cert=-----BEGIN CERTIFICATE-----",
        "key=-----BEGIN PRIVATE KEY-----",
        "keyfile=C:\\keys\\me.pem",
        "username=me",
        "auth-type=legacy",
        "my-registry-token=abc",
        "proxy-password=hunter2",
        "noproxy=",
        "obj='{\"a\": 1}'",
    ])
    def test_an_npmrc_that_cannot_be_forwarded_is_still_read(self, tmp_path, line):
        """Losing an install that worked through a private mirror is worse
        than reading the file; the cache is WinZapp's own either way."""
        parent = _home(tmp_path, npmrc="registry=https://mirror.corp/npm/\n" + line + "\n")
        child = npm_environment(parent, str(tmp_path / "npm"))
        assert "npm_config_userconfig" not in child
        assert "npm_config_registry" not in child
        assert child["npm_config_cache"] == str(tmp_path / "npm" / "cache")


# The same file through npm's own parser (ini 5.0.0 from npm 10.9.7, Node
# 22.22.2): `ini.decode()` gave exactly this object.
SAMPLE_NPMRC = """# comment
; comment
registry = https://r/ ;trailing
puppeteer_download_base_url="https://m/x#y"
python='C:\\Py\\python.exe'
msvs_version=2022
foo
esc=a\\;b\\#c\\d
q1='true'
q2="123"
empty=
spaced key = v
  [notsection]
dup=1
dup=2
arr[]=a
arr[]=b
[sec]
inner=1
"""
SAMPLE_DECODED = {
    "registry": "https://r/", "puppeteer_download_base_url": "https://m/x#y",
    "python": "C:\\Py\\python.exe", "msvs_version": "2022", "foo": True,
    "esc": "a;b#c\\d", "q1": True, "q2": "123", "empty": "", "spaced key": "v",
    "[notsection]": True, "dup": "2", "arr": ["a", "b"], "sec": {"inner": "1"},
}


def _as_ini_object(entries):
    out = {}
    for section, key, value, is_array in entries:
        target = out if section is None else out.setdefault(section, {})
        if is_array:
            target.setdefault(key, []).append(value)
        else:
            target[key] = value
    return out


class TestTheParserIsNpms:
    def test_it_decodes_like_npms_ini_package(self):
        assert _as_ini_object(parse_npmrc(SAMPLE_NPMRC)) == SAMPLE_DECODED

    def test_against_the_bundled_npm_when_there_is_one(self, tmp_path):
        """Live check against client/node's own npm, when present."""
        node = ROOT / "client" / "node" / ("node.exe" if sys.platform == "win32" else "node")
        ini = ROOT / "client" / "node" / "node_modules" / "npm" / "node_modules" / "ini"
        if not (node.is_file() and ini.is_dir()):
            pytest.skip("no bundled Node.js/npm in client/node")
        sample = tmp_path / "npmrc"
        sample.write_text(SAMPLE_NPMRC, encoding="utf-8")
        script = ("const ini=require(process.argv[1]);"
                  "console.log(JSON.stringify(ini.decode(require('fs').readFileSync(process.argv[2],'utf8'))))")
        out = subprocess.run([str(node), "-e", script, str(ini), str(sample)],
                             capture_output=True, text=True, timeout=30,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        assert out.returncode == 0, out.stderr
        assert json.loads(out.stdout) == _as_ini_object(parse_npmrc(SAMPLE_NPMRC))

    def test_comments_and_blank_lines_are_skipped(self):
        assert parse_npmrc("# a\n; b\n\n   \nregistry = r\n") == [(None, "registry", "r", False)]
        assert forwarded_settings("") == ({}, False)


# ── Every place WinZapp runs npm goes through the helper ───────────────────

_NPM_NAMES = {"npm_cmd", "npm_cli", "npm_bin", "win_npm"}
_SPAWNERS = {"Popen", "run", "check_output", "call", "_run", "_run_subprocess"}
_CALL_SITES = [
    ROOT / "client" / "main_window" / "wpp_server.py",
    ROOT / "client" / "main_window" / "runtime_setup.py",
    ROOT / "client" / "ui" / "dialogs" / "api_setup.py",
    ROOT / "setup_api.py",
]


def _npm_spawns(path):
    """(function name, call) for every call whose arguments name an npm
    command — subprocess.run/Popen, ApiSetupDialog._run_subprocess,
    setup_api._run."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for call in ast.walk(func):
            if not isinstance(call, ast.Call) or not call.args:
                continue
            callee = getattr(call.func, "attr", None) or getattr(call.func, "id", "")
            if callee not in _SPAWNERS:
                continue
            names = {n.id for n in ast.walk(call.args[0]) if isinstance(n, ast.Name)}
            if names & _NPM_NAMES:
                yield func, call


class TestEveryNpmRunUsesIt:
    @pytest.mark.parametrize("path", _CALL_SITES, ids=lambda p: p.name)
    def test_every_npm_spawn_passes_an_environment_from_the_helper(self, path):
        found = list(_npm_spawns(path))
        assert found, f"no npm spawn found in {path.name} — update this guard"
        for func, call in found:
            assert any(k.arg == "env" for k in call.keywords), (
                f"{path.name}:{call.lineno} runs npm without env= "
                "(core/npm_environment.py)")
            body = ast.get_source_segment(path.read_text(encoding="utf-8"), func) or ""
            if func.name == "node_runtime_needs_download":
                continue  # takes the environment as a parameter; its caller is checked below
            assert "npm_environment(" in body or "_npm_env()" in body, (
                f"{path.name}:{func.name} runs npm with an environment not "
                "built by npm_environment()")

    def test_the_npm_probe_is_given_the_helpers_environment(self):
        src = (ROOT / "client" / "main_window" / "wpp_server.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        calls = [c for c in ast.walk(tree) if isinstance(c, ast.Call)
                 and getattr(c.func, "id", "") == "node_runtime_needs_download"]
        assert calls
        for call in calls:
            env = [k for k in call.keywords if k.arg == "npm_env"]
            assert env and "npm_environment(" in ast.get_source_segment(src, env[0].value)

    def test_node_itself_gets_it_for_start_js_npx_fallback(self):
        """start.js runs npx on its own when the browser is missing; that
        npx inherits Node's environment."""
        src = (ROOT / "client" / "main_window" / "wpp_server.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        func = next(f for f in ast.walk(tree)
                    if isinstance(f, ast.FunctionDef) and f.name == "_start_wpp_background")
        popens = [c for c in ast.walk(func) if isinstance(c, ast.Call)
                  and getattr(c.func, "attr", "") == "Popen"]
        assert len(popens) == 1
        env = next(k for k in popens[0].keywords if k.arg == "env")
        assert "npm_environment(" in ast.get_source_segment(src, env.value)
        start_js = (ROOT / "client" / "api_patches" / "start.js").read_text(encoding="utf-8")
        fallback = start_js.split("[chrome-install]", 1)[1].split("execSync(npxCmd", 1)[0]
        assert "...process.env" in fallback, (
            "start.js's npx must keep inheriting process.env, or it loses "
            "WinZapp's npm cache and config")


class TestTheHeadlessShellDownloadRunsWithIt:
    """Behavioural: the one npm spawn reachable through a plain stub."""

    def test_puppeteer_browsers_install_gets_winzapps_cache(self, tmp_path, monkeypatch):
        import main
        from main import MainWindow
        from tests.god_modules import patch_main_global

        (tmp_path / "api" / ".cache").mkdir(parents=True)
        patch_main_global(monkeypatch, "resource_path", lambda *p: str(tmp_path.joinpath(*p)))
        patch_main_global(monkeypatch, "global_dir", lambda *p: str(tmp_path.joinpath("global", *p)))

        class _Stub:
            find_headless_shell = lambda self: None
            iter_incomplete_browsers = lambda self: iter(())
            ensure_headless_shell_installed = MainWindow.ensure_headless_shell_installed

        seen = {}

        class _Proc:
            returncode = 0

            def communicate(self, timeout=None):
                return b"", b""

        def _popen(cmd, **kw):
            seen["env"] = kw.get("env") or {}
            return _Proc()

        monkeypatch.setattr(main.subprocess, "Popen", _popen)
        _Stub().ensure_headless_shell_installed()
        assert seen["env"]["npm_config_cache"] == str(tmp_path / "global" / "npm" / "cache")
        assert seen["env"]["npm_config_update_notifier"] == "false"
