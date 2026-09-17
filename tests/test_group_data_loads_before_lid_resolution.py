"""The group data dialog shows its data before resolving @lid participants.

Reported as: open a group's data, move to another tab (Participants) and the
member list never loads — Overview stuck on "Carregando..." too, until the
dialog was closed and opened again.

The tab switch was a red herring; it is simply what anyone does while nothing
appears. _fetch_data() used to call resolve_lid_jids_via_api() on every
unmapped @lid participant BEFORE posting _populate_group(), and that resolution
is sequential and throttled: one /contact/pn-lid call, usually a profile call
after it, and a fixed 0.5 s sleep — ~0.6 s per participant, measured. On a real
account a 368-member group had 305 unmapped members, so both tabs sat on
"loading" for over three minutes with the /group-info answer in hand after
50 ms. Running the real dialog in CI with every kind of tab switch (programmatic,
navigation event, simulated keyboard) loaded fine every time, which is what
ruled the tabs out.

ConversationDataDialog is a wx.Dialog, so the methods are bound onto a stub
carrying only what they touch (see CLAUDE.md's Tests section).
"""

import threading
import types

import pytest
import wx

import ui.dialogs.conversation_data_dialog as cdd
from core.utils import format_number
from ui.dialogs.conversation_data_dialog import (
    ConversationDataDialog,
    unresolved_participant_lids,
)

GROUP = "120363000000000000@g.us"


def _lid(n):
    return f"{100000000000000 + n}@lid"


class TestUnresolvedParticipantLids:
    def test_only_unmapped_lids_in_list_order(self):
        participants = [
            {"id": _lid(3)},
            {"id": "5511999990000@s.whatsapp.net"},
            {"id": _lid(1)},
            {"id": _lid(2)},
        ]
        mapping = {_lid(1): "5511999990001@s.whatsapp.net"}
        assert unresolved_participant_lids(participants, mapping) == [_lid(3), _lid(2)]

    def test_tolerates_junk_and_duplicates(self):
        participants = [None, "x", {"id": None}, {}, {"id": _lid(1)}, {"id": _lid(1)}]
        assert unresolved_participant_lids(participants, {}) == [_lid(1)]

    def test_missing_participants(self):
        assert unresolved_participant_lids(None, {}) == []


class _Mw:
    def __init__(self, participants, events, fail=False):
        self._participants = participants
        self._events = events
        self._fail = fail
        self._lid_to_phone = {}

    def get_group_info(self, jid):
        self._events.append(("get_group_info",))
        return {"subject": "Grupo", "participants": self._participants}

    def resolve_lid_jids_via_api(self, jids):
        self._events.append(("resolve", list(jids)))
        if self._fail:
            raise RuntimeError("boom")


def _named(fn, name):
    fn.__name__ = name
    return fn


def _stub(mw, events):
    stub = types.SimpleNamespace(
        _is_group=True,
        _jid=GROUP,
        _mw=mw,
        _closed=threading.Event(),
        _LID_RESOLVE_CHUNK=ConversationDataDialog._LID_RESOLVE_CHUNK,
        _LID_RESOLVE_BATCH=ConversationDataDialog._LID_RESOLVE_BATCH,
        _load_media_history=lambda: events.append(("media",)),
        _populate_group=lambda data: None,
        _refresh_participant_rows=_named(lambda: None, "_refresh_participant_rows"),
    )
    stub._resolve_participant_lids = types.MethodType(
        ConversationDataDialog._resolve_participant_lids, stub
    )
    return stub


@pytest.fixture
def events(monkeypatch):
    recorded = []

    def _call_after(fn, *args, **kwargs):
        recorded.append(("CallAfter", getattr(fn, "__name__", repr(fn)), args))

    monkeypatch.setattr(wx, "CallAfter", _call_after)
    return recorded


def _names(events):
    return [e[1] if e[0] == "CallAfter" else e[0] for e in events]


class TestFetchPostsTheDataFirst:
    def test_populate_is_posted_before_any_resolution_call(self, events):
        participants = [{"id": _lid(i)} for i in range(3)]
        stub = _stub(_Mw(participants, events), events)
        stub._populate_group = _named(lambda data: None, "_populate_group")
        ConversationDataDialog._fetch_data(stub)

        names = _names(events)
        assert names.index("_populate_group") < names.index("resolve")
        populate = next(e for e in events if e[0] == "CallAfter" and e[1] == "_populate_group")
        assert populate[2][0]["participants"] == participants

    def test_small_batches_repainting_every_chunk_and_at_the_end(self, events):
        chunk = ConversationDataDialog._LID_RESOLVE_CHUNK
        batch = ConversationDataDialog._LID_RESOLVE_BATCH
        participants = [{"id": _lid(i)} for i in range(chunk * 2 + 1)]
        stub = _stub(_Mw(participants, events), events)
        stub._resolve_participant_lids({"participants": participants})

        resolves = [e[1] for e in events if e[0] == "resolve"]
        assert [len(r) for r in resolves] == [batch] * (2 * chunk // batch) + [1]
        assert [j for r in resolves for j in r] == [_lid(i) for i in range(chunk * 2 + 1)]
        repaint = "_refresh_participant_rows"
        per_chunk = ["resolve"] * (chunk // batch) + [repaint]
        assert _names(events) == per_chunk * 2 + ["resolve", repaint]

    def test_batch_is_small_and_divides_the_repaint_chunk(self):
        """Each resolve_lid_jids_via_api() call ends with a full chat-list
        rebuild, so one JID per call is too many calls; and closing is only
        noticed between calls, so a whole chunk per call is too slow to stop.
        Dividing the chunk keeps the repaint cadence exact."""
        chunk = ConversationDataDialog._LID_RESOLVE_CHUNK
        batch = ConversationDataDialog._LID_RESOLVE_BATCH
        assert 1 < batch < chunk and chunk % batch == 0

    def test_an_exact_multiple_does_not_repaint_twice(self, events):
        chunk = ConversationDataDialog._LID_RESOLVE_CHUNK
        participants = [{"id": _lid(i)} for i in range(chunk)]
        stub = _stub(_Mw(participants, events), events)
        stub._resolve_participant_lids({"participants": participants})
        assert _names(events).count("_refresh_participant_rows") == 1

    def test_nothing_to_resolve_posts_no_repaint(self, events):
        stub = _stub(_Mw([], events), events)
        stub._resolve_participant_lids({"participants": [{"id": "1@s.whatsapp.net"}]})
        assert events == []


class TestClosingStopsTheResolution:
    def test_closed_before_start_calls_nothing(self, events):
        participants = [{"id": _lid(i)} for i in range(3)]
        stub = _stub(_Mw(participants, events), events)
        stub._closed.set()
        stub._resolve_participant_lids({"participants": participants})
        assert events == []

    def test_closed_mid_round_stops_at_the_next_lookup(self, events):
        """Not at the end of the round: that was up to ~15 s of API calls on
        behalf of a dialog nobody has open any more."""
        chunk = ConversationDataDialog._LID_RESOLVE_CHUNK
        participants = [{"id": _lid(i)} for i in range(chunk * 3)]
        mw = _Mw(participants, events)
        stub = _stub(mw, events)
        original = mw.resolve_lid_jids_via_api

        def _resolve_then_close(jids):
            original(jids)
            stub._closed.set()

        mw.resolve_lid_jids_via_api = _resolve_then_close
        stub._resolve_participant_lids({"participants": participants})
        assert [e[0] for e in events] == ["resolve"]


class TestAResolutionFailureKeepsTheList:
    def test_no_empty_repopulate_after_a_failed_resolution(self, events):
        """_fetch_data()'s own handler posts _populate_group({}) — reaching it
        from here would wipe the rows that were just shown."""
        participants = [{"id": _lid(1)}]
        stub = _stub(_Mw(participants, events, fail=True), events)
        ConversationDataDialog._fetch_data(stub)

        posted = [e for e in events if e[0] == "CallAfter"]
        assert len(posted) == 1
        assert posted[0][2][0]["participants"] == participants

    def test_a_bug_before_the_lookups_does_not_wipe_the_list_either(self, events):
        participants = [{"id": _lid(1)}]
        stub = _stub(_Mw(participants, events), events)

        def _broken(data):
            raise AttributeError("bug outside the lookup loop")

        stub._resolve_participant_lids = _broken
        ConversationDataDialog._fetch_data(stub)
        posted = [e for e in events if e[0] == "CallAfter"]
        assert [p[2][0] for p in posted] == [{"subject": "Grupo", "participants": participants}]


class _FakeList:
    def __init__(self, rows):
        self.rows = [list(r) for r in rows]
        self.writes = []

    def GetItemCount(self):
        return len(self.rows)

    def GetItemText(self, idx, col=0):
        return self.rows[idx][col]

    def SetItem(self, idx, col, text):
        self.writes.append((idx, col, text))
        self.rows[idx][col] = text

    def Freeze(self):
        pass

    def Thaw(self):
        pass


class _I18n:
    def t(self, key):
        return {"group_admin_suffix": "administrador"}.get(key, key)


class _RowMw:
    def __init__(self):
        self._lid_to_phone = {}
        self.names = {}
        self.resolve_missing_args = []

    def _resolve_jid_name(self, jid, chat_jid_norm="", *, resolve_missing=True):
        self.resolve_missing_args.append(resolve_missing)
        return self.names.get(jid, "")


def _row_stub(raw_jids, admins, mw):
    stub = types.SimpleNamespace(
        _i18n=_I18n(),
        _mw=mw,
        _jid=GROUP,
        _participant_raw_jids=list(raw_jids),
        _participant_is_admin=list(admins),
        _participant_names=[""] * len(raw_jids),
        _participant_jids=list(raw_jids),
    )
    stub._participant_row = types.MethodType(ConversationDataDialog._participant_row, stub)
    return stub


class TestRefreshRewritesOnlyWhatChanged:
    def _first_fill(self, stub):
        rows = []
        for jid, admin in zip(stub._participant_raw_jids, stub._participant_is_admin):
            label, phone, _name, _nav = stub._participant_row(jid, admin)
            rows.append([label, phone, ""])
        return _FakeList(rows)

    def test_resolved_member_gets_name_phone_and_navigation_jid(self):
        mw = _RowMw()
        stub = _row_stub([_lid(1), _lid(2)], [False, True], mw)
        stub._part_list = self._first_fill(stub)
        assert stub._part_list.rows[0][0] == _lid(1).rsplit("@", 1)[0]

        phone = "5511999990001@s.whatsapp.net"
        mw._lid_to_phone[_lid(1)] = phone
        mw.names[_lid(1)] = "Ana"
        ConversationDataDialog._refresh_participant_rows(stub)

        assert stub._part_list.rows[0][:2] == ["Ana", format_number(phone)]
        assert stub._participant_names[0] == "Ana"
        assert stub._participant_jids[0] == phone
        # The unresolved admin row was not touched at all.
        assert all(w[0] == 0 for w in stub._part_list.writes)

    def test_building_a_row_never_starts_its_own_lookup(self):
        """MainWindow._resolve_jid_name() starts a resolution thread per
        unnamed @lid by default. With rows now built before resolution that
        was one unthrottled thread per unmapped member, and the dialog's own
        throttled loop then skipped them all as in flight — caught in review."""
        mw = _RowMw()
        stub = _row_stub([_lid(1), _lid(2)], [False, False], mw)
        stub._part_list = self._first_fill(stub)
        ConversationDataDialog._refresh_participant_rows(stub)
        assert mw.resolve_missing_args and not any(mw.resolve_missing_args)

    def test_admin_suffix_survives_the_repaint(self):
        mw = _RowMw()
        stub = _row_stub([_lid(1)], [True], mw)
        stub._part_list = self._first_fill(stub)
        mw._lid_to_phone[_lid(1)] = "5511999990001@s.whatsapp.net"
        mw.names[_lid(1)] = "Bia"
        ConversationDataDialog._refresh_participant_rows(stub)
        assert stub._part_list.rows[0][0] == "Bia, administrador"

    def test_nothing_resolved_writes_nothing(self):
        """A rewrite of an unchanged focused row still makes NVDA re-read it."""
        mw = _RowMw()
        stub = _row_stub([_lid(1), "5511999990009@s.whatsapp.net"], [False, False], mw)
        stub._part_list = self._first_fill(stub)
        ConversationDataDialog._refresh_participant_rows(stub)
        assert stub._part_list.writes == []

    def test_list_rebuilt_shorter_meanwhile_does_not_index_past_it(self):
        mw = _RowMw()
        stub = _row_stub([_lid(1), _lid(2)], [False, False], mw)
        stub._part_list = self._first_fill(stub)
        stub._part_list.rows.pop()
        mw.names[_lid(2)] = "Caio"
        ConversationDataDialog._refresh_participant_rows(stub)
        assert stub._part_list.writes == []


class TestThePopulateNoLongerResolves:
    def test_fetch_data_does_not_resolve_before_posting(self):
        import inspect

        src = inspect.getsource(ConversationDataDialog._fetch_data)
        group_branch = src[src.index("if self._is_group:"):src.index("else:")]
        assert group_branch.index("wx.CallAfter(self._populate_group, data)") < \
            group_branch.index("self._resolve_participant_lids(data)")
        assert "resolve_lid_jids_via_api" not in group_branch

    def test_the_dialog_marks_itself_closed_on_destroy(self):
        import inspect

        assert "EVT_WINDOW_DESTROY" in inspect.getsource(ConversationDataDialog.__init__)
        assert "self._closed.set()" in inspect.getsource(
            ConversationDataDialog._on_window_destroy
        )

    def test_destroy_marks_it_closed_before_destroying(self):
        """A top-level Destroy() is deferred to idle, so the destroy event
        alone let a whole further chunk of API calls run after closing —
        measured with the real dialog in CI. (super() needs a real instance,
        hence source order rather than a call on a stub.)"""
        import inspect

        src = inspect.getsource(ConversationDataDialog.Destroy)
        assert src.index("self._closed.set()") < src.index("super().Destroy()")
