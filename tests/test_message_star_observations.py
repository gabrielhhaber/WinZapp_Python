"""Redelivery and unloaded legacy rows update stars without replacing content."""
from core.message_stars import merge_star_state, remote_star_state
from main_window import message_stars as stars
from tests.test_live_caption_edit import _Stub


def message(star=False, observed=10):
    return {"key": {"id": "M"}, "messageType": "conversation",
            "message": {"conversation": "keep"}, **remote_star_state({"star": star}, observed)}


class MW(_Stub, stars.MessageStarsMixin):
    def __init__(self):
        super().__init__()
        self.writes, self.rows = [], []
        self.db.update_message_star_state = lambda *a: self.writes.append(a)
        self.conversations_panel.conversation = {"remoteJid": "test@s.whatsapp.net"}
        self.conversations_panel._repaint_or_repopulate = lambda ids: self.rows.append(ids)
        self.is_chat_locked = lambda jid: False


def test_live_same_id_star_uses_atomic_flag_write_and_single_row_repaint(monkeypatch):
    monkeypatch.setattr(stars.wx, "CallAfter", lambda f, *a: f(*a))
    mw = MW()
    existing = message(False)
    mw._apply_possible_edit(existing, message(True, 20), "test@s.whatsapp.net")
    assert existing["starred"] is True and existing["message"]["conversation"] == "keep"
    assert len(mw.writes) == 1 and mw.rows == [["M"]]
    assert mw.db.inserted == []
    mw._apply_possible_edit(existing, message(True, 30), "test@s.whatsapp.net")
    assert len(mw.writes) == 1 and mw.rows == [["M"]]


def test_disk_only_legacy_star_is_restored_on_ui_callback(monkeypatch):
    callbacks = []
    monkeypatch.setattr(stars.wx, "CallAfter", lambda f, *a: callbacks.append(lambda: f(*a)))
    mw = MW()
    msg = message(False)
    mw.chats = {"test@s.whatsapp.net": {"messages": {"messages": {"records": [msg]}}}}
    mw.db.merge_message_star_states = lambda jid, rows: [merge_star_state(m, {"starred": True}) for m in rows]
    mw._insert_message_preserving_stars("test@s.whatsapp.net", msg)
    assert msg["starred"] is False  # worker never mutates the UI dict
    callbacks.pop(0)()
    assert msg["starred"] is True and msg["_star_local"] is True
    assert mw.rows == [["M"]]


def test_late_restore_does_not_resurrect_deleted_row_or_repaint_another_chat(monkeypatch):
    monkeypatch.setattr(stars.wx, "CallAfter", lambda f, *a: f(*a))
    mw = MW()
    mw.chats = {}
    mw._apply_preserved_star("test@s.whatsapp.net", "M", remote_star_state({"star": True}, 20))
    assert not mw.rows and not mw.writes and not mw.chats


def test_locked_chat_discards_restore_callback(monkeypatch):
    mw = MW()
    msg = message(False)
    mw.chats = {"test@s.whatsapp.net": {"messages": {"messages": {"records": [msg]}}}}
    mw.is_chat_locked = lambda jid: True
    mw._chat_lock_unlocked = False
    mw._apply_preserved_star("test@s.whatsapp.net", "M", remote_star_state({"star": True}, 20))
    assert not msg["starred"] and not mw.rows


def test_decrypting_placeholder_preserves_legacy_star_without_preserving_old_body():
    from main import MainWindow
    existing = {"key": {"id": "M"}, "messageType": "ciphertext", "starred": True}
    result = MainWindow._adopt_decrypted_copy(existing, message(False))
    assert result is existing and result["starred"] is True and result["_star_local"] is True
    assert result["messageType"] == "conversation" and result["message"]["conversation"] == "keep"
