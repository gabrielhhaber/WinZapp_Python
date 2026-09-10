"""A browser that cannot start must not be mistaken for a working install.

WinZapp ships its own Chromium under ``api/.cache``, and every "is the API set
up?" check looked only for the *executable*. Measured on a real install
(2026-09-10, WinZapp 1.1.0.2681alpha), where every session start died before a
browser existed::

    error: [<session>:browser] Failed to launch the browser process:  Code: 2147483651
    stderr:
    [0910/103543.323:ERROR:base\\i18n\\icu_util.cc:232] Invalid file descriptor to ICU data received.

``2147483651`` is ``0x80000003`` (STATUS_BREAKPOINT): Chromium aborted during
startup. The line above says why — it could not read ``icudtl.dat``. The
executable was present, so ``find_headless_shell()`` reported
``[headless-shell] Already installed: ...`` on every launch and nothing ever
looked again.

The expensive part was not the failed connection. ``ProfileHealthTracker``
counts sessions that die without connecting, which is precisely what a failed
browser launch produces, so the profile recovery fired for a fault that had
nothing to do with the profile, spent both snapshot generations on it, and left
the account unpaired — with no route back, because pairing needs the browser
that will not start::

    10:26:07  STARTUP ... paired=True  login_store=files=113 bytes=208775977
    10:27:39  profile suspect - session started and died 3x without connecting
    10:27:40  profile restored from snapshot
    10:30:20  Chrome released the profile before the kill  login_store=absent
    10:35:41  MainWindow: WhatsApp connection not paired. Showing connection dialog...
    10:36:14  [display_qrcode_image] No QR reached the screen (no QR within 25s)

``core/browser_payload.py`` is plain functions over a directory, so the
behaviour is exercised directly against real files here. The ``MainWindow``
halves are wx and cannot be instantiated, so they are bound to a stub the way
the rest of the suite does it.
"""

import os
import types
from pathlib import Path

import pytest

from core import browser_payload
from main import MainWindow


def _make_browser(root, version="win64-148.0.7778.97", icu_bytes=b"x" * 32,
                  product="chrome", payload="chrome-win64", binary="chrome.exe"):
    """Lay out a @puppeteer/browsers-shaped cache and return the binary path."""
    payload_dir = root / product / version / payload
    payload_dir.mkdir(parents=True, exist_ok=True)
    binary_path = payload_dir / binary
    binary_path.write_bytes(b"MZ")
    if icu_bytes is not None:
        (payload_dir / "icudtl.dat").write_bytes(icu_bytes)
    return binary_path


class TestPayloadProblem:
    def test_a_complete_payload_has_no_problem(self, tmp_path):
        binary = _make_browser(tmp_path)
        assert browser_payload.payload_problem(str(binary)) is None

    def test_a_missing_icudtl_is_named(self, tmp_path):
        binary = _make_browser(tmp_path, icu_bytes=None)
        assert browser_payload.payload_problem(str(binary)) == "icudtl.dat is missing"

    def test_a_quarantine_stub_counts_as_missing(self, tmp_path):
        """Antivirus routinely replaces the file with a zero-byte placeholder
        rather than deleting it, and Chromium fails on that identically."""
        binary = _make_browser(tmp_path, icu_bytes=b"")
        assert browser_payload.payload_problem(str(binary)) == "icudtl.dat is empty"

    @pytest.mark.parametrize("path", ["", None, "Z:/nowhere/chrome.exe"])
    def test_it_never_disqualifies_on_a_path_it_cannot_read(self, path):
        """A disqualifier must only ever disqualify on evidence: the caller's
        response is to delete the install and download it again, so "I could
        not look" has to mean "nothing is wrong"."""
        assert browser_payload.payload_problem(path) is None

    def test_it_checks_beside_the_binary_not_the_cache_root(self, tmp_path):
        """icudtl.dat is loaded from the binary's own directory. One sitting
        further up is a different install and proves nothing."""
        binary = _make_browser(tmp_path, icu_bytes=None)
        (tmp_path / "icudtl.dat").write_bytes(b"x" * 32)
        assert browser_payload.payload_problem(str(binary)) == "icudtl.dat is missing"


class TestInstalledVersionDir:
    """What the repair deletes. The containment check is the whole safety of
    it: nothing outside WinZapp's own browser cache may ever be named."""

    def test_it_names_the_version_directory(self, tmp_path):
        binary = _make_browser(tmp_path)
        assert browser_payload.installed_version_dir(str(binary), str(tmp_path)) == str(
            tmp_path / "chrome" / "win64-148.0.7778.97"
        )

    def test_it_stops_at_the_version_not_the_payload(self, tmp_path):
        """@puppeteer/browsers skips its install while the *version* directory
        exists, so removing only chrome-win64/ would repair nothing."""
        binary = _make_browser(tmp_path)
        named = browser_payload.installed_version_dir(str(binary), str(tmp_path))
        assert not named.endswith("chrome-win64")

    def test_a_binary_outside_the_cache_is_refused(self, tmp_path):
        outside = tmp_path / "elsewhere" / "chrome-win64"
        outside.mkdir(parents=True)
        (outside / "chrome.exe").write_bytes(b"MZ")
        cache = tmp_path / "cache"
        cache.mkdir()
        assert browser_payload.installed_version_dir(
            str(outside / "chrome.exe"), str(cache)
        ) is None

    def test_a_binary_sitting_in_the_cache_root_is_refused(self, tmp_path):
        """There is no version directory to remove, and the only thing above it
        is the cache itself."""
        (tmp_path / "chrome.exe").write_bytes(b"MZ")
        assert browser_payload.installed_version_dir(
            str(tmp_path / "chrome.exe"), str(tmp_path)
        ) is None

    def test_a_product_level_binary_is_refused(self, tmp_path):
        """<cache>/chrome/chrome.exe names the product directory, and removing
        that takes every version with it."""
        product = tmp_path / "chrome"
        product.mkdir()
        (product / "chrome.exe").write_bytes(b"MZ")
        assert browser_payload.installed_version_dir(
            str(product / "chrome.exe"), str(tmp_path)
        ) is None

    @pytest.mark.parametrize(
        "binary,cache", [("", "c:/cache"), ("c:/cache/x/y/chrome.exe", "")]
    )
    def test_empty_arguments_name_nothing(self, binary, cache):
        assert browser_payload.installed_version_dir(binary, cache) is None


class _StubWindow:
    """Carries only what the two methods under test actually touch."""

    _HEADLESS_SHELL_NAMES = ("chrome-headless-shell.exe", "chrome-headless-shell")
    _WINDOWS_CHROME_NAMES = ("chrome.exe",)

    def __init__(self, cache_dir):
        self._cache_dir = str(cache_dir)


@pytest.fixture
def window(tmp_path, monkeypatch):
    """MainWindow's two finders, bound to a stub, with resource_path pointed at
    a temporary cache. main.py is ~29k lines and a wx.Frame; this is the
    established way the suite reaches a method on it."""
    import main as winzapp_main

    cache = tmp_path / "api" / ".cache"
    cache.mkdir(parents=True)

    def fake_resource_path(*parts):
        if parts[:2] == ("api", ".cache"):
            return str(cache)
        return str(tmp_path.joinpath(*parts))

    monkeypatch.setattr(winzapp_main, "resource_path", fake_resource_path)

    stub = _StubWindow(cache)
    stub.find_headless_shell = winzapp_main.MainWindow.find_headless_shell.__get__(stub)
    stub.find_incomplete_browser = (
        winzapp_main.MainWindow.find_incomplete_browser.__get__(stub)
    )
    stub.cache = cache
    return stub


@pytest.mark.skipif(os.name != "nt", reason="the finders select by Windows binary name")
class TestTheFindersDisagreeOnPurpose:
    def test_a_complete_install_is_found(self, window):
        binary = _make_browser(window.cache)
        assert window.find_headless_shell() == str(binary)
        assert window.find_incomplete_browser() == (None, None)

    def test_a_broken_install_is_not_reported_as_installed(self, window):
        """The whole bug: this returned the path, so every setup check passed
        and the download that would have fixed it never ran."""
        _make_browser(window.cache, icu_bytes=None)
        assert window.find_headless_shell() is None

    def test_a_broken_install_is_reported_as_broken(self, window):
        binary = _make_browser(window.cache, icu_bytes=None)
        found, problem = window.find_incomplete_browser()
        assert found == str(binary)
        assert problem == "icudtl.dat is missing"

    def test_a_good_version_beside_a_broken_one_still_starts(self, window):
        """The walk skips rather than gives up, so a cache holding both is
        usable — the ordinary state right after a repair that could not delete
        the old directory."""
        _make_browser(window.cache, version="win64-147.0.0.0", icu_bytes=None)
        good = _make_browser(window.cache, version="win64-148.0.7778.97")
        assert window.find_headless_shell() == str(good)

    def test_an_empty_cache_is_neither(self, window):
        assert window.find_headless_shell() is None
        assert window.find_incomplete_browser() == (None, None)


@pytest.fixture(scope="module")
def recovery_source():
    """_recover_suspect_profile() as source. It is a MainWindow method whose
    body closes over wx and the live API, so what is pinned here is its
    ordering — the way tests/test_stale_browser_lock_recovery.py pins the Node
    side's."""
    source = (
        Path(__file__).resolve().parents[1] / "client" / "main.py"
    ).read_text(encoding="utf-8")
    start = source.index("    def _recover_suspect_profile(")
    return source[start : source.index("\n    def ", start + 10)]


class TestTheRecoveryRefusesWhenTheBrowserIsTheFault:
    """Restoring a snapshot cannot put an ``icudtl.dat`` back, and the attempt
    costs a generation of the ladder — two launches of it cost one install its
    pairing."""

    def test_the_browser_is_checked_before_anything_is_spent(self, recovery_source):
        """Before the once-per-launch latch, so a launch spent refusing here
        still has its one real recovery left for the fault this exists to fix.
        """
        refusal = recovery_source.index("find_incomplete_browser()")
        latch = recovery_source.index("self._profile_recovery_attempted = True")
        assert refusal < latch

    def test_the_refusal_returns_false_without_restoring(self, recovery_source):
        head = recovery_source[: recovery_source.index("session_name =")]
        assert "return False" in head[head.index("find_incomplete_browser()") :]
        assert "restore_snapshot" not in head

    def test_the_refusal_is_audited(self, recovery_source):
        """shutdown_audit.log is the only log that survives the next launch,
        and this is a diagnosis someone will be reading after the fact."""
        assert "_shutdown_audit(" in recovery_source[
            recovery_source.index("find_incomplete_browser()") :
            recovery_source.index("self._profile_recovery_attempted = True")
        ]

    def test_the_user_is_told_out_loud(self, recovery_source):
        """By construction this only happens while already offline, where
        _set_wa_connected(False, ...) hits its no-change early return and says
        nothing at all."""
        assert "_announce_browser_beyond_repair" in recovery_source


class _MetadataDB:
    """The generation ladder is persisted; nothing here needs a real database."""

    def __init__(self):
        self.values = {}

    def get_metadata_json(self, key, default=None):
        return self.values.get(key, default)

    def set_metadata_json(self, key, value):
        self.values[key] = value


class _RecoveryStub:
    """Carries only what _recover_suspect_profile() touches. Same shape as
    tests/test_profile_recovery_wiring.py's stub, which is how the suite
    reaches methods on MainWindow (a wx.Frame)."""

    def __init__(self, broken):
        self.settings = {"privateinfo": {"paired": True}}
        self.token = "sess123:tok"
        # A directory with no snapshot in it, so the healthy-browser case falls
        # all the way through the guard and stops at the next real decision
        # rather than needing the whole restore machinery stubbed.
        self.global_dir = "/no-such-global-dir"
        self.background_mode = True
        self.audits = []
        self.announced = []
        self.error_sound = types.SimpleNamespace(play=lambda: None)
        self.i18n = types.SimpleNamespace(t=lambda key: key)
        self.db = _MetadataDB()
        self._broken = broken

    def find_incomplete_browser(self):
        return self._broken

    def _announce_profile_beyond_repair(self):
        import main as winzapp_main

        winzapp_main.MainWindow._announce_profile_beyond_repair(self)

    def _shutdown_audit(self, msg):
        self.audits.append(msg)

    def output(self, text, interrupt=False):
        self.announced.append(text)

    def _announce_browser_beyond_repair(self):
        # Bound from the real class, not faked: the announcement IS the
        # user-visible half here, and a stub that only recorded a call would
        # pass while it said nothing.
        import main as winzapp_main

        winzapp_main.MainWindow._announce_browser_beyond_repair(self)

    # Bound from the real class: the ladder decides WHICH snapshot goes back,
    # and faking it would let this drift from the module its own tests cover.
    _PROFILE_RECOVERY_GENERATION_KEY = MainWindow._PROFILE_RECOVERY_GENERATION_KEY
    _profile_recovery_generation = MainWindow._profile_recovery_generation
    _set_profile_recovery_generation = MainWindow._set_profile_recovery_generation


class TestTheRefusalActuallyRuns:
    """The ordering assertions above read the source; these run it."""

    @pytest.fixture(autouse=True)
    def _no_wx(self, monkeypatch):
        monkeypatch.setattr("main.wx.CallAfter", lambda fn, *a, **kw: fn(*a, **kw))

    def _recover(self, stub):
        import main as winzapp_main

        return winzapp_main.MainWindow._recover_suspect_profile(stub)

    def test_a_broken_browser_refuses_and_spends_nothing(self):
        stub = _RecoveryStub(("C:/api/.cache/chrome/win64-1/x/chrome.exe",
                              "icudtl.dat is missing"))
        assert self._recover(stub) is False
        # The latch is what makes recovery once-per-launch. Taking it here
        # would mean the real fault, if it turned up later in the same run,
        # got no recovery at all.
        assert getattr(stub, "_profile_recovery_attempted", False) is False

    def test_the_refusal_reaches_the_log_that_survives(self):
        stub = _RecoveryStub(("C:/api/.cache/chrome/win64-1/x/chrome.exe",
                              "icudtl.dat is empty"))
        self._recover(stub)
        assert any("browser payload incomplete" in a for a in stub.audits)
        assert any("icudtl.dat is empty" in a for a in stub.audits)

    def test_the_refusal_is_spoken(self):
        stub = _RecoveryStub(("C:/api/.cache/chrome/win64-1/x/chrome.exe",
                              "icudtl.dat is missing"))
        self._recover(stub)
        assert stub.announced == ["browser_install_broken"]

    def test_a_healthy_browser_does_not_refuse(self):
        """It must fall through to the real recovery — the guard is a
        disqualifier, not a new gate on the feature. This stub has no snapshot
        on disk, so it lands on the pre-existing dead end instead, which is
        exactly the proof wanted: a *different* verdict, reached past the
        guard."""
        stub = _RecoveryStub((None, None))
        assert self._recover(stub) is False
        assert not any("browser payload" in a for a in stub.audits)
        assert stub.announced == ["profile_corrupted_repair_needed"]
        assert stub._profile_recovery_attempted is True


class TestTheStringExistsInEveryLocale:
    def test_browser_install_broken_is_translated(self):
        import json

        languages = Path(__file__).resolve().parents[1] / "client" / "languages"
        for code in ("pt-BR", "pt-PT", "en-US", "es-ES", "pl"):
            data = json.loads((languages / f"{code}.json").read_text(encoding="utf-8"))
            assert data.get("browser_install_broken"), code
