"""Tests for repainting only the rows a resolved batch can have changed.

The message window stopped having a ceiling when it started preserving the
history the user pulls in with Home (see
tests/test_history_window_preservation.py), so a conversation can legitimately
sit at thousands of rows. Every one of them used to be re-rendered by
``refresh_active_conversation_messages()`` once per resolved batch — and the
three background loops that trigger it ([Contact Resolution], [Mentions Scan]
and [LID Resolution]) deliver batches continuously for minutes on a fresh
account, while each batch typically renames a single person. The scheduler was
a signal with no payload even though those loops know exactly which JIDs they
just resolved.

Two halves are pinned here, and the second one is where the accessibility risk
sits:

* ``MainWindow._schedule_refresh_active_messages(jids)`` accumulates the JIDs
  across every batch inside one debounce window, drains them when it fires, and
  keeps ``None``/empty meaning "I don't know what changed, repaint everything".

* ``ConversationsPanel.refresh_active_conversation_messages(jids=...)`` repaints
  a row when *any* JID the row renders a name for — sender, quoted sender,
  mentions in the body, mentions in the quoted preview — matches, compared
  across the @lid <-> phone bridge rather than as raw strings. A row wrongly
  left out does not merely look stale: ``_get_participant_name()`` falls back to
  ``participant_jid.rsplit("@", 1)[0]``, i.e. the screen reader reads raw @lid
  or phone digits, forever. So anything the selective path cannot address
  (a matching row carrying no ``key.id``) must fall back to the full repaint.

MainWindow is a wx.Frame and ConversationsPanel a wx.Panel, so both are driven
as unbound methods against small stubs — same approach as
tests/test_refresh_messages_debounce.py and
tests/test_history_window_preservation.py.
"""

import json
import os

import pytest

from main import MainWindow
from ui.conversations import ConversationsPanel

PHONE = "5511900000001@s.whatsapp.net"
LID = "111122223333@lid"
OTHER = "5511900000002@s.whatsapp.net"


# ── Half 1: the scheduler's JID accumulator ─────────────────────────────────


class _Panel:
    def __init__(self, conversation=None, on_repaint=None):
        self.conversation = conversation
        self.repaint_jids = []
        self._sorted_messages = []
        self._on_repaint = on_repaint

    def refresh_active_conversation_messages(self, jids=None):
        self.repaint_jids.append(None if jids is None else set(jids))
        if self._on_repaint is not None:
            self._on_repaint()
        return 0


class _MainStub:
    _schedule_refresh_active_messages = MainWindow._schedule_refresh_active_messages
    _do_scheduled_refresh_active_messages = MainWindow._do_scheduled_refresh_active_messages
    _ACTIVE_REFRESH_DEBOUNCE_MS = MainWindow._ACTIVE_REFRESH_DEBOUNCE_MS

    def __init__(self, panel=None):
        self.conversations_panel = panel


@pytest.fixture
def later(monkeypatch):
    """Capture wx.CallLater instead of needing a running event loop."""
    calls = []
    monkeypatch.setattr("main.wx.CallLater",
                        lambda delay, fn, *a, **kw: calls.append((delay, fn, a, kw)))
    # Inline, unlike CallLater: the failure path reschedules through CallAfter
    # and the retry it queues is exactly what has to be observable here.
    monkeypatch.setattr("main.wx.CallAfter", lambda fn, *a, **kw: fn(*a, **kw))
    return calls


def _fire(later_calls):
    """Run everything wx.CallLater has queued, as the event loop would."""
    pending, later_calls[:] = list(later_calls), []
    for _delay, fn, a, kw in pending:
        fn(*a, **kw)


class TestScheduledJidAccumulation:
    def test_batches_inside_one_window_are_unioned(self, later):
        panel = _Panel(conversation={"remoteJid": "g@g.us"})
        s = _MainStub(panel)
        s._schedule_refresh_active_messages({PHONE})
        s._schedule_refresh_active_messages({OTHER})
        s._schedule_refresh_active_messages([LID])
        assert len(later) == 1          # still one timer, as before
        _fire(later)
        assert panel.repaint_jids == [{PHONE, OTHER, LID}]

    def test_no_jids_still_repaints_everything(self, later):
        """The safe default every caller that cannot tell keeps using."""
        panel = _Panel(conversation={"remoteJid": "g@g.us"})
        s = _MainStub(panel)
        s._schedule_refresh_active_messages()
        _fire(later)
        assert panel.repaint_jids == [None]

    def test_an_empty_collection_means_the_same_as_none(self, later):
        panel = _Panel(conversation={"remoteJid": "g@g.us"})
        s = _MainStub(panel)
        s._schedule_refresh_active_messages(set())
        _fire(later)
        assert panel.repaint_jids == [None]

    def test_one_unknown_batch_widens_the_whole_window(self, later):
        """A caller that cannot name what it changed must not have its request
        narrowed by a later batch that can — the union of "everything" and a
        JID set is still everything."""
        panel = _Panel(conversation={"remoteJid": "g@g.us"})
        s = _MainStub(panel)
        s._schedule_refresh_active_messages({PHONE})
        s._schedule_refresh_active_messages()
        s._schedule_refresh_active_messages({OTHER})
        _fire(later)
        assert panel.repaint_jids == [None]

    def test_blank_entries_are_dropped_and_the_batch_degrades_to_full(self, later):
        """Filtering every JID out leaves us knowing nothing again."""
        panel = _Panel(conversation={"remoteJid": "g@g.us"})
        s = _MainStub(panel)
        s._schedule_refresh_active_messages(["", None, 7])
        _fire(later)
        assert panel.repaint_jids == [None]

    def test_the_accumulator_is_drained_between_windows(self, later):
        panel = _Panel(conversation={"remoteJid": "g@g.us"})
        s = _MainStub(panel)
        s._schedule_refresh_active_messages({PHONE})
        _fire(later)
        s._schedule_refresh_active_messages({OTHER})
        _fire(later)
        assert panel.repaint_jids == [{PHONE}, {OTHER}]

    def test_a_batch_arriving_during_the_repaint_is_not_lost(self, later):
        """The accumulator and the pending flag are both cleared before the
        render runs, so a batch landing mid-repaint schedules its own round
        instead of being silently swallowed by the one already in flight."""
        s = _MainStub()
        panel = _Panel(
            conversation={"remoteJid": "g@g.us"},
            on_repaint=lambda: s._schedule_refresh_active_messages({OTHER}),
        )
        s.conversations_panel = panel
        s._schedule_refresh_active_messages({PHONE})
        _fire(later)
        assert panel.repaint_jids == [{PHONE}]
        assert len(later) == 1          # the mid-repaint batch queued its own
        _fire(later)
        assert panel.repaint_jids == [{PHONE}, {OTHER}]

    def test_a_batch_arriving_during_the_repaint_is_not_repainted_twice(self, later):
        """Its JID must land in the *next* round only — folding it back into
        the running one would repaint it here and again a second later."""
        s = _MainStub()
        panel = _Panel(
            conversation={"remoteJid": "g@g.us"},
            on_repaint=lambda: s._schedule_refresh_active_messages({OTHER}),
        )
        s.conversations_panel = panel
        s._schedule_refresh_active_messages({PHONE})
        _fire(later)
        _fire(later)
        assert panel.repaint_jids[1] == {OTHER}

    def test_a_raising_repaint_degrades_to_a_full_one_and_retries(self, later):
        """The batch was drained before the call, so a failure would otherwise
        lose those JIDs for good: the next batch is scoped to its own, and the
        rows this round was meant to fix stay on raw digits forever. Before the
        repaint became scoped this was harmless — the next one was always full.
        _render_message_line() has raised in production, which is why the
        try/except is there at all."""
        calls = []

        def _boom(jids=None):
            calls.append(None if jids is None else set(jids))
            if len(calls) == 1:
                raise RuntimeError("render failed")
            return 0

        panel = _Panel(conversation={"remoteJid": "g@g.us"})
        panel.refresh_active_conversation_messages = _boom
        s = _MainStub(panel)
        s._schedule_refresh_active_messages({PHONE})
        _fire(later)                       # raises, degrades, reschedules
        assert calls == [{PHONE}]
        assert len(later) == 1
        _fire(later)
        assert calls == [{PHONE}, None], "the retry must repaint everything"

    def test_a_deterministic_failure_stops_after_one_retry(self, later):
        """The retry is bounded on purpose. A malformed record makes the render
        raise every single time, and rescheduling unconditionally turns that
        into a permanent 1 Hz Freeze/SetItemText/Thaw cycle on messages_list —
        the very accessibility-event flood the Freeze() exists to prevent, now
        for the rest of the session — plus log.log growing without bound on one
        repeated traceback, in the file this project diagnoses everything from.
        """
        calls = []

        def _always_boom(jids=None):
            calls.append(None if jids is None else set(jids))
            raise RuntimeError("malformed record")

        panel = _Panel(conversation={"remoteJid": "g@g.us"})
        panel.refresh_active_conversation_messages = _always_boom
        s = _MainStub(panel)
        s._schedule_refresh_active_messages({PHONE})
        for _ in range(50):
            _fire(later)
        assert calls == [{PHONE}, None], "one scoped attempt, then one full retry"
        assert later == [], "nothing left queued — the loop must not be endless"

    def test_the_retry_budget_is_restored_by_a_successful_repaint(self):
        """Otherwise a single transient failure early in the session would
        leave every later one un-retried for good."""
        panel = _Panel(conversation={"remoteJid": "g@g.us"})
        s = _MainStub(panel)
        s._refresh_active_failed_once = True
        s._do_scheduled_refresh_active_messages()
        assert s._refresh_active_failed_once is False

    def test_a_failure_after_a_success_still_gets_its_retry(self, later):
        outcomes = iter([None, RuntimeError("render failed"), None])

        def _flaky(jids=None):
            outcome = next(outcomes, None)
            if isinstance(outcome, Exception):
                raise outcome
            return 0

        panel = _Panel(conversation={"remoteJid": "g@g.us"})
        panel.refresh_active_conversation_messages = _flaky
        s = _MainStub(panel)
        s._schedule_refresh_active_messages({PHONE})
        _fire(later)                       # succeeds, clears the budget
        s._schedule_refresh_active_messages({OTHER})
        _fire(later)                       # raises, must still reschedule
        assert len(later) == 1

    def test_a_batch_after_a_failure_is_still_widened_to_full(self, later):
        """The degraded request is sticky, so a batch landing before the retry
        runs cannot narrow it back down and re-lose the failed rows."""
        def _boom(jids=None):
            raise RuntimeError("render failed")

        panel = _Panel(conversation={"remoteJid": "g@g.us"})
        panel.refresh_active_conversation_messages = _boom
        s = _MainStub(panel)
        s._schedule_refresh_active_messages({PHONE})
        _fire(later)
        panel.refresh_active_conversation_messages = _Panel.refresh_active_conversation_messages.__get__(panel)
        s._schedule_refresh_active_messages({OTHER})
        _fire(later)
        assert panel.repaint_jids == [None]


# ── Half 2: which rows the panel actually repaints ──────────────────────────


class _FakeList:
    """Just enough wx.ListCtrl to record what was re-rendered."""

    def __init__(self, item_count=0):
        self.painted = []
        self.frozen = 0
        self.thawed = 0
        # O caminho seletivo escreve por indice, entao ele confere se a lista
        # esta em passo com o controle antes de enderecar linha nenhuma - como
        # _repaint_message_rows() ja fazia. Um fake sem contagem deixaria essa
        # guarda sem cobertura.
        self.item_count = item_count

    def GetItemCount(self):
        return self.item_count

    def Freeze(self):
        self.frozen += 1

    def Thaw(self):
        self.thawed += 1

    def SetItemText(self, idx, text):
        self.painted.append(idx)


class _FakeMainWindow:
    """MainWindow's real JID normalization/bridging, nothing else.

    Bound rather than reimplemented on purpose: comparing raw JID strings is
    the classic bug in this repository, and a hand-rolled equivalence in the
    test would happily pass while the product code kept missing @lid rows.
    """

    _normalize_jid = staticmethod(MainWindow._normalize_jid)
    _jid_address_forms = MainWindow._jid_address_forms

    def __init__(self, lid_to_phone=None):
        self._lid_to_phone = dict(lid_to_phone or {})
        self._phone_to_lid = {p: l for l, p in self._lid_to_phone.items()}


class _PanelStub:
    """Minimal stand-in for ConversationsPanel for the selective repaint."""

    # Real implementations under test.
    refresh_active_conversation_messages = ConversationsPanel.refresh_active_conversation_messages
    _message_ids_touching_jids = ConversationsPanel._message_ids_touching_jids
    _row_jids = ConversationsPanel._row_jids
    _set_message_row_texts = ConversationsPanel._set_message_row_texts
    _is_separator = ConversationsPanel._is_separator
    _get_context_info = ConversationsPanel._get_context_info
    _raw_mentioned_jids = staticmethod(ConversationsPanel._raw_mentioned_jids)

    def __init__(self, messages, main_window=None):
        self.conversation = {"remoteJid": "group-1@g.us"}
        self.main_window = main_window if main_window is not None else _FakeMainWindow()
        self._sorted_messages = list(messages)
        self.messages_list = _FakeList(len(self._sorted_messages))

    def _render_message_line(self, msg, index=None, total=None):
        return f"row {index}"


def _msg(mid, participant=None, **extra):
    msg = {
        "key": {"id": mid, "fromMe": False, "remoteJid": "group-1@g.us"},
        "messageType": "conversation",
        "message": {"conversation": "oi"},
    }
    if participant is not None:
        msg["key"]["participant"] = participant
    msg.update(extra)
    return msg


def _mention_msg(mid, participant, mentioned):
    return {
        "key": {"id": mid, "fromMe": False, "remoteJid": "group-1@g.us",
                "participant": participant},
        "messageType": "extendedTextMessage",
        "message": {"extendedTextMessage": {
            "text": "oi @5511900000001",
            "contextInfo": {"mentionedJid": list(mentioned)},
        }},
    }


class TestSelectiveRepaint:
    def test_only_the_resolved_senders_row_is_repainted(self):
        panel = _PanelStub([
            _msg("a", participant=PHONE),
            _msg("b", participant=OTHER),
            _msg("c", participant=OTHER),
        ])
        painted = panel.refresh_active_conversation_messages(jids={PHONE})
        assert panel.messages_list.painted == [0]
        assert painted == 1

    def test_no_jids_repaints_every_row(self):
        panel = _PanelStub([
            _msg("a", participant=PHONE),
            _msg("b", participant=OTHER),
        ])
        assert panel.refresh_active_conversation_messages() == 2
        assert panel.messages_list.painted == [0, 1]

    def test_the_batch_freeze_is_kept_on_both_paths(self):
        """One accessibility event instead of one per row — the flood is what
        made the reader unusable during a resolution storm."""
        panel = _PanelStub([_msg("a", participant=PHONE)])
        panel.refresh_active_conversation_messages(jids={PHONE})
        panel.refresh_active_conversation_messages()
        assert panel.messages_list.frozen == panel.messages_list.thawed == 2

    def test_separators_are_never_addressed(self):
        panel = _PanelStub([
            {"_type": "unread_separator", "count": 2},
            _msg("a", participant=PHONE),
        ])
        panel.refresh_active_conversation_messages(jids={PHONE})
        assert panel.messages_list.painted == [1]


class TestOneBadRowDoesNotStopTheRest:
    """Predates the scoped repaint and is what turns one malformed record into
    a whole conversation that stops being repainted: the full pass used to die
    on the first row that raised, so every row after it kept its old text —
    and on a name-resolution repaint "old text" means the screen reader goes on
    reading raw @lid or phone digits."""

    def _panel_with_a_bad_row(self):
        panel = _PanelStub([_msg("a", participant=PHONE),
                            _msg("bad", participant=OTHER),
                            _msg("c", participant=OTHER)])
        real = panel._render_message_line

        def _render(msg, index=None, total=None):
            if (msg.get("key") or {}).get("id") == "bad":
                raise ValueError("malformed record")
            return real(msg, index=index, total=total)

        panel._render_message_line = _render
        return panel

    def test_the_rows_after_it_are_still_painted(self):
        panel = self._panel_with_a_bad_row()
        assert panel.refresh_active_conversation_messages() == 2
        assert panel.messages_list.painted == [0, 2]

    def test_the_list_is_still_thawed(self):
        """A Freeze() left standing is a dead UI, so the finally has to survive
        the row that raised as much as the pass does."""
        panel = self._panel_with_a_bad_row()
        panel.refresh_active_conversation_messages()
        assert panel.messages_list.frozen == panel.messages_list.thawed == 1


class TestJidEquivalence:
    """A row carrying the @lid while the loop reported the phone JID (or the
    other way round) is the whole reason this cannot be a string comparison:
    missing it leaves the reader announcing raw digits for the session."""

    def test_a_lid_row_matches_a_resolved_phone_jid(self):
        mw = _FakeMainWindow(lid_to_phone={LID: PHONE})
        panel = _PanelStub([_msg("a", participant=LID),
                            _msg("b", participant=OTHER)], main_window=mw)
        panel.refresh_active_conversation_messages(jids={PHONE})
        assert panel.messages_list.painted == [0]

    def test_a_phone_row_matches_a_resolved_lid(self):
        mw = _FakeMainWindow(lid_to_phone={LID: PHONE})
        panel = _PanelStub([_msg("a", participant=PHONE),
                            _msg("b", participant=OTHER)], main_window=mw)
        panel.refresh_active_conversation_messages(jids={LID})
        assert panel.messages_list.painted == [0]

    def test_the_legacy_c_us_form_matches_too(self):
        panel = _PanelStub([_msg("a", participant="5511900000001@c.us"),
                            _msg("b", participant=OTHER)])
        panel.refresh_active_conversation_messages(jids={PHONE})
        assert panel.messages_list.painted == [0]

    def test_a_baileys_device_suffix_still_matches(self):
        panel = _PanelStub([_msg("a", participant="5511900000001:60@s.whatsapp.net"),
                            _msg("b", participant=OTHER)])
        panel.refresh_active_conversation_messages(jids={PHONE})
        assert panel.messages_list.painted == [0]

    def test_an_unbridged_lid_does_not_drag_in_unrelated_rows(self):
        panel = _PanelStub([_msg("a", participant=LID),
                            _msg("b", participant=OTHER)])
        panel.refresh_active_conversation_messages(jids={PHONE})
        assert panel.messages_list.painted == []


class TestRowsThatNameSomeoneElse:
    """_render_message_line() resolves three more names besides the sender, and
    each is a way a row changes without its own sender changing."""

    def test_a_mention_of_the_resolved_jid_is_repainted(self):
        panel = _PanelStub([
            _mention_msg("a", OTHER, [PHONE]),
            _msg("b", participant=OTHER),
        ])
        panel.refresh_active_conversation_messages(jids={PHONE})
        assert panel.messages_list.painted == [0]

    def test_a_mention_carried_as_a_lid_matches_the_phone_jid(self):
        mw = _FakeMainWindow(lid_to_phone={LID: PHONE})
        panel = _PanelStub([_mention_msg("a", OTHER, [LID])], main_window=mw)
        panel.refresh_active_conversation_messages(jids={PHONE})
        assert panel.messages_list.painted == [0]

    def test_the_mentionedjidlist_spelling_is_covered(self):
        """_get_message_content() accepts both spellings; _raw_mentioned_jids()
        only reads mentionedJid, so the list form needs its own collection."""
        msg = _mention_msg("a", OTHER, [])
        msg["message"]["extendedTextMessage"]["contextInfo"] = {
            "mentionedJidList": [PHONE]
        }
        panel = _PanelStub([msg])
        panel.refresh_active_conversation_messages(jids={PHONE})
        assert panel.messages_list.painted == [0]

    def test_the_quoted_senders_row_is_repainted(self):
        reply = _msg("a", participant=OTHER)
        reply["contextInfo"] = {"participant": PHONE,
                                "quotedMessage": {"conversation": "oi"}}
        panel = _PanelStub([reply, _msg("b", participant=OTHER)])
        panel.refresh_active_conversation_messages(jids={PHONE})
        assert panel.messages_list.painted == [0]

    def test_a_quote_without_a_participant_follows_its_stanza_id(self):
        """_get_quoted_sender() resolves the quoted message's own recorded
        sender in that case, so the reply's text depends on a *different*
        row's participant."""
        quoted = _msg("q1", participant=PHONE)
        reply = _msg("a", participant=OTHER)
        reply["contextInfo"] = {"stanzaId": "q1",
                                "quotedMessage": {"conversation": "oi"}}
        panel = _PanelStub([quoted, reply, _msg("b", participant=OTHER)])
        panel.refresh_active_conversation_messages(jids={PHONE})
        assert panel.messages_list.painted == [0, 1]

    def test_a_mention_inside_the_quoted_preview_is_repainted(self):
        reply = _msg("a", participant=OTHER)
        reply["contextInfo"] = {
            "participant": OTHER,
            "quotedMessage": {"conversation": "oi @5511900000001",
                              "mentionedJid": [PHONE]},
        }
        panel = _PanelStub([reply, _msg("b", participant=OTHER)])
        panel.refresh_active_conversation_messages(jids={PHONE})
        assert panel.messages_list.painted == [0]


class TestFallbackToTheFullRepaint:
    """Never leave a row stale: anything the selective path cannot address
    goes back through the full pass."""

    def test_a_matching_row_without_a_message_id_forces_a_full_repaint(self):
        """_set_message_row_texts() addresses rows by key.id, so a matching row
        that carries none simply cannot be reached — repaint everything rather
        than skip it. (Virtual pending sends are *not* the case in point: they
        do get a key.id, the _local_id uuid. This guards the malformed/legacy
        record, where the shape is whatever an older build wrote to disk.)"""
        idless = _mention_msg("", OTHER, [PHONE])
        panel = _PanelStub([_msg("a", participant=OTHER), idless,
                            _msg("b", participant=OTHER)])
        assert panel._message_ids_touching_jids({PHONE}) is None
        panel.refresh_active_conversation_messages(jids={PHONE})
        assert panel.messages_list.painted == [0, 1, 2]

    def test_a_non_matching_row_without_an_id_does_not_force_it(self):
        panel = _PanelStub([_msg("", participant=OTHER),
                            _msg("b", participant=PHONE)])
        panel.refresh_active_conversation_messages(jids={PHONE})
        assert panel.messages_list.painted == [1]

    def test_jids_that_normalize_to_nothing_repaint_everything(self):
        panel = _PanelStub([_msg("a", participant=PHONE),
                            _msg("b", participant=OTHER)])
        assert panel._message_ids_touching_jids(["", None]) is None

    def test_no_conversation_open_is_a_noop(self):
        panel = _PanelStub([_msg("a", participant=PHONE)])
        panel.conversation = None
        assert panel.refresh_active_conversation_messages(jids={PHONE}) == 0
        assert panel.messages_list.painted == []


class TestGroupNotifications:
    """A group notification renders its author AND its recipients through
    _get_participant_name(), and the recipients appear nowhere else in the
    message — key.participant only mirrors the author, by way of
    WebSocketClient copying it there.

    Missing them closed the worst possible loop: the "X entrou no grupo" row
    with an unbridged @lid recipient falls back to raw digits,
    _get_participant_name() itself fires resolve_lid_jids_via_api() to fix that
    very row, the resolution schedules a scoped repaint — and the row that
    started it was the only one not in it.
    """

    def _notif(self, mid, author, recipients):
        return {
            "key": {"id": mid, "fromMe": False, "remoteJid": "group-1@g.us",
                    "participant": author},
            "messageType": "groupNotification",
            "message": {"groupNotification": {"subtype": "add", "author": author,
                                              "recipients": list(recipients)}},
        }

    def test_a_recipient_is_repainted(self):
        panel = _PanelStub([self._notif("n1", OTHER, [LID]),
                            _msg("b", participant=OTHER)])
        panel.refresh_active_conversation_messages(jids={LID})
        assert panel.messages_list.painted == [0]

    def test_a_recipient_matches_across_the_lid_bridge(self):
        mw = _FakeMainWindow(lid_to_phone={LID: PHONE})
        panel = _PanelStub([self._notif("n1", OTHER, [LID])], main_window=mw)
        panel.refresh_active_conversation_messages(jids={PHONE})
        assert panel.messages_list.painted == [0]

    def test_the_author_is_repainted(self):
        panel = _PanelStub([self._notif("n1", PHONE, [OTHER])])
        panel.refresh_active_conversation_messages(jids={PHONE})
        assert panel.messages_list.painted == [0]

    def test_a_legacy_wid_dict_on_disk_is_tolerated(self):
        """Records written before WebSocketClient normalised these to strings
        still hold a raw WPPConnect Wid dict — _get_message_content() guards
        for it, and so must this."""
        notif = self._notif("n1", OTHER, [])
        notif["message"]["groupNotification"]["recipients"] = [{"_serialized": LID}]
        notif["message"]["groupNotification"]["author"] = {"_serialized": OTHER}
        panel = _PanelStub([notif])
        panel.refresh_active_conversation_messages(jids={LID})
        assert panel.messages_list.painted == [0]

    def test_an_unrelated_notification_is_left_alone(self):
        panel = _PanelStub([self._notif("n1", OTHER, [OTHER])])
        panel.refresh_active_conversation_messages(jids={PHONE})
        assert panel.messages_list.painted == []


# ── The acceptance criterion, differentially ────────────────────────────────


def _translations():
    """The real pt-BR strings: an i18n stub returning the bare key would drop
    every {name}/{author} placeholder, and with the names gone from the
    rendered line the differential below would see no change and pass on
    everything."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "client", "languages", "pt-BR.json")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


class _I18n:
    def __init__(self):
        self._t = _translations()

    def t(self, key):
        return self._t.get(key, key)


class _RenderMainWindow(_FakeMainWindow):
    """Everything the *real* renderer reaches for while resolving a name."""

    _is_bad_contact_name = staticmethod(MainWindow._is_bad_contact_name)

    def __init__(self, lid_to_phone=None):
        super().__init__(lid_to_phone=lid_to_phone)
        self.i18n = _I18n()
        self.app_name = "WinZapp"
        self.contacts = {}
        self.chats = {}
        self._presence_pushname_map = {}
        self.my_jid = "5511900000099@s.whatsapp.net"
        self.settings = {"user_interface": {}, "speech_content": {}}

    def get_chat(self, jid):
        return self.chats.get(jid)

    def _is_self_jid(self, jid):
        return self._normalize_jid(jid or "") == self.my_jid

    def self_reference_label(self):
        return "Eu"

    # The real one: the 1:1 branches of _get_quoted_sender() resolve the
    # other party through it, so a stub returning chat["name"] would make
    # those branches insensitive to exactly the caches the resolution loops
    # write to, and the 1:1 scenario below would pass vacuously.
    _resolve_contact_name = MainWindow._resolve_contact_name
    _get_contact_tolerant = MainWindow._get_contact_tolerant
    # Tried only after _resolve_contact_name(), and it memoises per chat — the
    # real behaviour, and harmless here because the conversation carries no
    # records for it to find a pushName in.
    find_name_through_messages = MainWindow.find_name_through_messages

    def register_jid_mapping(self, lid_jid, phone_jid, save=True, defer_ui=False):
        self._lid_to_phone[lid_jid] = phone_jid
        self._phone_to_lid[phone_jid] = lid_jid

    def resolve_lid_jids_via_api(self, jids):
        pass


class _RenderPanel(_PanelStub):
    """_PanelStub with the real _render_message_line() and every name-resolving
    helper it calls left real. Only the parts orthogonal to name resolution —
    the clock, the delivery status, reactions — are stubbed out, so any change
    a rendered line shows is a name change by construction."""

    _render_message_line = ConversationsPanel._render_message_line
    _render_separator = ConversationsPanel._render_separator
    _get_message_content = ConversationsPanel._get_message_content
    _sender_label = ConversationsPanel._sender_label
    _get_quoted_sender = ConversationsPanel._get_quoted_sender
    _get_quoted_preview = ConversationsPanel._get_quoted_preview
    _resolve_mentions_in_text = ConversationsPanel._resolve_mentions_in_text
    _get_participant_name = ConversationsPanel._get_participant_name
    _is_system_event = staticmethod(ConversationsPanel._is_system_event)
    _is_message_forwarded = ConversationsPanel._is_message_forwarded
    _extract_timestamp = ConversationsPanel._extract_timestamp

    def __init__(self, messages, main_window=None):
        super().__init__(messages, main_window=main_window or _RenderMainWindow())
        self.selected_messages = set()
        self._media_upload_progress = {}
        self._download_progress = {}
        self._message_list_mode = "classic"
        self._group_participants_cache = []

    def _format_date(self, ts):
        return ""

    def _map_status(self, msg):
        return ""

    def _reaction_counts(self, msg_id):
        return {}

    def _render_all(self):
        total = len(self._sorted_messages)
        return {
            m["key"]["id"]: self._render_message_line(m, index=i, total=total)
            for i, m in enumerate(self._sorted_messages)
        }


def _rows_the_full_pass_would_change(panel, mutate):
    """Render everything, apply *mutate* to the name caches, render again, and
    report the ids whose text actually moved."""
    before = panel._render_all()
    mutate(panel.main_window)
    after = panel._render_all()
    return {mid for mid, text in after.items() if before[mid] != text}


class TestTheSelectiveSetCoversWhateverTheFullPassWouldChange:
    """The acceptance criterion, stated as a property instead of a hand-written
    row list.

    An earlier version of this test asserted a literal [0, 1, 2, 3] — which is
    how the groupNotification recipients hole shipped green: the row simply was
    not in the list anyone thought to write down. Rendering for real, moving a
    name, and re-rendering finds every JID source _render_message_line() has,
    including ones added later that nobody remembers to mirror into
    _row_jids().
    """

    def _rows(self):
        quoted = _msg("q1", participant=LID)
        reply_by_stanza = _msg("r1", participant=OTHER)
        reply_by_stanza["contextInfo"] = {"stanzaId": "q1",
                                          "quotedMessage": {"conversation": "oi"}}
        reply_by_participant = _msg("r2", participant=OTHER)
        reply_by_participant["contextInfo"] = {
            "participant": PHONE,
            "quotedMessage": {"conversation": "oi"},
        }
        quoted_mention = _msg("r3", participant=OTHER)
        quoted_mention["contextInfo"] = {
            "participant": OTHER,
            "quotedMessage": {"conversation": "oi @5511900000001",
                              "mentionedJid": [PHONE]},
        }
        notif = {
            "key": {"id": "n1", "fromMe": False, "remoteJid": "group-1@g.us",
                    "participant": OTHER},
            "messageType": "groupNotification",
            "message": {"groupNotification": {"subtype": "add", "author": OTHER,
                                              "recipients": [LID]}},
        }
        return [
            quoted,
            reply_by_stanza,
            reply_by_participant,
            quoted_mention,
            _mention_msg("m1", OTHER, [LID]),
            notif,
            _msg("u1", participant=OTHER),
            _msg("u2", participant="5511900000009@s.whatsapp.net"),
        ]

    def _assert_covers(self, panel, jids, mutate):
        changed = _rows_the_full_pass_would_change(panel, mutate)
        assert changed, "the scenario must actually change something to be a test"
        selected = panel._message_ids_touching_jids(jids)
        if selected is None:
            return                        # degraded to the full pass: covered
        missing = changed - selected
        assert not missing, (
            f"rows {sorted(missing)} change when {sorted(jids)} resolves but "
            f"would not be repainted — the screen reader keeps reading raw "
            f"digits on them")

    def test_a_name_learned_under_the_phone_jid_reaches_every_row(self):
        mw = _RenderMainWindow(lid_to_phone={LID: PHONE})
        panel = _RenderPanel(self._rows(), main_window=mw)
        self._assert_covers(
            panel, {PHONE},
            lambda m: m.contacts.__setitem__(PHONE, {"name": "Fulano"}))

    def test_a_name_learned_under_the_lid_reaches_every_row(self):
        mw = _RenderMainWindow(lid_to_phone={LID: PHONE})
        panel = _RenderPanel(self._rows(), main_window=mw)
        self._assert_covers(
            panel, {LID},
            lambda m: m.contacts.__setitem__(LID, {"name": "Fulano"}))

    def test_a_pushname_learned_from_presence_reaches_every_row(self):
        """_learn_sender_name() writes here, and it is what the mentions scan's
        bulk pass fills — the case that now forces the full repaint."""
        mw = _RenderMainWindow(lid_to_phone={LID: PHONE})
        panel = _RenderPanel(self._rows(), main_window=mw)
        self._assert_covers(
            panel, {PHONE},
            lambda m: m._presence_pushname_map.__setitem__(PHONE, "Fulano"))

    def test_a_bridge_learned_later_reaches_every_row(self):
        """No name at all, just the @lid -> phone mapping: the rows switch from
        raw @lid digits to a formatted phone number, which is a change of its
        own and the most common one on a fresh account."""
        mw = _RenderMainWindow()
        panel = _RenderPanel(self._rows(), main_window=mw)

        def _bridge(m):
            m._lid_to_phone[LID] = PHONE
            m._phone_to_lid[PHONE] = LID

        self._assert_covers(panel, {LID}, _bridge)

    def test_the_selective_set_is_not_simply_everything(self):
        """Guards the other direction: a property test that repainted every row
        would pass vacuously and buy nothing over the old full pass."""
        mw = _RenderMainWindow(lid_to_phone={LID: PHONE})
        panel = _RenderPanel(self._rows(), main_window=mw)
        selected = panel._message_ids_touching_jids({PHONE})
        assert selected is not None
        assert "u1" not in selected and "u2" not in selected


class TestTheSameCriterionInAOneToOneChat:
    """The group scenarios never reach _get_quoted_sender()'s 1:1 branches
    (a reply with no contextInfo participant, resolved from the conversation
    itself rather than from a participant JID), nor a fromMe row.

    In a 1:1 chat every row carries the same key.remoteJid, so the selective
    set naturally widens to the whole list — this pins that it really does,
    rather than leaving the branch untested on the assumption that it must.
    """

    def _rows(self):
        incoming = _msg("i1", participant=None)
        incoming["key"]["remoteJid"] = PHONE
        outgoing = _msg("o1", participant=None)
        outgoing["key"]["remoteJid"] = PHONE
        outgoing["key"]["fromMe"] = True
        reply_to_them = _msg("r1", participant=None)
        reply_to_them["key"]["remoteJid"] = PHONE
        reply_to_them["contextInfo"] = {"_quotedFromMe": False,
                                        "quotedMessage": {"conversation": "oi"}}
        reply_to_me = _msg("r2", participant=None)
        reply_to_me["key"]["remoteJid"] = PHONE
        reply_to_me["contextInfo"] = {"_quotedFromMe": True,
                                      "quotedMessage": {"conversation": "oi"}}
        reply_by_stanza = _msg("r3", participant=None)
        reply_by_stanza["key"]["remoteJid"] = PHONE
        reply_by_stanza["contextInfo"] = {"stanzaId": "i1",
                                          "quotedMessage": {"conversation": "oi"}}
        return [incoming, outgoing, reply_to_them, reply_to_me, reply_by_stanza]

    def _panel(self):
        mw = _RenderMainWindow()
        panel = _RenderPanel(self._rows(), main_window=mw)
        panel.conversation = {"remoteJid": PHONE}
        return panel

    def test_every_changed_row_is_in_the_selective_set(self):
        panel = self._panel()
        changed = _rows_the_full_pass_would_change(
            panel, lambda m: m.contacts.__setitem__(PHONE, {"name": "Fulano"}))
        assert changed, "the scenario must actually change something"
        selected = panel._message_ids_touching_jids({PHONE})
        assert selected is None or not (changed - selected)

    def test_the_outgoing_row_is_covered_like_the_rest(self):
        """A fromMe row renders "Eu" as its sender, but its quoted preview and
        mentions still name the other party, so it must not be excluded on the
        strength of its sender alone."""
        panel = self._panel()
        selected = panel._message_ids_touching_jids({PHONE})
        assert selected is None or "o1" in selected


class TestAOneToOneChatWhoseRowsCarryTheLid:
    """O caso que a classe acima não alcança: as linhas sob a forma @lid e a
    conversa sob o telefone, sem bridge entre as duas ainda.

    _sender_label() e os dois ramos 1:1 de _get_quoted_sender() resolvem o nome
    a partir de self.conversation, e não de nenhum JID que esteja na mensagem.
    Coletando só os JIDs da mensagem, o conjunto seletivo não cruza com o JID
    de telefone que o laço de resolução reporta, e a linha muda sem ser
    repintada — o leitor de tela continua lendo o número cru. Por isso
    _row_jids() coleta também o JID da conversa.

    Sem bridge de propósito: é justamente o estado em que as duas formas não se
    encontram por normalização nenhuma.
    """

    def _panel(self):
        rows = []
        incoming = _msg("i1", participant=None)
        incoming["key"]["remoteJid"] = LID
        rows.append(incoming)
        reply = _msg("r1", participant=None)
        reply["key"]["remoteJid"] = LID
        reply["contextInfo"] = {"_quotedFromMe": False,
                                "quotedMessage": {"conversation": "oi"}}
        rows.append(reply)
        mw = _RenderMainWindow()  # sem _lid_to_phone
        panel = _RenderPanel(rows, main_window=mw)
        panel.conversation = {"remoteJid": PHONE}
        return panel

    def test_every_changed_row_is_in_the_selective_set(self):
        panel = self._panel()
        changed = _rows_the_full_pass_would_change(
            panel, lambda m: m.contacts.__setitem__(PHONE, {"name": "Fulano"}))
        assert changed, "o cenário precisa mudar alguma linha de verdade"
        selected = panel._message_ids_touching_jids({PHONE})
        assert selected is None or not (changed - selected)

    def test_a_group_conversation_jid_is_not_collected(self):
        """Em grupo o nome nunca vem da conversa, e nenhum laço de resolução
        reporta um @g.us — incluí-lo só alargaria o conjunto à toa."""
        panel = self._panel()
        panel.conversation = {"remoteJid": "12036304@g.us"}
        assert "12036304@g.us" not in panel._row_jids(panel._sorted_messages[0])


class TestTheListOutOfStepWithTheControl:
    """Escrever por índice numa lista fora de passo põe o texto certo na linha
    errada, e um descompasso só de prefixo não levanta exceção nenhuma: o
    leitor de tela simplesmente passa a ler a mensagem trocada. É a mesma
    guarda que _repaint_message_rows() já tinha, e o caminho seletivo herda os
    mesmos índices."""

    def _panel(self):
        rows = [_msg("m1", participant=PHONE), _msg("m2", participant=OTHER)]
        return _PanelStub(rows)

    def test_a_mismatch_degrades_to_the_full_pass(self):
        panel = self._panel()
        panel.messages_list.item_count = 5  # controle com mais linhas que a lista
        painted = panel.refresh_active_conversation_messages(jids={PHONE})
        # Passe completo: as duas linhas, não só a que casa com PHONE.
        assert painted == 2
        assert sorted(panel.messages_list.painted) == [0, 1]

    def test_in_step_still_takes_the_selective_path(self):
        panel = self._panel()
        painted = panel.refresh_active_conversation_messages(jids={PHONE})
        assert painted == 1
        assert panel.messages_list.painted == [0]
