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
import re

import pytest

from core.wppconnect_host_layer_patch import (
    ORIGINAL_CHECK_QR_CODE, V1_CHECK_QR_CODE, V2_CHECK_QR_CODE,
    V3_CHECK_QR_CODE, V4_CHECK_QR_CODE, V5_CHECK_QR_CODE, V6_CHECK_QR_CODE,
    V7_CHECK_QR_CODE, PATCHED_CHECK_QR_CODE,
    ORIGINAL_GET_QR_CODE, PATCHED_GET_QR_CODE,
    ORIGINAL_WAIT_FOR_QR_CODE_SCAN, PATCHED_WAIT_FOR_QR_CODE_SCAN,
    ORIGINAL_LOGIN_BY_CODE, LEGACY_LOGIN_BY_CODE_RAW, PATCHED_LOGIN_BY_CODE,
    MANAGED_LINK_MARKER,
    MANAGED_ORIGINAL_CHECK_QR_CODE, MANAGED_PATCHED_CHECK_QR_CODE,
    MANAGED_ORIGINAL_LOGIN_BY_CODE, MANAGED_PATCHED_LOGIN_BY_CODE,
    MANAGED_ORIGINAL_ON_LINK_CODE, MANAGED_PATCHED_ON_LINK_CODE,
    MANAGED_ORIGINAL_LINK_CODE_HOOKS, MANAGED_PATCHED_LINK_CODE_HOOKS,
    MANAGED_ORIGINAL_LINK_CODE_LISTENER, MANAGED_PATCHED_LINK_CODE_LISTENER,
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


def _write_managed(host_layer, checkqrcode_text=MANAGED_ORIGINAL_CHECK_QR_CODE,
                   loginbycode_text=MANAGED_ORIGINAL_LOGIN_BY_CODE,
                   onlinkcode_text=MANAGED_ORIGINAL_ON_LINK_CODE,
                   hooks_text=MANAGED_ORIGINAL_LINK_CODE_HOOKS,
                   listener_text=MANAGED_ORIGINAL_LINK_CODE_LISTENER,
                   getqrcode_text=ORIGINAL_GET_QR_CODE,
                   waitforscan_text=ORIGINAL_WAIT_FOR_QR_CODE_SCAN):
    """_write()'s counterpart for the file wppconnect >= 2.3.2 ships.

    refreshLinkCode() is spelled out rather than omitted: it is the marker
    patch_host_layer_source() reads to tell the two runtimes apart, so a
    fixture without it is a 2.3.1 file as far as the patcher is concerned —
    which is precisely the confusion these tests exist to rule out."""
    host_layer.write_text(
        "class HostLayer {\n"
        "    urlCode = '';\n"
        "    attempt = 0;\n"
        "    async start() {\n"
        + hooks_text +
        "    }\n"
        "    async afterPageScriptInjected() {\n"
        "        if (typeof this.options.phoneNumber === 'string') {\n"
        "            await (0, helpers_1.evaluateAndReturn)(this.page, () => {\n"
        "                WPP.on('conn.link_code_change', window.onLinkCode);\n"
        "                WPP.on('conn.link_code_expired', window.onLinkCodeExpired);\n"
        + listener_text +
        "            }).catch(() => null);\n"
        "        }\n"
        "    }\n"
        + checkqrcode_text
        + loginbycode_text
        + onlinkcode_text
        + MANAGED_LINK_MARKER +
        "        return await (0, helpers_1.evaluateAndReturn)(this.page, () => WPP.conn.refreshLinkDeviceCode());\n"
        "    }\n"
        + getqrcode_text
        + waitforscan_text +
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
        cooldown = PATCHED_CHECK_QR_CODE.index("this.linkCodeIssuedAt && (now - this.linkCodeIssuedAt) < reuseWindow")
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
                == V7_CHECK_QR_CODE[V7_CHECK_QR_CODE.index(marker):])

    def test_v7_kept_the_head_v8_inherited(self):
        head = PATCHED_CHECK_QR_CODE[:PATCHED_CHECK_QR_CODE.index("if (!needScan)")]
        assert head == V7_CHECK_QR_CODE[:V7_CHECK_QR_CODE.index("if (!needScan)")]


class TestV8StopsBurningThePairingCodeQuota:
    """checkQrCode runs on every auth-code rotation — roughly once a minute
    for as long as the page is unpaired. v2..v7 paced that with a flat 60s
    reuse cooldown, which is right while somebody is reading the code off the
    screen and wrong the moment nobody is.

    From a real wppconnect.log: a session WhatsApp had logged out of, left
    running by WinZapp, asked for a fresh pairing code roughly every minute for
    eighteen minutes and was then refused with
    ``{"name":"IQErrorRateOverlimit","value":{"text":"rate-overlimit",
    "code":429}}``. The quota is per phone number and lives on WhatsApp's side,
    so it outlives both the session and the process — which is the whole of the
    "the first pairing code after a drop always fails, the second works"
    report."""

    def test_the_reuse_window_is_no_longer_a_flat_minute(self):
        assert "(now - this.linkCodeIssuedAt) < 60000" in V7_CHECK_QR_CODE
        assert "(now - this.linkCodeIssuedAt) < 60000" not in PATCHED_CHECK_QR_CODE
        assert "(now - this.linkCodeIssuedAt) < reuseWindow" in PATCHED_CHECK_QR_CODE

    def test_the_window_widens_with_unclaimed_codes_and_has_a_ceiling(self):
        assert (
            "const reuseWindow = Math.min(60000 * Math.pow(2, "
            "Math.max(0, issued - 1)), 240000);" in PATCHED_CHECK_QR_CODE
        )

    def test_the_first_two_reissues_still_come_fast(self):
        """Those are the attended ones — a mistyped or expired code while the
        pairing dialog is open. Mirrors the JS so a change to the formula has
        to be a deliberate one."""
        def window(issued):
            return min(60000 * 2 ** max(0, issued - 1), 240000)

        assert [window(n) for n in (0, 1, 2, 3, 4)] == [
            60000, 60000, 120000, 240000, 240000,
        ]
        assert window(99) == 240000

    def test_the_reuse_ceiling_never_outlasts_whatsapps_own_code_rotation(self):
        """checkQrCode() is also the ONLY thing that refreshes the code shown
        in the pairing dialog (on_wpp_phone_code -> update_pairing_code). In
        the captured log WhatsApp issued a genuinely new code about every 3.5
        minutes, so a reuse ceiling above that leaves a blind user typing a
        code WhatsApp has already rotated past, with nothing on screen saying
        so — in the one flow that is their only way back into the app.

        The 15-minute figure belongs to the rate-limit backoff, which only
        starts after WhatsApp has already refused, and which a manual retry
        clears anyway because it mints a fresh session.
        """
        def window(issued):
            return min(60000 * 2 ** max(0, issued - 1), 240000)

        assert max(window(n) for n in range(200)) <= 240000
        assert "? 900000" in PATCHED_CHECK_QR_CODE  # the backoff, not the reuse

    def test_the_counter_only_advances_on_a_code_that_was_actually_issued(self):
        """Same property v2 established for linkCodeIssuedAt: a failed
        loginByCode() must not widen the window, or one transient failure
        would push a waiting user's next code minutes away."""
        login_call = PATCHED_CHECK_QR_CODE.index(
            "await this.loginByCode(this.options.phoneNumber);"
        )
        bump = PATCHED_CHECK_QR_CODE.index("this.linkCodeIssues = issued + 1;")
        catch_block = PATCHED_CHECK_QR_CODE.index("catch (error) {", login_call)
        assert login_call < bump < catch_block

    def test_a_successful_pairing_resets_the_counter(self):
        """Otherwise a re-pair later in the same session would start out
        throttled by codes nobody is still waiting on."""
        head = PATCHED_CHECK_QR_CODE[
            PATCHED_CHECK_QR_CODE.index("if (!needScan) {"):
            PATCHED_CHECK_QR_CODE.index("if (typeof this.options.phoneNumber")
        ]
        assert "this.linkCodeIssues = 0;" in head

    def test_a_rate_limited_failure_backs_off_far_longer_than_the_ladder(self):
        """Retrying a 429 on the generic 20s first step is what keeps the
        limit alive."""
        assert "const backoff = rateLimited" in PATCHED_CHECK_QR_CODE
        assert "? 900000" in PATCHED_CHECK_QR_CODE
        # ...and the ordinary ladder is untouched for everything else.
        assert (
            "Math.min(20000 * Math.pow(2, this.linkCodeFailures - 1), 300000)"
            in PATCHED_CHECK_QR_CODE
        )

    def test_the_rate_limit_is_read_off_whatsapps_own_answer(self):
        assert "/rate-overlimit|RateOverlimit/i.test(" in PATCHED_CHECK_QR_CODE
        assert "detail = JSON.stringify(error?.winzappDetails || {});" in PATCHED_CHECK_QR_CODE

    def test_the_flag_reaches_the_reporting_hook(self):
        """createSessionUtil forwards it to the client, which is what lets the
        pairing dialog say "wait a few minutes" instead of showing the class
        name CompanionHelloError."""
        assert "rateLimited: rateLimited," in PATCHED_CHECK_QR_CODE

    def test_v7_is_recognised_and_upgraded(self, fake_wppconnect_dist):
        setup_api = _load_setup_api()
        api_dir, host_layer = fake_wppconnect_dist
        _write(host_layer, V7_CHECK_QR_CODE, PATCHED_LOGIN_BY_CODE,
               PATCHED_GET_QR_CODE, PATCHED_WAIT_FOR_QR_CODE_SCAN)

        assert setup_api._patch_wppconnect_host_layer(str(api_dir)) is True
        content = host_layer.read_text(encoding="utf-8")
        assert PATCHED_CHECK_QR_CODE in content
        assert V7_CHECK_QR_CODE not in content

    def test_v7_and_v8_are_distinct(self):
        assert V7_CHECK_QR_CODE != PATCHED_CHECK_QR_CODE


class TestTheTwoRuntimesAreToldApartByTheFileItself:
    """wppconnect 2.3.2 restructured the pairing flow, so there are two patch
    sets now — and picking between them by matching, rather than by a version
    string nothing here can see, is the whole safety property.

    Both callers re-run these patches on every launch against whatever
    node_modules holds, and a WinZapp update on its own never reinstalls
    node_modules: an install that paired fine on 2.3.1 stays on 2.3.1 until the
    user reinstalls the API. Writing the 2.3.2 checkQrCode into that file would
    delete the only place that runtime calls loginByCode() from — pairing by
    code, dead, with every patch still reporting OK.
    """

    def test_a_2_3_1_file_never_receives_the_2_3_2_text(self, fake_wppconnect_dist):
        setup_api = _load_setup_api()
        api_dir, host_layer = fake_wppconnect_dist
        _write(host_layer, ORIGINAL_CHECK_QR_CODE)

        assert setup_api._patch_wppconnect_host_layer(str(api_dir)) is True

        content = host_layer.read_text(encoding="utf-8")
        assert PATCHED_CHECK_QR_CODE in content
        assert MANAGED_PATCHED_CHECK_QR_CODE not in content
        assert MANAGED_PATCHED_LOGIN_BY_CODE not in content
        # The forward into loginByCode() is the 2.3.1 runtime's only route to
        # a pairing code. It has to still be there.
        assert "await this.loginByCode(this.options.phoneNumber);" in content

    def test_a_2_3_2_file_never_receives_the_2_3_1_text(self, fake_wppconnect_dist):
        setup_api = _load_setup_api()
        api_dir, host_layer = fake_wppconnect_dist
        _write_managed(host_layer)

        assert setup_api._patch_wppconnect_host_layer(str(api_dir)) is True

        content = host_layer.read_text(encoding="utf-8")
        assert MANAGED_PATCHED_CHECK_QR_CODE in content
        assert MANAGED_PATCHED_LOGIN_BY_CODE in content
        assert PATCHED_CHECK_QR_CODE not in content
        assert "linkCodeIssuedAt" not in content

    def test_an_install_already_on_v8_is_left_alone_until_node_modules_moves(
        self, fake_wppconnect_dist
    ):
        """The migration case that matters: updating WinZapp does not update
        node_modules. A 2.3.1 tree already carrying v8 must come out
        byte-identical, not half-rewritten into a shape its runtime cannot
        use."""
        setup_api = _load_setup_api()
        api_dir, host_layer = fake_wppconnect_dist
        _write(host_layer, PATCHED_CHECK_QR_CODE, PATCHED_LOGIN_BY_CODE,
               PATCHED_GET_QR_CODE, PATCHED_WAIT_FOR_QR_CODE_SCAN)
        before = host_layer.read_text(encoding="utf-8")

        assert setup_api._patch_wppconnect_host_layer(str(api_dir)) is True
        assert host_layer.read_text(encoding="utf-8") == before

    def test_the_bound_in_the_header_matches_the_one_in_give_up(self):
        """The header is documentation of `giveUp`, so a change to one that
        misses the other silently halves or doubles the attempts — and the
        ceiling is connect.py's own 90 s patience, not anything here."""
        bound = int(
            re.search(r"attempt <= (\d+)", MANAGED_PATCHED_LOGIN_BY_CODE).group(1)
        )
        give_up = int(
            re.search(r"attempt >= (\d+)", MANAGED_PATCHED_LOGIN_BY_CODE).group(1)
        )
        assert bound == give_up == 2

    def test_the_marker_is_a_method_no_patch_rewrites(self):
        """A marker that one of the patches also edits stops identifying the
        file the moment that patch applies, and the next run would fall back
        to the wrong set."""
        for patched in (
            MANAGED_PATCHED_CHECK_QR_CODE, MANAGED_PATCHED_LOGIN_BY_CODE,
            MANAGED_PATCHED_ON_LINK_CODE, MANAGED_PATCHED_LINK_CODE_HOOKS,
            PATCHED_CHECK_QR_CODE, PATCHED_LOGIN_BY_CODE,
            PATCHED_GET_QR_CODE, PATCHED_WAIT_FOR_QR_CODE_SCAN,
        ):
            assert MANAGED_LINK_MARKER not in patched

    def test_the_marker_survives_a_full_patch_pass(self, fake_wppconnect_dist):
        setup_api = _load_setup_api()
        api_dir, host_layer = fake_wppconnect_dist
        _write_managed(host_layer)

        setup_api._patch_wppconnect_host_layer(str(api_dir))

        assert MANAGED_LINK_MARKER in host_layer.read_text(encoding="utf-8")

    def test_a_file_that_is_neither_is_reported_and_left_untouched(
        self, fake_wppconnect_dist
    ):
        setup_api = _load_setup_api()
        api_dir, host_layer = fake_wppconnect_dist
        host_layer.write_text("// a future wppconnect rewrote this again\n", encoding="utf-8")

        assert setup_api._patch_wppconnect_host_layer(str(api_dir)) is False
        assert host_layer.read_text(encoding="utf-8") == (
            "// a future wppconnect rewrote this again\n"
        )


class TestManagedCheckQrCodeKeepsTheProbeFixAndDropsTheCooldown:
    """v8's two halves parted ways on 2.3.2, and only one of them was ported.

    The reuse cooldown paced a mint loop that no longer exists: checkQrCode()
    does not call loginByCode() any more, and afterPageScriptInjected() does
    not even register it on `conn.auth_code_change` when a phone number is set.
    wa-js's own linkDeviceCodeLifecycle reuses the active code for repeat calls
    and re-mints on its own timer, bounded — so the cooldown was removed rather
    than carried over. A cooldown wrapped around a call that cannot happen is
    text that reads like a guard.

    v7's auth-probe fix has nothing to do with that loop and is ported
    unchanged: `!null` is still `true`, and checkQrCode() still runs on every
    auth-code rotation in QR mode, concurrently with waitForQrCodeScan().
    """

    def test_upstream_itself_no_longer_mints_from_here(self):
        assert "loginByCode" not in MANAGED_ORIGINAL_CHECK_QR_CODE
        assert "loginByCode" in ORIGINAL_CHECK_QR_CODE

    def test_the_cooldown_went_with_the_loop_it_paced(self):
        assert "linkCode" not in MANAGED_PATCHED_CHECK_QR_CODE
        assert "reuseWindow" not in MANAGED_PATCHED_CHECK_QR_CODE
        assert "loginByCode" not in MANAGED_PATCHED_CHECK_QR_CODE

    def test_the_v7_auth_probe_fix_survives(self):
        assert (
            "await (0, auth_1.needsToScan)(this.page).catch(() => null)"
            not in MANAGED_PATCHED_CHECK_QR_CODE
        )
        probe = MANAGED_PATCHED_CHECK_QR_CODE.index(
            "needScan = await (0, auth_1.needsToScan)(this.page);"
        )
        catch = MANAGED_PATCHED_CHECK_QR_CODE.index("catch (error) {", probe)
        bail = MANAGED_PATCHED_CHECK_QR_CODE.index("return;", catch)
        assigns = MANAGED_PATCHED_CHECK_QR_CODE.index("this.isLogged = !needScan;")
        assert catch < bail < assigns

    def test_nothing_else_about_upstreams_method_changed(self):
        """Everything past the probe head is upstream's own text, so a future
        release that touches the QR branch shows up as a DID NOT MATCH rather
        than as WinZapp quietly reverting it."""
        tail = "        this.isLogged = !needScan;\n"
        assert (
            MANAGED_PATCHED_CHECK_QR_CODE[MANAGED_PATCHED_CHECK_QR_CODE.index(tail):]
            == MANAGED_ORIGINAL_CHECK_QR_CODE[MANAGED_ORIGINAL_CHECK_QR_CODE.index(tail):]
        )

    def test_a_pristine_install_is_patched(self, fake_wppconnect_dist):
        setup_api = _load_setup_api()
        api_dir, host_layer = fake_wppconnect_dist
        _write_managed(host_layer)

        assert setup_api._patch_wppconnect_host_layer(str(api_dir)) is True
        assert MANAGED_PATCHED_CHECK_QR_CODE in host_layer.read_text(encoding="utf-8")


MANAGED_MAX_MINT_ATTEMPTS = int(
    re.search(r"attempt >= (\d+)", MANAGED_PATCHED_LOGIN_BY_CODE).group(1)
)
# Both halves of the wall-clock budget come off the patch itself: one probe per
# second, so the probe ceiling *is* the gate in seconds. Hardcoding either side
# would let the patch move without the bound noticing.
MANAGED_AUTH_GATE_SECONDS = int(
    re.search(r"probe <= (\d+)", MANAGED_PATCHED_LOGIN_BY_CODE).group(1)
)


class TestManagedLoginByCodeIsTheWholePairingAttempt:
    """2.3.2 calls loginByCode() exactly once, from afterPageScriptInjected(),
    under a `.catch(error => this.log('error', error))`. Everything checkQrCode
    used to do around that call has to live inside it now, or not at all."""

    def test_upstream_awaits_bare(self):
        assert "__winzappError" not in MANAGED_ORIGINAL_LOGIN_BY_CODE
        assert "catchLinkCodeError" not in MANAGED_ORIGINAL_LOGIN_BY_CODE

    def test_the_auth_state_gate_runs_before_the_mint(self):
        """v6's finding, and 2.3.2 walked straight back into it: 2.3.1's
        upstream reached this call only after getQrCode() had produced a
        urlCode, and calling the link-device API before that makes WhatsApp Web
        throw `Invariant Violation #56367`."""
        gate = MANAGED_PATCHED_LOGIN_BY_CODE.index("ready = await this.getQrCode();")
        mint = MANAGED_PATCHED_LOGIN_BY_CODE.index(
            "await WPP.conn.startLinkDeviceCodeForPhoneNumber(phone);"
        )
        assert gate < mint

    def test_an_already_registered_session_never_asks_for_a_code(self):
        """A page reload inside a pairing attempt re-enters this method, and
        wa-js refuses a code for a registered session. Polling for an auth code
        that by definition will never come would burn the whole gate window and
        then report a failure for a session that is fine."""
        short_circuit = MANAGED_PATCHED_LOGIN_BY_CODE.index("if (needScan === false) {")
        mint = MANAGED_PATCHED_LOGIN_BY_CODE.index(
            "await WPP.conn.startLinkDeviceCodeForPhoneNumber(phone);"
        )
        assert short_circuit < mint

    def test_the_gate_is_bounded_and_says_so_when_it_gives_up(self):
        assert "probe <= 60" in MANAGED_PATCHED_LOGIN_BY_CODE
        assert "LinkCodeAuthStateTimeout" in MANAGED_PATCHED_LOGIN_BY_CODE

    def test_the_error_capture_survives(self):
        """The diagnostics v3/v4 were written for. Upstream's bare await lets a
        refusal cross the CDP boundary as the minified "t: t"."""
        assert "Object.getOwnPropertyNames(Object(error))" in MANAGED_PATCHED_LOGIN_BY_CODE
        assert "__winzappManagedApi" in MANAGED_PATCHED_LOGIN_BY_CODE
        assert "failure.winzappDetails" in MANAGED_PATCHED_LOGIN_BY_CODE
        assert (
            "String(error?.message || error?.reason || error?.text || error)"
            in MANAGED_PATCHED_LOGIN_BY_CODE
        )

    def test_a_failure_reaches_the_client_not_just_wppconnect_log(self):
        assert "this.options.catchLinkCodeError?.({" in MANAGED_PATCHED_LOGIN_BY_CODE
        assert "rateLimited: rateLimited," in MANAGED_PATCHED_LOGIN_BY_CODE
        assert "attempt: attempt," in MANAGED_PATCHED_LOGIN_BY_CODE

    def test_a_cdp_level_rejection_is_reported_too(self):
        """evaluateAndReturn can reject on its own (a destroyed execution
        context, a closed target) without the page-side catch ever running. v8
        caught that around the call; here it has to be caught inside."""
        outer_catch = MANAGED_PATCHED_LOGIN_BY_CODE.index("            catch (error) {\n")
        report = MANAGED_PATCHED_LOGIN_BY_CODE.index("name: failed.name,")
        assert outer_catch < report

    def test_a_transient_failure_is_retried_because_nothing_else_will(self):
        """v2..v8 got a retry for free — `conn.auth_code_change` re-entered
        checkQrCode() every minute or so. Nothing re-enters this method, so a
        single "Execution context was destroyed" would otherwise end pairing
        for the session with nothing on screen."""
        assert "attempt >= 2" in MANAGED_PATCHED_LOGIN_BY_CODE
        assert (
            "const backoff = giveUp ? 0 : 20000 * Math.pow(2, attempt - 1);"
            in MANAGED_PATCHED_LOGIN_BY_CODE
        )

    def test_the_whole_ladder_fits_inside_the_client_wait(self):
        """The bound is connect.py's, not the browser's: _bg_pairing_flow()
        waits 90 s for a phoneCode and then clears the token, registers the
        session as abandoned and shows `no_pairing_code_received`. Nothing
        closes that session — `autoClose`/`deviceSyncTimeout` are pinned to 0 —
        so a ladder outliving the wait goes on minting real codes against the
        user's number for a dialog that no longer exists. That is a smaller
        version of the thing the v8 cooldown existed to prevent.

        Worst case here: the auth-state gate spends its full 60 probes, then
        attempt 1 fails and attempt 2 fires 20 s later — still inside the
        window, so one hiccup still does not end pairing. A third attempt
        would have landed at ~120 s and a fourth at ~200 s.
        """
        assert "await (0, sleep_1.sleep)(1000);" in MANAGED_PATCHED_LOGIN_BY_CODE
        backoffs = [20 * 2 ** (n - 1) for n in range(1, MANAGED_MAX_MINT_ATTEMPTS)]
        assert MANAGED_AUTH_GATE_SECONDS + sum(backoffs) <= 90

    def test_a_rate_limited_answer_stops_instead_of_backing_off(self):
        """v8 answered a 429 with a 15-minute backoff because it could not
        stop the loop it was in. Here there is no loop to slow down, only one
        to not start — and the quota is per phone number and outlives the
        process, so retrying at all is what keeps it alive."""
        assert "const giveUp = rateLimited || attempt >= 2" in MANAGED_PATCHED_LOGIN_BY_CODE
        assert "900000" not in MANAGED_PATCHED_LOGIN_BY_CODE
        assert "/rate-overlimit|RateOverlimit/i.test(" in MANAGED_PATCHED_LOGIN_BY_CODE

    def test_a_pristine_install_is_patched(self, fake_wppconnect_dist):
        setup_api = _load_setup_api()
        api_dir, host_layer = fake_wppconnect_dist
        _write_managed(host_layer)

        assert setup_api._patch_wppconnect_host_layer(str(api_dir)) is True
        content = host_layer.read_text(encoding="utf-8")
        assert MANAGED_PATCHED_LOGIN_BY_CODE in content
        assert MANAGED_ORIGINAL_LOGIN_BY_CODE not in content


class TestManagedOnLinkCodeAnnouncesEachCodeOnce:
    """The code now arrives twice by design — `conn.link_code_change` routes it
    to onLinkCode(), and startLinkDeviceCodeForPhoneNumber() also resolves with
    it, which loginByCode() hands to the same method.

    Neither route is redundant. The event is the only one that carries a later
    re-mint; the returned value is the only one that survives
    `WPP.on('conn.link_code_change', window.onLinkCode)` being registered
    before page.exposeFunction('onLinkCode', ...) has resolved — a race
    upstream loses by never producing a code at all.
    """

    def test_the_second_delivery_of_the_same_code_is_dropped(self):
        assert "if (!code || this.lastLinkCode === code) {" in MANAGED_PATCHED_ON_LINK_CODE
        guard = MANAGED_PATCHED_ON_LINK_CODE.index("this.lastLinkCode === code")
        emit = MANAGED_PATCHED_ON_LINK_CODE.index("this.catchLinkCode?.(code);")
        assert guard < emit

    def test_a_genuinely_new_code_still_gets_through(self):
        assert "this.lastLinkCode = code;" in MANAGED_PATCHED_ON_LINK_CODE

    def test_login_by_code_delivers_through_the_same_door(self):
        assert "this.onLinkCode(outcome?.code);" in MANAGED_PATCHED_LOGIN_BY_CODE
        assert "this.catchLinkCode" not in MANAGED_PATCHED_LOGIN_BY_CODE

    def test_upstream_had_no_guard_at_all(self):
        assert "lastLinkCode" not in MANAGED_ORIGINAL_ON_LINK_CODE

    def test_a_re_entered_login_by_code_starts_from_a_clean_slate(self):
        """The dedup is per invocation, not per session, and only clearing it
        here makes that true. `page.on('load')` -> afterPageLoad() ->
        afterPageScriptInjected() -> loginByCode() re-runs on every WhatsApp
        Web reload, on the same HostLayer, while wa-js's state inside the page
        is reset. If the post-reload mint answered with the code already on
        screen, a surviving lastLinkCode would drop both deliveries of it —
        catchLinkCode never fires, no phoneCode reaches Python, and
        connect.py's 90 s wait ends in "no pairing code received" for a session
        that had a perfectly good code.

        Defensive: WhatsApp re-issuing an identical code was not reproduced.
        The fix is one line and matches what the expiry hook already does.
        """
        assert MANAGED_PATCHED_LOGIN_BY_CODE.index("this.lastLinkCode = null;") < (
            MANAGED_PATCHED_LOGIN_BY_CODE.index("let ready = null;")
        )


class TestManagedLinkCodeHooksReachTheClient:
    """From the first code onwards, every further failure and the end of the
    stream arrive through `conn.link_code_expired`/`conn.link_code_error` and
    nowhere else — loginByCode() has long since returned. Upstream writes both
    to wppconnect.log, which is the one place a blind user pairing cannot look.
    """

    def test_upstream_only_logs_them(self):
        assert "catchLinkCodeError" not in MANAGED_ORIGINAL_LINK_CODE_HOOKS

    def test_an_expired_code_is_reported(self):
        expiry = MANAGED_PATCHED_LINK_CODE_HOOKS[
            MANAGED_PATCHED_LINK_CODE_HOOKS.index("'onLinkCodeExpired'"):
            MANAGED_PATCHED_LINK_CODE_HOOKS.index("'onLinkCodeError'")
        ]
        assert "name: 'LinkCodeExpired'," in expiry
        assert "this.lastLinkCode = null;" in expiry

    def test_a_refresh_failure_is_reported(self):
        """This is the only route a rate-limit hit on a *refresh* has: the
        mint happens inside wa-js, and its rejection never reaches Node's own
        promise chain."""
        assert (
            "name: hadCode ? 'LinkCodeRefreshFailed' : 'LinkCodeError',"
            in MANAGED_PATCHED_LINK_CODE_HOOKS
        )

    def test_a_refresh_failure_is_named_apart_from_a_first_mint(self):
        """`conn.link_code_error` carries two very different situations.

        After a code has been delivered it is terminal: wa-js clears its 195 s
        timer before minting and re-arms it only from a successful mint, so the
        code on screen is dead and nothing will replace it — the Python end has
        to say so. Before the first code it is just the initial mint failing,
        which loginByCode()'s own ladder retries and connect.py's 90 s wait
        reports; announcing "your code expired" there, over a dialog with no
        code on it, would be noise. `lastLinkCode` is the only thing that tells
        them apart, and only this side has it."""
        hook = MANAGED_PATCHED_LINK_CODE_HOOKS[
            MANAGED_PATCHED_LINK_CODE_HOOKS.index("'onLinkCodeError'"):
        ]
        assert "const hadCode = this.lastLinkCode != null;" in hook
        # Read before it is cleared, or every failure looks like a first mint.
        assert hook.index("const hadCode") < hook.index("this.lastLinkCode = null;")

    def test_the_error_hook_clears_the_last_code_too(self):
        """Same reason as the expiry hook's own line: the code is gone from
        wa-js either way, so if WhatsApp Web does push
        `refresh_alt_linking_code` and the mint behind it answers with the same
        code, a surviving value would have onLinkCode's dedup swallow the one
        delivery that would have recovered the attempt."""
        hook = MANAGED_PATCHED_LINK_CODE_HOOKS[
            MANAGED_PATCHED_LINK_CODE_HOOKS.index("'onLinkCodeError'"):
        ]
        assert "this.lastLinkCode = null;" in hook

    def test_the_upstream_logging_is_kept_as_well(self):
        assert (
            "Login by code expired; call refreshLinkCode() to retry"
            in MANAGED_PATCHED_LINK_CODE_HOOKS
        )
        assert (
            "Login by code failed: ${failed.name || 'Error'}: ${message}"
            in MANAGED_PATCHED_LINK_CODE_HOOKS
        )

    def test_the_hooks_are_patched_by_both_entry_points(self, tmp_path):
        from ui.dialogs.api_setup import ApiSetupDialog
        setup_api = _load_setup_api()

        outputs = []
        for name in ("setup_api", "api_setup"):
            api_dir = tmp_path / name
            layers = api_dir / "node_modules" / "@wppconnect-team" / "wppconnect" / "dist" / "api" / "layers"
            layers.mkdir(parents=True)
            host_layer = layers / "host.layer.js"
            _write_managed(host_layer)

            if name == "setup_api":
                assert setup_api._patch_wppconnect_host_layer(str(api_dir)) is True
            else:
                assert ApiSetupDialog._patch_wppconnect_host_layer(
                    str(api_dir / "node_modules" / "@wppconnect-team" / "wppconnect" / "dist" / "api")
                ) is True
            outputs.append(host_layer.read_text(encoding="utf-8"))

        assert MANAGED_PATCHED_LINK_CODE_HOOKS in outputs[0]
        assert MANAGED_PATCHED_LINK_CODE_LISTENER in outputs[0]
        assert outputs[0] == outputs[1]


class TestAQuotaRefusalOnARefreshIsNamedAsOne:
    """"Cancel and try again" and "wait a few minutes" are opposite
    instructions, and the first one is what spends the quota that produced the
    refusal — so the Python end needs to know which ending this was. It cannot
    work it out from what upstream forwards: wa-js emits `conn.link_code_error`
    through `e instanceof Error ? e : new Error(String(e))` and upstream passes
    only `error.message` to Node, while WhatsApp's own answer lives in the
    error's own properties.
    """

    def test_upstream_forwards_only_the_message(self):
        assert (
            "window.onLinkCodeError(error.message)"
            in MANAGED_ORIGINAL_LINK_CODE_LISTENER
        )

    def test_the_listener_serialises_the_error_inside_the_page(self):
        """page.exposeFunction() serialises its arguments and an Error
        serialises to `{}`, so the walk has to happen page-side — the same walk
        loginByCode()'s own catch does, down to the guards."""
        assert (
            "Object.getOwnPropertyNames(Object(error))"
            in MANAGED_PATCHED_LINK_CODE_LISTENER
        )
        assert "'[unserializable]'" in MANAGED_PATCHED_LINK_CODE_LISTENER
        assert "details: details," in MANAGED_PATCHED_LINK_CODE_LISTENER
        assert (
            "name: String(error?.name || 'Error'),"
            in MANAGED_PATCHED_LINK_CODE_LISTENER
        )

    def test_the_hook_classifies_it_the_way_login_by_code_does(self):
        """One shape reaching phone_code_error_is_rate_limit() from both mint
        paths, rather than a second rule for the refresh half."""
        hook = MANAGED_PATCHED_LINK_CODE_HOOKS[
            MANAGED_PATCHED_LINK_CODE_HOOKS.index("'onLinkCodeError'"):
        ]
        assert "/rate-overlimit|RateOverlimit/i.test(" in hook
        assert "rateLimited: rateLimited," in hook
        assert "details: failed.details || {}," in hook
        # Same regex as the first-mint path, not a second dialect of it.
        assert "/rate-overlimit|RateOverlimit/i.test(" in MANAGED_PATCHED_LOGIN_BY_CODE

    def test_a_plain_string_is_still_accepted(self):
        """The hook and the listener are matched and replaced independently, so
        an install where only the hook took must degrade to a message-only
        report rather than to a report with no message in it at all."""
        hook = MANAGED_PATCHED_LINK_CODE_HOOKS[
            MANAGED_PATCHED_LINK_CODE_HOOKS.index("'onLinkCodeError'"):
        ]
        assert (
            "const failed = (report && typeof report === 'object') ? report : "
            "{ message: String(report || '') };" in hook
        )

    def test_re_running_the_patcher_changes_nothing(self, fake_wppconnect_dist):
        """Both call sites run on every launch against whatever node_modules
        holds."""
        setup_api = _load_setup_api()
        api_dir, host_layer = fake_wppconnect_dist
        _write_managed(host_layer)

        assert setup_api._patch_wppconnect_host_layer(str(api_dir)) is True
        once = host_layer.read_text(encoding="utf-8")
        assert setup_api._patch_wppconnect_host_layer(str(api_dir)) is True

        assert host_layer.read_text(encoding="utf-8") == once


class TestAHalfPatched232InstallIsCompleted:
    """The state a real machine is already in. Before the runtime was pinned,
    upstream's own ^2.2.7 resolved to 2.3.2, and the patcher applied the two
    methods that DID still match — getQrCode() and waitForQrCodeScan() — while
    checkQrCode() and loginByCode() were skipped with a warning.

    So this is not a hypothetical fixture: it is what the previous release left
    behind on every install that reinstalled the API after 2.3.2 shipped.
    """

    def test_the_two_skipped_methods_are_finished_off(self, fake_wppconnect_dist):
        setup_api = _load_setup_api()
        api_dir, host_layer = fake_wppconnect_dist
        _write_managed(
            host_layer,
            getqrcode_text=PATCHED_GET_QR_CODE,
            waitforscan_text=PATCHED_WAIT_FOR_QR_CODE_SCAN,
        )

        assert setup_api._patch_wppconnect_host_layer(str(api_dir)) is True

        content = host_layer.read_text(encoding="utf-8")
        assert MANAGED_PATCHED_CHECK_QR_CODE in content
        assert MANAGED_PATCHED_LOGIN_BY_CODE in content
        assert PATCHED_GET_QR_CODE in content
        assert PATCHED_WAIT_FOR_QR_CODE_SCAN in content

    def test_the_shared_methods_are_patched_the_same_on_both_runtimes(
        self, fake_wppconnect_dist
    ):
        """getQrCode() and waitForQrCodeScan() are byte-identical in 2.3.1 and
        2.3.2, so they belong to neither branch and are applied once for
        both."""
        setup_api = _load_setup_api()
        api_dir, host_layer = fake_wppconnect_dist
        _write_managed(host_layer)

        assert setup_api._patch_wppconnect_host_layer(str(api_dir)) is True

        content = host_layer.read_text(encoding="utf-8")
        assert PATCHED_GET_QR_CODE in content
        assert PATCHED_WAIT_FOR_QR_CODE_SCAN in content

    def test_reapplying_changes_nothing(self, fake_wppconnect_dist):
        setup_api = _load_setup_api()
        api_dir, host_layer = fake_wppconnect_dist
        _write_managed(host_layer)

        setup_api._patch_wppconnect_host_layer(str(api_dir))
        first_pass = host_layer.read_text(encoding="utf-8")
        ok = setup_api._patch_wppconnect_host_layer(str(api_dir))

        assert ok is True
        assert host_layer.read_text(encoding="utf-8") == first_pass
