"""Tests that contact-sync diagnostics log shape, not content.

Same class of bug as get_remote_chats() (tests/test_chat_log_has_no_pii.py),
found in two more places while auditing every log statement for a report
that phone numbers and contact names were leaking into log.log/wppconnect.log
shared in the public test group:

- get_remote_contacts() logged the first contact's whole raw dict AND the
  actual text of up to 50 contact names, at INFO, on every contact sync.
- LID Resolution's profile-name-rejected branch logged the candidate name and
  the whole raw profile API response, also at INFO.

Key names/types (or, for the rejection case, why a name was rejected) are
what was ever useful for debugging either path.
"""

import inspect

from main import MainWindow


def _contacts_source():
    return inspect.getsource(MainWindow.get_remote_contacts)


class TestGetRemoteContactsLogsShapeNotContent:
    def test_no_log_call_interpolates_a_whole_contact(self):
        source = _contacts_source()
        for line in source.splitlines():
            if "logging." not in line:
                continue
            assert "filtered_contacts[0]}" not in line, line
            assert "filtered_contacts[0])" not in line, line

    def test_no_log_call_joins_actual_contact_names(self):
        """The old line joined up to 50 real contact names into one log
        message — the exact leak this test exists to catch a regression of."""
        source = _contacts_source()
        assert "names_with_values" not in source or "join(names_with_values" not in source

    def test_the_shape_is_still_logged(self):
        source = _contacts_source()
        assert "First contact shape" in source
        assert "type(v).__name__" in source

    def test_the_debug_lines_no_longer_interpolate_name(self):
        """The two `logging.debug(...)` lines used to format "{name} ({jid})"
        — dropped the name entirely, not just masked the jid, since a name
        has no pattern the logging formatter's phone/JID masking can catch."""
        source = _contacts_source()
        for line in source.splitlines():
            if "logging.debug" not in line:
                continue
            assert "{name}" not in line, line


class TestLidResolutionRejectedNameLogsShapeNotContent:
    def test_profile_rejection_no_longer_logs_the_name_or_raw_response(self):
        """Search main.py directly rather than a specific method name, since
        this diagnostic sits inside a large sync method not worth coupling
        the test to."""
        import main

        source = inspect.getsource(main)
        marker = "Profile name not resolved/accepted for"
        assert marker in source
        line = next(
            l for l in source.splitlines() if marker in l
        )
        assert "{name}" not in line, line
        assert "{res_data}" not in line, line
        assert "Response shape" in line
