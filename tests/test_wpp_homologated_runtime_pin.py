"""The executable WPPConnect runtime is one homologated pair, pinned exactly.

WinZapp does not ship "some WPPConnect Server plus whatever npm resolves". It
ships a *pair*: one server tag (client/wpp_minimum_version.txt) together with
the one @wppconnect-team/wppconnect + @wppconnect/wa-js release its patch set
was actually validated against. Those two packages are compiled code running
inside WhatsApp Web, and WinZapp reaches into them directly — the node_modules
patches in client/core/wppconnect_*_patch.py rewrite them by literal
search-and-replace, and deviceController.ts drives private WA-JS loader APIs.

Left on upstream's caret range, a plain `npm install` of the *same* server tag
could move the browser-side send and status APIs underneath an unchanged
WinZapp build, with nothing failing until a user tried to send something. That
is why the pin is back. Moving it is a deliberate act: bump this pair and
client/wpp_minimum_version.txt in the same commit, after actually running
against the new one.

Two independent installers have to agree, for the same reason CLAUDE.md gives
for the node_modules patches — setup_api.py runs for dev/CI, ApiSetupDialog
runs on the end user's machine (where npm install re-fetches everything from
scratch), and a policy enforced in only one of them is enforced for nobody.

@wppconnect/wa-version is deliberately NOT part of the pair: it is the
expiring catalogue of WhatsApp Web builds, not an API surface, and pinning it
would freeze an install onto a catalogue whose entries stop being served.
"""

import json
import subprocess
from pathlib import Path

import pytest
from packaging.version import Version

import setup_api
from ui.dialogs.api_setup import _PATCHED_DEPENDENCY_KEYS as _DIALOG_KEYS


ROOT = Path(__file__).resolve().parents[1]

#: The executable half of the homologated pair. wa-js is named explicitly
#: rather than left to arrive transitively: wppconnect declares it as a caret
#: range too, so "pin the adapter" alone still lets the code that actually runs
#: in the page change on a reinstall.
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
    """api_patches/package.json is the file both installers merge *from*, so
    this is where the pair is declared — a range here is a range everywhere."""
    version = _patch_package_json()["dependencies"].get(package_name, "")
    assert version and not version.startswith(("^", "~", "git+")), (
        f"{package_name} is declared as {version!r} in "
        f"client/api_patches/package.json. The executable WPPConnect runtime "
        f"is pinned to the exact pair WinZapp was validated against; a range "
        f"lets a reinstall change the browser APIs under an unchanged build."
    )


@pytest.mark.parametrize("package_name", _PINNED_RUNTIME)
def test_both_installers_apply_the_runtime_pin(package_name):
    assert package_name in setup_api._PATCHED_DEPENDENCY_KEYS
    assert package_name in _DIALOG_KEYS


def test_expiring_wa_version_catalogue_remains_unpinned():
    """The one WPPConnect package that must keep floating: each of its
    versions.json entries carries an ~2-month expiry, and start.js can only
    pin a WhatsApp Web build the catalogue still serves. Freezing it strands
    an install on a catalogue that has gone stale."""
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
        "@wppconnect-team/wppconnect",  # the homologated runtime pair, see
        "@wppconnect/wa-js",            # this module's own docstring
    }


def test_the_installed_runtime_is_the_one_that_was_pinned():
    """The pin only means something if a real install actually lands on it.

    This machine ran @wppconnect-team/wppconnect 2.3.2 for a while, resolved
    from upstream's own ^2.2.7 because nothing pinned the key yet — and 2.3.2
    rewrote host.layer.js's checkQrCode()/loginByCode(), so the pairing-code
    rotation cooldown (the patch that exists because a code stream nobody is
    watching earned a real account ban) silently stopped applying. Nothing
    failed; setup_api.py printed two warnings among forty lines of output.
    """
    live = ROOT / "client" / "api" / "node_modules" / "@wppconnect-team" / "wppconnect" / "package.json"
    if not live.exists():
        pytest.skip("client/api/node_modules not present")
    installed = json.loads(live.read_text(encoding="utf-8"))["version"]
    expected = _patch_package_json()["dependencies"]["@wppconnect-team/wppconnect"]
    assert installed == expected, (
        f"client/api/node_modules holds @wppconnect-team/wppconnect "
        f"{installed}, but the homologated pin is {expected}. Re-run "
        f"setup_api.py; a drifting runtime is how the node_modules patches "
        f"stop matching without anything failing."
    )


def _npm_range_allows(pinned: str, npm_range: str) -> bool:
    """Whether an exact version satisfies a single npm range operator.

    Only the three forms upstream actually uses are understood — a caret, a
    tilde, and a bare exact version. Anything else raises rather than quietly
    passing: a range this cannot read is a range nobody is checking.
    """
    low = Version(pinned)
    if npm_range.startswith("^"):
        floor = Version(npm_range[1:])
        # A caret allows changes that do not modify the left-most *non-zero*
        # component, so ^0.2.3 stops at 0.3.0 and ^0.0.3 at 0.0.4 — widening
        # those to 1.0.0 would let a range nobody satisfies read as satisfied.
        if floor.major:
            ceiling = Version(f"{floor.major + 1}.0.0")
        elif floor.minor:
            ceiling = Version(f"0.{floor.minor + 1}.0")
        else:
            ceiling = Version(f"0.0.{floor.micro + 1}")
    elif npm_range.startswith("~"):
        floor = Version(npm_range[1:])
        ceiling = Version(f"{floor.major}.{floor.minor + 1}.0")
    else:
        return low == Version(npm_range)
    return floor <= low < ceiling


class TestNpmRangeReading:
    """The check below is only as trustworthy as this reader, and a range read
    too widely reports a pin as satisfied when it is not."""

    def test_a_caret_stops_at_the_next_major(self):
        assert _npm_range_allows("2.10.16", "^2.2.7")
        assert not _npm_range_allows("3.0.0", "^2.2.7")
        assert not _npm_range_allows("2.2.6", "^2.2.7")

    def test_a_caret_below_1_0_0_stops_at_the_next_minor(self):
        """npm treats the left-most non-zero component as the one a caret may
        not change: ^0.2.3 is <0.3.0, not <1.0.0."""
        assert _npm_range_allows("0.2.9", "^0.2.3")
        assert not _npm_range_allows("0.3.0", "^0.2.3")

    def test_a_caret_below_0_1_0_stops_at_the_next_patch(self):
        assert _npm_range_allows("0.0.3", "^0.0.3")
        assert not _npm_range_allows("0.0.4", "^0.0.3")

    def test_a_tilde_stops_at_the_next_minor(self):
        assert _npm_range_allows("2.2.9", "~2.2.7")
        assert not _npm_range_allows("2.3.0", "~2.2.7")

    def test_a_bare_version_must_match_exactly(self):
        assert _npm_range_allows("2.10.16", "2.10.16")
        assert not _npm_range_allows("2.10.17", "2.10.16")


def test_the_pin_still_satisfies_the_servers_own_declared_range():
    """wpp_minimum_version.txt and api_patches/package.json are two files
    edited by hand, and nothing tied them together.

    Moving the server tag to a release that requires ^2.4.0 while the pin
    stays at 2.3.1 reproduces exactly the bug that made somebody remove the
    pin the first time: a stale 2.2.4 sitting under a server whose own
    package.json had already moved to ^2.2.6, so WinZapp ran a pairing
    implementation the server was never built against.

    The pristine upstream package.json is read out of git rather than off
    disk, because the live copy is the one both installers already merged the
    pin into — it can only ever agree with itself.
    """
    api_dir = ROOT / "client" / "api"
    if not (api_dir / ".git").exists():
        pytest.skip("client/api not cloned")

    try:
        upstream = subprocess.run(
            ["git", "show", "HEAD:package.json"],
            cwd=api_dir,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"git unavailable: {exc}")
    if upstream.returncode != 0:
        pytest.skip(f"could not read upstream package.json: {upstream.stderr.strip()}")

    declared = json.loads(upstream.stdout)["dependencies"]
    pinned = _patch_package_json()["dependencies"]

    for package_name in _PINNED_RUNTIME:
        npm_range = declared.get(package_name)
        if not npm_range:
            # wa-js arrives transitively through wppconnect on most tags; the
            # server only declares it when it imports it directly.
            continue
        assert _npm_range_allows(pinned[package_name], npm_range), (
            f"client/api_patches/package.json pins {package_name} "
            f"{pinned[package_name]}, but WPPConnect Server "
            f"{json.loads(upstream.stdout).get('version')} declares "
            f"{npm_range}. Move the pin and client/wpp_minimum_version.txt "
            f"together, after running the node_modules patches against the "
            f"new runtime."
        )


def test_every_node_modules_patch_still_matches_the_pinned_runtime():
    """The four node_modules patches are idempotent search-and-replace, so a
    runtime whose source moved is reported as a warning and skipped, never as
    an error. Assert the return value nothing else looks at.

    Runs against a throwaway copy so the test never writes into the real
    node_modules.
    """
    dist = (
        ROOT / "client" / "api" / "node_modules" / "@wppconnect-team"
        / "wppconnect" / "dist"
    )
    if not (dist / "api" / "layers" / "host.layer.js").exists():
        pytest.skip("client/api/node_modules not present")

    import shutil
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "node_modules" / "@wppconnect-team" / "wppconnect" / "dist"
        # Only the two directories the four patches read; copying the whole
        # package would be hundreds of megabytes per run.
        shutil.copytree(dist / "api" / "layers", dest / "api" / "layers")
        shutil.copytree(dist / "controllers", dest / "controllers")
        assert setup_api._patch_wppconnect_host_layer(tmp)
        assert setup_api._patch_wppconnect_status_layer(tmp)
        assert setup_api._patch_wppconnect_sender_layer(tmp)
        assert setup_api._patch_wppconnect_welcome_layer(tmp)
