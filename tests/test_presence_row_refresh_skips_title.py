"""A presence event refreshes its chat row without rescanning every chat.

Presence arrives dozens of times a second; the title and tray-tooltip passes
over self.chats froze the UI thread for seconds on a busy account.
"""
from main_window.chat_events import ChatEventsMixin


class _Stub:
    _refresh_chat_row_in_list = ChatEventsMixin._refresh_chat_row_in_list

    def __init__(self):
        self.title_updates = 0

    def _update_title(self):
        self.title_updates += 1

    def _normalize_jid(self, jid):
        return jid


def test_default_refresh_still_updates_the_title():
    stub = _Stub()
    stub._refresh_chat_row_in_list("1@s.whatsapp.net")
    assert stub.title_updates == 1


def test_presence_refresh_skips_the_title():
    stub = _Stub()
    stub._refresh_chat_row_in_list("1@s.whatsapp.net", update_title=False)
    assert stub.title_updates == 0
