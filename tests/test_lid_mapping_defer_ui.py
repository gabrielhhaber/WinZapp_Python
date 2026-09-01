"""Tests for register_jid_mapping()'s deferred chat-list refresh.

Every learned @lid -> phone mapping used to schedule a chat-list rebuild.
_schedule_set_chats() debounces at 300 ms, which is right for a message
arriving on its own and useless against a batch: measured on a live 935-chat
sync, resolve_lid_jids_via_api() learned 1245 mappings over eight minutes and
that still came to roughly 380 full recomputations of the list, each one
resolving the name of all 935 chats.

`defer_ui` lets a batch caller suppress the per-mapping refresh and schedule
one when it finishes. The live message path must NOT pass it — a single
arriving message still has to refresh the list immediately — so both
directions are pinned here.

MainWindow is a wx.Frame and cannot be instantiated without a running wx.App,
so the method is bound to a plain stub, as elsewhere in this suite.
"""

import threading
import types

import pytest

import main
from main import MainWindow


class _DB:
    def set_lid_mapping(self, *a, **kw):
        pass

    def upsert_contacts_batch(self, *a, **kw):
        pass


class _Stub:
    def __init__(self):
        self.contacts = {}
        self.chats = {}
        self.db = _DB()
        self.my_lid = ""
        self.my_jid = ""
        self._lid_to_phone = {}
        self._phone_to_lid = {}
        self._unresolvable_lids = set()
        # register_jid_mapping() writes the mapping caches under this lock
        # (see tests/test_lid_mapping_thread_safety.py for what it guards).
        self._lid_mapping_lock = threading.RLock()
        self.refreshes = 0
        self.message_refresh_jids = []

    def _is_self_jid(self, jid):
        return False

    def _schedule_set_chats(self):
        self.refreshes += 1

    def _schedule_refresh_active_messages(self, jids=None):
        self.message_refresh_jids.append(None if jids is None else set(jids))


LID = "111222333444555@lid"
PHONE = "5511999999999@s.whatsapp.net"


@pytest.fixture
def stub(monkeypatch):
    monkeypatch.setattr(main.wx, "CallAfter", lambda fn, *a, **kw: fn(*a, **kw))
    s = _Stub()
    s.register_jid_mapping = types.MethodType(
        MainWindow.__dict__["register_jid_mapping"], s)
    return s


class TestRegisterJidMapping:
    def test_a_single_mapping_refreshes_the_list(self, stub):
        """The live path: a message arrives carrying a new mapping and the
        chat list must pick the name up at once."""
        stub.register_jid_mapping(LID, PHONE)
        assert stub._lid_to_phone[LID] == PHONE
        assert stub.refreshes == 1

    def test_defer_ui_learns_the_mapping_but_does_not_refresh(self, stub):
        stub.register_jid_mapping(LID, PHONE, defer_ui=True)
        assert stub._lid_to_phone[LID] == PHONE, "the mapping itself must still be learned"
        assert stub._phone_to_lid[PHONE] == LID
        assert stub.refreshes == 0

    def test_a_batch_costs_one_refresh_instead_of_one_each(self, stub):
        """The shape that mattered: 60 mappings in a row, as the backfill's
        chunk resolves them."""
        for i in range(60):
            stub.register_jid_mapping(f"{i:015d}@lid", f"55119999{i:05d}@s.whatsapp.net",
                                      defer_ui=True)
        assert len(stub._lid_to_phone) == 60
        assert stub.refreshes == 0
        # …and the caller schedules exactly one when the batch ends.
        stub._schedule_set_chats()
        assert stub.refreshes == 1

    def test_the_same_mapping_twice_refreshes_once(self, stub):
        """Unchanged behaviour: the refresh has always been inside the
        "this is new" branch."""
        stub.register_jid_mapping(LID, PHONE)
        stub.register_jid_mapping(LID, PHONE)
        assert stub.refreshes == 1

    def test_defer_ui_still_persists_the_mapping(self, stub):
        """save and defer_ui are independent — deferring the UI must not
        quietly stop the mapping reaching the database, or it would be gone
        on the next launch."""
        written = []
        stub.db.set_lid_mapping = lambda l, p: written.append((l, p))
        stub.register_jid_mapping(LID, PHONE, defer_ui=True)
        assert written == [(LID, PHONE)]


class TestTheOpenConversationIsRepaintedToo:
    """Only relevant since the message repaint became scoped.

    The non-batch mapping writers — _extract_lid_mapping() on the Socket.IO
    thread and _get_participant_name()'s inline learn — never scheduled a
    message repaint of their own; they were fixed up by whatever unrelated full
    repaint happened next. Now that every repaint is scoped to the JIDs its own
    caller resolved, that free ride is gone and a chat whose @lid was just
    bridged would keep announcing raw digits until it was reopened.
    """

    def test_a_new_mapping_repaints_the_open_conversation(self, stub):
        stub.register_jid_mapping(LID, PHONE)
        assert stub.message_refresh_jids == [{LID, PHONE}]

    def test_both_address_forms_are_handed_over(self, stub):
        """The rows can carry either one, and which they carry depends on who
        sent the message."""
        stub.register_jid_mapping(LID, PHONE)
        assert stub.message_refresh_jids[0] == {LID, PHONE}

    def test_defer_ui_suppresses_it_like_the_chat_list(self, stub):
        stub.register_jid_mapping(LID, PHONE, defer_ui=True)
        assert stub.message_refresh_jids == []

    def test_a_mapping_already_known_schedules_nothing(self, stub):
        """What keeps this terminating: _get_participant_name() can reach here
        from inside a render, so a repaint could schedule a repaint. A mapping
        is only `changed` once, so the second pass is silent."""
        stub.register_jid_mapping(LID, PHONE)
        stub.register_jid_mapping(LID, PHONE)
        assert len(stub.message_refresh_jids) == 1
