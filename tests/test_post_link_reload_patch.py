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
    "            else if (authenticated) {",
    m.ORIGINAL_LOGIN_SUCCESS,
    "            }",
])


def test_all_three_parts_are_patched():
    notes = []
    out = m._patch_post_link_reload(SOURCE, notes)
    assert m.PATCHED_POST_SCAN_AUTH_CHECK in out
    assert m.PATCHED_RELOAD_LOGIN_BY_CODE in out
    assert m.PATCHED_LOGIN_SUCCESS in out
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
    assert sum("DID NOT MATCH" in n for n in notes) == 3


def test_reload_waits_for_the_new_page_before_giving_up():
    assert "waitForPageLoad()" in m.PATCHED_POST_SCAN_AUTH_CHECK
    assert "this.__wzCodeAccepted" in m.PATCHED_RELOAD_LOGIN_BY_CODE
