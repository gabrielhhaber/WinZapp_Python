"""Tests for the welcome.js latest-version ESM-require patch.

Bug: welcome.js's `const latest_version_1 = __importDefault(require("latest-
version"));` throws ERR_REQUIRE_ESM on Node 20+, since `latest-version` is a
pure-ESM package as of its own more recent majors — crashing the whole
WPPConnect Server at startup. WinZapp has no use for the update-check this
import exists for at all (client/updater.py checks WinZapp's own GitHub
releases, not WPPConnect Server's), so the fix stubs the require() out.

Regression covered here: an earlier attempt at this fix replaced the
require() call with `{ default: async () => "" }` — but the surrounding
code is `__importDefault(require(...))`, and __importDefault(mod) itself
does `mod.__esModule ? mod : { default: mod }`. Feeding it an
already-`{ default: fn }`-shaped value wraps it a SECOND time into
`{ default: { default: fn } }`, so the later `latest_version_1.default(...)`
call in welcome.js's checkUpdates() throws "is not a function" — a
different crash than ERR_REQUIRE_ESM, not an actual fix. The correct
replacement is a bare function, wrapped exactly once.

Both setup_api.py and ApiSetupDialog (client/ui/dialogs/api_setup.py) apply
the same patch, importing shared source-text constants from
client/core/wppconnect_welcome_layer_patch.py.
"""

import importlib.util
import os

import pytest

from core.wppconnect_welcome_layer_patch import (
    ALL_PATCHES, PATCHED_LATEST_VERSION_REQUIRE,
    latest_version_dependency_is_gone,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_setup_api():
    spec = importlib.util.spec_from_file_location(
        "setup_api", os.path.join(REPO_ROOT, "setup_api.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_PRISTINE_WELCOME_JS = '''"use strict";
var __importDefault = (this && this.__importDefault) || function (mod) {
    return (mod && mod.__esModule) ? mod : { "default": mod };
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.welcomeScreen = welcomeScreen;
exports.checkUpdates = checkUpdates;
const boxen_1 = __importDefault(require("boxen"));
const chalk_1 = __importDefault(require("chalk"));
const latest_version_1 = __importDefault(require("latest-version"));
const logger_1 = require("../utils/logger");
async function checkUpdates() {
    const latest = await (0, latest_version_1.default)('@wppconnect-team/wppconnect');
    return latest;
}
'''


@pytest.fixture
def fake_wppconnect_dist(tmp_path):
    """Builds both path conventions used across the two callers:
    setup_api.py's _patch_wppconnect_welcome_layer(client_api_dir) takes
    the OUTER client/api root (tmp_path itself, matching CLIENT_API_DIR),
    while ApiSetupDialog's own copy takes the INNER .../dist/api subdir
    (same convention as its sibling _patch_wppconnect_sender_layer, etc.)
    — welcome.js sits one level up from THAT, under dist/controllers/.
    """
    controllers_dir = tmp_path / "node_modules" / "@wppconnect-team" / "wppconnect" / "dist" / "controllers"
    controllers_dir.mkdir(parents=True)
    welcome_js = controllers_dir / "welcome.js"
    welcome_js.write_text(_PRISTINE_WELCOME_JS, encoding="utf-8")
    dist_api_dir = controllers_dir.parent / "api"
    dist_api_dir.mkdir()
    return tmp_path, dist_api_dir, welcome_js


class TestPatchIsNotDoubleWrapped:
    """The actual bug this module's own docstring warns about."""

    def test_patched_value_is_not_wrapped_in_default(self):
        for _, patched in ALL_PATCHES:
            assert "{ default:" not in patched and '{"default":' not in patched, (
                "wrapping the replacement in { default: ... } gets double-wrapped "
                "by __importDefault(), breaking latest_version_1.default(...)"
            )

    def test_original_and_patched_differ(self):
        for original, patched in ALL_PATCHES:
            assert original != patched


class TestSetupApiPatch:
    """setup_api.py's copy takes the OUTER client/api root (CLIENT_API_DIR
    convention), unlike ApiSetupDialog's own copy below."""

    def test_patches_the_require_call(self, fake_wppconnect_dist):
        setup_api = _load_setup_api()
        client_api_root, _, welcome_js = fake_wppconnect_dist

        ok = setup_api._patch_wppconnect_welcome_layer(str(client_api_root))

        assert ok is True
        content = welcome_js.read_text(encoding="utf-8")
        assert 'require("latest-version")' not in content
        assert "__importDefault((async () => \"\"))" in content
        # Untouched: only the latest-version require is replaced.
        assert 'require("boxen")' in content
        assert 'require("chalk")' in content

    def test_result_is_not_double_wrapped_in_default(self, fake_wppconnect_dist):
        """Simulates __importDefault()'s own runtime logic against the
        patched text to confirm latest_version_1.default ends up callable,
        not an object — the exact failure mode of the earlier broken fix
        this module's docstring describes."""
        setup_api = _load_setup_api()
        client_api_root, _, welcome_js = fake_wppconnect_dist
        setup_api._patch_wppconnect_welcome_layer(str(client_api_root))
        content = welcome_js.read_text(encoding="utf-8")
        assert "const latest_version_1 = __importDefault((async () => \"\"));" in content

        # Reproduce __importDefault()'s own logic in Python against an
        # equivalent bare callable (what "(async () => \"\")" is: a plain
        # function, not a { default: ... }-shaped object) instead of
        # eval()-ing the JS text itself.
        def __importDefault(mod):
            return mod if (isinstance(mod, dict) and mod.get("__esModule")) else {"default": mod}

        bare_function = lambda: ""  # noqa: E731 — stand-in for "(async () => \"\")"
        result = __importDefault(bare_function)
        assert callable(result["default"]), "double-wrapped: .default is not directly callable"

    def test_is_idempotent(self, fake_wppconnect_dist):
        setup_api = _load_setup_api()
        client_api_root, _, welcome_js = fake_wppconnect_dist

        setup_api._patch_wppconnect_welcome_layer(str(client_api_root))
        first_pass = welcome_js.read_text(encoding="utf-8")
        ok = setup_api._patch_wppconnect_welcome_layer(str(client_api_root))
        second_pass = welcome_js.read_text(encoding="utf-8")

        assert ok is True
        assert first_pass == second_pass

    def test_missing_file_is_a_safe_no_op(self, tmp_path):
        setup_api = _load_setup_api()
        ok = setup_api._patch_wppconnect_welcome_layer(str(tmp_path))
        assert ok is False


class TestApiSetupDialogPatch:
    """ApiSetupDialog's own copy (client/ui/dialogs/api_setup.py) — the
    real end-user install flow, since npm install on an end user's machine
    re-fetches unpatched node_modules fresh. Takes the INNER .../dist/api
    subdir, same convention as its sibling _patch_wppconnect_sender_layer."""

    def test_patches_the_require_call(self, fake_wppconnect_dist):
        from ui.dialogs.api_setup import ApiSetupDialog
        _, dist_api_dir, welcome_js = fake_wppconnect_dist

        ok = ApiSetupDialog._patch_wppconnect_welcome_layer(str(dist_api_dir))

        assert ok is True
        content = welcome_js.read_text(encoding="utf-8")
        assert 'require("latest-version")' not in content
        assert "__importDefault((async () => \"\"))" in content


#: welcome.js as wppconnect 2.3.2 ships it: `latest-version` is gone from
#: package.json entirely and the update check asks the npm registry over
#: `fetch`. There is no require() left to stub, so the crash this whole module
#: exists for cannot happen on that runtime.
_WELCOME_JS_WITHOUT_LATEST_VERSION = '''"use strict";
var __importDefault = (this && this.__importDefault) || function (mod) {
    return (mod && mod.__esModule) ? mod : { "default": mod };
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.welcomeScreen = welcomeScreen;
exports.checkUpdates = checkUpdates;
const boxen_1 = __importDefault(require("boxen"));
const chalk_1 = __importDefault(require("chalk"));
const logger_1 = require("../utils/logger");
async function checkUpdates() {
    const response = await fetch(`https://registry.npmjs.org/${packageName}/latest`);
    return (await response.json()).version;
}
'''


class TestARuntimeThatNeverImportsItIsAlreadySatisfied:
    """wppconnect 2.3.2 removed the dependency, which is the patch's goal
    reached — not a pattern that failed to match.

    Reported as a miss it is a warning on every single launch (both callers
    re-run these patches from an already-installed tree), and a warning that
    means "everything is fine" is exactly how the two real misses on that same
    release — checkQrCode and loginByCode silently unpatched — went unread for
    a whole version.
    """

    def test_the_helper_reads_the_absence_as_satisfied(self):
        assert latest_version_dependency_is_gone(_WELCOME_JS_WITHOUT_LATEST_VERSION)

    def test_a_file_that_still_imports_it_is_not(self):
        assert not latest_version_dependency_is_gone(_PRISTINE_WELCOME_JS)

    def test_an_already_patched_file_is_not_mistaken_for_one(self):
        """Applying the patch is itself what removes the last mention of the
        package name, so the package-name check alone would report a file this
        module had just fixed as one that never needed fixing."""
        patched = _PRISTINE_WELCOME_JS.replace(
            'require("latest-version")', PATCHED_LATEST_VERSION_REQUIRE, 1
        )
        assert "latest-version" not in patched
        assert not latest_version_dependency_is_gone(patched)

    def test_setup_api_reports_success_and_writes_nothing(self, fake_wppconnect_dist):
        setup_api = _load_setup_api()
        client_api_root, _, welcome_js = fake_wppconnect_dist
        welcome_js.write_text(_WELCOME_JS_WITHOUT_LATEST_VERSION, encoding="utf-8")

        ok = setup_api._patch_wppconnect_welcome_layer(str(client_api_root))

        assert ok is True
        assert welcome_js.read_text(encoding="utf-8") == _WELCOME_JS_WITHOUT_LATEST_VERSION

    def test_the_dialog_agrees(self, fake_wppconnect_dist):
        from ui.dialogs.api_setup import ApiSetupDialog
        _, dist_api_dir, welcome_js = fake_wppconnect_dist
        welcome_js.write_text(_WELCOME_JS_WITHOUT_LATEST_VERSION, encoding="utf-8")

        ok = ApiSetupDialog._patch_wppconnect_welcome_layer(str(dist_api_dir))

        assert ok is True
        assert welcome_js.read_text(encoding="utf-8") == _WELCOME_JS_WITHOUT_LATEST_VERSION
