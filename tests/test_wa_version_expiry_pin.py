"""The pinned WhatsApp Web build has to be one Meta still serves.

start.js pins a build from @wppconnect/wa-version's catalogue, and it used to
take `available[available.length - 1]` — the newest entry — validating it with
getPageContent(). That only proves the HTML can be assembled from files on
disk. Meta expires a build on its own side (the catalogue records an ~2-month
window per entry in `released`/`expire`), and getPageContent() keeps succeeding
long after that, so a stale install pins a build WhatsApp no longer serves and
nothing in the log says so.

Seen live: a catalogue of 390 entries pinned 2.3000.1046208945 while WhatsApp
was serving 2.3000.1046553522; reinstalling the API (which updated the package)
pinned 2.3000.1046540740 and everything worked. Until then the only mechanism
protecting the user was "reinstall the API", which nobody knows to do.

The selection is pure logic in JS, so it is exercised as such: the two
functions are sliced out of the real api_patches/start.js source and run under
node against a fake catalogue. Requiring the whole of start.js is not an
option — its top-level code scans for Chrome and will happily start
downloading one.
"""

import json
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
START_JS = ROOT / "client" / "api_patches" / "start.js"

DAY_MS = 24 * 60 * 60 * 1000
NOW_MS = 1_800_000_000_000

HARNESS = r"""
'use strict';
const scenario = JSON.parse(process.argv[2]);

const pageContentAsked = [];
const catalogue = {
  getAvailableVersions: () => scenario.versions.map((v) => v.version),
  getPageContent: (version) => {
    pageContentAsked.push(version);
    const entry = scenario.versions.find((v) => v.version === version);
    if (!entry || entry.htmlMissing) throw new Error('Version not available for ' + version);
    return '<html></html>';
  },
};
if (!scenario.withoutMetadata) {
  catalogue.getVersionInfo = (version) => {
    const entry = scenario.versions.find((v) => v.version === version);
    if (!entry) throw new Error('Version not available for ' + version);
    return { version, beta: false, released: entry.released, expire: entry.expire };
  };
}

__SELECTOR_SOURCE__

const selected = selectServableVersion(catalogue, scenario.now);
console.log('__RESULT__' + JSON.stringify({ selected, pageContentAsked }));
"""


def _selector_source():
    """The real shipped source of versionExpiry()/selectServableVersion().

    Sliced between two anchors that are load-bearing code rather than markers
    left for the test: the first function's definition, and the definition of
    the caller that follows it.
    """
    source = START_JS.read_text(encoding="utf-8")
    start = source.index("function versionExpiry(")
    end = source.index("function resolveWhatsappVersion(", start)
    return source[start:end]


def _select(tmp_path, versions, now=NOW_MS, without_metadata=False):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH")
    harness = tmp_path / "harness.js"
    harness.write_text(
        HARNESS.replace("__SELECTOR_SOURCE__", _selector_source()), encoding="utf-8")
    scenario = {"versions": versions, "now": now, "withoutMetadata": without_metadata}
    proc = subprocess.run(
        [node, str(harness), json.dumps(scenario)],
        capture_output=True, text=True, timeout=60,
    )
    for line in proc.stdout.splitlines():
        if line.startswith("__RESULT__"):
            return json.loads(line[len("__RESULT__"):])
    raise AssertionError(
        f"harness produced no result.\nexit={proc.returncode}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


def _entry(version, expire_offset_days, html_missing=False):
    return {
        "version": version,
        "released": "2026-07-01T00:00:00.000Z",
        "expire": None if expire_offset_days is None
        else _iso(NOW_MS + expire_offset_days * DAY_MS),
        "htmlMissing": html_missing,
    }


def _iso(ms):
    import datetime
    return datetime.datetime.fromtimestamp(
        ms / 1000, datetime.timezone.utc).isoformat().replace("+00:00", "Z")


class TestTheNewestStillValidBuildIsPinned:
    def test_an_expired_newest_entry_is_skipped(self, tmp_path):
        """The whole point: the last entry in the catalogue is not necessarily
        one Meta still serves."""
        result = _select(tmp_path, [
            _entry("2.3000.1000000001", 10),
            _entry("2.3000.1000000002", -1),
            _entry("2.3000.1000000003", -5),
        ])
        assert result["selected"]["version"] == "2.3000.1000000001"
        assert result["selected"]["expired"] is False

    def test_a_valid_newest_entry_is_taken_as_is(self, tmp_path):
        result = _select(tmp_path, [
            _entry("2.3000.1000000001", 10),
            _entry("2.3000.1000000002", 40),
        ])
        assert result["selected"]["version"] == "2.3000.1000000002"

    def test_expiry_is_checked_before_the_html_is_read(self, tmp_path):
        """Reading every HTML file down a 400-entry catalogue is minutes of
        I/O on a machine that is trying to start up."""
        result = _select(tmp_path, [
            _entry("2.3000.1000000001", 10),
            _entry("2.3000.1000000002", -1),
        ])
        assert result["pageContentAsked"] == ["2.3000.1000000001"]

    def test_a_build_this_install_cannot_assemble_is_skipped(self, tmp_path):
        """Unchanged from before: getPageContent() throwing means the HTML is
        not on disk here, so pinning it would land in the silent fallback."""
        result = _select(tmp_path, [
            _entry("2.3000.1000000001", 10),
            _entry("2.3000.1000000002", 40, html_missing=True),
        ])
        assert result["selected"]["version"] == "2.3000.1000000001"


class TestTheStableChannelIsPreferredOverPreRelease:
    """Meta's expiry window says "still served", not "safe to link a device
    against": a pre-release build nowhere near expired still got every
    freshly-paired session unpaired within minutes ("Session Unpaired" then
    "notLogged", on every re-pair attempt). Each tier is therefore walked
    stable-first — while being honest that on the catalogue shipping today it
    changes nothing at all (see the second test)."""

    def test_a_stable_build_wins_over_a_newer_alpha(self, tmp_path):
        result = _select(tmp_path, [
            _entry("2.3000.1000000001", 10),
            _entry("2.3000.1000000002-alpha", 40),
        ])
        assert result["selected"]["version"] == "2.3000.1000000001"

    def test_an_all_alpha_catalogue_still_pins_its_newest_entry(self, tmp_path):
        """The real world today: @wppconnect/wa-version 1.5.4763 ships 392
        entries and every one of them is an -alpha, so the stable pass matches
        nothing and the selection is identical to before the preference
        existed. It must be — the alternative is pinning nothing."""
        result = _select(tmp_path, [
            _entry("2.3000.1000000001-alpha", 10),
            _entry("2.3000.1000000002-alpha", 40),
        ])
        assert result["selected"]["version"] == "2.3000.1000000002-alpha"
        assert result["selected"]["expired"] is False

    def test_the_preference_also_applies_once_everything_has_expired(self, tmp_path):
        """The expired tier is the one a user who has not reinstalled the API
        in months actually lands in — the channel preference is worth as much
        there as anywhere."""
        result = _select(tmp_path, [
            _entry("2.3000.1000000001", -30),
            _entry("2.3000.1000000002-alpha", -2),
        ])
        assert result["selected"]["version"] == "2.3000.1000000001"
        assert result["selected"]["expired"] is True

    def test_the_html_of_a_version_is_only_read_once(self, tmp_path):
        """Each tier is walked twice, so without memoisation the same entry is
        handed to getPageContent up to four times — and one call is a readdir
        of html/ (392 files) plus a ~600 KB read, during session startup."""
        result = _select(tmp_path, [
            _entry("2.3000.1000000001-alpha", 10),
            _entry("2.3000.1000000002", 40, html_missing=True),
        ])
        # The stable pass asks for the newest (stable) entry and is refused;
        # the any-channel pass walks past it again and must not re-read it.
        assert result["pageContentAsked"] == [
            "2.3000.1000000002", "2.3000.1000000001-alpha"]
        assert result["selected"]["version"] == "2.3000.1000000001-alpha"


class TestACatalogueWithNothingValidLeft:
    """The state a user who has not rebuilt the API in months ends up in."""

    def test_the_newest_build_is_still_pinned(self, tmp_path):
        """Running unpinned has a measured cost — WhatsApp Web serves a build
        the bundled wa-js may not support, and sending to an individual
        contact then fails silently. An expired pin fails visibly."""
        result = _select(tmp_path, [
            _entry("2.3000.1000000001", -30),
            _entry("2.3000.1000000002", -2),
        ])
        assert result["selected"]["version"] == "2.3000.1000000002"

    def test_and_it_is_reported_as_expired_so_the_log_can_say_so(self, tmp_path):
        result = _select(tmp_path, [_entry("2.3000.1000000001", -2)])
        assert result["selected"]["expired"] is True

    def test_an_empty_catalogue_pins_nothing(self, tmp_path):
        assert _select(tmp_path, [])["selected"] is None


class TestACatalogueWithoutDates:
    """An older wa-version, or an entry with no metadata: unknown expiry must
    read as "cannot prove it is dead", never as expired — otherwise the fix
    would break installs it was never about."""

    def test_the_newest_entry_is_pinned_unchanged(self, tmp_path):
        result = _select(tmp_path, [
            _entry("2.3000.1000000001", 10),
            _entry("2.3000.1000000002", 40),
        ], without_metadata=True)
        assert result["selected"]["version"] == "2.3000.1000000002"
        assert result["selected"]["expire"] is None
        assert result["selected"]["expired"] is False

    def test_a_single_entry_with_a_null_expire_is_still_usable(self, tmp_path):
        result = _select(tmp_path, [_entry("2.3000.1000000001", None)])
        assert result["selected"]["version"] == "2.3000.1000000001"
        assert result["selected"]["expired"] is False


class TestTheSourceSaysWhatToDoAboutIt:
    """Cheap source checks: the point of the change is that the next user log
    names the cause instead of nobody knowing, so the message itself is part
    of the contract."""

    def test_the_resolver_selects_by_validity_not_by_position(self):
        source = START_JS.read_text(encoding="utf-8")
        assert "selectServableVersion(waVersion, Date.now())" in source
        assert "available[available.length - 1]" not in source

    def test_an_expired_catalogue_is_logged_with_the_command_that_fixes_it(self):
        source = START_JS.read_text(encoding="utf-8")
        assert "EXPIRED" in source
        assert source.count("npm update @wppconnect/wa-version") >= 3


# ── The expired-catalogue fallback ──────────────────────────────────────────
#
# When every local build has expired the old code had two options and both were
# bad: pin a build Meta may already refuse, or run unpinned — and unpinned is
# the one with the measured silent failure (usync hanging, isSendFailure with
# ack 0, REST answering 200). Asking Meta for the document it is serving right
# now beats both: the page is still substituted through our own document-only
# interception, so the backend worker is never starved, and the build is by
# definition one Meta still serves.

LIVE_HARNESS = r"""
'use strict';
const scenario = JSON.parse(process.argv[2]);

const called = [];
let waVersion = null;
if (scenario.channels !== null) {
  waVersion = {};
  for (const [name, behaviour] of Object.entries(scenario.channels)) {
    waVersion[name] = () => {
      called.push(name);
      if (behaviour.mode === 'throws') return Promise.reject(new Error('offline'));
      if (behaviour.mode === 'hangs') return new Promise(() => {});
      return Promise.resolve(behaviour.html);
    };
  }
}

__HELPER_SOURCE__

(async () => {
  const started = Date.now();
  const html = await fetchLiveWhatsappDocument();
  console.log('__RESULT__' + JSON.stringify({
    ok: typeof html === 'string',
    length: typeof html === 'string' ? html.length : 0,
    called,
    elapsed: Date.now() - started,
  }));
  process.exit(0);
})();
"""

GOOD_HTML = "<!doctype html>" + ("x" * 2000) + "web.whatsapp.com"


def _helper_source():
    """The real shipped source of fetchLiveWhatsappDocument(), plus the timeout
    constant it reads.

    Sliced on load-bearing code, like _selector_source() above: a reorder that
    broke the slice raises ValueError here rather than quietly passing.
    """
    source = START_JS.read_text(encoding="utf-8")
    start = source.index("const LIVE_DOCUMENT_FETCH_TIMEOUT_MS")
    end = source.index("const whatsappVersion = resolveWhatsappVersion();", start)
    return source[start:end]


def _fetch_live(tmp_path, mode="ok", html=None, channels=None):
    """Run the real fetchLiveWhatsappDocument() under node.

    ``channels`` gives per-channel behaviour ({name: {"mode", "html"}}) for the
    stable-vs-alpha ordering tests; ``mode``/``html`` is the older shorthand
    that applies the same behaviour to both channels, so the cases written
    before the stable channel existed keep reading the way they did.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH")
    if channels is None:
        if html is None:
            html = GOOD_HTML
        behaviour = {"mode": mode, "html": html}
        if mode == "no-package":
            channels = None
        elif mode == "no-method":
            channels = {}
        else:
            channels = {"fetchLatest": behaviour, "fetchLatestAlpha": behaviour}
    harness = tmp_path / "live_harness.js"
    harness.write_text(
        LIVE_HARNESS.replace("__HELPER_SOURCE__", _helper_source()), encoding="utf-8")
    proc = subprocess.run(
        [node, str(harness), json.dumps({"channels": channels})],
        capture_output=True, text=True, timeout=60,
    )
    for line in proc.stdout.splitlines():
        if line.startswith("__RESULT__"):
            return json.loads(line[len("__RESULT__"):])
    raise AssertionError(
        f"live harness produced no result.\nexit={proc.returncode}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


class TestTheLiveDocumentFallback:
    def test_a_real_document_is_accepted(self, tmp_path):
        assert _fetch_live(tmp_path)["ok"] is True

    def test_a_network_failure_answers_none_instead_of_throwing(self, tmp_path):
        """Startup must survive a dead network: the caller falls back to the
        expired local build, which is still better than running unpinned."""
        assert _fetch_live(tmp_path, mode="throws")["ok"] is False

    def test_a_hanging_fetch_gives_up_on_the_budget(self, tmp_path):
        """Pairing cannot be held hostage by a fetch that never answers."""
        result = _fetch_live(tmp_path, mode="hangs")
        assert result["ok"] is False
        assert result["elapsed"] < 30000, "the timeout did not fire"

    def test_an_older_wa_version_without_the_method_is_not_a_crash(self, tmp_path):
        """fetchLatestAlpha is not in every published copy of the package."""
        assert _fetch_live(tmp_path, mode="no-method")["ok"] is False

    def test_no_package_at_all_is_not_a_crash(self, tmp_path):
        assert _fetch_live(tmp_path, mode="no-package")["ok"] is False

    @pytest.mark.parametrize("body, why", [
        ("<html>Sign in to the hotel wifi</html>", "captive portal"),
        ("", "empty body"),
        ("<html>502 Bad Gateway</html>", "proxy error page"),
    ])
    def test_something_that_is_not_whatsapp_is_refused(self, tmp_path, body, why):
        """Anything can answer an HTTP request. Serving a captive portal AS the
        WhatsApp Web document would read as a WhatsApp bug, not a network one."""
        assert _fetch_live(tmp_path, html=body)["ok"] is False, why

    def test_a_long_page_that_never_mentions_whatsapp_is_refused(self, tmp_path):
        assert _fetch_live(tmp_path, html="<html>" + ("y" * 5000) + "</html>")["ok"] is False


class TestTheLiveFetchAsksTheStableChannelFirst:
    """This branch only runs when the whole local catalogue has expired — i.e.
    for the user least able to diagnose anything. Asking fetchLatestAlpha()
    there, as this used to, handed exactly that user a pre-release build, which
    is the thing the stable-first selection above exists to avoid. Stable
    first; alpha only as the last step before falling back to an expired local
    build."""

    def test_the_stable_channel_is_used_when_it_answers(self, tmp_path):
        result = _fetch_live(tmp_path, channels={
            "fetchLatest": {"mode": "ok", "html": GOOD_HTML},
            "fetchLatestAlpha": {"mode": "ok", "html": GOOD_HTML},
        })
        assert result["ok"] is True
        assert result["called"] == ["fetchLatest"]

    def test_the_alpha_channel_is_the_fallback_when_stable_is_missing(self, tmp_path):
        """Older copies of the package may not export fetchLatest — the typeof
        guard has to keep both directions working, not just the new one."""
        result = _fetch_live(tmp_path, channels={
            "fetchLatestAlpha": {"mode": "ok", "html": GOOD_HTML},
        })
        assert result["ok"] is True
        assert result["called"] == ["fetchLatestAlpha"]

    @pytest.mark.parametrize("stable_mode, stable_html, why", [
        ("throws", None, "the stable request failed outright"),
        ("ok", "<html>Sign in to the hotel wifi</html>", "a captive portal answered it"),
    ])
    def test_a_stable_answer_that_is_not_the_app_shell_falls_through(
            self, tmp_path, stable_mode, stable_html, why):
        result = _fetch_live(tmp_path, channels={
            "fetchLatest": {"mode": stable_mode, "html": stable_html},
            "fetchLatestAlpha": {"mode": "ok", "html": GOOD_HTML},
        })
        assert result["ok"] is True, why
        assert result["called"] == ["fetchLatest", "fetchLatestAlpha"]

    def test_two_channels_do_not_mean_two_timeout_windows(self, tmp_path):
        """The 10 s budget covers the SET. A user on a dead network must not
        wait twice as long for pairing now that there are two channels."""
        result = _fetch_live(tmp_path, channels={
            "fetchLatest": {"mode": "hangs", "html": None},
            "fetchLatestAlpha": {"mode": "hangs", "html": None},
        })
        assert result["ok"] is False
        assert result["elapsed"] < 15000, "the budget was spent per channel"

    def test_the_source_asks_for_the_stable_channel(self):
        source = START_JS.read_text(encoding="utf-8")
        stable = source.index("attempt('fetchLatest')")
        alpha = source.index("attempt('fetchLatestAlpha')")
        assert stable < alpha


class TestTheFallbackIsWiredOnlyToTheExpiredBranch:
    """The live document must NOT become the default. A live build can be ahead
    of what the bundled wa-js supports, which is the same silent-send failure by
    another door; an unexpired pinned build is the known-good pairing."""

    def test_the_flag_is_set_from_the_selection(self):
        source = START_JS.read_text(encoding="utf-8")
        assert "pinnedCatalogueExpired = Boolean(selected.expired);" in source

    def test_the_fetch_is_guarded_by_that_flag(self):
        source = START_JS.read_text(encoding="utf-8")
        guard = source.index("if (pinnedCatalogueExpired) {")
        call = source.index("await fetchLiveWhatsappDocument();")
        assert guard < call, "the live fetch must sit inside the expired-only guard"

    def test_the_local_read_still_happens_when_the_fetch_returned_nothing(self):
        source = START_JS.read_text(encoding="utf-8")
        assert "if (!body) {" in source
        assert "waVersion.getPageContent(version)" in source

    def test_it_never_falls_through_to_running_unpinned(self):
        """The whole point: no path added here leaves WPPConnect to install its
        blanket interception, which starves the backend worker."""
        source = START_JS.read_text(encoding="utf-8")
        start = source.index("if (pinnedCatalogueExpired) {")
        end = source.index("if (body) {", start)
        assert "version = undefined" not in source[start:end]
