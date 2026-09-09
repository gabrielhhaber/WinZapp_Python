"""Tests for backfilling reactions on messages already synced locally.

Reported live: reactions on old messages never arrive after reconnecting or
opening WinZapp later — only ever a *live* reaction, from someone who reacts
while WinZapp is actually connected, ever shows up at all. Root cause: a
reactionMessage only ever arrives as a live WebSocket event
(WebSocketClient.on_wpp_reaction()/on_messages_upsert() ->
apply_incoming_reaction()); a normal sync round re-fetches WhatsApp Web's
own message *history* via get-messages, which does not replay reactions on
messages it already has. So a reaction added while WinZapp was disconnected
is invisible forever unless something asks for it explicitly, after the
fact — which nothing did.

wppconnect-server already exposes GET /api/:session/reactions/:id
(DeviceController.getReactions(), unmodified upstream — see
client/api_patches/src/routes/index.ts), returning a live snapshot of every
current reaction on one message. WinZapp's Python side never called it.
MainWindow.fetch_message_reactions() (see tests/test_fetch_message_reactions.py)
is the network boundary; this file covers what ConversationsPanel does with
the result — bounded to the messages currently loaded for the open
conversation, since there is no cheap way to know in advance which ones
have anything to find, and asking for an entire history would be thousands
of requests for a handful of hits.

ConversationsPanel is a wx.Panel and cannot be instantiated without a
running wx.App, so its methods are exercised unbound against a small stub —
same pattern as tests/test_reaction_persisted_from_others.py.
"""

import threading

from main import MainWindow
from ui.conversations import ConversationsPanel


class _FakeDB:
    def __init__(self):
        self.inserted = []

    def insert_message(self, jid, record):
        self.inserted.append((jid, record))


class _FakeMainWindow:
    def __init__(self, chat, reactions_by_msg_id=None, lid_to_phone=None):
        self._chat = chat
        self.db = _FakeDB()
        self._reactions_by_msg_id = reactions_by_msg_id or {}
        self.fetch_calls = []
        # The real thing, not a fake: the whole point of the canonicalization
        # under test is that it agrees with what the rest of the app does to
        # a JID, so substituting a simplified version here would test nothing.
        self._normalize_jid = MainWindow._normalize_jid
        self._lid_to_phone = dict(lid_to_phone or {})
        self.my_jid = ME
        self.my_lid = ""
        # The real fetch_message_reactions() refuses to send anything while
        # this is False, so the backfill refuses to spend its cooldown on a
        # pass that could not fetch — see TestItDoesNotSpendItsCooldownOffline.
        self._wa_connected = True

    def get_chat(self, jid):
        return self._chat

    def fetch_message_reactions(self, msg_id):
        self.fetch_calls.append(msg_id)
        return self._reactions_by_msg_id.get(msg_id)


class _Stub:
    _backfill_reactions_for_open_conversation = ConversationsPanel._backfill_reactions_for_open_conversation
    _do_backfill_reactions      = ConversationsPanel._do_backfill_reactions
    _apply_backfilled_reactions = ConversationsPanel._apply_backfilled_reactions
    _merge_fetched_reactions    = ConversationsPanel._merge_fetched_reactions
    _persist_reaction_record    = ConversationsPanel._persist_reaction_record
    _reactor_key_from_msg       = ConversationsPanel._reactor_key_from_msg
    _reactor_key_from_api       = ConversationsPanel._reactor_key_from_api
    _canonical_reactor_key      = ConversationsPanel._canonical_reactor_key
    _is_self_reactor            = ConversationsPanel._is_self_reactor
    _reaction_backfill_is_due   = ConversationsPanel._reaction_backfill_is_due
    _chat_records_for           = ConversationsPanel._chat_records_for
    _extract_timestamp          = ConversationsPanel._extract_timestamp
    _SELF_REACTOR_KEY           = ConversationsPanel._SELF_REACTOR_KEY
    _REACTION_BACKFILL_LIMIT    = ConversationsPanel._REACTION_BACKFILL_LIMIT
    _REACTION_BACKFILL_COOLDOWN_SECONDS = (
        ConversationsPanel._REACTION_BACKFILL_COOLDOWN_SECONDS
    )

    def __init__(self, jid, records=None, reactions_by_msg_id=None,
                 lid_to_phone=None):
        chat = {"messages": {"messages": {"records": list(records or [])}}}
        self.main_window = _FakeMainWindow(chat, reactions_by_msg_id, lid_to_phone)
        self.conversation = {"remoteJid": jid, "messages": chat["messages"]}
        self._reaction_backfill_generation = 0
        self._reaction_backfill_last = {}
        self.populate_calls = 0

    def populate_messages(self, preserve_focus=False):
        self.populate_calls += 1


JID = "5511999999999@s.whatsapp.net"
#: This account. _is_self_reactor() compares against main_window.my_jid.
ME = "5511000000000@s.whatsapp.net"


def _msg(mid, ts):
    return {
        "key": {"id": mid, "fromMe": False},
        "messageType": "conversation",
        "message": {"conversation": "oi"},
        "messageTimestamp": ts,
    }


def _reactions_payload(senders, me_jid=None):
    """senders: list of (jid, emoji). me_jid, if given, marks that sender as
    reactionByMe — mirrors the real /reactions/{id} response shape
    (retriever.layer.d.ts)."""
    by_emoji = {}
    for jid, emoji in senders:
        by_emoji.setdefault(emoji, []).append(
            {"senderUserJid": jid, "reactionText": emoji}
        )
    payload = {
        "reactions": [
            {"aggregateEmoji": emoji, "hasReactionByMe": False, "senders": s}
            for emoji, s in by_emoji.items()
        ],
    }
    if me_jid:
        payload["reactionByMe"] = {"senderUserJid": me_jid}
    return payload


class TestCandidateSelection:
    def test_picks_the_most_recent_messages_first(self, monkeypatch):
        """threading.Thread.start() replaced with a synchronous call so the
        background step runs inline and its ordering can be asserted."""
        monkeypatch.setattr(
            threading.Thread, "start",
            lambda self: self._target(*self._args, **self._kwargs),
        )
        records = [_msg("old", 100), _msg("new", 300), _msg("mid", 200)]
        stub = _Stub(JID, records=records)

        stub._backfill_reactions_for_open_conversation()

        assert stub.main_window.fetch_calls == ["new", "mid", "old"]

    def test_reaction_records_are_never_asked_about_themselves(self, monkeypatch):
        monkeypatch.setattr(
            threading.Thread, "start",
            lambda self: self._target(*self._args, **self._kwargs),
        )
        rxn = {
            "key": {"id": "_rxn_old_a", "fromMe": False},
            "messageType": "reactionMessage",
            "message": {"reactionMessage": {"key": {"id": "old"}, "text": "👍"}},
            "messageTimestamp": 400,
        }
        stub = _Stub(JID, records=[_msg("old", 100), rxn])

        stub._backfill_reactions_for_open_conversation()

        assert stub.main_window.fetch_calls == ["old"]

    def test_bounded_to_the_configured_limit(self, monkeypatch):
        monkeypatch.setattr(
            threading.Thread, "start",
            lambda self: self._target(*self._args, **self._kwargs),
        )
        records = [_msg(f"m{i}", i) for i in range(60)]
        stub = _Stub(JID, records=records)

        stub._backfill_reactions_for_open_conversation()

        assert len(stub.main_window.fetch_calls) == ConversationsPanel._REACTION_BACKFILL_LIMIT

    def test_no_candidates_starts_no_thread(self, monkeypatch):
        started = []
        monkeypatch.setattr(threading.Thread, "start", lambda self: started.append(1))
        stub = _Stub(JID, records=[])

        stub._backfill_reactions_for_open_conversation()

        assert started == []


class TestMergeFetchedReactions:
    def test_a_reaction_nobody_knew_about_is_persisted(self):
        stub = _Stub(JID, records=[_msg("m1", 100)])

        changed = stub._merge_fetched_reactions(
            JID, "m1", _reactions_payload([("a@s.whatsapp.net", "👍")]),
        )

        assert changed is True
        records = stub._chat_records_for(JID)
        rxn = [r for r in records if r.get("messageType") == "reactionMessage"]
        assert len(rxn) == 1
        assert rxn[0]["message"]["reactionMessage"]["text"] == "👍"
        assert rxn[0]["key"]["participant"] == "a@s.whatsapp.net"

    def test_own_reaction_is_recognised_from_our_own_jid(self):
        """Not from the response's reactionByMe: that field is absent exactly
        when we hold no reaction on the message yet, which is the state a
        reaction made on the phone while offline starts from — the one this
        backfill exists to find."""
        stub = _Stub(JID, records=[_msg("m1", 100)])

        stub._merge_fetched_reactions(
            JID, "m1", _reactions_payload([(ME, "❤️")]),
        )

        records = stub._chat_records_for(JID)
        rxn = [r for r in records if r.get("messageType") == "reactionMessage"][0]
        assert rxn["key"]["fromMe"] is True
        assert rxn["key"]["id"] == "_rxn_m1"  # the SELF namespacing _persist_reaction_record() uses

    def test_own_reaction_is_recognised_through_our_own_lid(self):
        stub = _Stub(JID, records=[_msg("m1", 100)])
        stub.main_window.my_lid = "77777@lid"

        stub._merge_fetched_reactions(
            JID, "m1", _reactions_payload([("77777@lid", "❤️")]),
        )

        rxn = [r for r in stub._chat_records_for(JID)
               if r.get("messageType") == "reactionMessage"][0]
        assert rxn["key"]["fromMe"] is True

    def test_a_reaction_already_known_with_the_same_emoji_reports_no_change(self):
        stub = _Stub(JID, records=[_msg("m1", 100)])
        stub._merge_fetched_reactions(
            JID, "m1", _reactions_payload([("a@s.whatsapp.net", "👍")]),
        )
        before = len(stub._chat_records_for(JID))

        changed = stub._merge_fetched_reactions(
            JID, "m1", _reactions_payload([("a@s.whatsapp.net", "👍")]),
        )

        assert changed is False
        assert len(stub._chat_records_for(JID)) == before

    def test_a_changed_emoji_from_the_same_sender_updates_in_place(self):
        stub = _Stub(JID, records=[_msg("m1", 100)])
        stub._merge_fetched_reactions(
            JID, "m1", _reactions_payload([("a@s.whatsapp.net", "👍")]),
        )

        changed = stub._merge_fetched_reactions(
            JID, "m1", _reactions_payload([("a@s.whatsapp.net", "😂")]),
        )

        assert changed is True
        records = stub._chat_records_for(JID)
        rxn = [r for r in records if r.get("messageType") == "reactionMessage"]
        assert len(rxn) == 1  # updated, not duplicated
        assert rxn[0]["message"]["reactionMessage"]["text"] == "😂"

    def test_a_sender_missing_from_a_populated_response_is_treated_as_removed(self):
        """They reacted, then removed it while WinZapp could not see either
        event. Somebody else is still listed, which is what proves the
        response was actually read rather than merely empty."""
        stub = _Stub(JID, records=[_msg("m1", 100)])
        stub._merge_fetched_reactions(
            JID, "m1", _reactions_payload([
                ("a@s.whatsapp.net", "👍"), ("b@s.whatsapp.net", "😂"),
            ]),
        )

        changed = stub._merge_fetched_reactions(
            JID, "m1", _reactions_payload([("b@s.whatsapp.net", "😂")]),
        )

        assert changed is True
        rxn = {r["key"].get("participant"): r["message"]["reactionMessage"]["text"]
               for r in stub._chat_records_for(JID)
               if r.get("messageType") == "reactionMessage"}
        assert rxn["a@s.whatsapp.net"] == ""
        assert rxn["b@s.whatsapp.net"] == "😂"

    def test_an_already_removed_sender_is_not_reprocessed(self):
        stub = _Stub(JID, records=[_msg("m1", 100)])
        stub._merge_fetched_reactions(
            JID, "m1", _reactions_payload([
                ("a@s.whatsapp.net", "👍"), ("b@s.whatsapp.net", "😂"),
            ]),
        )
        stub._merge_fetched_reactions(
            JID, "m1", _reactions_payload([("b@s.whatsapp.net", "😂")]),
        )
        before = len(stub.main_window.db.inserted)

        changed = stub._merge_fetched_reactions(
            JID, "m1", _reactions_payload([("b@s.whatsapp.net", "😂")]),
        )

        assert changed is False
        assert len(stub.main_window.db.inserted) == before

    def test_malformed_payload_is_ignored(self):
        stub = _Stub(JID, records=[_msg("m1", 100)])

        assert stub._merge_fetched_reactions(JID, "m1", {}) is False
        assert stub._merge_fetched_reactions(JID, "m1", {"reactions": "nope"}) is False

    def test_multiple_senders_on_the_same_message_all_persist(self):
        stub = _Stub(JID, records=[_msg("m1", 100)])

        stub._merge_fetched_reactions(
            JID, "m1", _reactions_payload([
                ("a@s.whatsapp.net", "👍"), ("b@s.whatsapp.net", "😂"),
            ]),
        )

        records = stub._chat_records_for(JID)
        rxn = [r for r in records if r.get("messageType") == "reactionMessage"]
        assert len(rxn) == 2


class TestTheReactorIsTheSamePersonOnBothSides:
    """The bug that made this whole pass destructive rather than merely
    useless. `senderUserJid` is built by wa-js as createWid(...) and carries
    WhatsApp Web's own JID forms, while a stored reaction's participant was
    normalized by the WebSocketClient on the way in. Compared raw, one person
    held two keys: their reaction was persisted a SECOND time (the count
    visibly inflating) and, in the same pass, their original record was
    marked removed. Every time the conversation was opened."""

    def test_a_c_us_response_matches_a_stored_s_whatsapp_net_reaction(self):
        stub = _Stub(JID, records=[_msg("m1", 100)])
        stub._merge_fetched_reactions(
            JID, "m1", _reactions_payload([("a@s.whatsapp.net", "👍")]),
        )

        changed = stub._merge_fetched_reactions(
            JID, "m1", _reactions_payload([("a@c.us", "👍")]),
        )

        assert changed is False
        rxn = [r for r in stub._chat_records_for(JID)
               if r.get("messageType") == "reactionMessage"]
        assert len(rxn) == 1                     # not duplicated
        assert rxn[0]["message"]["reactionMessage"]["text"] == "👍"  # not removed

    def test_a_device_suffix_does_not_split_one_person_in_two(self):
        stub = _Stub(JID, records=[_msg("m1", 100)])
        stub._merge_fetched_reactions(
            JID, "m1", _reactions_payload([("a@s.whatsapp.net", "👍")]),
        )

        stub._merge_fetched_reactions(
            JID, "m1", _reactions_payload([("a:60@c.us", "👍")]),
        )

        assert len([r for r in stub._chat_records_for(JID)
                    if r.get("messageType") == "reactionMessage"]) == 1

    def test_a_lid_response_matches_through_the_lid_cache(self):
        """@lid survives _normalize_jid untouched by design — only
        main.py's _lid_to_phone bridges it to the phone JID."""
        stub = _Stub(JID, records=[_msg("m1", 100)],
                     lid_to_phone={"12345@lid": "a@s.whatsapp.net"})
        stub._merge_fetched_reactions(
            JID, "m1", _reactions_payload([("a@s.whatsapp.net", "👍")]),
        )

        changed = stub._merge_fetched_reactions(
            JID, "m1", _reactions_payload([("12345@lid", "👍")]),
        )

        assert changed is False
        assert len([r for r in stub._chat_records_for(JID)
                    if r.get("messageType") == "reactionMessage"]) == 1

    def test_a_reaction_stored_under_a_lid_is_updated_not_duplicated(self):
        """The mirror case: the live event arrived as @lid, the response
        comes back as the phone JID. The update has to land on the record
        that already exists — _persist_reaction_record() dedups by
        "_rxn_{id}_{sender_key}", so writing under the canonical key would
        append a second record for the same person."""
        stored = {
            "key": {"id": "_rxn_m1_12345@lid", "fromMe": False,
                    "participant": "12345@lid"},
            "messageType": "reactionMessage",
            "message": {"reactionMessage": {"key": {"id": "m1"},
                                            "text": "👍"}},
            "messageTimestamp": 400,
        }
        stub = _Stub(JID, records=[_msg("m1", 100), stored],
                     lid_to_phone={"12345@lid": "a@s.whatsapp.net"})

        changed = stub._merge_fetched_reactions(
            JID, "m1", _reactions_payload([("a@s.whatsapp.net", "😂")]),
        )

        assert changed is True
        rxn = [r for r in stub._chat_records_for(JID)
               if r.get("messageType") == "reactionMessage"]
        assert len(rxn) == 1
        assert rxn[0]["key"]["id"] == "_rxn_m1_12345@lid"   # updated in place
        assert rxn[0]["message"]["reactionMessage"]["text"] == "😂"

    def test_a_wid_that_arrived_as_an_object_is_still_read(self):
        """createWid() returns a Wid; whether it reaches Python as its
        string or as the object it serializes to depends on the WhatsApp Web
        build. As an object it used to be handed straight to a dict key —
        TypeError: unhashable type: 'dict', on the wx main thread."""
        stub = _Stub(JID, records=[_msg("m1", 100)])
        wid = {"server": "c.us", "user": "a", "_serialized": "a@c.us"}

        changed = stub._merge_fetched_reactions(
            JID, "m1", _reactions_payload([(wid, "👍")]),
        )

        assert changed is True
        rxn = [r for r in stub._chat_records_for(JID)
               if r.get("messageType") == "reactionMessage"][0]
        assert rxn["key"]["participant"] == "a@s.whatsapp.net"

    def test_an_unusable_sender_is_skipped_rather_than_crashing(self):
        stub = _Stub(JID, records=[_msg("m1", 100)])

        for junk in (None, "", {}, "no-at-sign", 42):
            assert stub._merge_fetched_reactions(
                JID, "m1", _reactions_payload([(junk, "👍")]),
            ) is False


class TestAnEmptyResponseIsNotConfirmedRemoval:
    """/reactions/{id} reads WhatsApp Web's live Store, not a history: a
    message the Store does not currently hold — routine for the older end of
    the 40 this backfill walks — answers 200 with nothing at all. Reading
    that as "nobody reacted" wiped every reaction the app already knew."""

    def test_an_empty_response_leaves_known_reactions_alone(self):
        stub = _Stub(JID, records=[_msg("m1", 100)])
        stub._merge_fetched_reactions(
            JID, "m1", _reactions_payload([("a@s.whatsapp.net", "👍")]),
        )
        before = len(stub.main_window.db.inserted)

        changed = stub._merge_fetched_reactions(JID, "m1", _reactions_payload([]))

        assert changed is False
        assert len(stub.main_window.db.inserted) == before
        rxn = [r for r in stub._chat_records_for(JID)
               if r.get("messageType") == "reactionMessage"][0]
        assert rxn["message"]["reactionMessage"]["text"] == "👍"

    def test_an_empty_response_on_a_message_with_no_reactions_is_a_no_op(self):
        stub = _Stub(JID, records=[_msg("m1", 100)])

        assert stub._merge_fetched_reactions(JID, "m1", _reactions_payload([])) is False


class TestTheCooldown:
    """One open costs up to _REACTION_BACKFILL_LIMIT sequential requests;
    alternating between two chats must not pay that every single time."""

    def test_reopening_the_same_chat_does_not_refetch(self, monkeypatch):
        monkeypatch.setattr(
            threading.Thread, "start",
            lambda self: self._target(*self._args, **self._kwargs),
        )
        stub = _Stub(JID, records=[_msg("m1", 100)])

        stub._backfill_reactions_for_open_conversation()
        stub._backfill_reactions_for_open_conversation()

        assert stub.main_window.fetch_calls == ["m1"]

    def test_the_cooldown_expires(self, monkeypatch):
        monkeypatch.setattr(
            threading.Thread, "start",
            lambda self: self._target(*self._args, **self._kwargs),
        )
        stub = _Stub(JID, records=[_msg("m1", 100)])

        stub._backfill_reactions_for_open_conversation()
        # Pushed far enough into the past that the window has elapsed,
        # without the test having to wait it out.
        for key in stub._reaction_backfill_last:
            stub._reaction_backfill_last[key] -= (
                stub._REACTION_BACKFILL_COOLDOWN_SECONDS + 1
            )
        stub._backfill_reactions_for_open_conversation()

        assert stub.main_window.fetch_calls == ["m1", "m1"]

    def test_the_lid_and_phone_forms_of_one_chat_share_a_cooldown(self, monkeypatch):
        monkeypatch.setattr(
            threading.Thread, "start",
            lambda self: self._target(*self._args, **self._kwargs),
        )
        stub = _Stub(JID, records=[_msg("m1", 100)],
                     lid_to_phone={"12345@lid": JID})

        stub._backfill_reactions_for_open_conversation()
        stub.conversation = dict(stub.conversation, remoteJid="12345@lid")
        stub._backfill_reactions_for_open_conversation()

        assert stub.main_window.fetch_calls == ["m1"]

    def test_a_different_chat_is_not_blocked_by_another_chats_cooldown(self, monkeypatch):
        monkeypatch.setattr(
            threading.Thread, "start",
            lambda self: self._target(*self._args, **self._kwargs),
        )
        stub = _Stub(JID, records=[_msg("m1", 100)])

        stub._backfill_reactions_for_open_conversation()
        stub.conversation = dict(stub.conversation, remoteJid="other@s.whatsapp.net")
        stub._backfill_reactions_for_open_conversation()

        assert stub.main_window.fetch_calls == ["m1", "m1"]


class TestItDoesNotSpendItsCooldownOffline:
    """The cooldown may only be spent on a pass that could actually fetch.

    Every request this makes bails on `_wa_connected` inside
    MainWindow.fetch_message_reactions(), so a pass armed while disconnected
    fetches nothing and still locks the chat out for five minutes. The pass
    most likely to run disconnected is the one right after a reconnection —
    the health poll can take ~30 s to confirm it — which is exactly the case
    this backfill exists for: reactions that arrived while WinZapp was away.
    """

    def test_a_disconnected_open_fetches_nothing(self, monkeypatch):
        monkeypatch.setattr(
            threading.Thread, "start",
            lambda self: self._target(*self._args, **self._kwargs),
        )
        stub = _Stub(JID, records=[_msg("m1", 100)])
        stub.main_window._wa_connected = False

        stub._backfill_reactions_for_open_conversation()

        assert stub.main_window.fetch_calls == []

    def test_and_leaves_the_cooldown_unspent(self, monkeypatch):
        """The point of the fix: the open that follows, once the connection is
        up, still does the work instead of waiting out five minutes."""
        monkeypatch.setattr(
            threading.Thread, "start",
            lambda self: self._target(*self._args, **self._kwargs),
        )
        stub = _Stub(JID, records=[_msg("m1", 100)])
        stub.main_window._wa_connected = False

        stub._backfill_reactions_for_open_conversation()
        assert stub._reaction_backfill_last == {}, (
            "the cooldown was stamped by a pass that could not fetch"
        )

        stub.main_window._wa_connected = True
        stub._backfill_reactions_for_open_conversation()

        assert stub.main_window.fetch_calls == ["m1"]


class TestGenerationGuard:
    """A background fetch for a conversation the user has since navigated
    away from must not write its results into whatever is open by the time
    it completes."""

    def test_stale_generation_is_dropped(self):
        stub = _Stub(JID, records=[_msg("m1", 100)])
        stub._reaction_backfill_generation = 2  # a newer open superseded gen 1

        stub._apply_backfilled_reactions(
            JID, [("m1", _reactions_payload([("a@s.whatsapp.net", "👍")]))], generation=1,
        )

        assert stub.populate_calls == 0
        assert stub._chat_records_for(JID) == [_msg("m1", 100)]

    def test_conversation_switched_before_results_arrived_is_dropped(self):
        stub = _Stub(JID, records=[_msg("m1", 100)])
        stub._reaction_backfill_generation = 1
        stub.conversation = {"remoteJid": "someone-else@s.whatsapp.net"}

        stub._apply_backfilled_reactions(
            JID, [("m1", _reactions_payload([("a@s.whatsapp.net", "👍")]))], generation=1,
        )

        assert stub.populate_calls == 0

    def test_matching_generation_and_conversation_applies_and_refreshes(self):
        stub = _Stub(JID, records=[_msg("m1", 100)])
        stub._reaction_backfill_generation = 1

        stub._apply_backfilled_reactions(
            JID, [("m1", _reactions_payload([("a@s.whatsapp.net", "👍")]))], generation=1,
        )

        assert stub.populate_calls == 1

    def test_no_actual_change_does_not_trigger_a_rebuild(self):
        stub = _Stub(JID, records=[_msg("m1", 100)])
        stub._reaction_backfill_generation = 1

        stub._apply_backfilled_reactions(JID, [("m1", {})], generation=1)

        assert stub.populate_calls == 0
