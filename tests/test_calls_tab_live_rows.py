"""The Calls tab's live rows never mislead the screen reader.

- First open: history and the ongoing-call check load separately; the first
  ongoing reply used to repaint every tab, so the focused "Loading..." row
  read "No calls." before the history had arrived.
- Reopen: the hide handler dropped the cached live rows and the next reply
  added them back, rebuilding the list (and re-announcing the focused row)
  twice per visit. They now stay listed, joinable only once this visit's
  reply confirms them.
- A live row that cannot be used (call ended, chat locked) used to make
  Enter, the button and the context menu do nothing at all.

CallsPanel is a wx.Panel, so its methods run on the plain stub of
test_calls_ongoing.py; no window is created (CLAUDE.md rule 1).
"""
from types import SimpleNamespace

from calls_panel import CallsPanel
from core.call_log import TAB_ALL, TAB_ONGOING
from core.ongoing_calls import ongoing_entries
from tests.test_calls_ongoing import CALL, ME_LID, PERSON, PERSON_CHAT, Panel as _OngoingPanel
from tests.test_calls_ongoing import polling  # noqa: F401 — pytest fixture


class Panel(_OngoingPanel):
    _show_placeholder = CallsPanel._show_placeholder
    _apply_entries = CallsPanel._apply_entries

    def __init__(self, loaded=True):
        super().__init__()
        self._loaded = loaded


def _hide(panel):
    event = SimpleNamespace(GetEventObject=lambda: panel, IsShown=lambda: False, Skip=lambda: None)
    panel._on_ongoing_visibility(event)


def _writes(lst):
    return (len(lst.inserted), len(lst.deleted), len(lst.set_texts), lst.deletes)


class TestFirstOpen:
    def test_the_ongoing_reply_does_not_say_no_calls_while_history_loads(self, polling):
        queued, _ = polling
        panel = Panel(loaded=False)
        panel._ongoing_entries = []
        panel.reply = {"response": []}
        panel._show_placeholder("[loading]")  # what on_show() shows first
        panel._watch_ongoing_calls()
        assert panel._lists[TAB_ALL].items == ["[loading]"]
        assert panel._lists[TAB_ONGOING].items == ["[loading]"]
        queued.pop()()
        assert panel._lists[TAB_ALL].items == ["[loading]"]
        assert panel._lists[TAB_ONGOING].items == ["[calls_ongoing_empty]"]
        panel._apply_entries([])
        assert panel._lists[TAB_ALL].items == ["[calls_empty]"]
        assert panel._lists[TAB_ONGOING].items == ["[calls_ongoing_empty]"]

    def test_history_loaded_first_still_waits_for_the_ongoing_reply(self, polling):
        queued, _ = polling
        panel = Panel(loaded=False)
        panel._ongoing_entries = []
        panel._watch_ongoing_calls()
        panel._apply_entries([])
        assert panel._lists[TAB_ONGOING].items == ["[loading]"]
        queued.pop()()
        assert panel._rows[TAB_ONGOING] and panel._rows[TAB_ALL][0]["ongoing"]


class TestReopen:
    def test_cached_rows_stay_but_join_only_once_this_visit_confirms_them(self, polling):
        queued, _ = polling
        panel = Panel()
        panel._watch_ongoing_calls()
        queued.pop()()
        lst = panel._lists[TAB_ALL]
        assert panel._rows[TAB_ALL][0]["ongoing"] and panel._ongoing_join_btn.shown
        before = _writes(lst)

        _hide(panel)
        panel._watch_ongoing_calls()

        assert panel._rows[TAB_ALL][0]["ongoing"]
        assert not panel._ongoing_join_btn.shown
        panel._on_item_activated(None)
        assert not panel.joins and panel.main_window.spoken == ["[loading]"]

        queued.pop()()

        assert _writes(lst) == before
        assert panel._ongoing_join_btn.shown
        panel._on_item_activated(None)
        assert len(panel.joins) == 1


class TestUnusableRow:
    def _ended_current_call(self, polling):
        queued, _ = polling
        panel = Panel()
        panel.reply = {"response": []}
        panel._ongoing_entries = []
        panel._watch_ongoing_calls()
        queued.pop()()
        mw = panel.main_window
        mw._voice_call_last_announced_state = "ACTIVE"
        mw._active_voice_call = {"call_id": "individual", "peer_jid": PERSON, "is_video": False}
        panel._populate_all()
        mw._active_voice_call = None  # hung up before the list caught up
        return panel

    def test_enter_on_an_ended_call_says_so_and_drops_the_row(self, polling):
        panel = self._ended_current_call(polling)
        panel._join_ongoing_call()
        assert not panel.joins
        assert panel.main_window.spoken == ["[call_participants_ended]"]
        assert not panel._rows[TAB_ONGOING] and not panel._rows[TAB_ALL]

    def test_the_context_menu_says_so_instead_of_opening(self, polling):
        panel = self._ended_current_call(polling)
        panel._ongoing_context_menu()
        assert panel.main_window.spoken == ["[call_participants_ended]"]

    def test_the_participants_button_says_so_too(self, polling):
        panel = self._ended_current_call(polling)
        opened = []
        panel.main_window.show_call_participants = lambda *a, **kw: opened.append(a)
        panel._show_ongoing_participants()
        assert not opened and panel.main_window.spoken == ["[call_participants_ended]"]

    def test_a_locked_chats_row_is_refused_out_loud(self, polling):
        queued, _ = polling
        panel = Panel()
        panel._watch_ongoing_calls()
        queued.pop()()
        panel.main_window._locked_jids.add(panel._rows[TAB_ALL][0]["jid"])
        panel._join_ongoing_call()
        assert not panel.joins
        assert panel.main_window.spoken == ["[call_participants_ended]"]
        assert not panel._rows[TAB_ONGOING]



class TestCallNamedAfterItsPeople:
    """A call started on the phone is filed under the own chat and named
    after its people: a lock on one of them must hide it, or the row reads
    the locked contact's name."""

    def _panel(self):
        panel = Panel()
        panel._ongoing_entries = ongoing_entries({"response": [
            {**CALL, "chatJid": ME_LID, "people": [PERSON_CHAT]}]})
        panel.main_window.ongoing_call_people_text = (
            lambda ongoing: "Ana" if ongoing.get("people") else "")
        return panel

    def test_a_locked_person_hides_the_row(self, polling):
        queued, _ = polling
        panel = self._panel()
        panel.reply = {"response": [{**CALL, "chatJid": ME_LID, "people": [PERSON_CHAT]}]}
        panel.main_window._locked_jids.add(PERSON_CHAT)
        panel._watch_ongoing_calls()
        queued.pop()()
        assert not panel._rows[TAB_ONGOING] and not panel._rows[TAB_ALL]
        assert not any("Ana" in text for text in panel._lists[TAB_ONGOING].items)

    def test_the_participants_window_is_titled_like_the_row(self, polling):
        panel = self._panel()
        opened = []
        panel.main_window.show_call_participants = lambda *a, **kw: opened.append((a, kw))
        panel._populate_all()
        panel._show_ongoing_participants()
        assert opened == [(("C1", ME_LID), {"parent": panel, "title": "Ana",
                                            "people": [PERSON_CHAT]})]
