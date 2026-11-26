"""Tests for the alpha release channel (client/updater.py).

Context: .github/workflows/alpha-release.yml publishes a build for every
commit that lands on main, tagged with the literal word "alpha". Those builds
live in the same GitHub Releases list as the hand-cut stable releases, so the
updater has to tell the two apart and only ever offer an alpha to a user who
ticked "check for alpha updates" in Settings > General (off by default).

WinZapp's real releases are versioned `0.<minor>.<patch>.<build>beta`
(v0.25.0.0beta and friends), and an alpha is derived from the current stable
one by putting the commit count in the fourth component:
`0.25.0.0beta` + 1500 commits -> `0.25.0.1500alpha`. The ordering that has to
hold, and that these tests pin down, is:

    0.25.0.0beta  <  0.25.0.1500alpha  <  0.25.0.1501alpha  <  0.26.0.0beta

i.e. an alpha is newer than the stable it was cut from, alphas advance among
themselves, and the NEXT stable release outranks every alpha — so an alpha user
is pulled back onto the stable line instead of being stranded on the channel.
"""

import pytest

import updater


def _assets(*names):
    return [
        {"name": n, "browser_download_url": f"https://example.invalid/{n}"}
        for n in names
    ]


def _release(tag, name=None, draft=False, prerelease=False, assets=None):
    return {
        "tag_name": tag,
        "name": name if name is not None else tag,
        "draft": draft,
        "prerelease": prerelease,
        # Every real release carries these three (see the workflows' upload
        # step); tests that care about their absence pass assets= explicitly.
        "assets": _assets("WinZappInstaller.exe", "WinZapp.zip", "SHA256SUMS.txt")
        if assets is None else assets,
    }


# ── is_alpha_release ──────────────────────────────────────────────────────────

def test_alpha_detected_in_tag():
    assert updater.is_alpha_release(_release("v0.25.0.1500alpha", name="Build")) is True


def test_alpha_detected_in_name_only():
    assert updater.is_alpha_release(
        _release("v0.25.0.1500", name="WinZapp Alpha 0.25.0.1500")
    ) is True


def test_stable_release_is_not_alpha():
    """The stable line itself ships beta-suffixed versions (v0.25.0.0beta) —
    those are normal releases, not alpha-channel builds."""
    assert updater.is_alpha_release(_release("v0.25.0.0beta")) is False


def test_prerelease_flag_alone_does_not_make_it_alpha():
    """Unrelated prereleases exist (see .github/workflows/prerelease-test.yml).
    Opting into alpha must not silently opt the user into those as well."""
    assert updater.is_alpha_release(
        _release("v2026.08.20.1200", name="PRÉ-RELEASE DE TESTE", prerelease=True)
    ) is False


# ── find_zip_asset ────────────────────────────────────────────────────────────

def test_zip_asset_prefers_exact_winzapp_zip():
    url = updater.find_zip_asset(_assets("extras.zip", "WinZapp.zip", "SHA256SUMS.txt"))
    assert url.endswith("/WinZapp.zip")


def test_zip_asset_falls_back_to_any_zip():
    url = updater.find_zip_asset(_assets("SHA256SUMS.txt", "portable-build.zip"))
    assert url.endswith("/portable-build.zip")


def test_zip_asset_absent():
    assert updater.find_zip_asset(_assets("SHA256SUMS.txt")) == ""
    assert updater.find_zip_asset([]) == ""
    assert updater.find_zip_asset(None) == ""


# ── select_release ────────────────────────────────────────────────────────────

def test_alpha_skipped_when_channel_disabled():
    releases = [
        _release("v0.25.0.1500alpha"),
        _release("v0.25.0.0beta"),
    ]
    picked = updater.select_release(releases, include_alpha=False)
    assert picked["tag_name"] == "v0.25.0.0beta"


def test_alpha_offered_when_channel_enabled():
    releases = [
        _release("v0.25.0.1500alpha"),
        _release("v0.25.0.0beta"),
    ]
    picked = updater.select_release(releases, include_alpha=True)
    assert picked["tag_name"] == "v0.25.0.1500alpha"


def test_newest_stable_wins_over_older_stable_even_when_alphas_come_first():
    """The listing is ordered by publish date and mixes both channels, so with
    alphas filtered out the newest stable can sit several entries down."""
    releases = [
        _release("v0.25.0.1502alpha"),
        _release("v0.25.0.1501alpha"),
        _release("v0.25.0.0beta"),
        _release("v0.24.2.0beta"),
    ]
    picked = updater.select_release(releases, include_alpha=False)
    assert picked["tag_name"] == "v0.25.0.0beta"


def test_next_stable_outranks_every_alpha():
    """An alpha user must be pulled back onto the stable line as soon as a new
    stable release ships — not stranded on the alpha channel forever."""
    releases = [
        _release("v0.26.0.0beta"),
        _release("v0.25.0.1502alpha"),
    ]
    picked = updater.select_release(releases, include_alpha=True)
    assert picked["tag_name"] == "v0.26.0.0beta"


def test_newest_alpha_wins_among_alphas():
    releases = [
        _release("v0.25.0.1500alpha"),
        _release("v0.25.0.1502alpha"),
        _release("v0.25.0.1501alpha"),
    ]
    picked = updater.select_release(releases, include_alpha=True)
    assert picked["tag_name"] == "v0.25.0.1502alpha"


def test_drafts_are_never_selected():
    """alpha-release.yml uploads into a draft and only publishes it once every
    asset is up, so a cancelled run leaves a draft behind. It must be invisible."""
    releases = [
        _release("v0.25.0.1503alpha", draft=True),
        _release("v0.25.0.1502alpha"),
    ]
    picked = updater.select_release(releases, include_alpha=True)
    assert picked["tag_name"] == "v0.25.0.1502alpha"


def test_release_without_zip_asset_is_skipped_not_blocking():
    """A release whose ZIP upload was cut short must not block updates for
    everyone until the next one is cut — the previous good one still wins."""
    releases = [
        _release("v0.25.0.1503alpha", assets=_assets("SHA256SUMS.txt")),
        _release("v0.25.0.1502alpha"),
    ]
    picked = updater.select_release(releases, include_alpha=True)
    assert picked["tag_name"] == "v0.25.0.1502alpha"


def test_unparseable_tags_are_ignored():
    """The joaopr4 pre-release workflow publishes "-pre" tags that aren't
    WinZapp versions at all; they can't be compared, so they can't be chosen."""
    releases = [
        _release("v2026.08.21.1200-pre"),
        _release("v0.25.0.0beta"),
    ]
    picked = updater.select_release(releases, include_alpha=False)
    assert picked["tag_name"] == "v0.25.0.0beta"


def test_a_neonize_dll_release_is_never_offered_as_a_winzapp_update():
    """neonize-dll.yml publishes the bridge's neonize build in this repository,
    with a .zip asset, under a tag that starts with "neonize-" (the workflow
    refuses any other): it must never reach the updater, on either channel."""
    neonize = _release("neonize-0.6.0-winzapp.1", prerelease=True,
                       assets=_assets("neonize-0.6.0-winzapp-windows-amd64.zip", "SHA256SUMS.txt"))
    for include_alpha in (False, True):
        assert updater.select_release([neonize], include_alpha=include_alpha) is None
        assert updater.select_release([neonize, _release("v0.25.0.0")],
                                      include_alpha=include_alpha)["tag_name"] == "v0.25.0.0"


def test_a_neteq_component_release_is_never_offered_as_a_winzapp_update():
    """neteq-dll.yml publishes the optional call playback component here, under
    a tag that starts with "neteq-" (and carries no zip): never an update."""
    neteq = _release("neteq-1.0.1-winzapp.1", prerelease=True,
                     assets=_assets("winzapp_call_neteq.dll", "winzapp_call_neteq_NOTICES.txt",
                                    "SHA256SUMS.txt"))
    for include_alpha in (False, True):
        assert updater.select_release([neteq], include_alpha=include_alpha) is None
        assert updater.select_release([neteq, _release("v0.25.0.0")],
                                      include_alpha=include_alpha)["tag_name"] == "v0.25.0.0"


def test_returns_none_when_nothing_eligible():
    assert updater.select_release([_release("v0.25.0.1500alpha")], include_alpha=False) is None
    assert updater.select_release([], include_alpha=True) is None


# ── Version comparison across the two channels ────────────────────────────────

def test_alpha_version_parses():
    """The "alpha" suffix alpha-release.yml bakes into the tag has to survive
    parse_version(), or is_newer() would refuse every alpha outright."""
    assert updater.parse_version("0.25.0.1500alpha") == ((0, 25, 0, 1500), "alpha")


def test_alpha_ordering_against_the_stable_line():
    stable_base = "0.25.0.0beta"
    alpha_a     = "0.25.0.1500alpha"
    alpha_b     = "0.25.0.1501alpha"
    next_stable = "0.26.0.0beta"

    # An alpha is newer than the stable release it was cut from...
    assert updater.is_newer(alpha_a, stable_base) is True
    # ...alphas advance among themselves...
    assert updater.is_newer(alpha_b, alpha_a) is True
    # ...and the next stable outranks them all.
    assert updater.is_newer(next_stable, alpha_b) is True
    assert updater.is_newer(alpha_b, next_stable) is False


def test_alpha_ordering_once_the_stable_line_drops_the_suffix():
    """WinZapp ships `0.25.0.0beta` today, but the alpha channel has to keep
    working when releases become plain `a.b.c.d`. The suffix is optional in
    parse_version() and the numeric components are compared first, so the same
    relationships must hold with no suffix at all."""
    assert updater.parse_version("1.0.0.0") == ((1, 0, 0, 0), "")
    assert updater.is_newer("1.0.0.2159alpha", "1.0.0.0") is True      # alpha > its base
    assert updater.is_newer("1.0.0.2160alpha", "1.0.0.2159alpha") is True
    assert updater.is_newer("1.1.0.0", "1.0.0.2160alpha") is True      # next stable wins
    assert updater.is_newer("2.0.0.0", "1.0.0.2160alpha") is True


def test_alpha_ordering_across_the_beta_to_stable_transition():
    """Leaving beta by advancing the numbers keeps every alpha user reachable."""
    alpha_on_beta_line = "0.25.0.2159alpha"
    assert updater.is_newer("1.0.0.0", alpha_on_beta_line) is True
    assert updater.is_newer("0.26.0.0", alpha_on_beta_line) is True
    # Dropping only the suffix does NOT advance past them — that is the case
    # .github/scripts/check_stable_release_ordering.py exists to catch.
    assert updater.is_newer("0.25.0.0", alpha_on_beta_line) is False


def test_select_release_on_a_suffixless_stable_line():
    releases = [_release("v1.0.0.2159alpha"), _release("v1.0.0.0")]
    assert updater.select_release(releases, include_alpha=False)["tag_name"] == "v1.0.0.0"
    assert updater.select_release(releases, include_alpha=True)["tag_name"] == "v1.0.0.2159alpha"
    # And the next stable pulls alpha users back onto the stable line.
    releases.append(_release("v1.1.0.0"))
    assert updater.select_release(releases, include_alpha=True)["tag_name"] == "v1.1.0.0"


def test_a_date_shaped_version_would_break_the_stable_line():
    """Guards the reasoning in alpha-release.yml's version step: a timestamp
    version (2026.08.22.1530) outranks every 0.x stable release, so shipping
    one would mean no future stable release could ever be offered again."""
    assert updater.is_newer("2026.08.22.1530alpha", "0.26.0.0beta") is True
    assert updater.is_newer("0.26.0.0beta", "2026.08.22.1530alpha") is False


# ── UpdateChecker._alpha_enabled ──────────────────────────────────────────────

class _StubMainWindow:
    def __init__(self, settings):
        self.settings = settings


def _checker(settings):
    return updater.UpdateChecker(_StubMainWindow(settings))


def test_alpha_channel_is_off_by_default():
    assert _checker({"general": {}})._alpha_enabled() is False
    assert _checker({})._alpha_enabled() is False


def test_alpha_channel_reads_the_setting():
    assert _checker({"general": {"alpha_updates_enabled": True}})._alpha_enabled() is True
    assert _checker({"general": {"alpha_updates_enabled": False}})._alpha_enabled() is False


def test_alpha_channel_is_read_fresh_each_time():
    """The checker outlives the settings dialog, so ticking the box mid-session
    must take effect without a restart."""
    settings = {"general": {"alpha_updates_enabled": False}}
    checker = _checker(settings)
    assert checker._alpha_enabled() is False
    settings["general"]["alpha_updates_enabled"] = True
    assert checker._alpha_enabled() is True


def test_alpha_channel_survives_broken_settings():
    """A missing/exploded settings object must not take the updater down with
    it — it just means "no alpha"."""
    class _Exploding:
        @property
        def settings(self):
            raise RuntimeError("settings not loaded yet")

    assert updater.UpdateChecker(_Exploding())._alpha_enabled() is False


# ── _fetch_releases: listing + /releases/latest ────────────────────────────────

def _fetching_checker(responses):
    """Build a checker whose _get_json() answers from *responses* (url
    substring -> payload, or an Exception instance to raise)."""
    checker = _checker({"general": {}})
    calls = []

    def _get_json(url, params=None):
        calls.append((url, params))
        for fragment, payload in responses.items():
            if url.endswith(fragment):
                if isinstance(payload, Exception):
                    raise payload
                return payload
        raise AssertionError(f"unexpected url {url}")

    checker._get_json = _get_json
    checker._calls = calls
    return checker


def test_fetch_merges_listing_and_latest_stable():
    """The listing is the only place alphas appear; /releases/latest is the only
    thing that keeps finding the newest STABLE release once alphas fill the
    listing's first page. Both are needed."""
    listing = [_release(f"v0.25.0.{n}alpha") for n in range(1500, 1600)]
    stable  = _release("v0.25.0.0beta")
    checker = _fetching_checker({"/releases/latest": stable, "/releases": listing})

    fetched = checker._fetch_releases()
    tags = {r["tag_name"] for r in fetched}
    assert "v0.25.0.0beta" in tags          # would have been off the page
    assert "v0.25.0.1599alpha" in tags

    # And the stable user still gets the stable release out of it.
    picked = updater.select_release(fetched, include_alpha=False)
    assert picked["tag_name"] == "v0.25.0.0beta"


def test_fetch_asks_for_a_full_page():
    """Default paging is 30; alphas land far more often than stable releases."""
    checker = _fetching_checker({
        "/releases/latest": _release("v0.25.0.0beta"),
        "/releases": [],
    })
    checker._fetch_releases()
    listing_call = [c for c in checker._calls if c[0].endswith("/releases")][0]
    assert listing_call[1] == {"per_page": 100}


def test_fetch_dedupes_a_release_returned_by_both_endpoints():
    stable = dict(_release("v0.25.0.0beta"), id=42)
    checker = _fetching_checker({"/releases/latest": stable, "/releases": [stable]})
    assert len(checker._fetch_releases()) == 1


def test_fetch_survives_a_failing_listing():
    """A rate-limited listing still leaves the stable release reachable."""
    checker = _fetching_checker({
        "/releases/latest": _release("v0.25.0.0beta"),
        "/releases": RuntimeError("HTTP 403 rate limited"),
    })
    fetched = checker._fetch_releases()
    assert [r["tag_name"] for r in fetched] == ["v0.25.0.0beta"]


def test_fetch_survives_a_missing_latest_stable():
    """/releases/latest 404s on a repo that only ever published prereleases."""
    checker = _fetching_checker({
        "/releases/latest": RuntimeError("HTTP 404"),
        "/releases": [_release("v0.25.0.1500alpha")],
    })
    fetched = checker._fetch_releases()
    assert [r["tag_name"] for r in fetched] == ["v0.25.0.1500alpha"]


def test_fetch_raises_only_when_both_endpoints_fail():
    checker = _fetching_checker({
        "/releases/latest": RuntimeError("HTTP 404"),
        "/releases": RuntimeError("HTTP 403 rate limited"),
    })
    with pytest.raises(Exception):
        checker._fetch_releases()


def test_fetch_accepts_a_single_release_object_from_a_fork():
    """A fork could point the configured URL at one release rather than a list."""
    checker = _fetching_checker({
        "/releases/latest": RuntimeError("HTTP 404"),
        "/releases": _release("v0.25.0.0beta"),
    })
    assert [r["tag_name"] for r in checker._fetch_releases()] == ["v0.25.0.0beta"]


def test_configured_stable_url_is_the_listing_url_plus_latest():
    import config
    assert config.GITHUB_API_LATEST_STABLE_RELEASE == (
        config.GITHUB_API_LATEST_RELEASE + "/latest"
    )


# ── End-to-end: _check_once picks the right release and offers it ─────────────

class _I18n:
    # The language the window shows, as core.i18n.I18n always carries it:
    # the updater reads this rather than asking get_language(), which on the
    # window's own instance re-reads the install-wide setting.
    language = "pt-BR"

    def get_language(self):
        return "pt-BR"

    def t(self, key):
        return key


def _end_to_end_checker(monkeypatch, alpha_enabled, releases, local_version):
    """Drive the real _check_once() with the network and wx stubbed out, and
    report what it decided to offer the user."""
    mw = _StubMainWindow({"general": {"alpha_updates_enabled": alpha_enabled}})
    mw.i18n = _I18n()
    checker = updater.UpdateChecker(mw)

    monkeypatch.setattr(updater, "__version__", local_version)
    monkeypatch.setattr(checker, "_fetch_releases", lambda: releases)
    # Don't leave a real 3-hour threading.Timer behind.
    outcome = {"offered": None, "retried": False}
    monkeypatch.setattr(checker, "_schedule_retry", lambda: outcome.update(retried=True))
    monkeypatch.setattr(
        updater.wx, "CallAfter",
        lambda fn, *a, **kw: outcome.update(offered=(getattr(fn, "__name__", ""), a)),
    )

    checker._check_once()
    return outcome


def test_end_to_end_stable_user_is_offered_the_stable_release(monkeypatch):
    releases = [_release("v0.25.0.2154alpha"), _release("v0.25.0.0beta")]
    out = _end_to_end_checker(monkeypatch, False, releases, "0.24.2.0beta")
    assert out["offered"][0] == "_show_update_dialog"
    version, _changelog, zip_url, sums_url = out["offered"][1]
    assert version == "0.25.0.0beta"
    assert zip_url.endswith("/WinZapp.zip")
    assert sums_url.endswith("/SHA256SUMS.txt")


def test_end_to_end_alpha_user_is_offered_the_alpha(monkeypatch):
    releases = [_release("v0.25.0.2154alpha"), _release("v0.25.0.0beta")]
    out = _end_to_end_checker(monkeypatch, True, releases, "0.25.0.0beta")
    assert out["offered"][0] == "_show_update_dialog"
    assert out["offered"][1][0] == "0.25.0.2154alpha"


def test_end_to_end_stable_user_running_latest_is_not_offered_an_alpha(monkeypatch):
    """The regression that matters most: someone on the current stable release
    must be told nothing is available, not handed an alpha build."""
    releases = [_release("v0.25.0.2154alpha"), _release("v0.25.0.0beta")]
    out = _end_to_end_checker(monkeypatch, False, releases, "0.25.0.0beta")
    assert out["offered"] is None
    assert out["retried"] is True


def test_end_to_end_alpha_user_gets_pulled_back_to_the_next_stable(monkeypatch):
    releases = [_release("v0.26.0.0beta"), _release("v0.25.0.2154alpha")]
    out = _end_to_end_checker(monkeypatch, True, releases, "0.25.0.2154alpha")
    assert out["offered"][1][0] == "0.26.0.0beta"


def test_end_to_end_network_failure_just_retries(monkeypatch):
    mw = _StubMainWindow({"general": {}})
    mw.i18n = _I18n()
    checker = updater.UpdateChecker(mw)
    retried = []
    monkeypatch.setattr(checker, "_fetch_releases", lambda: (_ for _ in ()).throw(RuntimeError("no net")))
    monkeypatch.setattr(checker, "_schedule_retry", lambda interval=None: retried.append(interval))
    checker._check_once()
    # A failed fetch retries soon first (see UpdateChecker._FETCH_RETRY_DELAYS).
    assert retried == [updater.UpdateChecker._FETCH_RETRY_DELAYS[0]]


# ── Global (cross-account) setting ────────────────────────────────────────────

def test_alpha_setting_is_install_wide(tmp_path):
    """One install runs one binary, so the alpha opt-in can't be per-account."""
    import os
    import app_settings as aset

    gd = str(tmp_path / "global")
    os.makedirs(gd, exist_ok=True)
    s = aset.AppSettings(gd)
    assert s.get("alpha_updates_enabled") is False
    s.set("alpha_updates_enabled", True)
    assert aset.AppSettings(gd).get("alpha_updates_enabled") is True
    assert "alpha_updates_enabled" in aset._GENERAL_GLOBAL


def test_alpha_setting_extracted_from_legacy_settings():
    import app_settings as aset

    glob, per = aset.split_legacy_settings(
        {"general": {"alpha_updates_enabled": True, "notifications_enabled": True}}
    )
    assert glob["alpha_updates_enabled"] is True
    assert "alpha_updates_enabled" not in per["general"]


# ── Re-confirming the newest release after the user says Yes ─────────────────

def _full_release(tag):
    return _release(tag, assets=_assets("WinZapp.zip"))


def test_a_newer_alpha_published_meanwhile_replaces_the_offered_one():
    releases = [_full_release("v0.25.0.1500alpha"), _full_release("v0.25.0.1502alpha"),
                _full_release("v0.25.0.1501alpha")]
    got = updater.newer_release_than_offered(releases, True, "0.25.0.0beta", "0.25.0.1501alpha")
    assert got["tag_name"] == "v0.25.0.1502alpha"


def test_the_offered_release_still_the_newest_is_kept():
    releases = [_full_release("v0.25.0.1501alpha"), _full_release("v0.25.0.1500alpha")]
    assert updater.newer_release_than_offered(
        releases, True, "0.25.0.0beta", "0.25.0.1501alpha") is None


def test_an_alpha_is_never_swapped_in_when_the_channel_is_off():
    releases = [_full_release("v0.25.0.1502alpha"), _full_release("v0.26.0.0beta")]
    got = updater.newer_release_than_offered(releases, False, "0.25.0.0beta", "0.26.0.0beta")
    assert got is None


def test_nothing_is_swapped_in_when_no_release_is_listed():
    assert updater.newer_release_than_offered([], True, "0.25.0.0beta", "0.25.0.1501alpha") is None


class _Stub:
    """UpdateChecker with just what the reconfirmation touches."""

    def __init__(self, releases=None, fail=False, shutting_down=False):
        self._releases, self._fail = releases or [], fail
        self._mw = type("MW", (), {"_shutting_down": shutting_down})()
        self._install_pending = True
        self.calls = []

    def _fetch_releases(self):
        if self._fail:
            raise RuntimeError("offline")
        return self._releases

    def _alpha_enabled(self):
        return True

    def _do_install(self, *args):
        self.calls.append(("install", args))

    def _release_prompt(self):
        self.calls.append(("release",))

    def _install_reconfirmed(self, *args):
        pass

    def _schedule_retry(self):
        self.calls.append(("retry",))


def _run_worker(stub, monkeypatch, offered):
    import update_reconfirm as ur
    sent = []
    monkeypatch.setattr(ur.wx, "CallAfter", lambda fn, *a: sent.append((fn, a)), raising=False)
    monkeypatch.setattr(ur, "__version__", "0.25.0.0beta")
    ur.ReconfirmMixin._reconfirm_worker(stub, *offered)
    assert len(sent) == 1
    return sent[0][1]


OFFERED = ("0.25.0.1501alpha", "https://x/zip", "https://x/sums", "", True)


def test_worker_hands_over_the_newer_release(monkeypatch):
    newer = _release("v0.25.0.1502alpha", assets=_assets("WinZapp.zip", "SHA256SUMS.txt"))
    args = _run_worker(_Stub([newer]), monkeypatch, OFFERED)
    assert args[0] == "0.25.0.1502alpha"
    assert args[1].endswith("WinZapp.zip") and args[2].endswith("SHA256SUMS.txt")
    assert args[4] is True


def test_worker_keeps_the_offered_release_when_the_fetch_fails(monkeypatch):
    assert _run_worker(_Stub(fail=True), monkeypatch, OFFERED) == OFFERED


def test_worker_ignores_a_newer_release_without_a_zip(monkeypatch):
    broken = _release("v0.25.0.1502alpha", assets=_assets("SHA256SUMS.txt"))
    assert _run_worker(_Stub([broken]), monkeypatch, OFFERED) == OFFERED


def test_a_release_not_newer_than_the_running_version_is_not_swapped_in():
    releases = [_full_release("v0.25.0.1500alpha")]
    assert updater.newer_release_than_offered(
        releases, True, "0.25.0.1600alpha", "0.25.0.1501alpha") is None


def test_install_hands_over_and_clears_the_pending_guard():
    import update_reconfirm as ur
    stub = _Stub()
    ur.ReconfirmMixin._install_reconfirmed(stub, *OFFERED)
    assert stub._install_pending is False
    assert stub.calls == [("install", OFFERED)]


def test_install_while_shutting_down_releases_the_prompt():
    import update_reconfirm as ur
    stub = _Stub(shutting_down=True)
    ur.ReconfirmMixin._install_reconfirmed(stub, *OFFERED)
    assert stub.calls == [("release",), ("retry",)]
