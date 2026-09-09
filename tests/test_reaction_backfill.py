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

from ui.conversations import ConversationsPanel


class _FakeDB:
    def __init__(self):
        self.inserted = []

    def insert_message(self, jid, record):
        self.inserted.append((jid, record))


class _FakeMainWindow:
    def __init__(self, chat, reactions_by_msg_id=None):
        self._chat = chat
        self.db = _FakeDB()
        self._reactions_by_msg_id = reactions_by_msg_id or {}
        self.fetch_calls = []

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
    _chat_records_for           = ConversationsPanel._chat_records_for
    _extract_timestamp          = ConversationsPanel._extract_timestamp
    _SELF_REACTOR_KEY           = ConversationsPanel._SELF_REACTOR_KEY
    _REACTION_BACKFILL_LIMIT    = ConversationsPanel._REACTION_BACKFILL_LIMIT

    def __init__(self, jid, records=None, reactions_by_msg_id=None):
        chat = {"messages": {"messages": {"records": list(records or [])}}}
        self.main_window = _FakeMainWindow(chat, reactions_by_msg_id)
        self.conversation = {"remoteJid": jid, "messages": chat["messages"]}
        self._reaction_backfill_generation = 0
        self.populate_calls = 0

    def populate_messages(self, preserve_focus=False):
        self.populate_calls += 1


JID = "5511999999999@s.whatsapp.net"


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

    def test_own_reaction_is_recognised_via_reactionByMe(self):
        stub = _Stub(JID, records=[_msg("m1", 100)])

        stub._merge_fetched_reactions(
            JID, "m1",
            _reactions_payload([("me@s.whatsapp.net", "❤️")], me_jid="me@s.whatsapp.net"),
        )

        records = stub._chat_records_for(JID)
        rxn = [r for r in records if r.get("messageType") == "reactionMessage"][0]
        assert rxn["key"]["fromMe"] is True
        assert rxn["key"]["id"] == "_rxn_m1"  # the SELF namespacing _persist_reaction_record() uses

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

    def test_a_sender_missing_from_the_response_is_treated_as_removed(self):
        """They reacted, then removed it while WinZapp could not see either
        event — the response simply does not list them any more."""
        stub = _Stub(JID, records=[_msg("m1", 100)])
        stub._merge_fetched_reactions(
            JID, "m1", _reactions_payload([("a@s.whatsapp.net", "👍")]),
        )

        changed = stub._merge_fetched_reactions(JID, "m1", _reactions_payload([]))

        assert changed is True
        records = stub._chat_records_for(JID)
        rxn = [r for r in records if r.get("messageType") == "reactionMessage"]
        assert len(rxn) == 1
        assert rxn[0]["message"]["reactionMessage"]["text"] == ""

    def test_an_already_removed_sender_is_not_reprocessed(self):
        stub = _Stub(JID, records=[_msg("m1", 100)])
        stub._merge_fetched_reactions(
            JID, "m1", _reactions_payload([("a@s.whatsapp.net", "👍")]),
        )
        stub._merge_fetched_reactions(JID, "m1", _reactions_payload([]))
        before = len(stub.main_window.db.inserted)

        changed = stub._merge_fetched_reactions(JID, "m1", _reactions_payload([]))

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
