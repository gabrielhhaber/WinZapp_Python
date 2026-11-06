"""Lock-owner transitions revoke names/publication without a real wx window."""
import hashlib
from types import SimpleNamespace

import pytest

from core.storage_manager.policy import Preferences
from core.storage_manager.references import Reference, References, chat_token
from core.storage_manager.service import StorageService
from core.storage_manager.types import Entry, Root, Snapshot, canonical
from main_window.chat_lock import ChatLockMixin
from main_window.phone_chat_lock import PhoneChatLockMixin
from main_window.storage_management import StorageContext
from main_window.storage_privacy import invalidate
from ui.dialogs.storage_sensitive import show_sensitive


PRIVATE = "Synthetic private group"
JID = "synthetic@s.whatsapp.net"


class View:
    def __init__(self): self.label = PRIVATE
    def clear_private_state(self): self.label = ""


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setattr("main_window.storage_management.app_paths.data_path", lambda: str(tmp_path))
    state = {"local": False}
    panel = SimpleNamespace(_all_chats_list=[{"remoteJid": JID}], _all_chat_names=[PRIVATE], conversation=None)
    window = SimpleNamespace(conversations_panel=panel, _chat_lock_unlocked=True,
        _phone_locked_chats=set(), chats={JID: {"remoteJid": JID}},
        _cancel_chat_lock_timeout=lambda: None, _refresh_chat_lock_navigation=lambda: None,
        _persist_chat_lock_vault=lambda: None, _schedule_set_chats=lambda: None,
        _chat_lock_canonical_jid=lambda jid: jid, output=lambda *a, **k: None,
        i18n=SimpleNamespace(t=lambda key: key))
    window.is_chat_locked = lambda jid: state["local"] or bool(window._phone_locked_chats)
    window.is_chat_hidden_by_vault = lambda jid: not window._chat_lock_unlocked
    window._chat_lock_vault = SimpleNamespace(configured=True, lock_chat=lambda jid: state.update(local=True))
    window._phone_lock_counterpart = lambda jid: ""
    window._phone_lock_fp = lambda jid: hashlib.sha256(jid.encode()).hexdigest() if jid else ""
    ctx = StorageContext(window)
    ctx.capture_chats()
    workers, dispatch = [], []
    store = SimpleNamespace(preferences=lambda **kw: Preferences(match_media=True))
    service = StorageService(store, None, lambda prefs: [], lambda cancel: References({}),
                             dispatch.append, launch=workers.append, busy=ctx.busy)
    entry = Entry(Root("media", str(tmp_path), "media", managed=True), "one.wzmedia", 3, 1,
        (1, 1, 3, 1, 1), "media", reason="received", media_id="one", chat_key=chat_token(JID), display_name=PRIVATE)
    service.snapshot = Snapshot(entries=[entry], named_chats={entry.chat_key})
    window._storage_context, window._storage_service = ctx, service
    view = View()
    service.register_view(view)
    monkeypatch.setattr("wx.IsMainThread", lambda: True)
    monkeypatch.setattr("wx.CallAfter", lambda fn, *a, **kw: fn(*a, **kw))
    return window, ctx, service, entry, view, workers, dispatch


@pytest.mark.parametrize("owner", ["local", "phone", "vault"])
def test_each_lock_owner_clears_open_view_names_and_cached_authority(context, owner):
    window, ctx, service, entry, view, *_ = context
    if owner == "local":
        ChatLockMixin.lock_chat(window, JID)
    elif owner == "phone":
        PhoneChatLockMixin._sync_phone_chat_lock(window, {"isLocked": True}, JID, window.chats)
    else:
        ChatLockMixin.lock_chat_vault(window, silent=True)
    assert service.snapshot is None and service.visibility_epoch == 1
    assert view.label == "" and ctx.chats == {} and ctx.chat_jids == {}
    assert not service.can_display(entry) and service.present(entry).display_name == ""


def test_cached_entries_are_redacted_at_consumption_even_before_a_hook(context):
    window, ctx, service, entry, *_ = context
    window._phone_locked_chats.add("synthetic-fingerprint")
    assert service.present(entry).display_name == ""
    assert ctx.allowed_chats() == {} and ctx.busy(entry)


def test_background_invalidation_revokes_authority_before_ui_dispatch(context):
    window, ctx, service, entry, view, *_ = context
    posted = []
    invalidate(window, dispatch=posted.append, is_main=lambda: False)
    assert service.snapshot is None and not service.can_display(entry)
    assert view.label == PRIVATE and len(posted) == 1
    posted.pop()()
    assert view.label == ""


def test_scan_started_before_lock_cannot_republish_old_names(context, tmp_path):
    window, ctx, service, entry, view, workers, dispatch = context
    path = tmp_path / "one.wzmedia"
    path.write_bytes(b"one")
    service.get_roots = lambda prefs: [entry.root]
    service.get_references = lambda cancel: References({canonical(path):
        Reference("one", entry.chat_key, "media", display_name=PRIVATE)})
    result = []
    assert service.scan(lambda value, error: result.append((value, error)))
    workers.pop()()
    window._phone_locked_chats.add("synthetic-fingerprint")
    invalidate(window, dispatch=lambda fn: fn(), is_main=lambda: True)
    dispatch.pop()()
    assert service.snapshot is None and result == [(None, "VisibilityChanged")]
    assert view.label == ""


def test_repeated_already_hidden_phone_records_do_not_reset_a_new_public_view(context):
    window, ctx, service, entry, view, *_ = context
    PhoneChatLockMixin._sync_phone_chat_lock(window, {"isLocked": True}, JID, window.chats)
    view.label = "Public category counts"
    PhoneChatLockMixin._sync_phone_chat_lock(window, {"isLocked": True}, JID, window.chats)
    assert service.visibility_epoch == 1 and view.label == "Public category counts"


@pytest.mark.parametrize("when", ["before", "during"])
def test_sensitive_modal_cannot_accept_results_from_an_old_epoch(context, when):
    import wx
    _, _, service, *_ = context
    class Modal(View):
        privacy_epoch = service.visibility_epoch
        called = False
        def ShowModal(self):
            self.called = True
            service.invalidate_visibility()
            return wx.ID_OK
    modal = Modal()
    if when == "before":
        service.invalidate_visibility()
    assert show_sensitive(service, modal) == wx.ID_CANCEL
    assert modal.called == (when == "during")
    assert modal not in service.views
