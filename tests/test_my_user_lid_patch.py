"""Sending to a group or an @lid chat failed on WhatsApp Web 2.3000.1055167106
with ``UserPrefs.getMeLidUserOrThrow is not a function`` (wa-js 4.8.0's
``getMyUserLid``). The patch falls back to ``getMaybeMeLidUser``, the way wa-js
4.8.2 does. Tests run the published expression under Node with synthetic
modules; no WhatsApp session is involved.
"""
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from core.wppconnect_wa_js_patch import (
    STATUS_ALREADY, STATUS_APPLIED, STATUS_NO_MATCH, WA_JS_BUNDLE_PARTS,
    patch_my_user_lid_bundle, patch_my_user_lid_source,
)

ROOT = Path(__file__).resolve().parents[1]
# The module as wa-js 4.8.0 ships it (dist/wppconnect-wa.js)
ORIGINAL = (
    '7379(e,t,r){"use strict";Object.defineProperty(t,"__esModule",{value:!0}),'
    't.getMyUserLid=function(){return n.UserPrefs.getMeLidUserOrThrow()};const n=r(14647)}'
)
# ... and as 4.8.2 does
UPSTREAM_FIX = (
    '7379(e,t,r){"use strict";t.getMyUserLid=function(){const e=n.UserPrefs;'
    'if("function"==typeof(null==e?void 0:e.getMeLidUserOrThrow))return e.getMeLidUserOrThrow();'
    'if("function"==typeof(null==e?void 0:e.getMaybeMeLidUser)){const t=e.getMaybeMeLidUser();'
    'if(t)return t}throw new Error("x")};const n=r(14647)}'
)


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(source, user_prefs_js):
    """What ``getMyUserLid()`` answers with the given UserPrefs."""
    if shutil.which("node") is None:
        pytest.skip("Node is not available")
    script = (
        "const t={};const n={UserPrefs:" + user_prefs_js + "};"
        "const r=()=>n;(function(e,t,r){" + source.split("(e,t,r){", 1)[1].rsplit("}", 1)[0] + "})({},t,r);"
        "let out;try{out=t.getMyUserLid()}catch(e){out='threw:'+e.message}"
        "console.log(JSON.stringify(out))"
    )
    done = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


class TestSource:
    def test_applied_once_then_already(self):
        patched, status = patch_my_user_lid_source(ORIGINAL)
        assert status == STATUS_APPLIED and "getMaybeMeLidUser" in patched
        again, status = patch_my_user_lid_source(patched)
        assert status == STATUS_ALREADY and again == patched

    def test_the_upstream_fix_is_left_alone(self):
        assert patch_my_user_lid_source(UPSTREAM_FIX) == (UPSTREAM_FIX, STATUS_ALREADY)

    def test_an_upstream_rewrite_is_reported_untouched(self):
        assert patch_my_user_lid_source("a rewrite") == ("a rewrite", STATUS_NO_MATCH)


class TestBehaviour:
    def test_original_dies_when_the_getter_is_gone(self):
        assert _run(ORIGINAL, "{getMaybeMeLidUser:()=>'me@lid'}").startswith("threw:")

    def test_patched_falls_back_when_the_getter_is_gone(self):
        patched, _ = patch_my_user_lid_source(ORIGINAL)
        assert _run(patched, "{getMaybeMeLidUser:()=>'me@lid'}") == "me@lid"

    def test_patched_keeps_using_the_getter_when_it_exists(self):
        patched, _ = patch_my_user_lid_source(ORIGINAL)
        assert _run(patched, "{getMeLidUserOrThrow:()=>'old@lid',getMaybeMeLidUser:()=>'new@lid'}") == "old@lid"


@pytest.fixture
def bundle(tmp_path):
    path = tmp_path.joinpath(*WA_JS_BUNDLE_PARTS)
    path.parent.mkdir(parents=True)
    path.write_bytes(('"use strict";\r\n' + ORIGINAL + ';\r\n//# sourceMappingURL=x\r\n').encode())
    return tmp_path, path


class TestFile:
    def test_only_the_expression_changes_and_a_repeat_keeps_the_bytes(self, bundle):
        api, path = bundle
        before = path.read_bytes()
        assert patch_my_user_lid_bundle(str(api))[0]
        after = path.read_bytes()
        patched, _ = patch_my_user_lid_source(ORIGINAL)
        assert after == before.replace(ORIGINAL.encode(), patched.encode())
        assert patch_my_user_lid_bundle(str(api))[0]
        assert path.read_bytes() == after

    def test_missing_bundle_is_reported(self, tmp_path):
        ok, note = patch_my_user_lid_bundle(str(tmp_path))
        assert not ok and "not found" in note

    def test_unrecognized_bundle_is_reported_without_writing(self, bundle):
        api, path = bundle
        path.write_bytes(b"a rewrite\r\n")
        ok, note = patch_my_user_lid_bundle(str(api))
        assert not ok and "did not match" in note
        assert path.read_bytes() == b"a rewrite\r\n"


class TestCallSites:
    def test_setup_api(self, bundle):
        api, path = bundle
        assert _load("setup_api")._patch_my_user_lid_bundle(str(api))
        assert patch_my_user_lid_source(path.read_text())[1] == STATUS_ALREADY

    def test_api_setup_on_startup(self, bundle):
        from ui.dialogs.api_setup import ApiSetupDialog
        api, path = bundle
        ApiSetupDialog._apply_node_modules_patches(str(api))
        assert patch_my_user_lid_source(path.read_text())[1] == STATUS_ALREADY

    def test_build_api(self, bundle):
        api, path = bundle
        _load("build_api")._apply_node_modules_patches(str(api))
        assert patch_my_user_lid_source(path.read_text())[1] == STATUS_ALREADY
