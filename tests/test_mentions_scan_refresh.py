"""Tests for the single chat-list refresh at the end of the mentions scan.

The scan walks every message of every chat, so the per-mapping refresh
register_jid_mapping() normally schedules is a rebuild storm — the same one
resolve_lid_jids_via_api() had. It therefore passes defer_ui=True and owes the
list exactly one refresh when it finishes.

That refresh is gated on `updated_contacts or mapped`, and `mapped` is there
for one case in particular: a scan that learned @lid <-> phone mappings but
changed no contact record. The gate was written *inside* `if phones_to_resolve:`,
which made it unreachable in precisely that case — the two are fed by unrelated
passes. `mapped` counts pairs found on `key.remoteJidAlt` while walking the
messages; `phones_to_resolve` holds *mentioned* phone JIDs that still need a
name. A scan that learns mappings, needs no @lid lookup (so
resolve_lid_jids_via_api()'s own unconditional refresh never fires) and finds no
unnamed mention refreshed nothing at all, and the list kept showing raw @lid
until something unrelated happened to rebuild it.

MainWindow is a wx.Frame and cannot be instantiated without a running wx.App,
so the method is bound to a plain stub, as elsewhere in this suite.
"""

import threading
import types

import pytest

import main
from main import MainWindow


LID = "111222333444555@lid"
PHONE = "5511999999999@s.whatsapp.net"


def _chat_with_alt(lid=LID, alt=PHONE):
    """A stored message carrying the @lid <-> phone bridge in its own key —
    the shape that needs no API lookup at all."""
    return {
        "remoteJid": lid,
        "messages": {"messages": {"records": [
            {"key": {"id": "m1", "remoteJid": lid, "remoteJidAlt": alt}},
        ]}},
    }


class _Stub:
    def __init__(self, chats):
        self.chats = chats
        self.contacts = {}
        self._lid_to_phone = {}
        self._phone_to_lid = {}
        self._contact_resolution_lock = threading.Lock()
        self.refreshes = 0
        self.message_refreshes = 0
        self.message_refresh_jids = []
        self.bulk_name_batches = []
        self.bulk_learns_names = False
        self.mapped_calls = []
        self.resolved_lid_batches = []
        self.profile_lookups = []

    # ── what the scan calls out to ───────────────────────────────────
    def register_jid_mapping(self, lid_jid, phone_jid, save=True, defer_ui=False):
        self.mapped_calls.append((lid_jid, phone_jid, defer_ui))
        self._lid_to_phone[lid_jid] = phone_jid

    def resolve_lid_jids_via_api(self, jids):
        # The real one ends with an unconditional refresh of its own; that is
        # exactly why it must not be what this test relies on.
        self.resolved_lid_batches.append(list(jids))

    def get_contact_profile(self, jid):
        self.profile_lookups.append(jid)
        return {"response": {"name": "Alguém"}}

    def _learn_sender_names_bulk(self, records):
        self.bulk_name_batches.append(list(records))
        return self.bulk_learns_names

    def _needs_sender_resolution(self, jid):
        return False

    def _normalize_jid(self, jid):
        return jid

    def _schedule_set_chats(self):
        self.refreshes += 1

    def _schedule_refresh_active_messages(self, jids=None):
        self.message_refreshes += 1
        self.message_refresh_jids.append(None if jids is None else set(jids))


def _make(chats):
    stub = _Stub(chats)
    stub.scan_all_cached_messages_for_mentions = types.MethodType(
        MainWindow.__dict__["scan_all_cached_messages_for_mentions"], stub)
    return stub


@pytest.fixture(autouse=True)
def _inline(monkeypatch):
    """The scan runs on its own thread and sleeps 3 s before starting."""
    monkeypatch.setattr(main.time, "sleep", lambda *_a: None)
    monkeypatch.setattr(main.wx, "CallAfter", lambda fn, *a, **kw: fn(*a, **kw))
    monkeypatch.setattr(
        main.threading, "Thread",
        lambda target=None, **kw: types.SimpleNamespace(start=lambda: target and target()))


class TestAScanThatOnlyLearnsMappings:
    """No @lid needs the API, no mention needs a name — the case `mapped`
    exists for, and the one the misplaced gate could not reach."""

    def test_it_refreshes_the_chat_list(self):
        stub = _make({LID: _chat_with_alt()})
        stub.scan_all_cached_messages_for_mentions()
        assert stub._lid_to_phone[LID] == PHONE, "the mapping itself must be learned"
        assert stub.refreshes == 1, (
            "the per-mapping refreshes were deferred, so this one is the only "
            "thing standing between a learned name and a list still showing @lid")

    def test_it_refreshes_the_open_conversation_too(self):
        stub = _make({LID: _chat_with_alt()})
        stub.scan_all_cached_messages_for_mentions()
        assert stub.message_refreshes == 1

    def test_the_repaint_is_scoped_to_both_sides_of_the_learned_mapping(self):
        """The rows of a conversation with thousands of loaded messages are
        repainted selectively now, and the open conversation may render this
        participant under either address — so both have to be handed over or
        the row keeps announcing raw @lid digits."""
        stub = _make({LID: _chat_with_alt()})
        stub.scan_all_cached_messages_for_mentions()
        assert stub.message_refresh_jids == [{LID, PHONE}]

    def test_nothing_was_resolved_through_the_api(self):
        """Pins the premise: this scan never reaches
        resolve_lid_jids_via_api(), whose own unconditional refresh would
        otherwise mask the bug."""
        stub = _make({LID: _chat_with_alt()})
        stub.scan_all_cached_messages_for_mentions()
        assert stub.resolved_lid_batches == []
        assert stub.profile_lookups == []

    def test_the_mappings_are_learned_with_the_ui_deferred(self):
        stub = _make({LID: _chat_with_alt()})
        stub.scan_all_cached_messages_for_mentions()
        assert stub.mapped_calls == [(LID, PHONE, True)]


class TestAScanWithNothingToLearn:
    def test_it_does_not_refresh(self):
        """The gate still gates: an idle scan must not schedule a rebuild of a
        935-chat list for nothing."""
        stub = _make({PHONE: {"remoteJid": PHONE,
                              "messages": {"messages": {"records": []}}}})
        stub.scan_all_cached_messages_for_mentions()
        assert stub.refreshes == 0
        assert stub.message_refreshes == 0

    def test_a_mapping_already_known_is_not_relearned(self):
        stub = _make({LID: _chat_with_alt()})
        stub._lid_to_phone[LID] = PHONE
        stub.scan_all_cached_messages_for_mentions()
        assert stub.mapped_calls == []
        assert stub.refreshes == 0


class TestGroupSenderLidsWithoutMentions:
    """Regression: a message from a group participant whose @lid can't be
    bridged (no remoteJidAlt on the message — the common case for group
    messages, which carry no such field) used to never reach the resolver
    unless that same participant was ALSO @mentioned by someone, leaving
    them "Participante sem nome" for the life of the chat.
    scan_all_cached_messages_for_mentions() also collects the SENDER of
    every message (key.participant), capped at 150, and feeds unresolved
    ones into the same resolve_lid_jids_via_api() batch as mentions."""

    def _stub_needing_resolution(self, chats, unresolved_lids):
        stub = _make(chats)
        stub._needs_sender_resolution = lambda jid, _u=unresolved_lids: jid in _u
        return stub

    def test_an_unresolved_sender_with_no_mention_still_gets_queued(self):
        sender_lid = "999888777666555@lid"
        chat = {
            "remoteJid": "group@g.us",
            "messages": {"messages": {"records": [
                {"key": {"id": "m1", "remoteJid": "group@g.us", "participant": sender_lid}},
            ]}},
        }
        stub = self._stub_needing_resolution({"group@g.us": chat}, {sender_lid})

        stub.scan_all_cached_messages_for_mentions()

        assert stub.resolved_lid_batches == [[sender_lid]]

    def test_the_lookup_is_capped_at_150_senders(self):
        senders = [f"{i:015d}@lid" for i in range(200)]
        records = [
            {"key": {"id": f"m{i}", "remoteJid": "group@g.us", "participant": s}}
            for i, s in enumerate(senders)
        ]
        chat = {"remoteJid": "group@g.us", "messages": {"messages": {"records": records}}}
        stub = self._stub_needing_resolution({"group@g.us": chat}, set(senders))

        stub.scan_all_cached_messages_for_mentions()

        assert len(stub.resolved_lid_batches[0]) == 150

    def test_a_sender_that_resolves_via_pushname_pass_is_never_queued(self):
        sender_lid = "999888777666555@lid"
        chat = {
            "remoteJid": "group@g.us",
            "messages": {"messages": {"records": [
                {"key": {"id": "m1", "remoteJid": "group@g.us", "participant": sender_lid}},
            ]}},
        }
        stub = self._stub_needing_resolution({"group@g.us": chat}, unresolved_lids=set())

        stub.scan_all_cached_messages_for_mentions()

        assert stub.resolved_lid_batches == []


class TestTheEmptyAccount:
    def test_no_chats_at_all_is_not_an_error(self):
        stub = _make({})
        stub.scan_all_cached_messages_for_mentions()
        assert stub.refreshes == 0


class TestTheBulkPushNamePassForcesAFullRepaint:
    """_learn_sender_names_bulk() writes names for arbitrary participant JIDs
    and reports none of them — not in `mapped_jids` (only remoteJidAlt pairs)
    and not in `updated_contacts` (only mentions resolved through the API).

    Routine on a new account: an open group of ~4000 rows, the scan learns
    pushNames for 40 participants plus one unrelated 1:1 mapping. `mapped == 1`
    opens the gate, the repaint goes out scoped to that one pair, no row of the
    group matches, and all 40 participants keep showing a formatted number
    until the conversation is reopened. The scan runs once, not at 1 Hz, so
    asking for the whole list back is free here.
    """

    def test_learning_any_pushname_asks_for_the_full_repaint(self):
        stub = _make({LID: _chat_with_alt()})
        stub.bulk_learns_names = True
        stub.scan_all_cached_messages_for_mentions()
        assert stub.message_refresh_jids == [None]

    def test_it_alone_is_enough_to_open_the_gate(self):
        """A scan that learned only pushNames changes no contact record and no
        mapping, so `updated_contacts or mapped` would have skipped the refresh
        entirely and left every one of those names unpainted."""
        chat = {PHONE: {"remoteJid": PHONE, "messages": {"messages": {"records": [
            {"key": {"id": "m1", "remoteJid": PHONE}},
        ]}}}}
        stub = _make(chat)
        stub.bulk_learns_names = True
        stub.scan_all_cached_messages_for_mentions()
        assert stub.message_refreshes == 1
        assert stub.message_refresh_jids == [None]
        assert stub.refreshes == 1

    def test_learning_nothing_keeps_the_repaint_scoped(self):
        """The widening must be conditional — otherwise the scan is back to a
        full repaint of a 4000-row conversation on every run."""
        stub = _make({LID: _chat_with_alt()})
        stub.bulk_learns_names = False
        stub.scan_all_cached_messages_for_mentions()
        assert stub.message_refresh_jids == [{LID, PHONE}]
