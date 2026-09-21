"""Tests for client/core/pii_redaction.py and its two call sites.

Reported by users of the public test group: log.log and wppconnect.log are
shared there for diagnosis, and they carried phone numbers and LIDs on
nearly every line — every `[api] rid=... GET /get-messages/<jid>` line, every
`[status-reaction] begin msgId=<...>@lid` line, and others. A prior fix
(get_remote_chats(), tests/test_chat_log_has_no_pii.py) covered one raw
object dump; this covers the pattern everywhere else: a JID logged directly
as plain text, at well over a hundred call sites.

redact_phone() is applied once, at the two places all of this passes through
by construction — main.py's logging.Formatter (every Python log line) and
core/api_client.py's redact_api_url() (every URL label) — rather than at each
of those call sites individually, which cannot be kept complete.
"""

import logging

import pytest

from core.pii_redaction import redact_phone


class TestRedactPhone:
    @pytest.mark.parametrize(
        "jid",
        [
            "5511999998888@s.whatsapp.net",
            "5511999998888@c.us",
            "68904344899801@lid",
            "120363067944453158@g.us",  # group id: 18 digits, longer than a phone/LID
        ],
    )
    def test_every_jid_suffix_is_masked(self, jid):
        digits = jid.split("@")[0]
        suffix = "@" + jid.split("@")[1]
        result = redact_phone(f"GET /get-messages/{jid}?count=200")
        assert digits not in result
        assert result.endswith(f"{suffix}?count=200")
        assert result.startswith("GET /get-messages/~")

    def test_a_bare_phone_number_with_no_jid_suffix_is_masked(self):
        """/last-seen/<phone> and similar routes take a bare number."""
        result = redact_phone("GET /last-seen/5521970076785 -> 200 in 42ms")
        assert "5521970076785" not in result

    def test_a_status_message_id_masks_only_the_trailing_jid(self):
        """A status message id's own hex segment is not a phone number and
        is left alone (best-effort; harmless either way) — the JID always
        appended as the poster's identity is the part that matters."""
        msg_id = "false_status@broadcast_4A86450069E7268788E7_100742836789440@lid"
        result = redact_phone(f"[status-reaction] begin msgId={msg_id} action=set")
        assert "100742836789440" not in result
        assert result.endswith("@lid action=set")

    def test_the_same_number_produces_the_same_tag(self):
        """Correlation across a log ('is this the same contact again?') is
        the one thing worth keeping — the tag must be stable, not random."""
        first = redact_phone("5511999998888@c.us")
        second = redact_phone("5511999998888@c.us")
        assert first == second

    def test_different_numbers_produce_different_tags(self):
        a = redact_phone("5511999998888@c.us")
        b = redact_phone("5511888887777@c.us")
        assert a != b

    def test_short_numbers_are_left_alone(self):
        """count=200, port numbers, durations, etc. are not phone numbers."""
        assert redact_phone("count=200 in 43ms") == "count=200 in 43ms"

    def test_a_hex_request_id_is_not_mistaken_for_a_phone_number(self):
        rid = "6f9ac9e02d9c4030b60e1c5022137a2c"
        assert redact_phone(f"rid={rid}") == f"rid={rid}"

    def test_idempotent(self):
        once = redact_phone("5511999998888@c.us")
        twice = redact_phone(once)
        assert once == twice

    def test_empty_and_none_do_not_crash(self):
        assert redact_phone("") == ""
        assert redact_phone(None) == ""


class TestRedactApiUrlMasksPhoneNumbers:
    def test_get_messages_endpoint_masks_the_jid(self):
        from core.api_client import redact_api_url

        label = redact_api_url(
            "http://127.0.0.1:6300/api/session:token/get-messages/5521970076785@s.whatsapp.net?count=200"
        )
        assert "5521970076785" not in label
        assert label.endswith("?count=200")

    def test_a_short_id_route_is_unaffected(self):
        """Existing behavior (test_api_client.py) must not regress for a
        route whose id is too short to be a real phone number."""
        from core.api_client import redact_api_url

        assert (
            redact_api_url(
                "http://127.0.0.1:6300/api/session:token/get-messages/5511@c.us?count=200"
            )
            == "/get-messages/5511@c.us?count=200"
        )


class TestTheGlobalFormatterMasksPhoneNumbers:
    """main.py's setup_logging() installs _PiiRedactingFormatter on the root
    logger's handler. This exercises the formatter class directly rather
    than the whole of setup_logging() (which touches real files), the same
    way the rest of the suite avoids real I/O for a unit-level check."""

    def test_a_percent_style_log_call_is_masked(self):
        from main import _PiiRedactingFormatter

        formatter = _PiiRedactingFormatter("%(message)s")
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname=__file__, lineno=1,
            msg="[merge_lid] moved %s -> %s in the database",
            args=("68904344899801@lid", "5521970076785@s.whatsapp.net"),
            exc_info=None,
        )
        rendered = formatter.format(record)
        assert "68904344899801" not in rendered
        assert "5521970076785" not in rendered
        # Still readable prose, not just a redacted blob.
        assert "@lid" in rendered and "@s.whatsapp.net" in rendered
        assert "[merge_lid] moved" in rendered

    def test_an_fstring_log_call_is_masked(self):
        """f-string calls have no separate args — the value is already
        merged into msg by the time logging builds the record, exactly like
        it will be merged into the final string by format()."""
        from main import _PiiRedactingFormatter

        formatter = _PiiRedactingFormatter("%(message)s")
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname=__file__, lineno=1,
            msg="[on_group_subject_updated] Updating group 120363067944453158@g.us",
            args=(),
            exc_info=None,
        )
        rendered = formatter.format(record)
        assert "120363067944453158" not in rendered
