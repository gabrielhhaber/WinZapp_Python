"""Empty-list creation and editing explanations on plain stubs, never wx windows."""

from types import SimpleNamespace

import pytest

from core.chat_lists import ListSnapshot, WhatsAppList
from ui.dialogs.chat_lists import WhatsAppListsDialog


class Choice:
    def __init__(self):
        self.items, self.selection = [], -1
    def GetSelection(self): return self.selection
    def GetItems(self): return self.items
    def SetItems(self, items): self.items = list(items)
    def SetSelection(self, index): self.selection = index
    def Freeze(self): pass
    def Thaw(self): pass


class Button:
    def Enable(self, enabled): self.enabled = enabled


def dialog_stub(snapshot, busy=False):
    stub = SimpleNamespace(_ids=[], _choice=Choice(), _busy=busy,
        _closed=False, _buttons={action: Button() for action in
            ('reload', 'create', 'rename', 'remove', 'addChats', 'removeChats')},
        _mw=SimpleNamespace(_wa_lists_state=lambda: snapshot))
    stub._selected_list = lambda: WhatsAppListsDialog._selected_list(stub)
    stub._refresh_list_manager = lambda: WhatsAppListsDialog._refresh_list_manager(stub)
    return stub


@pytest.mark.parametrize('editable,busy,has_list', [
    (True, False, False), (True, False, True),
    (False, False, False), (False, False, True),
    (True, True, False), (True, True, True),
])
def test_create_depends_on_capability_and_busy_state_not_existing_list(editable, busy, has_list):
    rows = (WhatsAppList('42', 'Synthetic', frozenset()),) if has_list else ()
    stub = dialog_stub(ListSnapshot(rows, editable), busy)
    stub._refresh_list_manager()
    assert stub._buttons['create'].enabled == (editable and not busy)
    assert stub._buttons['reload'].enabled == (not busy)
    for action in ('rename', 'remove', 'addChats', 'removeChats'):
        assert stub._buttons[action].enabled == (editable and has_list and not busy)


@pytest.mark.parametrize('editable,reason,outcome,expected', [
    (True, '', 'loaded', 'wa_lists_loaded'),
    (False, '', 'loaded', 'wa_lists_loaded wa_lists_read_only'),
    (False, 'account_disabled', 'loaded', 'wa_lists_loaded wa_lists_account_disabled'),
    (False, 'runtime_incomplete', 'loaded', 'wa_lists_loaded wa_lists_runtime_incomplete'),
    (False, 'capability_check_failed', 'loaded', 'wa_lists_loaded wa_lists_capability_check_failed'),
    (False, 'future_code', 'loaded', 'wa_lists_loaded wa_lists_read_only'),
    (False, 'account_disabled', 'failed', 'wa_lists_failed'),
])
def test_completion_announces_fixed_reason_and_releases_buttons(editable, reason, outcome, expected):
    stub = dialog_stub(ListSnapshot((), editable, reason))
    callbacks, labels, speech = [], [], []
    stub._status = SimpleNamespace(SetLabel=labels.append)
    stub.Layout = lambda: None
    stub._mw.i18n = SimpleNamespace(t=lambda key: key)
    stub._mw.output = speech.append
    stub._mw._request_wa_lists = lambda callback, command: callbacks.append(callback)
    WhatsAppListsDialog._submit_list_command(stub)
    assert stub._busy and not stub._buttons['create'].enabled
    callbacks[0](SimpleNamespace(outcome=outcome))
    assert not stub._busy and stub._buttons['reload'].enabled
    assert stub._buttons['create'].enabled == editable
    assert labels[-1] == expected and speech == [expected]


def test_a_read_superseded_by_a_pending_label_refresh_is_not_announced_read_only():
    stub = dialog_stub(ListSnapshot((), False, ''))
    callbacks, speech = [], []
    stub._status = SimpleNamespace(SetLabel=lambda text: None)
    stub.Layout = lambda: None
    stub._mw.i18n = SimpleNamespace(t=lambda key: key)
    stub._mw.output = speech.append
    stub._mw._request_wa_lists = lambda callback, command: callbacks.append(callback)
    stub._mw._wa_lists_remote_refresh_pending = True
    WhatsAppListsDialog._submit_list_command(stub)
    callbacks[0](SimpleNamespace(outcome='loaded'))
    assert speech == ['wa_lists_loaded']


def test_list_completion_text_only_explains_read_only_after_a_settled_read():
    from ui.conversation_panel.chat_lists import list_completion_text
    i18n = SimpleNamespace(t=lambda key: key)
    read_only = ListSnapshot((), False, 'account_disabled')
    assert list_completion_text(i18n, 'loaded', read_only) == 'wa_lists_loaded wa_lists_account_disabled'
    assert list_completion_text(i18n, 'loaded', read_only, refresh_pending=True) == 'wa_lists_loaded'
    assert list_completion_text(i18n, 'failed', read_only) == 'wa_lists_failed'
