"""Tests for ConversationDataDialog's "is the current user a group admin"
detection — _participant_phone_part() and _participant_is_me().

Reported live: the edit-name/edit-description buttons and the participant
context menu never showed up even for a group the user genuinely
administers, and Shift+F10 on the participants list fell through to the
generic OS context menu instead of the new one — both symptoms of
_populate_group_unsafe()'s admin-detection loop always evaluating to
"not admin". Root cause: it compared a bare
``participant_jid.split("@")[0]`` against the user's own JID digits with no
allowance for either (a) a ":device" companion suffix WPPConnect commonly
attaches to participant "id" values (e.g. "5511999999999:60@s.whatsapp.net"),
which a plain split never strips, or (b) the Brazilian 8/9-digit phone
variant. MainWindow._is_group_send_restricted() already solves both
correctly for the exact same "is this participant me" question — these two
helpers are a direct port of its logic so the two call sites can't drift
apart again.
"""

from ui.dialogs.conversation_data_dialog import _participant_is_me, _participant_phone_part


def _phone_digits_equivalent(a: str, b: str) -> bool:
    """Real MainWindow._phone_digits_equivalent() logic, duplicated here
    (it's a plain @staticmethod with no self-dependency) so this test file
    doesn't need to import all of main.py."""
    if a == b:
        return True
    if a.startswith("55") and b.startswith("55"):
        if len(a) == 13 and len(b) == 12 and a[4] == "9":
            return a[:4] + a[5:] == b
        if len(b) == 13 and len(a) == 12 and b[4] == "9":
            return b[:4] + b[5:] == a
    return False


class TestParticipantPhonePart:
    def test_strips_at_suffix(self):
        assert _participant_phone_part("5511999999999@s.whatsapp.net") == "5511999999999"

    def test_strips_device_suffix_too(self):
        """The exact reported bug: WPPConnect's participant "id" commonly
        carries a ":device" suffix a bare .split("@")[0] never stripped."""
        assert _participant_phone_part("5511999999999:60@s.whatsapp.net") == "5511999999999"

    def test_lid_jid(self):
        assert _participant_phone_part("123456789012345@lid") == "123456789012345"

    def test_non_string_is_empty(self):
        assert _participant_phone_part(None) == ""
        assert _participant_phone_part({"id": "x"}) == ""


class TestParticipantIsMe:
    def test_exact_phone_match(self):
        assert _participant_is_me(
            "5511999999999@s.whatsapp.net", "5511999999999", "", _phone_digits_equivalent
        )

    def test_device_suffix_no_longer_breaks_the_match(self):
        """Direct regression test for the reported bug: an admin whose own
        participant entry carries a device suffix must still match."""
        assert _participant_is_me(
            "5511999999999:60@s.whatsapp.net", "5511999999999", "", _phone_digits_equivalent
        )

    def test_brazilian_9th_digit_variant_matches(self):
        # My own JID has the 9th digit; the participant entry doesn't.
        assert _participant_is_me(
            "551199999999@s.whatsapp.net", "5511999999999", "", _phone_digits_equivalent
        )

    def test_lid_match_is_exact_not_digit_tolerant(self):
        assert _participant_is_me("123456789012345@lid", "", "123456789012345", _phone_digits_equivalent)
        assert not _participant_is_me("123456789012346@lid", "", "123456789012345", _phone_digits_equivalent)

    def test_a_different_participant_does_not_match(self):
        assert not _participant_is_me(
            "5511888888888@s.whatsapp.net", "5511999999999", "", _phone_digits_equivalent
        )

    def test_no_known_identity_never_matches(self):
        assert not _participant_is_me("5511999999999@s.whatsapp.net", "", "", _phone_digits_equivalent)

    def test_lid_digits_are_never_matched_as_a_phone_number(self):
        assert not _participant_is_me("5511999999999@lid", "5511999999999", "other", _phone_digits_equivalent)

    def test_phone_digits_are_never_matched_as_a_lid(self):
        assert not _participant_is_me("5511999999999@c.us", "other", "5511999999999", _phone_digits_equivalent)

    def test_empty_participant_jid_never_matches(self):
        assert not _participant_is_me("", "5511999999999", "", _phone_digits_equivalent)
