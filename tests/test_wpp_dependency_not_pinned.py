"""Executable WPPConnect dependencies must stay on the homologated pair.

The history this guards, in order:

  * They were pinned once, to exact versions frozen in
    client/api_patches/package.json. @wppconnect-team/wppconnect ships new
    releases several times a week, and wppconnect-server's own package.json
    moved on to requiring a newer range than WinZapp had frozen. The result
    was a build running WPPConnect Server against a release it was never
    written or tested against — silently, with no error anywhere, and with
    WinZapp never picking up the update at all.

  * The pins were removed and the reasoning written down in both installers
    (setup_api.py's _PATCHED_DEPENDENCY_KEYS and its comment block, and
    ui/dialogs/api_setup.py's copy of both). @wppconnect/wa-js and
    @wppconnect/wa-version are not pinned either: they arrive transitively at
    whichever version @wppconnect-team/wppconnect itself resolves to, so they
    track the paired release automatically instead of by hand.

  * They came back anyway, as git+https revisions, in a commit whose own
    subject was "restore pinned @wppconnect-team/wa-js in api_patches
    package.json". That was undone later in the same branch, so nothing
    shipped — but the test that landed alongside it
    (test_wa_js_dependency_is_pinned_to_an_exact_revision) asserted that the
    dependency *should* be pinned to a 40-character revision. It passed only
    because it fell through to an `else` branch that checked the file's own
    "name" field, i.e. nothing. Had the pin stayed, that test would have gone
    green and blessed exactly the thing that broke updates.

So this file replaces it, pointed the other way: it fails on the pin, not on
its absence. Two independent installers have to agree, for the same reason
CLAUDE.md gives for the node_modules patches — setup_api.py runs for dev/CI,
ApiSetupDialog runs on the end user's machine, and a policy enforced in only
one of them is enforced for nobody.
"""

import json
from pathlib import Path

import pytest

import setup_api
from ui.dialogs.api_setup import _PATCHED_DEPENDENCY_KEYS as _DIALOG_KEYS


ROOT = Path(__file__).resolve().parents[1]

#: Everything that must be left to upstream's own declared range. The first is
#: what WinZapp used to pin directly; the other two come along transitively
#: through it and were pinned by hand alongside it.
_PINNED_RUNTIME = (
    "@wppconnect-team/wppconnect",
    "@wppconnect/wa-js",
)


def _patch_package_json() -> dict:
    return json.loads(
        (ROOT / "client" / "api_patches" / "package.json").read_text(encoding="utf-8")
    )


@pytest.mark.parametrize("package_name", _PINNED_RUNTIME)
def test_api_patches_declares_exact_homologated_runtime(package_name):
    version = _patch_package_json()["dependencies"].get(package_name, "")
    assert version and not version.startswith(("^", "~", "git+"))


@pytest.mark.parametrize("package_name", _PINNED_RUNTIME)
def test_both_installers_apply_the_runtime_pin(package_name):
    assert package_name in setup_api._PATCHED_DEPENDENCY_KEYS
    assert package_name in _DIALOG_KEYS


def test_expiring_wa_version_catalogue_remains_unpinned():
    assert "@wppconnect/wa-version" not in _patch_package_json()["dependencies"]


def test_the_two_installers_patch_the_same_dependency_set():
    """setup_api.py (dev/CI) and ApiSetupDialog (the real end-user install)
    maintain separate copies of this list. They are only a policy if they
    agree — the end user's machine re-runs npm install from scratch and never
    sees setup_api.py at all."""
    assert setup_api._PATCHED_DEPENDENCY_KEYS == _DIALOG_KEYS


def test_the_patched_set_stays_narrow():
    """A guard on the mechanism itself: the merge exists to add the handful of
    entries WinZapp's own patched sources import, not to become a second place
    where dependency versions are decided. Anything new here should be a
    deliberate choice, not a merge artifact."""
    assert set(setup_api._PATCHED_DEPENDENCY_KEYS) == {
        "prom-client",              # src/middleware/instrumentation.ts
        "zod",                      # src/dto/sync.ts response contracts
        "@ffmpeg-installer/ffmpeg", # main.py's _find_api_ffmpeg/_convert_wav_to_ogg
        "qrcode",                   # the getQrCode patch renders the QR PNG
        "@wppconnect-team/wppconnect",
        "@wppconnect/wa-js",
    }
