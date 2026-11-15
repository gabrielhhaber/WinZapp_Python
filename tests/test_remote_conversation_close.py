"""A remote lock/deletion closes the open chat, says why and keeps focus on a visible control."""
from types import SimpleNamespace as N

from core import remote_conversation_close as closing

PHONE, LID = "1@s.whatsapp.net", "901@lid"


class Widget:
    def __init__(self, parent=None):
        self.parent = parent

    def GetParent(self):
        return self.parent


def window(open_jid=PHONE, mention_popup=False):
    calls, spoken = [], []
    pane = Widget()
    panel = N(conversation={"remoteJid": open_jid} if open_jid else None, conversation_panel=pane)
    state = {"popup": mention_popup}

    def close(kind):
        calls.append(kind)
        if state["popup"]:
            state["popup"] = False  # the first press only dismisses the popup
            return
        panel.conversation = None

    panel.close_conversation = lambda: close("esc")
    panel.close_conversation_for_panel_switch = lambda: close("switch")
    mw = N(conversations_panel=panel, i18n=N(t=lambda key: "T:" + key),
           output=lambda text, interrupt=False: spoken.append((text, interrupt)))
    return mw, pane, calls, spoken


def test_focus_inside_the_open_chat_uses_the_esc_close_that_restores_the_list(monkeypatch):
    mw, pane, calls, spoken = window()
    monkeypatch.setattr(closing, "focused_window", lambda: Widget(Widget(pane)))
    assert closing.close_for_remote_change(mw, {PHONE, LID}, "remote_chat_locked_closed") is True
    assert calls == ["esc"] and mw.conversations_panel.conversation is None
    assert spoken == [("T:remote_chat_locked_closed", True)]


def test_focus_elsewhere_closes_without_moving_focus(monkeypatch):
    mw, _pane, calls, spoken = window()
    monkeypatch.setattr(closing, "focused_window", lambda: Widget(Widget()))
    assert closing.close_for_remote_change(mw, {PHONE}, "remote_chat_deleted_closed") is True
    assert calls == ["switch"] and spoken == [("T:remote_chat_deleted_closed", True)]


def test_open_mention_popup_is_dismissed_first_then_the_chat_closes(monkeypatch):
    mw, pane, calls, spoken = window(mention_popup=True)
    monkeypatch.setattr(closing, "focused_window", lambda: pane)
    assert closing.close_for_remote_change(mw, {PHONE}, "remote_chat_locked_closed") is True
    assert calls == ["esc", "esc"] and len(spoken) == 1


def test_a_chat_that_refuses_to_close_is_reported_and_not_announced(monkeypatch):
    mw, _pane, calls, spoken = window()
    mw.conversations_panel.close_conversation_for_panel_switch = lambda: calls.append("switch")
    monkeypatch.setattr(closing, "focused_window", lambda: None)
    assert closing.close_for_remote_change(mw, {PHONE}, "remote_chat_deleted_closed") is False
    assert calls == ["switch", "switch"] and spoken == []


def test_another_open_chat_or_none_is_left_alone(monkeypatch):
    monkeypatch.setattr(closing, "focused_window", lambda: None)
    for open_jid in ("7@g.us", None):
        mw, _pane, calls, spoken = window(open_jid)
        assert closing.close_for_remote_change(mw, {PHONE, LID}, "remote_chat_locked_closed") is True
        assert calls == [] and spoken == []
