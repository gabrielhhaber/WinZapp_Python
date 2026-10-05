"""Verified star actions on plain stubs; no wx.App, windows or real API."""
from types import SimpleNamespace
import pytest
import wx
from ui.conversations import ConversationsPanel
from ui.conversation_panel import message_stars as actions


def message(mid="MSG1", starred=False, **extra):
    return {"key": {"id": mid, "fromMe": False}, "starred": starred,
            "messageType": "conversation", "message": {"conversation": "hello"}, **extra}


class I18n:
    def t(self, key):
        return {"star_sync_result": "{chat}: {confirmed}/{refused}/{unknown}/{skipped}",
                "star_sync_confirm": "{chat}: Sync {count}?"}.get(key, key)


class Panel(actions.StarActionsMixin):
    _on_menu_star = ConversationsPanel._on_menu_star
    _on_mass_star_messages = ConversationsPanel._on_mass_star_messages
    _mass_message_targets = ConversationsPanel._mass_message_targets
    _reject_system_event_action = ConversationsPanel._reject_system_event_action
    _is_system_event = staticmethod(ConversationsPanel._is_system_event)
    _is_separator = staticmethod(lambda m: False)

    def __init__(self, messages, outcomes=("confirmed",)):
        self.conversation = {"remoteJid": "test@s.whatsapp.net"}
        self.repainted, self.outputs, self.writes, self.calls = [], [], [], []
        self._sorted_messages = messages
        self.selected_messages = set()
        self.outcomes = iter(outcomes)
        chat = {"messages": {"messages": {"records": messages}}}
        self.main_window = SimpleNamespace(
            chats={"test@s.whatsapp.net": chat}, i18n=I18n(),
            db=SimpleNamespace(update_message_star_state=lambda *a: self.writes.append(a)),
            output=lambda text, **kw: self.outputs.append(text), star_message=self.star_message,
            _resolve_contact_name=lambda chat: "Synthetic chat")

    def star_message(self, *args):
        self.calls.append(args)
        return next(self.outcomes)

    def _repaint_or_repopulate(self, ids):
        self.repainted.append(sorted(ids))
    _refresh_message_rows_by_ids = _repaint_or_repopulate


@pytest.fixture
def queue(monkeypatch):
    workers, callbacks = [], []
    class Thread:
        def __init__(self, target, args=(), **kw):
            self.target, self.args = target, args
        def start(self):
            workers.append(lambda: self.target(*self.args))
    monkeypatch.setattr(actions.threading, "Thread", Thread)
    monkeypatch.setattr(actions.wx, "CallAfter", lambda fn, *a: callbacks.append(lambda: fn(*a)))
    return workers, callbacks


def drain(queue):
    workers, callbacks = queue
    while workers or callbacks:
        while workers:
            workers.pop(0)()
        while callbacks:
            callbacks.pop(0)()


@pytest.mark.parametrize("old", [False, True])
def test_toggle_waits_for_verification_and_persists_only_flags(queue, old):
    msg = message(starred=old)
    panel = Panel([msg])
    panel._on_menu_star(msg)
    assert msg["starred"] is old and panel.calls == panel.writes == []
    drain(queue)
    assert msg["starred"] is not old
    assert panel.writes[0][:2] == ("test@s.whatsapp.net", "MSG1")
    assert set(panel.writes[0][2]) == {"starred", "_star_remote", "_star_local", "_star_observed_at"}
    assert panel.repainted == [["MSG1"]] and panel.outputs[-1] == "Synthetic chat: 1/0/0/0"


@pytest.mark.parametrize("outcome", ["refused", "unknown"])
def test_failed_or_unverified_action_keeps_old_local_flag(queue, outcome):
    msg = message(starred=True)
    panel = Panel([msg], [outcome])
    panel._on_menu_star(msg)
    drain(queue)
    assert msg["starred"] is True and not panel.writes and not panel.repainted
    assert ("star_sync_unknown" in panel.outputs[-1]) is (outcome == "unknown")


def test_system_event_never_reaches_worker(queue):
    msg = message(messageType="groupNotification")
    panel = Panel([msg])
    panel._on_menu_star(msg)
    assert not queue[0] and not panel.calls


def test_changed_chat_and_replaced_record_keep_new_text(queue):
    msg = message()
    panel = Panel([msg])
    panel._on_menu_star(msg)
    replacement = message(message={"conversation": "edited meanwhile"})
    panel.main_window.chats["test@s.whatsapp.net"]["messages"]["messages"]["records"] = [replacement]
    panel.conversation = {"remoteJid": "other@s.whatsapp.net"}
    drain(queue)
    assert replacement["starred"] is True
    assert replacement["message"]["conversation"] == "edited meanwhile"
    assert not panel.repainted


def test_partial_bulk_is_sequential_and_reports_counts(queue):
    msgs = [message(str(i)) for i in range(3)]
    panel = Panel(msgs, ["confirmed", "refused", "unknown"])
    panel.selected_messages = {"0", "1", "2"}
    panel._on_mass_star_messages(None)
    assert len(queue[0]) == 1 and not any(m["starred"] for m in msgs)
    drain(queue)
    assert [m["starred"] for m in msgs] == [True, False, False]
    assert [c[1]["id"] for c in panel.calls] == ["0", "1", "2"]
    assert len(panel.writes) == 1
    assert panel.repainted == [["0", "1", "2"], ["0"]]
    assert panel.outputs[-1] == "Synthetic chat: 1/1/1/0 star_sync_unknown"


def test_busy_job_rejects_new_action_without_clearing_selection(queue):
    panel = Panel([message()])
    panel.main_window._star_sync_job = object()
    panel.selected_messages = {"MSG1"}
    panel._on_mass_star_messages(None)
    assert panel.selected_messages == {"MSG1"} and not queue[0]


def test_vault_lock_during_request_stops_remaining_items_and_discards_callbacks(queue):
    msgs = [message("1"), message("2")]
    panel = Panel(msgs)
    panel.main_window.is_chat_locked = lambda j: True
    panel.main_window._chat_lock_unlocked = True
    def star(*args):
        panel.calls.append(args)
        panel.main_window._chat_lock_unlocked = False
        return "confirmed"
    panel.main_window.star_message = star
    panel._sync_message_stars("test@s.whatsapp.net", msgs, True)
    drain(queue)
    assert len(panel.calls) == 1 and not panel.writes and not panel.repainted
    assert not any(m["starred"] for m in msgs)
    assert panel.main_window._star_sync_job is None


def test_pending_message_never_calls_api(queue):
    panel = Panel([message(_local_pending=True)])
    panel._on_menu_star(panel._sorted_messages[0])
    drain(queue)
    assert not panel.calls and not panel.writes
    assert panel.outputs[-1] == "Synthetic chat: 0/1/0/0"


def test_cancel_keeps_current_confirmed_result_and_skips_remaining_messages(queue):
    msgs = [message("1"), message("2")]
    panel = Panel(msgs)
    def star(*args):
        panel.calls.append(args)
        panel._on_cancel_star_sync()
        return "confirmed"
    panel.main_window.star_message = star
    panel._sync_message_stars("test@s.whatsapp.net", msgs, True)
    drain(queue)
    assert len(panel.calls) == len(panel.writes) == 1
    assert msgs[0]["starred"] is True and msgs[1]["starred"] is False
    assert panel.outputs[-1] == "Synthetic chat: 1/0/0/1"


def test_lock_then_unlock_during_request_does_not_resume_old_job(queue):
    from main_window.message_stars import MessageStarsMixin
    msgs = [message("1"), message("2")]
    panel = Panel(msgs)
    panel.main_window.is_chat_locked = lambda jid: True
    panel.main_window._chat_lock_unlocked = True
    def star(*args):
        panel.calls.append(args)
        panel.main_window._chat_lock_unlocked = False
        MessageStarsMixin._invalidate_star_sync_for_lock(panel.main_window)
        panel.main_window._chat_lock_unlocked = True
        return "confirmed"
    panel.main_window.star_message = star
    panel._sync_message_stars("test@s.whatsapp.net", msgs, True)
    drain(queue)
    assert len(panel.calls) == 1 and not panel.writes and not panel.repainted


def test_cancelling_old_star_scan_never_opens_confirmation(queue, monkeypatch):
    panel = Panel([])
    panel.main_window.db.get_messages = lambda *a, **kw: [message(starred=True)]
    prompts = []
    monkeypatch.setattr(actions, "message_box", lambda *a, **kw: prompts.append(a) or wx.YES)
    panel._on_sync_local_stars("test@s.whatsapp.net")
    panel._on_cancel_star_sync()
    drain(queue)
    assert not prompts and not panel.calls
    assert panel.main_window._star_sync_job is None


@pytest.mark.parametrize("answer", [wx.YES, wx.NO])
def test_old_stars_are_scanned_in_pages_and_require_confirmation(queue, monkeypatch, answer):
    old = message("old", starred=True)
    remote = message("remote", starred=True, _star_remote=True, _star_local=False)
    page = [message(str(i)) for i in range(497)] + [old, remote, message("sys", starred=True, messageType="groupNotification")]
    panel = Panel([])
    reads, prompts = [], []
    def get_messages(jid, limit, offset):
        reads.append((limit, offset))
        return page if offset == 0 else [old, message("second", starred=True)]
    panel.main_window.db.get_messages = get_messages
    panel.outcomes = iter(["confirmed", "confirmed"])
    monkeypatch.setattr(actions, "message_box", lambda *a, **kw: prompts.append(a) or answer)
    panel._on_sync_local_stars("test@s.whatsapp.net")
    assert not panel.calls
    queue[0].pop(0)()
    assert not panel.calls and not prompts
    queue[1].pop(0)()
    assert prompts[0][1] == "Synthetic chat: Sync 2?" and prompts[0][3] & wx.NO_DEFAULT
    drain(queue)
    assert reads == [(500, 0), (500, 500)]
    assert len(panel.calls) == (2 if answer == wx.YES else 0)
    assert panel.main_window._star_sync_job is None


def test_local_scan_failure_sends_nothing_and_clears_busy_job(queue):
    panel = Panel([])
    panel.main_window.db.get_messages = lambda *a, **kw: (_ for _ in ()).throw(OSError("synthetic"))
    panel._on_sync_local_stars("test@s.whatsapp.net")
    drain(queue)
    assert not panel.calls and not panel.writes
    assert panel.outputs[-1] == "star_sync_scan_failed" and panel.main_window._star_sync_job is None
