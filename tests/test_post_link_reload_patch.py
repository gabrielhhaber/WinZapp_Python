"""host.layer.js keeps the login WhatsApp Web finishes on the reload that
follows an accepted pairing code (macOS report, 2026-11-23)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "client"))

from core import wppconnect_host_layer_patch as m  # noqa: E402

SOURCE = "\n".join([
    "        if (typeof this.options.phoneNumber === 'string') {",
    m.ORIGINAL_RELOAD_LOGIN_BY_CODE,
    "        }",
    m.ORIGINAL_POST_SCAN_AUTH_CHECK,
    "                this.log('warn', 'Failed to authenticate');",
    "            }",
])


def test_both_parts_are_patched():
    notes = []
    out = m._patch_post_link_reload(SOURCE, notes)
    assert m.PATCHED_POST_SCAN_AUTH_CHECK in out
    assert m.PATCHED_RELOAD_LOGIN_BY_CODE in out
    assert not any("DID NOT MATCH" in n for n in notes)


def test_patching_twice_changes_nothing():
    once = m._patch_post_link_reload(SOURCE, [])
    notes = []
    assert m._patch_post_link_reload(once, notes) == once
    assert all("already" in n for n in notes)


def test_a_file_without_these_functions_is_left_alone():
    notes = []
    assert m._patch_post_link_reload("nothing here", notes) == "nothing here"
    assert not any("DID NOT MATCH" in n for n in notes)


def test_changed_text_inside_the_function_is_reported_not_guessed():
    src = "async waitForLogin() {}"
    notes = []
    assert m._patch_post_link_reload(src, notes) == src
    assert sum("DID NOT MATCH" in n for n in notes) == 2


def test_an_older_runtime_with_another_page_load_call_raises_no_alarm():
    # 2.3.x has afterPageScriptInjected() but not this runtime's waitForLogin().
    src = "async afterPageScriptInjected() { return this.loginByCode(phone); }"
    notes = []
    assert m._patch_post_link_reload(src, notes) == src
    assert not any("DID NOT MATCH" in n for n in notes)


def test_reload_waits_for_the_new_page_before_giving_up():
    assert "waitForPageLoad()" in m.PATCHED_POST_SCAN_AUTH_CHECK


def test_a_reload_with_a_code_on_screen_waits_for_the_login_first():
    # isLogged is often still false on that reload (the scan wait's probe
    # throws mid-reload), so the guard cannot rely on it alone: a code shown
    # before the reload means the phone may have accepted it.
    patched = m.PATCHED_RELOAD_LOGIN_BY_CODE
    assert "this.lastLinkCode != null" in patched
    assert "needsToScan" in patched
    wait = patched.index("needsToScan")
    assert patched.index("this.loginByCode(") > wait
    # Only a login that never came asks for a new code.
    assert "if (wzLoggedIn)" in patched and "else if (!this.page.isClosed())" in patched


def test_a_newer_reload_takes_over_the_wait():
    assert "wzLoad !== this.pageLoadPromise" in m.PATCHED_RELOAD_LOGIN_BY_CODE


def test_the_live_host_layer_is_patched_and_stays_idempotent():
    import pytest
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "client", "api", "node_modules", "@wppconnect-team",
                        "wppconnect", "dist", "api", "layers", "host.layer.js")
    if not os.path.exists(path):
        pytest.skip("client/api is not set up")
    with open(path, encoding="utf-8") as f:
        live = f.read()
    notes = []
    once = m._patch_post_link_reload(live, notes)
    assert not any("DID NOT MATCH" in n for n in notes), notes
    assert m._patch_post_link_reload(once, []) == once
