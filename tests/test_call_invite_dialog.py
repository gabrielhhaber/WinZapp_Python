"""CallInviteDialog._load and _on_add, called on a plain stub: no window is
built and the worker thread runs inline."""
import types

from ui.dialogs import call_invite

CALL = "call-1"


class Response:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class Mw:
    def __init__(self, chats, locked=(), deleted=()):
        self.chats, self.contacts, self._lid_to_phone = chats, {}, {}
        self._locked, self._deleted = set(locked), set(deleted)
        self.posts, self.spoken = [], []
        self.i18n = types.SimpleNamespace(t=lambda key: key)

    def is_chat_locked(self, jid):
        return jid in self._locked

    def is_chat_deleted(self, jid):
        return jid in self._deleted

    def _is_self_jid(self, jid):
        return False

    def _chat_jids_equivalent(self, a, b):
        return a == b

    def chat_display_name(self, chat):
        return "name:" + chat["remoteJid"].split("@")[0]

    def _raise_for_call_response(self, response, _what):
        return response

    def output(self, text, interrupt=False):
        self.spoken.append(text)

    def _post_call_control(self, action, body, timeout=0):
        self.posts.append((action, body))
        if action == "participants":
            return Response({"response": {"id": CALL, "available": True, "participants": []}})
        return Response({"response": {"id": CALL, "results": [
            {"jid": jid, "status": "invited"} for jid in body["participants"]]}})


def dialog(mw, group=False):
    return types.SimpleNamespace(_mw=mw, _group=group, _chat="1@s.whatsapp.net", _call_id=CALL,
                                 _show_rows=None, _finish_add=None)


def test_a_locked_or_deleted_chat_is_not_offered_by_name(monkeypatch):
    shown = []
    monkeypatch.setattr(call_invite.wx, "CallAfter", lambda fn, *a: shown.append(a))
    mw = Mw({"11@s.whatsapp.net": {}, "22@s.whatsapp.net": {}, "33@s.whatsapp.net": {}},
            locked={"22@s.whatsapp.net"}, deleted={"33@s.whatsapp.net"})
    call_invite.CallInviteDialog._load(dialog(mw))
    lists, _connected, failed = shown[0]
    assert not failed
    assert [jid for jid, _name in lists[1]] == ["11@s.whatsapp.net"]
