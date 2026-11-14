"""Tests for the status.layer.js posting-result patch.

Bug: sendTextStatus()/sendImageStatus()/sendVideoStatus() each call
Puppeteer's page.evaluate() with a callback that neither is `async` nor
awaits/returns the actual WPP.status.sendXStatus(...) call — so the
Node-side promise resolves immediately with `undefined`, regardless of
whether WhatsApp actually accepted the post. statusController.ts then
always reports HTTP 200/201 back to WinZapp no matter what happened, which
is why "posted a text status" never correctly showed success vs. failure.

Both setup_api.py and ApiSetupDialog (client/ui/dialogs/api_setup.py) apply
the same three-way patch, importing shared source-text constants from
client/core/wppconnect_status_layer_patch.py.
"""

import importlib.util
import os

import pytest

from core.wppconnect_status_layer_patch import ALL_PATCHES, ALTERNATIVE_ORIGINALS

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_setup_api():
    spec = importlib.util.spec_from_file_location(
        "setup_api", os.path.join(REPO_ROOT, "setup_api.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_PRISTINE_STATUS_LAYER = """class StatusLayer extends labels_layer_1.LabelsLayer {
    async sendImageStatus(pathOrBase64, options) {
        return await (0, helpers_1.evaluateAndReturn)(this.page, ({ base64, options }) => {
            WPP.status.sendImageStatus(base64, options);
        }, { base64, options });
    }
    async sendVideoStatus(pathOrBase64, options) {
        return await (0, helpers_1.evaluateAndReturn)(this.page, ({ base64, options }) => {
            WPP.status.sendVideoStatus(base64, options);
        }, { base64, options });
    }
    async sendTextStatus(text, options) {
        return await (0, helpers_1.evaluateAndReturn)(this.page, ({ text, options }) => {
            WPP.status.sendTextStatus(text, options);
        }, { text, options });
    }
}
"""


@pytest.fixture
def fake_wppconnect_dist(tmp_path):
    layers_dir = tmp_path / "node_modules" / "@wppconnect-team" / "wppconnect" / "dist" / "api" / "layers"
    layers_dir.mkdir(parents=True)
    status_layer = layers_dir / "status.layer.js"
    status_layer.write_text(_PRISTINE_STATUS_LAYER, encoding="utf-8")
    return tmp_path, status_layer


class TestSharedPatchesAreCorrect:
    def test_all_originals_differ_from_their_patched_form(self):
        for original, patched in ALL_PATCHES:
            assert original != patched

    def test_every_patch_makes_the_callback_async_and_awaits_the_call(self):
        for _, patched in ALL_PATCHES:
            assert "async (" in patched
            assert "return await WPP.status." in patched


class TestSetupApiPatch:
    def test_patches_all_three_methods(self, fake_wppconnect_dist):
        setup_api = _load_setup_api()
        api_dir, status_layer = fake_wppconnect_dist

        ok = setup_api._patch_wppconnect_status_layer(str(api_dir))

        assert ok is True
        content = status_layer.read_text(encoding="utf-8")
        assert "return await WPP.status.sendTextStatus(text, options);" in content
        assert "return await WPP.status.sendImageStatus(base64, options);" in content
        assert "return await WPP.status.sendVideoStatus(base64, options);" in content
        # The old fire-and-forget calls must be gone.
        assert "WPP.status.sendTextStatus(text, options);\n" not in content.replace(
            "return await WPP.status.sendTextStatus(text, options);\n", ""
        )

    def test_is_idempotent(self, fake_wppconnect_dist):
        setup_api = _load_setup_api()
        api_dir, status_layer = fake_wppconnect_dist

        setup_api._patch_wppconnect_status_layer(str(api_dir))
        first_pass = status_layer.read_text(encoding="utf-8")
        ok = setup_api._patch_wppconnect_status_layer(str(api_dir))
        second_pass = status_layer.read_text(encoding="utf-8")

        assert ok is True
        assert first_pass == second_pass

    def test_missing_file_is_a_safe_no_op(self, tmp_path):
        setup_api = _load_setup_api()
        ok = setup_api._patch_wppconnect_status_layer(str(tmp_path))
        assert ok is False

    def test_partial_match_patches_what_it_can_and_reports_the_rest(self, fake_wppconnect_dist):
        """If a future upstream release only changed one of the three
        methods, the other two must still get patched rather than the
        whole operation bailing out."""
        setup_api = _load_setup_api()
        api_dir, status_layer = fake_wppconnect_dist
        content = status_layer.read_text(encoding="utf-8")
        # Corrupt just the sendVideoStatus method's recognizable text.
        content = content.replace(
            "WPP.status.sendVideoStatus(base64, options);", "WPP.status.sendVideoStatusV2(base64, options);"
        )
        status_layer.write_text(content, encoding="utf-8")

        ok = setup_api._patch_wppconnect_status_layer(str(api_dir))

        assert ok is False  # one method didn't match
        final = status_layer.read_text(encoding="utf-8")
        assert "return await WPP.status.sendTextStatus(text, options);" in final
        assert "return await WPP.status.sendImageStatus(base64, options);" in final


class TestApiSetupDialogPatch:
    def test_patches_all_three_methods(self, fake_wppconnect_dist):
        from ui.dialogs.api_setup import ApiSetupDialog
        api_dir, status_layer = fake_wppconnect_dist
        wppconnect_api_dir = str(api_dir / "node_modules" / "@wppconnect-team" / "wppconnect" / "dist" / "api")

        ok = ApiSetupDialog._patch_wppconnect_status_layer(wppconnect_api_dir)

        assert ok is True
        content = status_layer.read_text(encoding="utf-8")
        assert "return await WPP.status.sendTextStatus(text, options);" in content

    def test_is_idempotent(self, fake_wppconnect_dist):
        from ui.dialogs.api_setup import ApiSetupDialog
        api_dir, status_layer = fake_wppconnect_dist
        wppconnect_api_dir = str(api_dir / "node_modules" / "@wppconnect-team" / "wppconnect" / "dist" / "api")

        ApiSetupDialog._patch_wppconnect_status_layer(wppconnect_api_dir)
        first_pass = status_layer.read_text(encoding="utf-8")
        ApiSetupDialog._patch_wppconnect_status_layer(wppconnect_api_dir)
        assert status_layer.read_text(encoding="utf-8") == first_pass

    def test_apply_node_modules_patches_includes_status_layer(self, fake_wppconnect_dist):
        from ui.dialogs.api_setup import ApiSetupDialog
        api_dir, status_layer = fake_wppconnect_dist

        ApiSetupDialog._apply_node_modules_patches(str(api_dir))

        assert "return await WPP.status.sendTextStatus(text, options);" in status_layer.read_text(encoding="utf-8")


class TestBothEntryPointsAgree:
    def test_both_produce_the_same_output(self, tmp_path):
        from ui.dialogs.api_setup import ApiSetupDialog
        setup_api = _load_setup_api()

        for label in ("a", "b"):
            layers = tmp_path / label / "node_modules" / "@wppconnect-team" / "wppconnect" / "dist" / "api" / "layers"
            layers.mkdir(parents=True)
            (layers / "status.layer.js").write_text(_PRISTINE_STATUS_LAYER, encoding="utf-8")

        setup_api._patch_wppconnect_status_layer(str(tmp_path / "a"))
        ApiSetupDialog._patch_wppconnect_status_layer(
            str(tmp_path / "b" / "node_modules" / "@wppconnect-team" / "wppconnect" / "dist" / "api")
        )

        a_content = (tmp_path / "a" / "node_modules" / "@wppconnect-team" / "wppconnect" / "dist" / "api" / "layers" / "status.layer.js").read_text(encoding="utf-8")
        b_content = (tmp_path / "b" / "node_modules" / "@wppconnect-team" / "wppconnect" / "dist" / "api" / "layers" / "status.layer.js").read_text(encoding="utf-8")
        assert a_content == b_content


_V240_STATUS_LAYER = """class StatusLayer extends labels_layer_1.LabelsLayer {
    async sendImageStatus(pathOrBase64, options) {
        return await (0, helpers_1.evaluateAndReturn)(this.page, async ({ base64, options }) => {
            const result = await WPP.status.sendImageStatus(base64, options);
            return { ack: result?.ack, id: result?.id };
        }, { base64, options });
    }
    async sendVideoStatus(pathOrBase64, options) {
        return await (0, helpers_1.evaluateAndReturn)(this.page, async ({ base64, options }) => {
            const result = await WPP.status.sendVideoStatus(base64, options);
            return { ack: result?.ack, id: result?.id };
        }, { base64, options });
    }
    async sendTextStatus(text, options) {
        return await (0, helpers_1.evaluateAndReturn)(this.page, async ({ text, options }) => {
            const result = await WPP.status.sendTextStatus(text, options);
            return { ack: result?.ack, id: result?.id };
        }, { text, options });
    }
}
"""


class TestWppconnect240ReturnsTooLittle:
    """wppconnect 2.4.0 awaits each WPP.status call but returns only {ack, id}.
    statusController.ts reads `sendMsgResult.messageSendResult` from that result
    to refuse a status WhatsApp rejected, so the 2.4.0 text is rewritten into
    the same full-result form as the older runtimes, not left as it is."""

    def test_there_is_one_alternative_per_patch_and_it_is_the_ack_id_form(self):
        assert len(ALTERNATIVE_ORIGINALS) == len(ALL_PATCHES)
        for alternatives, (_, patched) in zip(ALTERNATIVE_ORIGINALS, ALL_PATCHES):
            method = patched.split("WPP.status.")[1].split("(")[0]
            assert len(alternatives) == 1
            assert f"await WPP.status.{method}(" in alternatives[0]
            assert "return { ack: result?.ack, id: result?.id };" in alternatives[0]
            assert alternatives[0] != patched

    def test_the_server_really_reads_the_field_the_240_shape_drops(self):
        controller = os.path.join(
            REPO_ROOT, "client", "api_patches", "src", "controller", "statusController.ts"
        )
        with open(controller, encoding="utf-8") as fh:
            assert "sendMsgResult?.messageSendResult" in fh.read()

    def test_setup_api_rewrites_all_three_methods_to_the_full_result(self, fake_wppconnect_dist):
        setup_api = _load_setup_api()
        api_dir, status_layer = fake_wppconnect_dist
        status_layer.write_text(_V240_STATUS_LAYER, encoding="utf-8")

        assert setup_api._patch_wppconnect_status_layer(str(api_dir)) is True

        content = status_layer.read_text(encoding="utf-8")
        for method in ("sendTextStatus", "sendImageStatus", "sendVideoStatus"):
            assert f"return await WPP.status.{method}(" in content
        assert "ack: result?.ack" not in content

    def test_it_is_idempotent(self, fake_wppconnect_dist):
        setup_api = _load_setup_api()
        api_dir, status_layer = fake_wppconnect_dist
        status_layer.write_text(_V240_STATUS_LAYER, encoding="utf-8")

        setup_api._patch_wppconnect_status_layer(str(api_dir))
        first_pass = status_layer.read_text(encoding="utf-8")
        assert setup_api._patch_wppconnect_status_layer(str(api_dir)) is True
        assert status_layer.read_text(encoding="utf-8") == first_pass

    def test_the_setup_dialog_rewrites_it_the_same_way(self, fake_wppconnect_dist):
        from ui.dialogs.api_setup import ApiSetupDialog
        setup_api = _load_setup_api()
        api_dir, status_layer = fake_wppconnect_dist
        status_layer.write_text(_V240_STATUS_LAYER, encoding="utf-8")
        wppconnect_api_dir = str(api_dir / "node_modules" / "@wppconnect-team" / "wppconnect" / "dist" / "api")

        assert ApiSetupDialog._patch_wppconnect_status_layer(wppconnect_api_dir) is True
        dialog_output = status_layer.read_text(encoding="utf-8")

        status_layer.write_text(_V240_STATUS_LAYER, encoding="utf-8")
        setup_api._patch_wppconnect_status_layer(str(api_dir))
        assert status_layer.read_text(encoding="utf-8") == dialog_output

    def test_a_method_that_matches_neither_form_is_still_reported(self, fake_wppconnect_dist):
        setup_api = _load_setup_api()
        api_dir, status_layer = fake_wppconnect_dist
        status_layer.write_text(
            _V240_STATUS_LAYER.replace("sendVideoStatus(base64, options);", "sendVideoStatusV3(base64, options);"),
            encoding="utf-8",
        )

        assert setup_api._patch_wppconnect_status_layer(str(api_dir)) is False
        content = status_layer.read_text(encoding="utf-8")
        assert "return await WPP.status.sendTextStatus(text, options);" in content

