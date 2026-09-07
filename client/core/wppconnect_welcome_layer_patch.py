"""Shared source-text constant for patching @wppconnect-team/wppconnect's
compiled controllers/welcome.js — startup crash on Node 20+ when the
`latest-version` npm package (a pure-ESM module as of its own more recent
majors) is loaded via a plain CommonJS `require()`, which throws
`ERR_REQUIRE_ESM` instead of returning anything. welcome.js's own
checkUpdates() calls this purely to print a "new version available" notice
to the console at startup — WinZapp has no use for it at all (updates are
handled by client/updater.py against WinZapp's own GitHub releases, not
WPPConnect Server's) — so the fix stubs the import out entirely rather than
trying to make the real package load.

Replaces the require() call's argument with a plain async function directly
— NOT wrapped in `{ default: ... }` — because the surrounding code is
`__importDefault(require("latest-version"))`, and __importDefault(mod)
itself does `mod.__esModule ? mod : { default: mod }`. Passing an
already-`{ default: fn }`-shaped value here (an earlier version of this fix
did exactly that) gets wrapped a SECOND time into
`{ default: { default: fn } }`, so the later `latest_version_1.default(...)`
call throws "is not a function" instead of the ERR_REQUIRE_ESM crash it
replaced — a different crash, not a fix. A bare function passed to
__importDefault is wrapped exactly once, landing correctly on
`latest_version_1.default`.

Both setup_api.py and ApiSetupDialog (client/ui/dialogs/api_setup.py) apply
this patch to node_modules right after every `npm install` — see
client/core/wppconnect_host_layer_patch.py's module docstring for why that
sharing matters and why this can't go through the normal api_patches/
mechanism (welcome.js is compiled output of a THIRD-PARTY dependency, not
WPPConnect Server's own source).
"""

ORIGINAL_LATEST_VERSION_REQUIRE = 'require("latest-version")'
PATCHED_LATEST_VERSION_REQUIRE = '(async () => "")'

ALL_PATCHES = ((ORIGINAL_LATEST_VERSION_REQUIRE, PATCHED_LATEST_VERSION_REQUIRE),)


def latest_version_dependency_is_gone(content: str) -> bool:
    """Does this welcome.js not import `latest-version` at all?

    wppconnect 2.3.2 dropped the dependency and asks the npm registry over
    `fetch` instead, so there is no require() left to stub — the crash this
    module exists for cannot happen on that runtime. That is the patch's goal
    reached, not a pattern that failed to match, and both call sites have to
    say so: reported as a miss it is a warning on every launch, and a warning
    that means "everything is fine" is how the two real misses on 2.3.2 (the
    checkQrCode/loginByCode ones) went unread for a whole release.

    Checked by the package name rather than by the require() spelling, so a
    future re-introduction through any other import form counts as present and
    goes back through the patch above. The stub is looked for first because
    applying the patch is itself what removes the last mention of the package
    from the file — without that, the second run over a file this module had
    just fixed would report the fix as "never needed".
    """
    if PATCHED_LATEST_VERSION_REQUIRE in content:
        return False
    return "latest-version" not in content
