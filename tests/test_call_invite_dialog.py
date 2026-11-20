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


class Threads:
    """threading.Thread that runs its target at once."""
    def __init__(self, target, **_kw):
        self._target = target

    def start(self):
        self._target()


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


def on_add(mw, typed, monkeypatch, found="", dial="55"):
    monkeypatch.setattr(call_invite.threading, "Thread", Threads)
    monkeypatch.setattr(call_invite.wx, "CallAfter", lambda fn, *a: fn(*a))
    monkeypatch.setattr(call_invite, "resolve_typed_number", lambda _mw, _digits: found)
    done = []
    d = types.SimpleNamespace(
        _mw=mw, _busy=False, _alive=True, _allowed=lambda: True, _panes=[], _marks=set(),
        _number=types.SimpleNamespace(GetValue=lambda: typed, SetFocus=lambda: None),
        _country=types.SimpleNamespace(GetSelection=lambda: 0), _countries=[("Brasil (+55)", dial)],
        _connected=1, _call_id=CALL, _status=types.SimpleNamespace(SetLabel=lambda t: None),
        _finish_add=lambda spoken, error=None, invited=True: done.append((spoken, error, invited)))
    call_invite.CallInviteDialog._on_add(d)
    return done


def test_a_typed_number_is_invited_under_the_address_whatsapp_knows_it_by(monkeypatch):
    mw = Mw({})
    done = on_add(mw, "(11) 99999-0000", monkeypatch, found="551199990000@s.whatsapp.net")
    assert mw.posts[-1][1]["participants"] == ["551199990000@s.whatsapp.net"]
    assert done and done[0][1] is None and done[0][2] is True


def test_a_typed_number_stays_as_typed_when_the_lookup_says_nothing(monkeypatch):
    mw = Mw({})
    on_add(mw, "(11) 99999-0000", monkeypatch)
    assert mw.posts[-1][1]["participants"] == ["5511999990000@s.whatsapp.net"]


def test_every_field_of_the_dialog_has_its_own_label_right_before_it():
    """NVDA names a field by the StaticText just before it: the country combo
    sits between the custom-number label and the number field, so the field
    needs a label of its own."""
    import inspect
    src = inspect.getsource(call_invite.CallInviteDialog.__init__)
    country_field = src.index("self._country = wx.ComboBox")
    phone_label = src.index('i18n.t("add_member_phone_number_label")')
    number_field = src.index("self._number = wx.TextCtrl")
    assert country_field < phone_label < number_field
