"""Tests for MainWindow._resolve_jid_name — the participant-name resolver
behind the typing/recording indicator (chat-list label and the spoken
"<name> está digitando..." announcement).

Reported live (bug 1): after deleting a session and re-pairing, every
group's typing indicator showed the GROUP's own name instead of the
participant who was actually typing (e.g. "WinZapp | desenvolvedores e
testers está digitando..."). Root cause: some presence.update payloads key
the group-wide event by the group's own @g.us id rather than a specific
participant id. _resolve_jid_name(group_jid) used to fall through to
self.chats.get(group_jid) — which legitimately holds the group's own chat
entry — and returned the group's name as if it were a person's. A
participant JID is never a group JID, so that candidate must be skipped
rather than resolved via self.chats.

Reported live (bug 2): "participante sem nome está digitando" showing up
often for a group member whose name then rendered correctly the moment
their actual message arrived — because the message goes through
ui.conversations.ConversationsPanel._get_participant_name(), a much richer
resolver (checks the group's own message history, the open conversation's
participant cache, kicks a background @lid resolution) that
_resolve_jid_name() never consulted at all before giving up on an unmapped
@lid. It now takes the chat's jid and tries those same sources first.
"""

import threading

from main import MainWindow


class _I18n:
    TRANSLATIONS = {
        "unnamed_participant": "unnamed participant",
    }

    def t(self, key):
        return self.TRANSLATIONS.get(key, key)


class _Stub:
    _resolve_jid_name = MainWindow._resolve_jid_name
    _get_contact_tolerant = MainWindow._get_contact_tolerant
    _normalize_jid = staticmethod(MainWindow._normalize_jid)

    def __init__(self):
        self.i18n = _I18n()
        self.contacts = {}
        self.chats = {}
        self._phone_to_lid = {}
        self._lid_to_phone = {}
        self._presence_pushname_map = {}
        self.conversations_panel = None
        self.resolve_calls = []

    def resolve_lid_jids_via_api(self, jids):
        self.resolve_calls.append(list(jids))


def _chat_with_records(*records):
    return {"messages": {"messages": {"records": list(records)}}}


class TestGroupJidNeverResolvesToTheGroupsOwnName:
    def test_a_presence_event_keyed_by_the_group_itself_does_not_return_the_group_name(self):
        stub = _Stub()
        group_jid = "12345-6789@g.us"
        stub.chats[group_jid] = {"remoteJid": group_jid, "name": "WinZapp | desenvolvedores e testers"}

        name = stub._resolve_jid_name(group_jid)

        assert name != "WinZapp | desenvolvedores e testers"
        assert name == "unnamed participant"

    def test_a_real_participant_in_the_same_group_still_resolves_normally(self):
        stub = _Stub()
        group_jid = "12345-6789@g.us"
        participant_jid = "5511999999999@s.whatsapp.net"
        stub.chats[group_jid] = {"remoteJid": group_jid, "name": "WinZapp | desenvolvedores e testers"}
        stub.contacts[participant_jid] = {"name": "Fulano"}

        name = stub._resolve_jid_name(participant_jid)

        assert name == "Fulano"

    def test_an_at_lid_participant_still_falls_back_to_the_placeholder(self):
        """Same reasoning, different pre-existing case: no @lid->phone
        mapping and nothing else to try, so raw digits would be meaningless
        read aloud."""
        stub = _Stub()

        name = stub._resolve_jid_name("987654321@lid")

        assert name == "unnamed participant"


class TestUnmappedLidChecksTheGroupsOwnMessagesFirst:
    """The second reported bug: an @lid with no _lid_to_phone entry yet used
    to go straight to the placeholder. It now checks that group's own
    already-loaded message records for an earlier message from the same
    participant — the exact source _get_participant_name() (used once the
    message itself renders) would find — before giving up."""

    def test_resolves_from_an_earlier_message_by_the_same_participant(self):
        stub = _Stub()
        group_jid = "12345-6789@g.us"
        lid = "987654321@lid"
        stub.chats[group_jid] = _chat_with_records(
            {"key": {"participant": lid}, "pushName": "Ciclano"}
        )

        name = stub._resolve_jid_name(lid, group_jid)

        assert name == "Ciclano"

    def test_ignores_records_from_a_different_participant(self):
        stub = _Stub()
        group_jid = "12345-6789@g.us"
        lid = "987654321@lid"
        stub.chats[group_jid] = _chat_with_records(
            {"key": {"participant": "111111111@lid"}, "pushName": "Outra Pessoa"}
        )

        name = stub._resolve_jid_name(lid, group_jid)

        assert name == "unnamed participant"

    def test_a_phone_like_pushname_is_not_accepted_as_a_real_name(self):
        stub = _Stub()
        group_jid = "12345-6789@g.us"
        lid = "987654321@lid"
        stub.chats[group_jid] = _chat_with_records(
            {"key": {"participant": lid}, "pushName": "5511999999999"}
        )

        name = stub._resolve_jid_name(lid, group_jid)

        assert name == "unnamed participant"

    def test_checks_the_open_conversations_participant_cache_too(self):
        stub = _Stub()
        group_jid = "12345-6789@g.us"
        lid = "987654321@lid"
        stub.conversations_panel = type("P", (), {
            "conversation": {"remoteJid": group_jid},
            "_group_participants_cache": [("Beltrano", lid)],
        })()

        name = stub._resolve_jid_name(lid, group_jid)

        assert name == "Beltrano"

    def test_the_participant_cache_of_a_different_open_conversation_is_not_used(self):
        """The cache belongs to whichever conversation is currently open —
        must not leak into a presence event for a different group."""
        stub = _Stub()
        group_jid = "12345-6789@g.us"
        other_open_group = "99999-0000@g.us"
        lid = "987654321@lid"
        stub.conversations_panel = type("P", (), {
            "conversation": {"remoteJid": other_open_group},
            "_group_participants_cache": [("Beltrano", lid)],
        })()

        name = stub._resolve_jid_name(lid, group_jid)

        assert name == "unnamed participant"

    def test_still_placeholders_when_nothing_at_all_is_known_but_kicks_a_background_resolution(self, monkeypatch):
        """A real Thread is started in production; make it run inline here
        so the call is observable without racing a background thread."""
        class _Inline:
            def __init__(self, target=None, args=(), kwargs=None, daemon=None, **kw):
                self._target, self._args = target, args

            def start(self):
                self._target(*self._args)

        monkeypatch.setattr(threading, "Thread", _Inline)
        stub = _Stub()
        group_jid = "12345-6789@g.us"
        lid = "987654321@lid"
        stub.chats[group_jid] = _chat_with_records()

        name = stub._resolve_jid_name(lid, group_jid)

        assert name == "unnamed participant"
        assert stub.resolve_calls == [[lid]]

    def test_no_chat_jid_given_skips_the_message_lookup_but_still_kicks_resolution(self, monkeypatch):
        """Some callers (chat-list label) may not have a chat_jid_norm to
        hand — must not crash, just skip straight to the last resort."""
        class _Inline:
            def __init__(self, target=None, args=(), kwargs=None, daemon=None, **kw):
                self._target, self._args = target, args

            def start(self):
                self._target(*self._args)

        monkeypatch.setattr(threading, "Thread", _Inline)
        stub = _Stub()
        lid = "987654321@lid"

        name = stub._resolve_jid_name(lid)

        assert name == "unnamed participant"
        assert stub.resolve_calls == [[lid]]


class TestMappedLidStillPrefersTheGroupMessagePushName:
    """A participant who is not a saved contact has a pushName on the group's
    messages but no contact name. The typing announcement used to return the
    bare phone as soon as _lid_to_phone knew the @lid, while the message
    itself showed the name."""

    def test_pushname_from_group_messages_beats_the_formatted_phone(self):
        stub = _Stub()
        group_jid = "12345-6789@g.us"
        lid = "987654321@lid"
        stub._lid_to_phone[lid] = "5511988887777@s.whatsapp.net"
        stub.chats[group_jid] = _chat_with_records(
            {"key": {"participant": lid}, "pushName": "Ciclano"}
        )

        assert stub._resolve_jid_name(lid, group_jid) == "Ciclano"

    def test_message_keyed_by_the_phone_jid_also_counts(self):
        stub = _Stub()
        group_jid = "12345-6789@g.us"
        lid = "987654321@lid"
        phone = "5511988887777@s.whatsapp.net"
        stub._lid_to_phone[lid] = phone
        stub.chats[group_jid] = _chat_with_records(
            {"key": {"participant": phone}, "pushName": "Ciclano"}
        )

        assert stub._resolve_jid_name(lid, group_jid) == "Ciclano"

    def test_mapped_lid_without_any_name_still_shows_the_number(self):
        stub = _Stub()
        lid = "987654321@lid"
        stub._lid_to_phone[lid] = "5511988887777@s.whatsapp.net"

        name = stub._resolve_jid_name(lid, "12345-6789@g.us")

        assert "88887777" in name.replace(" ", "").replace("-", "")
        assert stub.resolve_calls == []
