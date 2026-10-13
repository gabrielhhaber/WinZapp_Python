"""Tests for where a chat's display identity comes from.

Two independent defects made conversations disappear from the list entirely.
Both were measured on a real 539-chat account that showed only 323 rows:

1. list-chats returns each individual chat with a nested `contact` block
   (name/shortName/pushname) and nothing read it, so every individual chat was
   stored and rendered nameless — 124 of 263 chats had a usable name in that
   block, 0 of 263 reached the database with one.

2. _compute_chat_lists() dropped a chat as "no content and no identity" using
   only the raw dict's name/pushName, ~30 lines *before* it called
   _resolve_contact_name(). WhatsApp Web returns `msgs: null` in list-chats, so
   lastMessage is empty for every chat right after a sync; any chat that also
   had no unread count was dropped before its name was ever looked up, even
   though all 263 had a matching contact record.

_lift_contact_identity covers (1) and is tested directly. (2) is a reordering
inside a large wx-bound method, so the decision it now makes is reproduced here
against the same inputs.
"""

import pytest

from main import MainWindow


lift = MainWindow._lift_contact_identity


class TestLiftContactIdentity:
    def test_takes_name_from_the_contact_block(self):
        chat = {"contact": {"name": "Tia Ana", "pushname": "Aninha"}}
        lift(chat)
        assert chat["name"] == "Tia Ana"
        assert chat["pushName"] == "Aninha"

    def test_falls_back_to_short_name(self):
        chat = {"contact": {"shortName": "Ana"}}
        lift(chat)
        assert chat["name"] == "Ana"

    def test_accepts_either_pushname_spelling(self):
        """WPPConnect spells it `pushname`; other payloads use `pushName`."""
        chat = {"contact": {"pushName": "Zé"}}
        lift(chat)
        assert chat["pushName"] == "Zé"

    def test_falls_back_to_the_verified_business_name(self):
        """A verified WhatsApp Business account (a bank, a payment app, a
        utility) sets no personal pushname: name, shortName and pushname all
        arrive empty and only contact.verifiedName carries the name
        WhatsApp's own client shows."""
        chat = {"contact": {"verifiedName": "99 Pay", "isBusiness": True}}
        lift(chat)
        assert chat["name"] == "99 Pay"
        assert "pushName" not in chat

    def test_name_and_short_name_still_win_over_the_verified_name(self):
        chat = {"contact": {"name": "Meu Apelido", "verifiedName": "99 Pay"}}
        lift(chat)
        assert chat["name"] == "Meu Apelido"
        chat = {"contact": {"shortName": "Apelido", "verifiedName": "99 Pay"}}
        lift(chat)
        assert chat["name"] == "Apelido"

    @pytest.mark.parametrize("blank", ["name", "shortName"])
    def test_a_blank_address_book_name_does_not_hide_the_verified_name(self, blank):
        chat = {"contact": {blank: "   ", "verifiedName": "99 Pay"}}
        lift(chat)
        assert chat["name"] == "99 Pay"

    @pytest.mark.parametrize("bad", [None, 42, "   ", {"x": 1}])
    def test_a_missing_or_malformed_verified_name_is_ignored(self, bad):
        chat = {"contact": {"verifiedName": bad}}
        lift(chat)
        assert "name" not in chat

    def test_never_overwrites_an_existing_top_level_name(self):
        chat = {"name": "Apelido meu", "contact": {"name": "Nome do contato"}}
        lift(chat)
        assert chat["name"] == "Apelido meu"

    def test_treats_a_blank_top_level_name_as_absent(self):
        chat = {"name": "   ", "contact": {"name": "Real"}}
        lift(chat)
        assert chat["name"] == "Real"

    def test_ignores_a_blank_contact_name(self):
        chat = {"contact": {"name": "  ", "shortName": ""}}
        lift(chat)
        assert "name" not in chat

    @pytest.mark.parametrize("chat", [
        {},                       # group chats carry no contact block
        {"contact": None},
        {"contact": "nope"},
        {"contact": []},
    ])
    def test_survives_a_missing_or_malformed_contact_block(self, chat):
        before = dict(chat)
        lift(chat)
        assert chat == before


def _keeps_chat(*, records, last_msg, unread, pinned, cleared, raw_name,
                raw_push, resolved_name, group_name=""):
    """The keep/drop decision _compute_chat_lists() makes, same inputs."""
    has_content = bool(records or last_msg or unread > 0 or pinned or cleared)
    name_hint = (raw_name or raw_push or resolved_name or group_name).strip()
    has_identity = bool(name_hint and not name_hint.isdigit() and len(name_hint) > 1)
    return has_content or has_identity


class TestKeepDecision:
    def _base(self, **over):
        args = dict(records=[], last_msg=None, unread=0, pinned=False,
                    cleared=False, raw_name="", raw_push="",
                    resolved_name="", group_name="")
        args.update(over)
        return args

    def test_the_regression_chat_is_kept(self):
        """No messages, no lastMessage, no unread, nameless dict — but the
        contact is known. This is the shape of all 218 vanished chats."""
        assert _keeps_chat(**self._base(resolved_name="Fulano de Tal")) is True

    def test_still_dropped_when_nothing_at_all_is_known(self):
        """The filter must keep dropping genuinely empty, anonymous entries —
        that is what it exists for."""
        assert _keeps_chat(**self._base()) is False

    def test_a_digits_only_hint_is_not_an_identity(self):
        assert _keeps_chat(**self._base(resolved_name="5511999999999")) is False

    def test_a_single_character_hint_is_not_an_identity(self):
        assert _keeps_chat(**self._base(resolved_name="A")) is False

    @pytest.mark.parametrize("field", ["records", "last_msg", "unread", "pinned", "cleared"])
    def test_content_alone_keeps_a_nameless_chat(self, field):
        value = {"records": [{"id": 1}], "last_msg": {"t": 1}, "unread": 3,
                 "pinned": True, "cleared": True}[field]
        assert _keeps_chat(**self._base(**{field: value})) is True

    def test_raw_name_still_wins_when_present(self):
        assert _keeps_chat(**self._base(raw_name="Grupo X")) is True

    def test_group_name_still_counts(self):
        assert _keeps_chat(**self._base(group_name="Equipe WinZapp")) is True


class _ResolverStub:
    """Just what the real _resolve_contact_name() reads, for a chat whose
    number is not in the address book."""

    _resolve_contact_name = MainWindow._resolve_contact_name
    _is_bad_contact_name = staticmethod(MainWindow._is_bad_contact_name)

    def __init__(self, contacts=None):
        self.contacts = contacts or {}
        self._presence_pushname_map = {}
        self._phone_to_lid = {}
        self._lid_to_phone = {}

    def _get_contact_tolerant(self, jid):
        return self.contacts.get(jid)

    def _find_alt_jid_from_messages(self, chat):
        return ""


class TestNotInTheAddressBook:
    """The list shows a name instead of the phone number for a number that
    was never saved: the verified name of a business, or the name a person
    chose for their own WhatsApp profile. These pin the lift and the real
    resolver together, so either regressing turns those rows back into
    numbers."""

    JID = "552123919910@s.whatsapp.net"

    def test_a_verified_business_is_named_by_the_resolver(self):
        chat = {"remoteJid": self.JID, "contact": {"verifiedName": "99 Pay"}}
        lift(chat)
        assert _ResolverStub()._resolve_contact_name(chat) == "99 Pay"

    def test_an_address_book_name_still_wins_over_the_verified_name(self):
        chat = {"remoteJid": self.JID, "contact": {"verifiedName": "99 Pay"}}
        lift(chat)
        stub = _ResolverStub({self.JID: {"name": "Carteira digital"}})
        assert stub._resolve_contact_name(chat) == "Carteira digital"

    def test_a_person_s_profile_name_is_lifted_for_the_list(self):
        """Nothing in the address book; the chat list then uses
        chat["pushName"], which the lift fills from the contact's own
        WhatsApp profile name."""
        chat = {"remoteJid": "5511999999999@s.whatsapp.net",
                "contact": {"pushname": "Maria da Padaria"}}
        lift(chat)
        assert chat["pushName"] == "Maria da Padaria"
