from types import SimpleNamespace as N

from core.remote_chat_lock import apply_phone_lock
from tests.test_phone_chat_lock import _Stub, PHONE, LID


def test_phone_lock_closes_alias_view_and_unlock_preserves_local_vault():
    mw = _Stub()
    calls = []
    mw.chats = {PHONE: {"remoteJid": PHONE}, LID: {"remoteJid": LID}}
    mw._lid_to_phone = {LID: PHONE}
    mw._phone_to_lid = {PHONE: LID}
    spoken = []
    mw.i18n = N(t=lambda key: key)
    mw.output = lambda text, interrupt=False: spoken.append(text)
    panel = N(conversation={"remoteJid": PHONE},
        close_ai_media=lambda **kw: calls.append("media"), drop_locked_translations=lambda: calls.append("translations"))
    def close():
        calls.append("close")
        panel.conversation = None
    panel.close_conversation_for_panel_switch = close
    mw.conversations_panel = panel
    mw._refresh_chat_lock_navigation = lambda: calls.append("navigation")
    mw._schedule_set_chats = lambda: calls.append("refresh")
    mw._invalidate_star_sync_for_lock = lambda: calls.append("stars")
    apply_phone_lock(mw, LID, True)
    assert mw.is_chat_phone_locked(PHONE) and mw.is_chat_phone_locked(LID)
    assert calls[:1] == ["close"] and "media" in calls and "translations" in calls
    assert spoken == ["remote_chat_locked_closed"]
    assert mw.chats[PHONE]["isLocked"] is True
    persisted = mw.db.stored[mw._PHONE_LOCK_KEY]
    assert persisted and all(len(fp) == 64 and "@" not in fp for fp in persisted)
    calls.clear()
    apply_phone_lock(mw, PHONE, False)
    assert not mw.is_chat_phone_locked(PHONE)
    assert "close" not in calls


def test_lock_for_a_different_chat_leaves_the_open_view_alone():
    mw = _Stub()
    calls = []
    mw.conversations_panel = N(conversation={"remoteJid": "7@g.us"},
        close_conversation_for_panel_switch=lambda: calls.append("close"))
    mw._refresh_chat_lock_navigation = lambda: None
    mw._schedule_set_chats = lambda: None
    apply_phone_lock(mw, PHONE, True)
    assert calls == [] and mw.is_chat_phone_locked(PHONE)
