"""Tests for the way out of the unresolvable-LID blacklist.

``unresolvable_lids`` records a LID whose phone number or display name a
resolution pass failed to find, so the app stops asking on every render.
Nothing ever removed an entry: the in-memory discard in register_jid_mapping()
left the SQLite row behind, _load_local_lid_cache() read it back on the next
launch, and _unresolvable_names had no discard at all.  A contact recorded once
therefore stayed "Contato sem nome" forever — which a screen reader announces
aloud every time they speak in a group — even after WhatsApp learned the name.

Three exits are pinned here: the age-based sweep at load, the delete when a
phone mapping is genuinely learned, and the delete when a pushName is.

MainWindow is a wx.Frame and cannot be instantiated without a running wx.App,
so the methods under test are bound to plain stubs, as elsewhere in this suite.
"""

import threading
import types

import pytest

import main
from main import MainWindow


# Fictional identifiers only, same shape as the real ones.
LID = "10000000000001@lid"
PHONE = "551199999999@s.whatsapp.net"


class _DB:
    """Records the blacklist writes the methods under test make."""

    def __init__(self, lids=None, names=None):
        self.lids = set(lids or ())
        self.names = set(names or ())
        self.purge_cutoffs = []
        self.deleted_lids = []
        self.deleted_names = []

    # ── reads ────────────────────────────────────────────────────────────
    def get_lid_mappings(self):
        return {}

    def get_unresolvable_lids(self):
        return set(self.lids), set(self.names)

    def get_status_updates(self):
        return {}

    # ── writes ───────────────────────────────────────────────────────────
    def delete_expired_unresolvable(self, cutoff_ts):
        self.purge_cutoffs.append(cutoff_ts)
        purged = len(self.lids) + len(self.names)
        self.lids.clear()
        self.names.clear()
        return purged

    def delete_unresolvable_lid(self, jid):
        self.deleted_lids.append(jid)
        self.lids.discard(jid)

    def delete_unresolvable_name(self, jid):
        self.deleted_names.append(jid)
        self.names.discard(jid)

    def set_lid_mapping(self, lid, phone):
        pass

    def upsert_contacts_batch(self, contacts):
        pass


class _Stub:
    def __init__(self, db=None, unresolvable_lids=(), unresolvable_names=()):
        self.db = db or _DB()
        self.chats = {}
        self.contacts = {}
        self.my_lid = ""
        self.my_jid = ""
        self._lid_to_phone = {}
        self._phone_to_lid = {}
        self._presence_pushname_map = {}
        self._unresolvable_lids = set(unresolvable_lids)
        self._unresolvable_names = set(unresolvable_names)
        self._status_updates = {}
        self._lid_mapping_lock = threading.RLock()

    _UNRESOLVABLE_MAX_AGE_SECONDS = MainWindow._UNRESOLVABLE_MAX_AGE_SECONDS
    _normalize_jid = staticmethod(MainWindow._normalize_jid)
    _load_local_lid_cache = MainWindow.__dict__["_load_local_lid_cache"]
    _learn_sender_name = MainWindow.__dict__["_learn_sender_name"]

    def _is_self_jid(self, jid):
        return False

    def _schedule_set_chats(self):
        pass

    def _schedule_refresh_active_messages(self, jids=None):
        pass


@pytest.fixture
def stub(monkeypatch):
    monkeypatch.setattr(main.wx, "CallAfter", lambda fn, *a, **kw: fn(*a, **kw))
    return _Stub


def _group_msg(participant, push_name):
    return {
        "key": {
            "remoteJid": "120363000000000001@g.us",
            "participant": participant,
            "fromMe": False,
            "id": "A1",
        },
        "pushName": push_name,
        "message": {"conversation": "oi"},
        "messageType": "conversation",
        "messageTimestamp": 1700000000,
    }


class TestExpirySweepAtLoad:
    def test_load_purges_entries_older_than_the_max_age(self, stub, monkeypatch):
        monkeypatch.setattr("main.time.time", lambda: 1_700_000_000)
        s = stub(db=_DB(lids={LID}, names={LID}))
        s._load_local_lid_cache()
        assert s.db.purge_cutoffs == [
            1_700_000_000 - MainWindow._UNRESOLVABLE_MAX_AGE_SECONDS
        ]

    def test_purged_entries_do_not_come_back_in_memory(self, stub):
        """The sweep runs before the read, so a swept LID is queryable again
        this launch and not merely on the next one."""
        s = stub(db=_DB(lids={LID}, names={LID}))
        s._load_local_lid_cache()
        assert s._unresolvable_lids == set()
        assert s._unresolvable_names == set()

    def test_a_failing_sweep_still_loads_the_caches(self, stub):
        """A failed purge only means "no retries this launch" — it must not
        cost the mappings and blacklist that were about to be read."""
        db = _DB(lids={LID})

        def _boom(cutoff_ts):
            raise RuntimeError("database is locked")

        db.delete_expired_unresolvable = _boom
        s = stub(db=db)
        s._load_local_lid_cache()
        assert s._unresolvable_lids == {LID}


class TestMappingLearned:
    @pytest.fixture
    def register(self, monkeypatch):
        monkeypatch.setattr(main.wx, "CallAfter", lambda fn, *a, **kw: fn(*a, **kw))

        def _make(**kwargs):
            s = _Stub(**kwargs)
            s.register_jid_mapping = types.MethodType(
                MainWindow.__dict__["register_jid_mapping"], s)
            return s

        return _make

    def test_learning_the_mapping_deletes_the_persisted_row(self, register):
        s = register(db=_DB(lids={LID}), unresolvable_lids={LID})
        s.register_jid_mapping(LID, PHONE)
        assert s._unresolvable_lids == set()
        assert s.db.deleted_lids == [LID], "the row used to survive the discard"

    def test_the_delete_happens_even_when_the_caller_defers_the_save(self, register):
        """resolve_lid_jids_via_api() passes save=False and persists the
        mapping itself, so the stale blacklist row has to go either way."""
        s = register(db=_DB(lids={LID}), unresolvable_lids={LID})
        s.register_jid_mapping(LID, PHONE, save=False, defer_ui=True)
        assert s.db.deleted_lids == [LID]

    def test_a_lid_that_was_not_blacklisted_costs_no_db_call(self, register):
        s = register()
        s.register_jid_mapping(LID, PHONE)
        assert s.db.deleted_lids == []

    def test_a_failing_delete_does_not_break_the_mapping(self, register):
        db = _DB(lids={LID})
        db.delete_unresolvable_lid = lambda jid: (_ for _ in ()).throw(RuntimeError("locked"))
        s = register(db=db, unresolvable_lids={LID})
        s.register_jid_mapping(LID, PHONE)
        assert s._lid_to_phone[LID] == PHONE


class TestNameLearned:
    def test_a_pushname_clears_the_name_blacklist(self, stub):
        s = stub(db=_DB(names={LID}), unresolvable_names={LID})
        assert s._learn_sender_name(_group_msg(LID, "Carlos")) is True
        assert s._unresolvable_names == set()
        assert s.db.deleted_names == [LID]

    def test_both_jid_forms_are_cleared_when_the_bridge_is_known(self, stub):
        s = stub(db=_DB(names={LID, PHONE}), unresolvable_names={LID, PHONE})
        s._lid_to_phone = {LID: PHONE}
        s._learn_sender_name(_group_msg(LID, "Carlos"))
        assert s._unresolvable_names == set()
        assert sorted(s.db.deleted_names) == sorted([LID, PHONE])

    def test_the_same_pushname_twice_costs_one_db_call(self, stub):
        """Nothing changed the second time, so nothing is written — this runs
        once per incoming message on the Socket.IO thread."""
        s = stub(db=_DB(names={LID}), unresolvable_names={LID})
        s._learn_sender_name(_group_msg(LID, "Carlos"))
        s._learn_sender_name(_group_msg(LID, "Carlos"))
        assert s.db.deleted_names == [LID]

    def test_a_sender_that_was_not_blacklisted_costs_no_db_call(self, stub):
        s = stub()
        s._learn_sender_name(_group_msg(LID, "Carlos"))
        assert s.db.deleted_names == []
