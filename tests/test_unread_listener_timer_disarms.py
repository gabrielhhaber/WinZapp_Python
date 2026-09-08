"""The unread-listener retry timer has to stop, and clearInterval does not.

Measured on a user's session: a **single** scheduled installer logged
"onUnreadCountChanged listener: installed after 0 retries" every 500 ms for
three minutes and ten seconds, stopping only when the session was torn down.
One `setInterval`, 364 log lines, all at exactly 500 ms, all reporting 0
retries — so the `if` body ran 364 times, which means `clearInterval(timer)`
ran 364 times and did not stop it.

The likely reason is that WhatsApp Web wraps `setInterval` for its own
scheduler and returns a handle the native `clearInterval` does not recognise.
But the fix must not depend on knowing that: whatever the cause, a
`page.evaluate`'d loop running at 2 Hz inside WhatsApp Web — while it is
loading a 900-chat account — is not something to leave to a call already
observed to fail.

The code's own comment had anticipated exactly this shape ("leaving a timer
firing every 500ms for the life of the page") for a different reason, which is
what makes it worth pinning rather than trusting.
"""

import re
from pathlib import Path

import pytest


SOURCE = (
    Path(__file__).resolve().parents[1]
    / "client" / "api_patches" / "src" / "util" / "createSessionUtil.ts"
).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def installer():
    """The unread listener's page.evaluate body, up to its `.then()`."""
    start = SOURCE.index("async onUnreadCountChanged(")
    return SOURCE[start:SOURCE.index(".then((result: string)", start)]


class TestTheCallbackDisarmsItself:
    def test_it_returns_early_once_settled(self, installer):
        """The interval may keep ticking; it must stop being able to do
        anything."""
        assert "if (settled) return;" in installer

    def test_settled_is_set_before_anything_else_in_the_branch(self, installer):
        body = installer[installer.index("if (install() || ++tries > 60)"):]
        settled = body.index("settled = true;")
        clear = body.index("clearInterval(timer)")
        log = body.index("console.log(")
        assert settled < clear < log, (
            "the flag has to be set before the call that has been observed to "
            "fail, or a failing clearInterval still leaves the callback armed"
        )

    def test_clearinterval_is_still_attempted_but_cannot_throw(self, installer):
        """Keep asking politely — just never depend on the answer."""
        block = installer[installer.index("settled = true;"):]
        block = block[:block.index("console.log(")]
        assert "try {" in block and "clearInterval(timer)" in block
        assert "catch" in block


class TestOnlyOneTimerPerPage:
    def test_a_second_installer_does_not_stack_another_timer(self, installer):
        """installListener is registered on page.on('load'), so it can be
        invoked repeatedly — and an uncancellable timer per invocation is how
        one becomes many."""
        assert "__winzappUnreadListenerScheduled" in installer
        assert "return 'already scheduled';" in installer

    def test_the_guard_is_checked_before_the_timer_is_created(self, installer):
        guard = installer.index("if (w.__winzappUnreadListenerScheduled)")
        create = installer.index("setInterval(")
        assert guard < create

    def test_the_slot_is_released_when_the_timer_settles(self, installer):
        """Otherwise a page that reloads never gets a listener again: the flag
        would still say one is scheduled, and the timer it named is gone."""
        block = installer[installer.index("settled = true;"):]
        assert "__winzappUnreadListenerScheduled = false;" in block[:400]

    def test_the_immediate_success_path_never_sets_the_guard(self, installer):
        """`if (install()) return 'installed'` happens first and schedules
        nothing, so it must not leave a flag behind claiming otherwise."""
        head = installer[:installer.index("if (install()) return 'installed';")]
        assert "__winzappUnreadListenerScheduled" not in head


class TestTheRetryBoundSurvives:
    def test_it_still_gives_up_after_60_tries(self, installer):
        assert "++tries > 60" in installer

    def test_it_still_reports_which_outcome_it_reached(self, installer):
        assert "installed after ${tries} retries" in installer
        assert "GAVE UP after 60 retries" in installer

    def test_the_interval_is_still_500ms(self, installer):
        assert re.search(r"\}, 500\);", installer)
