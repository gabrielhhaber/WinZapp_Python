"""Tests for ConversationsPanel._goto_quoted_status() — "ir para mensagem
citada" (Alt+Shift+Q) when the quoted message isn't in the current chat's
own message list at all because it's actually a reply to a STATUS.

Three cases, in priority order:
1. The status is still tracked in main_window._status_updates and it's
   one of the user's OWN posted statuses -> opens MyStatusDialog.
2. Still tracked, but posted by someone else -> opens the main status
   viewer (StatusPanel), focused on that contact/status.
3. Aged out of _status_updates entirely -> rebuilt from the quoted
   content WhatsApp still ships inline on the reply itself, then opened
   the same way as case 1/2 depending on who posted it.

ConversationsPanel is a wx.Panel and can't be instantiated without a
running wx.App, so the method under test is bound onto a plain stub;
_open_my_status_dialog_at()/_open_status_panel_at() (which would need a
real MyStatusDialog/StatusPanel) are replaced with call-recording stubs
so only the dispatch decision itself is under test here.
"""

import pytest

from ui.conversations import ConversationsPanel


class _FakeMainWindow:
    def __init__(self, status_updates=None, self_jid=""):
        self._status_updates = status_updates or {}
        self._self_jid = self_jid

    def _is_self_jid(self, jid):
        return bool(jid) and jid == self._self_jid

    def _resolve_contact_name(self, chat):
        return None


class _FakeStatusPanel:
    def _parse_statuses(self, items, i18n):
        my_statuses = []
        contacts_by_jid = {}
        for item in items:
            key = item.get("key", {})
            participant = key.get("participant", "")
            if key.get("fromMe") or participant == self._self_jid:
                my_statuses.append(item)
                continue
            entry = contacts_by_jid.setdefault(
                participant, {"name": participant, "jid": participant, "statuses": []}
            )
            entry["statuses"].append(item)
        return my_statuses, list(contacts_by_jid.values())

    _self_jid = ""


class _Stub:
    _goto_quoted_status = ConversationsPanel._goto_quoted_status

    def __init__(self, status_updates=None, self_jid=""):
        self.main_window = _FakeMainWindow(status_updates=status_updates, self_jid=self_jid)
        self.main_window.i18n = None
        self.status_panel = _FakeStatusPanel()
        self.status_panel._self_jid = self_jid
        self.main_window.status_panel = self.status_panel
        self.my_status_calls = []
        self.status_panel_calls = []

    def _open_my_status_dialog_at(self, my_statuses, idx):
        self.my_status_calls.append((my_statuses, idx))

    def _open_status_panel_at(self, sp, my_statuses, contacts, target_jid, s_idx):
        self.status_panel_calls.append((target_jid, s_idx))


def _status(status_id, participant, from_me=False, msg_type="conversation", body="oi"):
    return {
        "key": {"id": status_id, "fromMe": from_me, "participant": participant},
        "messageType": msg_type,
        "message": {msg_type: body} if msg_type == "conversation" else {msg_type: {}},
        "messageTimestamp": 1700000000,
    }


class TestStillTrackedInStatusUpdates:
    def test_own_status_opens_my_status_dialog(self):
        st = _status("s1", "me@lid", from_me=True)
        stub = _Stub(status_updates={"me@lid": [st]})

        found = stub._goto_quoted_status("s1", {})

        assert found is True
        assert stub.my_status_calls == [([st], 0)]
        assert stub.status_panel_calls == []

    def test_others_status_opens_the_status_panel(self):
        st = _status("s1", "a@s.whatsapp.net")
        stub = _Stub(status_updates={"a@s.whatsapp.net": [st]})

        found = stub._goto_quoted_status("s1", {})

        assert found is True
        assert stub.status_panel_calls == [("a@s.whatsapp.net", 0)]
        assert stub.my_status_calls == []

    def test_not_found_at_all_without_inline_quoted_content_returns_false(self):
        stub = _Stub(status_updates={})

        found = stub._goto_quoted_status("s1", {})

        assert found is False
        assert stub.my_status_calls == []
        assert stub.status_panel_calls == []


class TestAgedOutRebuiltFromInlineQuotedContent:
    """The status is no longer in _status_updates — WhatsApp still sends
    the quoted content inline on the reply (ctx["quotedMessage"])."""

    def test_own_status_rebuilt_and_opened_in_my_status_dialog(self):
        stub = _Stub(status_updates={}, self_jid="me@lid")
        ctx = {
            "remoteJid": "status@broadcast",
            "participant": "me@lid",
            "quotedMessage": {"conversation": "texto antigo do status"},
        }

        found = stub._goto_quoted_status("s1", ctx)

        assert found is True
        assert len(stub.my_status_calls) == 1
        rebuilt_statuses, idx = stub.my_status_calls[0]
        assert idx == 0
        assert rebuilt_statuses[0]["key"]["id"] == "s1"
        assert rebuilt_statuses[0]["key"]["fromMe"] is True
        assert rebuilt_statuses[0]["messageType"] == "conversation"

    def test_others_status_rebuilt_and_opened_in_status_panel(self):
        stub = _Stub(status_updates={})
        ctx = {
            "remoteJid": "status@broadcast",
            "participant": "a@s.whatsapp.net",
            "quotedMessage": {"conversation": "texto antigo do status"},
        }

        found = stub._goto_quoted_status("s1", ctx)

        assert found is True
        assert stub.status_panel_calls == [("a@s.whatsapp.net", 0)]

    def test_no_quoted_content_and_not_tracked_returns_false(self):
        stub = _Stub(status_updates={})

        found = stub._goto_quoted_status("s1", {"participant": "a@s.whatsapp.net"})

        assert found is False

    @pytest.mark.parametrize("remote_jid", [None, "123@g.us", "123@s.whatsapp.net"])
    def test_missing_chat_message_is_never_rebuilt_as_a_status(self, remote_jid):
        stub = _Stub(status_updates={})
        ctx = {
            "participant": "a@s.whatsapp.net",
            "quotedMessage": {"conversation": (
                "https://github.com/gabrielhhaber/WinZapp_Python/releases/"
                "download/v1.1.0.2592alpha/WinZapp.zip"
            )},
        }
        if remote_jid:
            ctx["remoteJid"] = remote_jid

        assert stub._goto_quoted_status("old-group-message", ctx) is False
        assert stub.my_status_calls == []
        assert stub.status_panel_calls == []

    def test_unrecognized_quoted_message_shape_returns_false(self):
        stub = _Stub(status_updates={})
        ctx = {"participant": "a@s.whatsapp.net", "quotedMessage": {"somethingElse": {}}}

        found = stub._goto_quoted_status("s1", ctx)

        assert found is False
