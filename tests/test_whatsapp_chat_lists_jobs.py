"""Account/vault guards and serialized jobs on stubs; no app, windows or HTTP."""

from types import SimpleNamespace
import pytest
from core.chat_lists import ListSnapshot, WhatsAppList
from core.chat_list_transport import ListResult
from main_window import chat_lists as jobs

PN = "551199990001@s.whatsapp.net"
LID = "900000000000001@lid"
LOCKED = "551199990002@s.whatsapp.net"
OTHER = "551199990003@s.whatsapp.net"


def panel(rows):
    chats, names = [{"remoteJid": jid} for jid, _name in rows], [name for _jid, name in rows]
    return SimpleNamespace(chats_list=chats, chat_names=names, _all_chats_list=chats,
                           _all_chat_names=names, _wa_list_id=None)


class Window(jobs.WhatsAppListsMixin):
    def __init__(self):
        self.account_id, self.my_jid = "synthetic", PN
        self.wpp_server, self.wpp_port, self.token = "synthetic", 1, "fake:token"
        self.locked, self._lid_to_phone = {LOCKED}, {LID: PN}
        self.conversations_panel = panel([(PN, "Ada"), (LOCKED, "Secret"), (OTHER, "Bea")])
        self.archived_conversations_panel = panel([(LID, "Duplicate Ada"), ("120363000000001@g.us", "Group")])
        self.refreshes = 0
        self.conversations_panel._refresh_wa_list_choices = self.refresh
        self._wa_lists_snapshot = ListSnapshot((WhatsAppList("42", "Friends", frozenset({LID, LOCKED})),), True)
        self._wa_lists_snapshot_context = self._wa_lists_context()
    def refresh(self):
        self.refreshes += 1
    def is_chat_locked(self, jid):
        return jid in self.locked
    def _resolve_jid_for_send(self, jid):
        return LID if jid == PN else jid.replace("@s.whatsapp.net", "@c.us")


@pytest.fixture
def queue(monkeypatch):
    workers, callbacks = [], []
    class Thread:
        def __init__(self, target, **kwargs):
            self.target = target
        def start(self):
            workers.append(self.target)
    monkeypatch.setattr(jobs.threading, "Thread", Thread)
    monkeypatch.setattr(jobs.wx, "CallAfter", lambda fn, *args: callbacks.append(lambda: fn(*args)))
    return workers, callbacks


def drain(queue):
    for pending in queue:
        while pending:
            pending.pop(0)()


def test_member_candidates_deduplicate_pn_lid_and_never_include_the_vault():
    window = Window()
    assert window._wa_list_candidates() == [(PN, "Ada"), (OTHER, "Bea"), ("120363000000001@g.us", "Group")]


def test_removing_selected_phone_uses_existing_remote_lid_only_and_keeps_hidden_members():
    window = Window()
    item = window._wa_lists_state().find("42")
    command = window._wa_list_member_command(item, "removeChats", [PN])
    assert command == {"action": "removeChats", "id": "42", "chatIds": [LID]}
    assert LOCKED not in command["chatIds"]
    assert window._wa_list_member_command(item, "addChats", [PN]) is None


def test_newly_locked_or_unknown_selection_never_produces_a_member_command():
    window = Window()
    item = window._wa_lists_state().find("42")
    window.locked.add(PN)
    assert window._wa_list_member_command(item, "removeChats", [PN]) is None
    assert window._wa_list_member_command(item, "removeChats", [LOCKED]) is None
    assert window._wa_list_member_command(item, "addChats", ["123@lid"]) is None


def test_write_starts_once_and_a_second_job_is_refused(queue, monkeypatch):
    window, calls, results = Window(), [], []
    monkeypatch.setattr(jobs, "request_lists", lambda *args: calls.append(args) or ListResult(window._wa_lists_state(), "changed"))
    command = {"action": "rename", "id": "42", "name": "Work"}
    window._request_wa_lists(results.append, command)
    window._request_wa_lists(results.append, command)
    assert results == [ListResult(None, "busy")] and len(queue[0]) == 1
    drain(queue)
    assert len(calls) == 1 and calls[0][2] == command
    assert results[-1].outcome == "changed" and window.refreshes == 1
    assert window._wa_lists_job is None


@pytest.mark.parametrize("change", ["token", "account", "reset", "shutdown"])
def test_context_change_before_worker_prevents_any_api_call(queue, monkeypatch, change):
    window, calls, results = Window(), [], []
    monkeypatch.setattr(jobs, "request_lists", lambda *args: calls.append(args))
    window._request_wa_lists(results.append)
    if change == "token":
        window.token = "new:fake"
    elif change == "account":
        window.account_id = "other"
    elif change == "reset":
        window._invalidate_wa_lists()
    else:
        window._shutting_down = True
    drain(queue)
    assert not calls and window._wa_lists_job is None
    assert results == ([] if change == "shutdown" else [ListResult(None, "cancelled")])


def test_reply_after_reset_cannot_restore_old_lists_or_call_ui_with_old_data(queue, monkeypatch):
    window, results = Window(), []
    old = window._wa_lists_state()
    monkeypatch.setattr(jobs, "request_lists", lambda *args: ListResult(old, "loaded"))
    window._request_wa_lists(results.append)
    queue[0].pop(0)()  # response arrived, UI callback is still queued
    window._invalidate_wa_lists()
    drain(queue)
    assert window._wa_lists_state() == ListSnapshot()
    assert results == [ListResult(None, "cancelled")]


def test_lock_after_selection_before_worker_prevents_post(queue, monkeypatch):
    window, calls, results = Window(), [], []
    monkeypatch.setattr(jobs, "request_lists", lambda *args: calls.append(args))
    window._request_wa_lists(results.append, {"action": "removeChats", "id": "42", "chatIds": [LID]})
    window.locked.add(LID)
    drain(queue)
    assert not calls and results == [ListResult(None, "refused")]


def test_failure_preserves_cached_filter_but_disables_writes_until_read_succeeds(queue, monkeypatch):
    window, results = Window(), []
    old = window._wa_lists_state()
    monkeypatch.setattr(jobs, "request_lists", lambda *args: ListResult(None, "failed"))
    window._request_wa_lists(results.append)
    drain(queue)
    assert window._wa_lists_state().lists == old.lists
    assert not window._wa_lists_state().can_edit
    window._request_wa_lists(results.append, {"action": "create", "name": "Work"})
    assert results[-1].outcome == "refused" and not queue[0]
    monkeypatch.setattr(jobs, "request_lists", lambda *args: ListResult(old, "loaded"))
    window._request_wa_lists(results.append)
    drain(queue)
    assert window._wa_lists_state().can_edit


def test_offline_mode_never_sends_read_or_write(queue):
    window, results = Window(), []
    window.offline_mode = True
    window._request_wa_lists(results.append)
    assert results == [ListResult(None, "failed")] and not queue[0]


@pytest.mark.parametrize("command,outcome", [(None, "failed"),
    ({"action": "rename", "id": "42", "name": "Work"}, "unconfirmed")])
def test_unexpected_worker_error_releases_job_without_retry_or_native_text(queue, monkeypatch, command, outcome):
    window, calls, results = Window(), [], []
    def broken(*args):
        calls.append(args)
        raise RuntimeError("private native error")
    monkeypatch.setattr(jobs, "request_lists", broken)
    window._request_wa_lists(results.append, command)
    drain(queue)
    assert len(calls) == 1 and window._wa_lists_job is None
    assert results == [ListResult(None, outcome)]


def test_thread_start_failure_releases_job_without_http(queue, monkeypatch):
    window, calls, results = Window(), [], []
    class BrokenThread:
        def __init__(self, **kwargs):
            pass
        def start(self):
            raise RuntimeError("cannot start")
    monkeypatch.setattr(jobs.threading, "Thread", BrokenThread)
    monkeypatch.setattr(jobs, "request_lists", lambda *args: calls.append(args))
    window._request_wa_lists(results.append)
    assert not calls and window._wa_lists_job is None
    assert results == [ListResult(None, "failed")]
