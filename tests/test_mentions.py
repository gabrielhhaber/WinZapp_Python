"""Tests for @mention handling in the conversation view.

Two reported bugs, both rooted in the same place — the two JID forms WhatsApp
uses for the same person:

1. A message mentioning everyone showed one hyperlink per participant (dozens
   of them) *only when WinZapp itself had sent it*. _extract_mentions() collapses
   an @todos into no links at all by comparing the mentioned set against the
   group's participants, but it compared them with _normalize_jid(), which
   rewrites @c.us → @s.whatsapp.net and deliberately leaves @lid alone. The
   participants cache holds @lid on LID-addressed accounts, while our own sent
   message carries the phone form (_canonical_mention_jids() converts it for the
   send endpoint) — so the two sets never intersected and the guard never fired.
   The same message received from someone else, whose mentions arrive still
   keyed by @lid, was collapsed correctly. _mention_identity() bridges both.

2. Editing a message to add an @mention produced no hyperlinks: the edit path
   posted the raw "@DisplayName" text (WhatsApp only highlights "@<phone>") and
   rewrote the local record as a plain `conversation`, dropping contextInfo
   entirely. _build_mention_payload() is now shared by the send and edit paths.

ConversationsPanel is a wx.Panel and cannot be instantiated without a running
wx.App, so the methods under test are exercised as plain functions against a
small stub — same approach as tests/test_message_bookmarks.py.
"""

import pytest

from ui.conversations import ConversationsPanel


class _FakeMainWindow:
    def __init__(self, lid_to_phone=None):
        self._lid_to_phone = lid_to_phone or {}

    @staticmethod
    def _normalize_jid(jid):
        if not jid:
            return jid
        if ":" in jid and "@" in jid:
            local, _, domain = jid.partition("@")
            jid = f"{local.split(':', 1)[0]}@{domain}"
        if jid.endswith("@c.us"):
            return jid[:-5] + "@s.whatsapp.net"
        return jid

    def _canonical_mention_jids(self, mentioned_jids):
        out, seen = [], set()
        for raw in mentioned_jids or []:
            jid = self._normalize_jid(str(raw or ""))
            if not jid:
                continue
            if jid.endswith("@lid"):
                jid = self._lid_to_phone.get(jid, jid)
            if jid not in seen:
                seen.add(jid)
                out.append(jid)
        return out


class _Stub:
    _mention_identity = ConversationsPanel._mention_identity
    _raw_mentioned_jids = staticmethod(ConversationsPanel._raw_mentioned_jids)
    _extract_mentions = ConversationsPanel._extract_mentions
    _mention_text = staticmethod(ConversationsPanel._mention_text)
    _individually_named_mentions = ConversationsPanel._individually_named_mentions
    _build_mention_payload = ConversationsPanel._build_mention_payload

    def __init__(self, main_window, participants=None):
        self.main_window = main_window
        self._group_participants_cache = participants or []
        self._pending_mentions = []
        self._pending_mention_display_names = {}

    def _get_participant_name(self, jid):
        return f"name-of-{jid}"


# Three participants, addressed by @lid — the shape a LID-addressed group's
# metadata actually has.
PARTICIPANTS = [
    ("Ana", "111@lid"),
    ("Bruno", "222@lid"),
    ("Carla", "333@lid"),
]
LID_TO_PHONE = {
    "111@lid": "5511111111111@s.whatsapp.net",
    "222@lid": "5522222222222@s.whatsapp.net",
    "333@lid": "5533333333333@s.whatsapp.net",
}


def _text_msg(mentioned, text="@todos bom dia"):
    return {
        "key": {"id": "X", "fromMe": True},
        "messageType": "extendedTextMessage",
        "message": {"extendedTextMessage": {"text": text}},
        "contextInfo": {"mentionedJid": list(mentioned)},
    }


class TestMentionIdentity:
    def test_lid_and_phone_resolve_to_the_same_person(self):
        s = _Stub(_FakeMainWindow(LID_TO_PHONE))
        assert s._mention_identity("111@lid") == s._mention_identity(
            "5511111111111@s.whatsapp.net"
        )

    def test_legacy_c_us_resolves_to_the_same_person(self):
        s = _Stub(_FakeMainWindow(LID_TO_PHONE))
        assert s._mention_identity("5511111111111@c.us") == s._mention_identity(
            "5511111111111@s.whatsapp.net"
        )

    def test_a_device_suffix_does_not_split_a_person_in_two(self):
        s = _Stub(_FakeMainWindow(LID_TO_PHONE))
        assert s._mention_identity("5511111111111:6@s.whatsapp.net") == s._mention_identity(
            "5511111111111@s.whatsapp.net"
        )

    def test_an_unmapped_lid_still_collides_with_itself(self):
        s = _Stub(_FakeMainWindow({}))
        assert s._mention_identity("999@lid") == s._mention_identity("999@lid")
        assert s._mention_identity("999@lid") != s._mention_identity("888@lid")

    def test_empty_jid(self):
        s = _Stub(_FakeMainWindow({}))
        assert s._mention_identity("") == ""


class TestExtractMentionsCollapsesMentionAll:
    def test_incoming_mention_all_keyed_by_lid_shows_no_links(self):
        """This case already worked — kept so a future change can't break it."""
        s = _Stub(_FakeMainWindow(LID_TO_PHONE), PARTICIPANTS)
        assert s._extract_mentions(_text_msg(LID_TO_PHONE.keys())) == []

    def test_our_own_mention_all_keyed_by_phone_also_shows_no_links(self):
        """The reported bug: identical message, sent by WinZapp, listed every
        participant as its own hyperlink."""
        s = _Stub(_FakeMainWindow(LID_TO_PHONE), PARTICIPANTS)
        assert s._extract_mentions(_text_msg(LID_TO_PHONE.values())) == []

    def test_the_sender_may_be_missing_from_their_own_mention_list(self):
        s = _Stub(_FakeMainWindow(LID_TO_PHONE), PARTICIPANTS)
        partial = list(LID_TO_PHONE.values())[:-1]
        assert s._extract_mentions(_text_msg(partial)) == []

    def test_a_single_individual_mention_still_gets_its_link(self):
        s = _Stub(_FakeMainWindow(LID_TO_PHONE), PARTICIPANTS)
        out = s._extract_mentions(_text_msg(["5511111111111@s.whatsapp.net"]))
        assert [jid for _, jid in out] == ["5511111111111@s.whatsapp.net"]

    def test_no_mentions_means_no_links(self):
        s = _Stub(_FakeMainWindow(LID_TO_PHONE), PARTICIPANTS)
        assert s._extract_mentions({"key": {"id": "X"}, "message": {}}) == []

    def test_a_tiny_group_is_not_treated_as_mention_all(self):
        """With 2 participants "everyone" and "one person" are indistinguishable,
        so the collapse must not kick in."""
        s = _Stub(_FakeMainWindow(LID_TO_PHONE), PARTICIPANTS[:2])
        out = s._extract_mentions(_text_msg(["5511111111111@s.whatsapp.net"]))
        assert len(out) == 1


class TestMentionAllNextToAnIndividualMention:
    """"@todos ... @Ana ...": the list covers the whole group either way, so
    only the text says Ana was also named. She keeps her link; "everyone" does
    not get one per participant."""

    def test_the_person_named_in_the_text_keeps_a_link(self):
        s = _Stub(_FakeMainWindow(LID_TO_PHONE), PARTICIPANTS)
        msg = _text_msg(LID_TO_PHONE.values(), "@todos, o @5522222222222 já sabe")
        assert [jid for _, jid in s._extract_mentions(msg)] == ["5522222222222@s.whatsapp.net"]

    def test_several_people_named_keep_their_links_in_list_order(self):
        s = _Stub(_FakeMainWindow(LID_TO_PHONE), PARTICIPANTS)
        msg = _text_msg(LID_TO_PHONE.values(), "@5533333333333 e @5511111111111 e @todos")
        assert [jid for _, jid in s._extract_mentions(msg)] == [
            "5511111111111@s.whatsapp.net", "5533333333333@s.whatsapp.net"]

    def test_a_person_received_as_lid_is_matched_by_phone_digits(self):
        s = _Stub(_FakeMainWindow(LID_TO_PHONE), PARTICIPANTS)
        msg = _text_msg(LID_TO_PHONE.keys(), "@todos @5511111111111")
        assert [jid for _, jid in s._extract_mentions(msg)] == ["111@lid"]

    def test_only_at_todos_still_shows_no_links(self):
        s = _Stub(_FakeMainWindow(LID_TO_PHONE), PARTICIPANTS)
        assert s._extract_mentions(_text_msg(LID_TO_PHONE.values(), "@todos bom dia")) == []

    def test_a_number_that_is_not_a_participant_is_ignored(self):
        s = _Stub(_FakeMainWindow(LID_TO_PHONE), PARTICIPANTS)
        msg = _text_msg(LID_TO_PHONE.values(), "@todos ligue para @5599999999999")
        assert s._extract_mentions(msg) == []

    def test_a_media_caption_counts_as_the_text(self):
        s = _Stub(_FakeMainWindow(LID_TO_PHONE), PARTICIPANTS)
        msg = {"key": {"id": "X"}, "contextInfo": {"mentionedJid": list(LID_TO_PHONE.values())},
               "message": {"imageMessage": {"caption": "@todos vejam @5511111111111"}}}
        assert [jid for _, jid in s._extract_mentions(msg)] == ["5511111111111@s.whatsapp.net"]


class TestRawMentionedJids:
    def test_top_level_context_info(self):
        msg = {"contextInfo": {"mentionedJid": ["a@lid"]}}
        assert _Stub._raw_mentioned_jids(msg) == ["a@lid"]

    def test_inside_extended_text_message(self):
        msg = {"message": {"extendedTextMessage": {"contextInfo": {"mentionedJid": ["b@lid"]}}}}
        assert _Stub._raw_mentioned_jids(msg) == ["b@lid"]

    def test_missing_everywhere(self):
        assert _Stub._raw_mentioned_jids({"message": {"conversation": "oi"}}) == []


class TestBuildMentionPayload:
    def test_no_pending_mentions_leaves_the_text_alone(self):
        s = _Stub(_FakeMainWindow(LID_TO_PHONE))
        assert s._build_mention_payload("bom dia") == ("bom dia", None)

    def test_display_names_become_phone_numbers(self):
        """WhatsApp only highlights a mention when the body carries @<phone>."""
        s = _Stub(_FakeMainWindow(LID_TO_PHONE))
        s._pending_mentions = ["111@lid"]
        s._pending_mention_display_names = {"111@lid": "Ana"}
        api_text, mentioned = s._build_mention_payload("@Ana bom dia")
        assert api_text == "@5511111111111 bom dia"
        assert mentioned == ["5511111111111@s.whatsapp.net"]

    def test_each_display_name_is_replaced_once_per_mention(self):
        s = _Stub(_FakeMainWindow(LID_TO_PHONE))
        s._pending_mentions = ["111@lid", "222@lid"]
        s._pending_mention_display_names = {"111@lid": "Ana", "222@lid": "Bruno"}
        api_text, mentioned = s._build_mention_payload("@Ana e @Bruno, bom dia")
        assert api_text == "@5511111111111 e @5522222222222, bom dia"
        assert mentioned == [
            "5511111111111@s.whatsapp.net",
            "5522222222222@s.whatsapp.net",
        ]

    def test_a_mention_without_a_known_display_name_keeps_the_text(self):
        s = _Stub(_FakeMainWindow(LID_TO_PHONE))
        s._pending_mentions = ["111@lid"]
        api_text, mentioned = s._build_mention_payload("bom dia")
        assert api_text == "bom dia"
        assert mentioned == ["5511111111111@s.whatsapp.net"]

    def test_the_edit_path_and_the_send_path_agree(self):
        """Both call this — an edit that adds a mention must produce exactly the
        same body and JID list a fresh send would."""
        send = _Stub(_FakeMainWindow(LID_TO_PHONE))
        send._pending_mentions = ["333@lid"]
        send._pending_mention_display_names = {"333@lid": "Carla"}

        edit = _Stub(_FakeMainWindow(LID_TO_PHONE))
        edit._pending_mentions = ["333@lid"]
        edit._pending_mention_display_names = {"333@lid": "Carla"}

        assert send._build_mention_payload("oi @Carla") == edit._build_mention_payload("oi @Carla")
