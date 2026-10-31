import message_rules as rules

NOW = 1_700_000_000


def test_edit_refused_when_the_message_is_not_mine():
    assert rules.refusal_reason(False, {"t": NOW}, NOW) == "not_mine"


def test_edit_refused_when_the_bridge_did_not_store_the_message():
    assert rules.refusal_reason(True, None, NOW) == "not_stored"


def test_edit_refused_outside_the_15_minute_window():
    assert rules.refusal_reason(True, {"t": NOW - 16 * 60}, NOW) == "outside_window"
    assert rules.refusal_reason(True, {"t": NOW - 14 * 60}, NOW) == ""


def test_edit_without_a_stored_time_is_not_refused_by_the_window():
    assert rules.refusal_reason(True, {}, NOW) == ""


def test_delete_for_everyone_my_message_is_signed_by_me():
    assert rules.revocation_author(True, False, "me@s.whatsapp.net", "", {}) == "me@s.whatsapp.net"


def test_delete_for_everyone_someone_elses_message_only_in_a_group():
    assert rules.revocation_author(False, False, "me@s.whatsapp.net", "", {}) == ""
    assert rules.revocation_author(
        False, True, "me@s.whatsapp.net", "5551@s.whatsapp.net", {}) == "5551@s.whatsapp.net"


def test_delete_for_everyone_in_a_group_uses_the_stored_author_if_the_id_does_not_carry_it():
    stored = {"author": "5551@c.us"}
    assert rules.revocation_author(
        False, True, "me@s.whatsapp.net", "", stored) == "5551@s.whatsapp.net"
    assert rules.revocation_author(False, True, "me@s.whatsapp.net", "", {}) == ""


def test_channel_posts_are_not_conversations():
    assert rules.is_channel_chat("120363123456789@newsletter")
    assert not rules.is_channel_chat("120363123456789@g.us")
    assert not rules.is_channel_chat("5551@s.whatsapp.net")
    assert not rules.is_channel_chat(None)
