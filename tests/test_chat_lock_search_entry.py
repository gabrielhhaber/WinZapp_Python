"""Regression coverage for opening the hidden vault from conversation search.

The tests bind ConversationsPanel methods to plain stubs.  They never create a
wx window or touch a WhatsApp session.
"""

import inspect

from ui.conversations import ConversationsPanel


class _SearchField:
    def __init__(self, value):
        self.value = value

    def GetValue(self):
        return self.value


class _MainWindow:
    def __init__(self, accepted):
        self.accepted = accepted
        self.values = []

    def try_reveal_locked_chats(self, value):
        self.values.append(value)
        return self.accepted


class _Event:
    def __init__(self):
        self.skipped = False

    def Skip(self):
        self.skipped = True


def _panel(value, accepted):
    return type("PanelStub", (), {
        "main_window": _MainWindow(accepted),
        "search_field": _SearchField(value),
    })()


def test_search_control_requests_and_binds_the_text_enter_event():
    source = inspect.getsource(ConversationsPanel.init_UI)

    assert "wx.TE_PROCESS_ENTER" in source
    assert "wx.EVT_TEXT_ENTER, self._on_search_field_enter" in source


def test_search_enter_passes_the_exact_value_to_the_vault_reveal_gate():
    panel = _panel("  gizli-kod  ", accepted=True)
    event = _Event()

    ConversationsPanel._on_search_field_enter(panel, event)

    assert panel.main_window.values == ["  gizli-kod  "]
    assert not event.skipped


def test_search_enter_keeps_normal_enter_behaviour_for_a_non_secret_query():
    panel = _panel("normal sohbet araması", accepted=False)
    event = _Event()

    ConversationsPanel._on_search_field_enter(panel, event)

    assert panel.main_window.values == ["normal sohbet araması"]
    assert event.skipped
