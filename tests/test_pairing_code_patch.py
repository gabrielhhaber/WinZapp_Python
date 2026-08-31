"""Tests for the pairing-code-rotation patch (GitHub issue #8) and its
later corrections.

Timeline:

* v0 (upstream bug, wppconnect-team/wppconnect#2836): host.layer.js's
  checkQrCode() dedupes the QR-image branch against `this.urlCode` before
  re-emitting it, but the phoneNumber (pairing-code) branch returns
  straight into loginByCode() with no equivalent guard — so every
  ~20-60s WhatsApp-side QR rotation generates a BRAND NEW pairing code,
  faster than a screen-reader user can read an 8-character code.

* v1 (WinZapp's first fix, shipped, then found unsafe): a
  `linkCodeGenerated` latch set to True BEFORE loginByCode() actually
  produced a code, cleared only on a successful login. Reported live: the
  fast rotation stopped, but the code then never updated again even after
  10 minutes — because the latch never gets reset if a refresh is ever
  legitimately needed (or if the very first loginByCode() call failed).

* v2: a 60-second reuse cooldown instead of a permanent latch, with the
  "issued" timestamp only recorded AFTER a code is actually produced, so a
  failed attempt self-recovers on the next tick instead of freezing forever.

* v3: v2 plus a catch around the loginByCode() call, and a companion patch to
  loginByCode() itself. v2's try/finally had no catch, so a rejected
  loginByCode() escaped checkQrCode() — which is called fire-and-forget — as
  an unhandled rejection, and the underlying browser error had already been
  flattened to the minified "t: t" by crossing the CDP exception boundary.
  Observed live: pairing simply never produced a code, the Python side sat
  out its full 90-second wait, and the only trace anywhere was "Unhandled
  Rejection: t: t" in wppconnect.log.

* v4: v3 plus a `catchLinkCodeError` hook, so the caught error actually
  reaches the person trying to pair. v3 made the failure real and non-fatal,
  but it still only ever landed in wppconnect.log — the user was left with the
  same generic "no pairing code received" after 90 seconds. The end-to-end
  path is covered by tests/test_pairing_code_error_reporting.py.

* v6 (current): the phoneNumber branch waits for WhatsApp Web's auth state
  before calling the link-device API, and getQrCode() reads the payload from
  wa-js instead of scraping the DOM. Both pairing routes were dead at the same
  time for unrelated reasons that looked identical from the outside (nothing
  appears on screen): the code threw Invariant Violation #56367 because v1..v5
  had hoisted its branch above the `await this.getQrCode()` that used to
  guarantee the auth state existed, and the QR emitted nothing because
  upstream's scraper looks for a <canvas> WhatsApp Web no longer renders —
  landing instead on the download banner's `https://wa.me/...` data-ref, which
  is not a login payload at all.

* v5: a doubling backoff between consecutive failures. v2's cooldown
  only ever gates a success, so a run of failures was paced by nothing at all —
  measured live at one attempt every 20 seconds, nine and counting, which for a
  failure that is plausibly rate-limiting made the problem self-sustaining.

Both setup_api.py and ApiSetupDialog (client/ui/dialogs/api_setup.py) apply
the same patch (see the "why two places" comment in api_setup.py). Since v3
both delegate the actual search-and-replace to patch_host_layer_source() in
client/core/wppconnect_host_layer_patch.py, so this file exercises that
shared module plus each of the two patch-applying entry points.
"""

import importlib.util
import os

import pytest

from core.wppconnect_host_layer_patch import (
    ORIGINAL_CHECK_QR_CODE, V1_CHECK_QR_CODE, V2_CHECK_QR_CODE,
    V3_CHECK_QR_CODE, V4_CHECK_QR_CODE, V5_CHECK_QR_CODE, V6_CHECK_QR_CODE,
    PATCHED_CHECK_QR_CODE,
    ORIGINAL_GET_QR_CODE, PATCHED_GET_QR_CODE,
    ORIGINAL_WAIT_FOR_QR_CODE_SCAN, PATCHED_WAIT_FOR_QR_CODE_SCAN,
    ORIGINAL_LOGIN_BY_CODE, LEGACY_LOGIN_BY_CODE_RAW, PATCHED_LOGIN_BY_CODE,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_setup_api():
    """setup_api.py lives at the repo root, outside pytest's `client`
    pythonpath — load it directly by file path."""
    spec = importlib.util.spec_from_file_location(
        "setup_api", os.path.join(REPO_ROOT, "setup_api.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fake_wppconnect_dist(tmp_path):
    """.../node_modules/@wppconnect-team/wppconnect/dist/api/layers/host.layer.js"""
    layers_dir = tmp_path / "node_modules" / "@wppconnect-team" / "wppconnect" / "dist" / "api" / "layers"
    layers_dir.mkdir(parents=True)
    host_layer = layers_dir / "host.layer.js"
    return tmp_path, host_layer


def _write(host_layer, checkqrcode_text, loginbycode_text=ORIGINAL_LOGIN_BY_CODE,
           getqrcode_text=ORIGINAL_GET_QR_CODE,
           waitforscan_text=ORIGINAL_WAIT_FOR_QR_CODE_SCAN):
    """Wrap the (v0/v1/v2/v3) checkQrCode() body in enough surrounding class
    boilerplate to look like the real compiled file, without needing the
    other unrelated methods.

    loginByCode() and getQrCode() come from the shared constants verbatim
    rather than being paraphrased here: the patcher rewrites those methods
    too, so an approximate copy would make every test in this file see a
    spurious "DID NOT MATCH" for a file the real patcher handles fine."""
    host_layer.write_text(
        "class HostLayer {\n"
        "    urlCode = '';\n"
        "    attempt = 0;\n"
        + checkqrcode_text
        + getqrcode_text
        + waitforscan_text
        + loginbycode_text +
        "}\n",
        encoding="utf-8",
    )


class TestSharedPatchTextsAreDistinct:
    """Guards against a future accidental edit collapsing two of the known
    variants back to identical text, which would silently break the
    idempotency/upgrade detection all the tests below rely on."""

    def test_the_known_variants_are_all_different(self):
        assert ORIGINAL_CHECK_QR_CODE != V1_CHECK_QR_CODE
        assert V1_CHECK_QR_CODE != PATCHED_CHECK_QR_CODE
        assert ORIGINAL_CHECK_QR_CODE != PATCHED_CHECK_QR_CODE

    def test_v2_never_permanently_latches(self):
        """The core correctness property distinguishing v2 from the unsafe
        v1: the "issued" flag must only be set AFTER loginByCode() returns,
        never before it — so a rejected call can't freeze the code forever."""
        issued_at_assignment = PATCHED_CHECK_QR_CODE.index("this.linkCodeIssuedAt = Date.now();")
        login_by_code_call = PATCHED_CHECK_QR_CODE.index("await this.loginByCode(this.options.phoneNumber);")
        assert login_by_code_call < issued_at_assignment

    def test_v2_uses_a_bounded_cooldown_not_an_unconditional_return(self):
        assert "linkCodeIssuedAt" in PATCHED_CHECK_QR_CODE
        assert "60000" in PATCHED_CHECK_QR_CODE

    def test_v1_is_the_unsafe_pre_set_latch_reported_live(self):
        """Documents exactly what made v1 unsafe: the latch write happens
        BEFORE the loginByCode() call, so a rejected/failed call still
        leaves the latch set — this is what froze the pairing code."""
        latch_set = V1_CHECK_QR_CODE.index("this.linkCodeGenerated = true;")
        login_by_code_call = V1_CHECK_QR_CODE.index("return this.loginByCode(this.options.phoneNumber);")
        assert latch_set < login_by_code_call


class TestSetupApiPatch:
    """setup_api.py's _patch_wppconnect_host_layer(client_api_dir)."""

    def test_patches_a_pristine_file_to_the_current_version(self, fake_wppconnect_dist):
        setup_api = _load_setup_api()
        api_dir, host_layer = fake_wppconnect_dist
        _write(host_layer, ORIGINAL_CHECK_QR_CODE)

        ok = setup_api._patch_wppconnect_host_layer(str(api_dir))

        assert ok is True
        assert PATCHED_CHECK_QR_CODE in host_layer.read_text(encoding="utf-8")

    def test_upgrades_an_existing_v1_installation(self, fake_wppconnect_dist):
        """The exact scenario from the live report: a machine that already
        got the unsafe v1 patch must be automatically upgraded to the
        current version on its next npm install / setup_api.py run, not
        left stuck."""
        setup_api = _load_setup_api()
        api_dir, host_layer = fake_wppconnect_dist
        _write(host_layer, V1_CHECK_QR_CODE)

        ok = setup_api._patch_wppconnect_host_layer(str(api_dir))

        assert ok is True
        content = host_layer.read_text(encoding="utf-8")
        assert PATCHED_CHECK_QR_CODE in content
        assert "linkCodeGenerated" not in content

    def test_is_idempotent_once_applied(self, fake_wppconnect_dist):
        setup_api = _load_setup_api()
        api_dir, host_layer = fake_wppconnect_dist
        _write(host_layer, ORIGINAL_CHECK_QR_CODE)

        setup_api._patch_wppconnect_host_layer(str(api_dir))
        first_pass = host_layer.read_text(encoding="utf-8")
        ok = setup_api._patch_wppconnect_host_layer(str(api_dir))
        second_pass = host_layer.read_text(encoding="utf-8")

        assert ok is True
        assert first_pass == second_pass

    def test_missing_file_is_a_safe_no_op(self, tmp_path):
        setup_api = _load_setup_api()
        ok = setup_api._patch_wppconnect_host_layer(str(tmp_path))
        assert ok is False

    def test_unrecognized_source_is_left_untouched(self, fake_wppconnect_dist):
        setup_api = _load_setup_api()
        api_dir, host_layer = fake_wppconnect_dist
        host_layer.write_text("// a future wppconnect rewrote this file entirely\n", encoding="utf-8")

        ok = setup_api._patch_wppconnect_host_layer(str(api_dir))

        assert ok is False
        content = host_layer.read_text(encoding="utf-8")
        assert "linkCodeIssuedAt" not in content
        assert "linkCodeGenerated" not in content


class TestApiSetupDialogPatch:
    """ApiSetupDialog's copy — a wx.Dialog subclass, but the patch method
    is a @staticmethod that touches no wx widgets, so it's callable
    directly without a running wx.App."""

    def _wppconnect_api_dir(self, api_dir):
        return str(api_dir / "node_modules" / "@wppconnect-team" / "wppconnect" / "dist" / "api")

    def test_patches_a_pristine_file_to_the_current_version(self, fake_wppconnect_dist):
        from ui.dialogs.api_setup import ApiSetupDialog
        api_dir, host_layer = fake_wppconnect_dist
        _write(host_layer, ORIGINAL_CHECK_QR_CODE)

        ok = ApiSetupDialog._patch_wppconnect_host_layer(self._wppconnect_api_dir(api_dir))

        assert ok is True
        assert PATCHED_CHECK_QR_CODE in host_layer.read_text(encoding="utf-8")

    def test_upgrades_an_existing_v1_installation(self, fake_wppconnect_dist):
        from ui.dialogs.api_setup import ApiSetupDialog
        api_dir, host_layer = fake_wppconnect_dist
        _write(host_layer, V1_CHECK_QR_CODE)

        ok = ApiSetupDialog._patch_wppconnect_host_layer(self._wppconnect_api_dir(api_dir))

        assert ok is True
        content = host_layer.read_text(encoding="utf-8")
        assert PATCHED_CHECK_QR_CODE in content
        assert "linkCodeGenerated" not in content

    def test_is_idempotent_once_applied(self, fake_wppconnect_dist):
        from ui.dialogs.api_setup import ApiSetupDialog
        api_dir, host_layer = fake_wppconnect_dist
        _write(host_layer, ORIGINAL_CHECK_QR_CODE)

        ApiSetupDialog._patch_wppconnect_host_layer(self._wppconnect_api_dir(api_dir))
        first_pass = host_layer.read_text(encoding="utf-8")
        ApiSetupDialog._patch_wppconnect_host_layer(self._wppconnect_api_dir(api_dir))
        assert host_layer.read_text(encoding="utf-8") == first_pass

    def test_apply_node_modules_patches_also_copies_decrypt_js(self, fake_wppconnect_dist):
        """_apply_node_modules_patches() is the entry point actually wired
        into the end-user install flow — it must copy decrypt.js into
        node_modules AND apply the host.layer.js patch, since previously
        neither ever reached node_modules for a real end-user install."""
        from ui.dialogs.api_setup import ApiSetupDialog
        api_dir, host_layer = fake_wppconnect_dist
        _write(host_layer, ORIGINAL_CHECK_QR_CODE)
        (api_dir / "decrypt.js").write_text("// patched decrypt.js\n", encoding="utf-8")

        ApiSetupDialog._apply_node_modules_patches(str(api_dir))

        decrypt_dest = (
            api_dir / "node_modules" / "@wppconnect-team" / "wppconnect"
            / "dist" / "api" / "helpers" / "decrypt.js"
        )
        assert decrypt_dest.is_file()
        assert decrypt_dest.read_text(encoding="utf-8") == "// patched decrypt.js\n"
        assert PATCHED_CHECK_QR_CODE in host_layer.read_text(encoding="utf-8")

    def test_apply_node_modules_patches_never_raises_when_nothing_is_there(self, tmp_path):
        from ui.dialogs.api_setup import ApiSetupDialog
        # No decrypt.js, no node_modules at all — must not raise.
        ApiSetupDialog._apply_node_modules_patches(str(tmp_path))


class TestBothEntryPointsAgree:
    """setup_api.py and ApiSetupDialog must patch to byte-identical text —
    the whole reason wppconnect_host_layer_patch.py exists as a shared
    module instead of two hand-duplicated copies (which is exactly how the
    v1 -> v2 correction risked applying to only one of the two paths)."""

    def test_both_produce_the_same_output_from_a_pristine_file(self, tmp_path):
        from ui.dialogs.api_setup import ApiSetupDialog
        setup_api = _load_setup_api()

        api_dir_a = tmp_path / "a"
        layers_a = api_dir_a / "node_modules" / "@wppconnect-team" / "wppconnect" / "dist" / "api" / "layers"
        layers_a.mkdir(parents=True)
        host_layer_a = layers_a / "host.layer.js"
        _write(host_layer_a, ORIGINAL_CHECK_QR_CODE)

        api_dir_b = tmp_path / "b"
        layers_b = api_dir_b / "node_modules" / "@wppconnect-team" / "wppconnect" / "dist" / "api" / "layers"
        layers_b.mkdir(parents=True)
        host_layer_b = layers_b / "host.layer.js"
        _write(host_layer_b, ORIGINAL_CHECK_QR_CODE)

        setup_api._patch_wppconnect_host_layer(str(api_dir_a))
        ApiSetupDialog._patch_wppconnect_host_layer(
            str(api_dir_b / "node_modules" / "@wppconnect-team" / "wppconnect" / "dist" / "api")
        )

        assert host_layer_a.read_text(encoding="utf-8") == host_layer_b.read_text(encoding="utf-8")


class TestV3CatchesPairingCodeFailures:
    """v3's addition to v2: the `await this.loginByCode(...)` inside
    checkQrCode() is wrapped in a catch.

    Without it a rejected loginByCode() propagated straight out of
    checkQrCode() — which host.layer.js calls fire-and-forget, both from its
    own initialize path and from the exposed `conn.auth_code_change`
    handler, with nobody awaiting or catching it. Observed live: a bare
    "Unhandled Rejection: t: t" in wppconnect.log, that checkQrCode() tick
    killed before it could do anything else, and the Python side left to sit
    out its full 90-second _phone_code_event wait before reporting the
    generic "no pairing code received" with nothing in log.log explaining
    why.
    """

    def test_v3_catches_a_failing_login_by_code(self):
        assert "catch (error) {" in PATCHED_CHECK_QR_CODE
        assert "Could not generate the pairing code" in PATCHED_CHECK_QR_CODE

    def test_v2_had_no_catch_at_all(self):
        """Documents precisely what v3 fixes — v2 has the try/finally but no
        catch, which is what let the rejection escape."""
        assert "try {" in V2_CHECK_QR_CODE
        assert "finally {" in V2_CHECK_QR_CODE
        # Not a bare "catch": v2 legitimately contains catchQR?.() and the
        # needsToScan(...).catch(() => null) chain — neither of which handles
        # a rejected loginByCode().
        assert "catch (" not in V2_CHECK_QR_CODE

    def test_v3_still_never_permanently_latches(self):
        """v3 must not regress v2's core self-recovery property: the
        "issued" timestamp is still only written AFTER loginByCode()
        succeeds, so a caught failure leaves it untouched and the next
        auth_code_change tick retries."""
        issued_at = PATCHED_CHECK_QR_CODE.index("this.linkCodeIssuedAt = Date.now();")
        login_call = PATCHED_CHECK_QR_CODE.index("await this.loginByCode(this.options.phoneNumber);")
        # Anchored AFTER the call: v7 added a catch at the head of the
        # method, so the first occurrence is no longer this branch's.
        catch_block = PATCHED_CHECK_QR_CODE.index("catch (error) {", login_call)
        assert login_call < issued_at < catch_block

    def test_every_checkqrcode_generation_is_distinct(self):
        """Each generation is a rung on the migration ladder — two of them
        collapsing to identical text would silently break the upgrade
        detection every test here relies on."""
        variants = [
            ORIGINAL_CHECK_QR_CODE, V1_CHECK_QR_CODE,
            V2_CHECK_QR_CODE, V3_CHECK_QR_CODE, PATCHED_CHECK_QR_CODE,
        ]
        assert len(set(variants)) == 5


class TestV4ReportsTheFailureToTheClient:
    """v4's addition to v3: the caught error is also handed to a
    `catchLinkCodeError` callback, so it can reach the person trying to pair
    instead of dying in wppconnect.log."""

    def test_v4_calls_the_hook_with_the_real_error(self):
        assert "this.options.catchLinkCodeError?.(" in PATCHED_CHECK_QR_CODE
        assert "name: String(error?.name || 'Error')," in PATCHED_CHECK_QR_CODE
        assert "message: String(error?.message || error)," in PATCHED_CHECK_QR_CODE

    def test_v3_had_no_hook(self):
        assert "catchLinkCodeError" not in V3_CHECK_QR_CODE

    def test_the_hook_is_optional(self):
        """Read through `?.` off this.options: WPPConnect knows nothing about
        this key, so anything not passing it (an older createSessionUtil, or a
        direct wppconnect user) must be an ordinary no-op, never a TypeError
        inside the catch block that is itself handling an error."""
        hook = PATCHED_CHECK_QR_CODE[
            PATCHED_CHECK_QR_CODE.index("catchLinkCodeError")
            - len("this.options.") :
        ]
        assert hook.startswith("this.options.catchLinkCodeError?.(")

    def test_v4_still_logs_as_well_as_reports(self):
        """The log line is the record that survives a closed dialog — the hook
        does not replace it."""
        assert "Could not generate the pairing code" in PATCHED_CHECK_QR_CODE

    def test_v4_still_never_permanently_latches(self):
        issued_at = PATCHED_CHECK_QR_CODE.index("this.linkCodeIssuedAt = Date.now();")
        login_call = PATCHED_CHECK_QR_CODE.index("await this.loginByCode(this.options.phoneNumber);")
        # Anchored AFTER the call: v7 added a catch at the head of the
        # method, so the first occurrence is no longer this branch's.
        catch_block = PATCHED_CHECK_QR_CODE.index("catch (error) {", login_call)
        assert login_call < issued_at < catch_block


class TestUpgradeFromV3:
    """The realistic upgrade path for anyone who ran the build that shipped
    v3: checkQrCode must move v3 -> v4 while loginByCode, already patched, is
    left exactly as it is."""

    def test_v3_install_is_upgraded_to_v4(self, fake_wppconnect_dist):
        setup_api = _load_setup_api()
        api_dir, host_layer = fake_wppconnect_dist
        _write(host_layer, V3_CHECK_QR_CODE, PATCHED_LOGIN_BY_CODE)

        assert setup_api._patch_wppconnect_host_layer(str(api_dir)) is True

        content = host_layer.read_text(encoding="utf-8")
        assert PATCHED_CHECK_QR_CODE in content
        assert V3_CHECK_QR_CODE not in content
        assert PATCHED_LOGIN_BY_CODE in content

    def test_both_entry_points_agree_on_the_v3_upgrade(self, tmp_path):
        from ui.dialogs.api_setup import ApiSetupDialog
        setup_api = _load_setup_api()

        outputs = []
        for name in ("setup_api", "api_setup"):
            api_dir = tmp_path / name
            layers = api_dir / "node_modules" / "@wppconnect-team" / "wppconnect" / "dist" / "api" / "layers"
            layers.mkdir(parents=True)
            host_layer = layers / "host.layer.js"
            _write(host_layer, V3_CHECK_QR_CODE, PATCHED_LOGIN_BY_CODE)

            if name == "setup_api":
                setup_api._patch_wppconnect_host_layer(str(api_dir))
            else:
                ApiSetupDialog._patch_wppconnect_host_layer(
                    str(api_dir / "node_modules" / "@wppconnect-team" / "wppconnect" / "dist" / "api")
                )
            outputs.append(host_layer.read_text(encoding="utf-8"))

        assert outputs[0] == outputs[1]


class TestLoginByCodeErrorDetail:
    """The pairing-code request itself must report the real browser-side
    error instead of the minified "t: t" that a page-context exception
    crossing the CDP boundary raw degrades into — same root cause and same
    fix as the sendFile() error-detail patch in
    wppconnect_sender_layer_patch.py."""

    def test_patched_catches_inside_the_page_and_returns_plain_data(self):
        """The fix only works if the error is caught INSIDE the page
        callback and RETURNED (structured cloning preserves plain string
        properties) rather than thrown across the CDP exception boundary."""
        assert "__winzappError" in PATCHED_LOGIN_BY_CODE
        page_callback_start = PATCHED_LOGIN_BY_CODE.index("async ({ phone }) => {")
        page_callback_end = PATCHED_LOGIN_BY_CODE.index("}, { phone });")
        page_body = PATCHED_LOGIN_BY_CODE[page_callback_start:page_callback_end]
        assert "catch (error) {" in page_body
        assert "return {" in page_body

    def test_patched_rethrows_a_real_error_on_the_node_side(self):
        assert "new Error(outcome.__winzappError.message)" in PATCHED_LOGIN_BY_CODE
        assert "throw failure;" in PATCHED_LOGIN_BY_CODE

    def test_original_had_no_error_handling_at_all(self):
        # "catch (" rather than "catch": the unpatched method already ends in
        # this.catchLinkCode?.(code), which is not error handling.
        assert "catch (" not in ORIGINAL_LOGIN_BY_CODE
        assert "__winzappError" not in ORIGINAL_LOGIN_BY_CODE

    def test_patched_still_delivers_the_code_on_success(self):
        """The happy path must be unchanged: catchLinkCode still receives
        the generated code."""
        assert "this.catchLinkCode?.(code);" in PATCHED_LOGIN_BY_CODE
        assert "const code = outcome?.code;" in PATCHED_LOGIN_BY_CODE

    def test_login_by_code_is_actually_patched_by_both_entry_points(self, tmp_path):
        from ui.dialogs.api_setup import ApiSetupDialog
        setup_api = _load_setup_api()

        results = {}
        for name, apply in (
            ("setup_api", lambda d: setup_api._patch_wppconnect_host_layer(str(d))),
            ("api_setup", lambda d: ApiSetupDialog._patch_wppconnect_host_layer(
                str(d / "node_modules" / "@wppconnect-team" / "wppconnect" / "dist" / "api"))),
        ):
            api_dir = tmp_path / name
            layers = api_dir / "node_modules" / "@wppconnect-team" / "wppconnect" / "dist" / "api" / "layers"
            layers.mkdir(parents=True)
            host_layer = layers / "host.layer.js"
            _write(host_layer, ORIGINAL_CHECK_QR_CODE)

            assert apply(api_dir) is True
            content = host_layer.read_text(encoding="utf-8")
            assert PATCHED_LOGIN_BY_CODE in content
            assert ORIGINAL_LOGIN_BY_CODE not in content
            results[name] = content

        assert results["setup_api"] == results["api_setup"]


class TestUpgradeFromAnAlreadyPatchedInstall:
    """The realistic upgrade path: an existing user's machine already
    carries v2 + an unpatched loginByCode (exactly what shipped before this
    change), and the next setup run must migrate both halves."""

    def test_v2_install_is_upgraded_to_v3_with_login_by_code_patched(self, fake_wppconnect_dist):
        setup_api = _load_setup_api()
        api_dir, host_layer = fake_wppconnect_dist
        _write(host_layer, V2_CHECK_QR_CODE)

        assert setup_api._patch_wppconnect_host_layer(str(api_dir)) is True

        content = host_layer.read_text(encoding="utf-8")
        assert PATCHED_CHECK_QR_CODE in content
        assert V2_CHECK_QR_CODE not in content
        assert PATCHED_LOGIN_BY_CODE in content

    def test_a_fully_patched_install_is_left_byte_identical(self, fake_wppconnect_dist):
        setup_api = _load_setup_api()
        api_dir, host_layer = fake_wppconnect_dist
        _write(host_layer, ORIGINAL_CHECK_QR_CODE)

        setup_api._patch_wppconnect_host_layer(str(api_dir))
        first = host_layer.read_text(encoding="utf-8")
        setup_api._patch_wppconnect_host_layer(str(api_dir))
        assert host_layer.read_text(encoding="utf-8") == first

    def test_an_unrecognised_file_is_reported_and_left_untouched(self, fake_wppconnect_dist):
        """A future upstream release that rewrites these methods must not be
        silently corrupted — the patcher returns False and writes nothing."""
        setup_api = _load_setup_api()
        api_dir, host_layer = fake_wppconnect_dist
        host_layer.write_text("class HostLayer { /* upstream moved on */ }\n", encoding="utf-8")
        before = host_layer.read_text(encoding="utf-8")

        assert setup_api._patch_wppconnect_host_layer(str(api_dir)) is False
        assert host_layer.read_text(encoding="utf-8") == before


class TestV5BacksOffBetweenFailures:
    """v5's addition to v4: consecutive failures back off instead of retrying
    on every auth-code rotation.

    v2's 60s cooldown only ever gates a *success* — linkCodeIssuedAt is
    written when a code is produced, so through a run of failures it stays 0
    and the cooldown check never fires. Measured on a real failing run: nine
    attempts, one every 20 seconds, with nothing pacing them but WhatsApp's
    own rotation rate.
    """

    def test_v4_had_no_backoff(self):
        assert "linkCodeRetryAfter" not in V4_CHECK_QR_CODE
        assert "linkCodeFailures" not in V4_CHECK_QR_CODE

    def test_v5_gates_on_a_retry_deadline(self):
        assert (
            "if (this.linkCodeRetryAfter && now < this.linkCodeRetryAfter) {"
            in PATCHED_CHECK_QR_CODE
        )

    def test_the_deadline_is_only_set_on_failure(self):
        """A success must clear the backoff, not extend it."""
        catch_start = PATCHED_CHECK_QR_CODE.index("catch (error) {")
        deadline = PATCHED_CHECK_QR_CODE.index("this.linkCodeRetryAfter = Date.now() + backoff;")
        assert deadline > catch_start

    def test_a_success_resets_the_failure_count_and_deadline(self):
        try_block = PATCHED_CHECK_QR_CODE[
            PATCHED_CHECK_QR_CODE.index("await this.loginByCode(this.options.phoneNumber);")
            : PATCHED_CHECK_QR_CODE.index(
                "catch (error) {",
                PATCHED_CHECK_QR_CODE.index("await this.loginByCode(this.options.phoneNumber);"),
            )
        ]
        assert "this.linkCodeFailures = 0;" in try_block
        assert "this.linkCodeRetryAfter = 0;" in try_block

    def test_a_completed_login_clears_the_backoff_too(self):
        """The !needScan branch runs once pairing actually succeeds — leaving
        a stale deadline there would delay a later, legitimate refresh."""
        head = PATCHED_CHECK_QR_CODE[: PATCHED_CHECK_QR_CODE.index("if (typeof this.options.phoneNumber")]
        assert "this.linkCodeFailures = 0;" in head
        assert "this.linkCodeRetryAfter = 0;" in head

    def test_the_backoff_doubles_and_is_capped(self):
        assert (
            "Math.min(20000 * Math.pow(2, this.linkCodeFailures - 1), 300000)"
            in PATCHED_CHECK_QR_CODE
        )

    def test_the_backoff_schedule_is_what_we_think_it_is(self):
        """Mirrors the JS expression so a future edit to one without the other
        is caught here rather than in production."""
        def backoff(failures):
            return min(20000 * 2 ** (failures - 1), 300000)

        assert [backoff(n) // 1000 for n in range(1, 7)] == [20, 40, 80, 160, 300, 300]

    def test_it_never_gives_up_entirely(self):
        """Whatever the cause, the user may resolve it — a permanent stop
        would mean a restart to recover, which is the v1 mistake in a new
        costume."""
        assert "linkCodeGaveUp" not in PATCHED_CHECK_QR_CODE
        assert "return false" not in PATCHED_CHECK_QR_CODE

    def test_the_hook_still_receives_the_error_plus_the_schedule(self):
        assert "attempt: this.linkCodeFailures," in PATCHED_CHECK_QR_CODE
        assert "retryInSeconds: retryInSeconds," in PATCHED_CHECK_QR_CODE

    def test_every_generation_is_still_distinct(self):
        variants = [
            ORIGINAL_CHECK_QR_CODE, V1_CHECK_QR_CODE, V2_CHECK_QR_CODE,
            V3_CHECK_QR_CODE, V4_CHECK_QR_CODE, PATCHED_CHECK_QR_CODE,
        ]
        assert len(set(variants)) == 6

    def test_a_v4_install_is_upgraded(self, fake_wppconnect_dist):
        setup_api = _load_setup_api()
        api_dir, host_layer = fake_wppconnect_dist
        _write(host_layer, V4_CHECK_QR_CODE, PATCHED_LOGIN_BY_CODE)

        assert setup_api._patch_wppconnect_host_layer(str(api_dir)) is True

        content = host_layer.read_text(encoding="utf-8")
        assert PATCHED_CHECK_QR_CODE in content
        assert V4_CHECK_QR_CODE not in content


class TestManagedLinkingApiMigration:
    """wppconnect calls the low-level WPP.conn.genLinkDeviceCodeForPhoneNumber().
    wa-js 4.6.0 also ships a managed flow whose documented behaviour — repeated
    calls for the same number reuse the active code, refreshes arrive via
    conn.link_code_change — is what every generation of the checkQrCode patch
    above has been hand-rolling since issue #8. This module's own docstring
    records that signal as not existing yet (wa-js PR #3554); it does now.

    The immediate trigger was a live failure: on a *fresh* Chrome profile the
    raw call threw `Invariant Violation: Minified invariant #56367` with
    `messageParams: [""]`. An invariant is an internal assertion, not a server
    refusal — it fires when a function is reached in a state it did not expect.
    """

    def test_the_managed_entry_point_is_preferred(self):
        assert "WPP.conn.startLinkDeviceCodeForPhoneNumber(phone)" in PATCHED_LOGIN_BY_CODE

    def test_it_falls_back_to_the_raw_call(self):
        """An older @wppconnect/wa-js has no managed API; the patch must not
        turn that into a TypeError."""
        assert (
            "typeof WPP.conn.startLinkDeviceCodeForPhoneNumber === 'function'"
            in PATCHED_LOGIN_BY_CODE
        )
        assert "WPP.conn.genLinkDeviceCodeForPhoneNumber(phone)" in PATCHED_LOGIN_BY_CODE

    def test_which_path_ran_is_recorded(self):
        """Both paths can produce a code and both can fail — a log that cannot
        say which one ran cannot tell you whether the migration helped."""
        assert "return { code: String(value), managed: managed };" in PATCHED_LOGIN_BY_CODE
        assert "'managed' : 'legacy raw'" in PATCHED_LOGIN_BY_CODE
        assert "__winzappManagedApi" in PATCHED_LOGIN_BY_CODE

    def test_the_legacy_raw_patch_is_still_a_distinct_rung(self):
        """Anyone already carrying the error-detail patch must be migrated
        forward, not left unrecognised."""
        assert LEGACY_LOGIN_BY_CODE_RAW != PATCHED_LOGIN_BY_CODE
        assert LEGACY_LOGIN_BY_CODE_RAW != ORIGINAL_LOGIN_BY_CODE
        assert "startLinkDeviceCodeForPhoneNumber" not in LEGACY_LOGIN_BY_CODE_RAW

    def test_error_capture_survives_the_migration(self):
        """The diagnostics that made this failure legible in the first place
        must not be lost while swapping the call underneath them."""
        assert "Object.getOwnPropertyNames(Object(error))" in PATCHED_LOGIN_BY_CODE
        assert "details: details," in PATCHED_LOGIN_BY_CODE
        assert "failure.winzappDetails" in PATCHED_LOGIN_BY_CODE
        assert (
            "String(error?.message || error?.reason || error?.text || error)"
            in PATCHED_LOGIN_BY_CODE
        )

    def test_an_install_on_the_raw_patch_is_migrated(self, fake_wppconnect_dist):
        setup_api = _load_setup_api()
        api_dir, host_layer = fake_wppconnect_dist
        _write(host_layer, PATCHED_CHECK_QR_CODE, LEGACY_LOGIN_BY_CODE_RAW)

        assert setup_api._patch_wppconnect_host_layer(str(api_dir)) is True

        content = host_layer.read_text(encoding="utf-8")
        assert PATCHED_LOGIN_BY_CODE in content
        assert LEGACY_LOGIN_BY_CODE_RAW not in content

    def test_a_pristine_install_goes_straight_to_the_managed_api(self, fake_wppconnect_dist):
        setup_api = _load_setup_api()
        api_dir, host_layer = fake_wppconnect_dist
        _write(host_layer, ORIGINAL_CHECK_QR_CODE, ORIGINAL_LOGIN_BY_CODE)

        assert setup_api._patch_wppconnect_host_layer(str(api_dir)) is True
        assert PATCHED_LOGIN_BY_CODE in host_layer.read_text(encoding="utf-8")

    def test_both_entry_points_agree_after_the_migration(self, tmp_path):
        from ui.dialogs.api_setup import ApiSetupDialog
        setup_api = _load_setup_api()

        outputs = []
        for name in ("setup_api", "api_setup"):
            api_dir = tmp_path / name
            layers = api_dir / "node_modules" / "@wppconnect-team" / "wppconnect" / "dist" / "api" / "layers"
            layers.mkdir(parents=True)
            host_layer = layers / "host.layer.js"
            _write(host_layer, PATCHED_CHECK_QR_CODE, LEGACY_LOGIN_BY_CODE_RAW)

            if name == "setup_api":
                setup_api._patch_wppconnect_host_layer(str(api_dir))
            else:
                ApiSetupDialog._patch_wppconnect_host_layer(
                    str(api_dir / "node_modules" / "@wppconnect-team" / "wppconnect" / "dist" / "api")
                )
            outputs.append(host_layer.read_text(encoding="utf-8"))

        assert outputs[0] == outputs[1]


class TestV6WaitsForTheAuthStateBeforeAskingForACode:
    """v1..v5 hoisted the phoneNumber branch above `await this.getQrCode()`
    to stop the pairing code regenerating on every rotation, and in doing so
    dropped the only thing that kept the call safe: upstream only ever
    reached loginByCode() once a urlCode existed, which is also when
    WhatsApp Web's user-prefs storage is initialised. Without that, wa-js
    walks setADVSecretKey -> getStorage into an uninitialised table and
    WhatsApp Web throws Invariant Violation #56367 — both pairing routes
    dead, nothing on screen, and the real error swallowed."""

    def test_the_gate_runs_before_login_by_code(self):
        gate = PATCHED_CHECK_QR_CODE.index("const ready = await this.getQrCode();")
        login_call = PATCHED_CHECK_QR_CODE.index(
            "await this.loginByCode(this.options.phoneNumber);"
        )
        assert gate < login_call

    def test_a_missing_auth_code_defers_instead_of_calling(self):
        """Returning (not throwing, not calling anyway) matters: checkQrCode is
        bound to the auth-code rotation, so a deferral is retried for free on
        the next tick — while calling anyway is the invariant."""
        assert "if (!ready?.urlCode) {" in PATCHED_CHECK_QR_CODE
        gate = PATCHED_CHECK_QR_CODE.index("if (!ready?.urlCode) {")
        login_call = PATCHED_CHECK_QR_CODE.index(
            "await this.loginByCode(this.options.phoneNumber);"
        )
        deferred_return = PATCHED_CHECK_QR_CODE.index("return;", gate)
        assert deferred_return < login_call

    def test_the_gate_does_not_cost_the_v5_cooldown(self):
        """The cooldown/backoff checks must still run BEFORE the gate: probing
        the auth state on every rotation while a code is already valid would
        undo v4/v5 and hammer WhatsApp for nothing."""
        cooldown = PATCHED_CHECK_QR_CODE.index("this.linkCodeIssuedAt && (now - this.linkCodeIssuedAt) < 60000")
        backoff = PATCHED_CHECK_QR_CODE.index("this.linkCodeRetryAfter && now < this.linkCodeRetryAfter")
        gate = PATCHED_CHECK_QR_CODE.index("const ready = await this.getQrCode();")
        assert cooldown < gate
        assert backoff < gate

    def test_v5_is_recognised_and_upgraded(self, fake_wppconnect_dist):
        setup_api = _load_setup_api()
        api_dir, host_layer = fake_wppconnect_dist
        _write(host_layer, V5_CHECK_QR_CODE, PATCHED_LOGIN_BY_CODE)

        assert setup_api._patch_wppconnect_host_layer(str(api_dir)) is True
        content = host_layer.read_text(encoding="utf-8")
        assert PATCHED_CHECK_QR_CODE in content
        assert V5_CHECK_QR_CODE not in content

    def test_v5_and_v6_are_distinct(self):
        assert V5_CHECK_QR_CODE != PATCHED_CHECK_QR_CODE


class TestGetQrCodeReadsWaJsNotTheDom:
    """Upstream scrapes `document.querySelector('canvas').closest('[data-ref]')`.
    Current WhatsApp Web often has no <canvas> when that runs, and once it
    does the nearest data-ref ancestor is the download / "link with phone
    number instead" banner, whose data-ref is a wa.me URL — so the emitted
    payload was not a login code at all, and a phone could never pair from
    it. A real payload starts with `2@`."""

    def test_the_dom_scraper_is_gone(self):
        assert "scrapeImg" in ORIGINAL_GET_QR_CODE
        assert "scrapeImg" not in PATCHED_GET_QR_CODE
        assert "querySelector" not in PATCHED_GET_QR_CODE

    def test_it_reads_the_auth_code_from_wa_js(self):
        assert "WPP.conn.getAuthCode()" in PATCHED_GET_QR_CODE
        assert "urlCode: auth.fullCode" in PATCHED_GET_QR_CODE

    def test_the_png_carries_no_quiet_zone(self):
        """connect.py's display_qrcode_image() adds its own quiet zone and then
        magnifies by a whole integer factor with nearest-neighbour, and
        documents that it is fed a borderless image. A margin here would
        double the border and shrink the modules — the exact shape of the
        "QR Code inválido" that scaling code already exists to prevent."""
        assert "margin: 0" in PATCHED_GET_QR_CODE

    def test_a_missing_auth_code_returns_undefined(self):
        """checkQrCode's own `!result?.urlCode` guard — and now the v6 pairing
        gate — both depend on this returning a falsy result rather than a
        half-built object when the auth state is not up yet."""
        assert "return undefined;" in PATCHED_GET_QR_CODE

    def test_a_pristine_install_is_patched(self, fake_wppconnect_dist):
        setup_api = _load_setup_api()
        api_dir, host_layer = fake_wppconnect_dist
        _write(host_layer, ORIGINAL_CHECK_QR_CODE, ORIGINAL_LOGIN_BY_CODE,
               ORIGINAL_GET_QR_CODE)

        assert setup_api._patch_wppconnect_host_layer(str(api_dir)) is True
        content = host_layer.read_text(encoding="utf-8")
        assert PATCHED_GET_QR_CODE in content
        assert ORIGINAL_GET_QR_CODE not in content

    def test_reapplying_is_idempotent(self, fake_wppconnect_dist):
        setup_api = _load_setup_api()
        api_dir, host_layer = fake_wppconnect_dist
        _write(host_layer, ORIGINAL_CHECK_QR_CODE, ORIGINAL_LOGIN_BY_CODE,
               ORIGINAL_GET_QR_CODE)

        setup_api._patch_wppconnect_host_layer(str(api_dir))
        once = host_layer.read_text(encoding="utf-8")
        setup_api._patch_wppconnect_host_layer(str(api_dir))
        assert host_layer.read_text(encoding="utf-8") == once


class TestWaitForQrCodeScanDoesNotMistakeAFailedProbeForALogin:
    """`this.isLogged = !needScan` where needScan came from
    `.catch(() => null)` reads "we could not find out" as "the user has
    logged in" — the strongest possible claim from the one value that
    carries no information. The scan wait then exits early, waitForLogin()
    re-probes, gets null again, and reports `Failed to authenticate` with
    the actual browser-side error written down nowhere."""

    def test_the_swallowing_catch_is_gone(self):
        assert ".catch(() => null)" in ORIGINAL_WAIT_FOR_QR_CODE_SCAN
        assert ".catch(() => null)" not in PATCHED_WAIT_FOR_QR_CODE_SCAN

    def test_a_failed_probe_keeps_waiting_instead_of_declaring_a_login(self):
        """The assignment must be unreachable from the failure path: on a
        throw we `continue`, so isLogged is never written from a probe that
        did not actually answer."""
        catch_block = PATCHED_WAIT_FOR_QR_CODE_SCAN.index("catch (error) {")
        continue_stmt = PATCHED_WAIT_FOR_QR_CODE_SCAN.index("continue;", catch_block)
        assignment = PATCHED_WAIT_FOR_QR_CODE_SCAN.index("this.isLogged = !needScan;")
        assert catch_block < continue_stmt < assignment

    def test_the_real_error_is_logged(self):
        assert "Auth probe failed" in PATCHED_WAIT_FOR_QR_CODE_SCAN
        assert "error?.message" in PATCHED_WAIT_FOR_QR_CODE_SCAN

    def test_a_permanently_broken_probe_still_gives_up(self):
        """Retrying forever would replace a wrong answer with a hang, which
        for a pairing dialog is no better. The bound is generous enough to
        ride out a navigation but finite, and it says why it stopped."""
        assert "giving up on the scan wait" in PATCHED_WAIT_FOR_QR_CODE_SCAN
        # Bounded by the WALL CLOCK, not by an iteration count. Each probe
        # goes through page.evaluate under a 300s protocolTimeout, so a
        # wedged renderer makes "150 iterations" mean 12.5 hours while the
        # log line claims 30 seconds.
        assert "Date.now() >= probeDeadline" in PATCHED_WAIT_FOR_QR_CODE_SCAN
        assert "probeFailures >= 150" not in PATCHED_WAIT_FOR_QR_CODE_SCAN

    def test_a_successful_probe_resets_the_failure_run(self):
        """Otherwise a session that hiccups once every few minutes would
        eventually cross the give-up threshold for no reason."""
        assert "probeFailures = 0;" in PATCHED_WAIT_FOR_QR_CODE_SCAN
        # The deadline has to be cleared alongside the counter, or a
        # run that recovers keeps an armed deadline and the NEXT
        # failure gives up against a clock that started minutes ago.
        assert "probeDeadline = 0;" in PATCHED_WAIT_FOR_QR_CODE_SCAN
        reset = PATCHED_WAIT_FOR_QR_CODE_SCAN.rindex("probeFailures = 0;")
        assignment = PATCHED_WAIT_FOR_QR_CODE_SCAN.index("this.isLogged = !needScan;")
        assert reset < assignment

    def test_a_pristine_install_is_patched(self, fake_wppconnect_dist):
        setup_api = _load_setup_api()
        api_dir, host_layer = fake_wppconnect_dist
        _write(host_layer, ORIGINAL_CHECK_QR_CODE, ORIGINAL_LOGIN_BY_CODE,
               ORIGINAL_GET_QR_CODE, ORIGINAL_WAIT_FOR_QR_CODE_SCAN)

        assert setup_api._patch_wppconnect_host_layer(str(api_dir)) is True
        content = host_layer.read_text(encoding="utf-8")
        assert PATCHED_WAIT_FOR_QR_CODE_SCAN in content
        assert ORIGINAL_WAIT_FOR_QR_CODE_SCAN not in content


class TestV7ClosesTheSameHoleInCheckQrCode:
    """waitForQrCodeScan stopped reading a failed probe as "the user logged
    in", but checkQrCode's own first two lines still did exactly that — and
    it runs CONCURRENTLY, invoked from the page on every auth-code rotation.
    One failed probe there set isLogged, and the scan wait this patch series
    had just made honest exited on its very next check."""

    def test_the_swallowing_catch_is_gone_from_the_head(self):
        head = PATCHED_CHECK_QR_CODE[:PATCHED_CHECK_QR_CODE.index("if (!needScan)")]
        assert ".catch(() => null)" in V6_CHECK_QR_CODE
        assert ".catch(() => null)" not in head

    def test_a_failed_probe_leaves_is_logged_alone(self):
        """Returning without writing isLogged is the whole point: the next
        rotation re-enters for free, whereas writing it ends someone else's
        loop."""
        # The FIRST catch is the head one v7 added — that is the one
        # under test here, not the pairing-code branch's.
        catch_block = PATCHED_CHECK_QR_CODE.index("catch (error) {")
        early_return = PATCHED_CHECK_QR_CODE.index("return;", catch_block)
        assignment = PATCHED_CHECK_QR_CODE.index("this.isLogged = !needScan;")
        assert catch_block < early_return < assignment

    def test_v6_is_recognised_and_upgraded(self, fake_wppconnect_dist):
        setup_api = _load_setup_api()
        api_dir, host_layer = fake_wppconnect_dist
        _write(host_layer, V6_CHECK_QR_CODE, PATCHED_LOGIN_BY_CODE,
               PATCHED_GET_QR_CODE, PATCHED_WAIT_FOR_QR_CODE_SCAN)

        assert setup_api._patch_wppconnect_host_layer(str(api_dir)) is True
        content = host_layer.read_text(encoding="utf-8")
        assert PATCHED_CHECK_QR_CODE in content
        assert V6_CHECK_QR_CODE not in content

    def test_v6_and_v7_differ_only_in_that_head(self):
        """Guards the upgrade arm: if a future edit changes the body too, the
        v6 -> v7 replacement silently stops matching real installs."""
        marker = "if (!needScan) {"
        assert (V6_CHECK_QR_CODE[V6_CHECK_QR_CODE.index(marker):]
                == PATCHED_CHECK_QR_CODE[PATCHED_CHECK_QR_CODE.index(marker):])
