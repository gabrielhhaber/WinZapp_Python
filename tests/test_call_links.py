"""Call links on the client: reading a link, the waiting room, and joining
(core/call_links.py, main_window/call_links.py)."""

import types

from core import call_links as cl
from main_window.call_links import CallLinksMixin

TOKEN = "AbCdEfGhIjKlMnOpQrStUv"
VOICE = "https://call.whatsapp.com/voice/" + TOKEN
VIDEO = "https://call.whatsapp.com/video/" + TOKEN


class I18n:
    def t(self, key):
        return {
            "call_link_waiting_one": "{name} quer entrar.",
            "call_link_waiting_many": "{count} querem entrar.",
            "call_link_unnamed_guest": "Um convidado",
            "call_link_unnamed_person": "Alguém",
            "call_link_waiting_room_button": "Sala de &espera",
            "call_link_waiting_room_button_count": "Sala de &espera ({count})",
        }.get(key, key)


def test_a_call_link_is_read_as_whatsapp_web_reads_it():
    assert cl.parse_call_link(VOICE) == (False, VOICE)
    assert cl.parse_call_link(" https://web.whatsapp.com/call/VIDEO/" + TOKEN + "?x=1 ") == (True, VIDEO)
    for wrong in ("", None, VOICE + "x", "https://call.whatsapp.com/voice/short",
                  "http://call.whatsapp.com/voice/" + TOKEN, "https://chat.whatsapp.com/" + TOKEN,
                  "veja " + VOICE):
        assert cl.parse_call_link(wrong) == (None, "")


def test_the_link_the_call_page_hands_to_windows_is_a_call_link():
    # WAWebCallLinkDesktopAppDetect opens whatsapp://call/<voice|video>/<token>
    assert cl.parse_call_link("whatsapp://call/voice/" + TOKEN) == (False, VOICE)
    assert cl.parse_call_link("WhatsApp://call/VIDEO/" + TOKEN + "/?auto_join=1") == (True, VIDEO)
    for wrong in ("whatsapp://call/voice/short", "whatsapp://call/audio/" + TOKEN,
                  "whatsapp://chat/voice/" + TOKEN, "whatsapp://call/voice/" + TOKEN + "x",
                  "whatsapp://call/voice/AbCdEfGhIjKlMnOpQrStÜv",
                  "whatsapp://call/voice/" + TOKEN + "?" + "a" * 600):
        assert cl.parse_call_link(wrong) == (None, "")


def test_a_link_with_non_ascii_word_characters_is_not_a_call_link():
    assert cl.parse_call_link("https://call.whatsapp.com/voice/AbCdEfGhIjKlMnOpQrStÜv") == (None, "")


def test_people_waiting_are_named_never_by_number_and_announced_once():
    response = {"people": [{"jid": "1@lid", "name": "Pedro", "guest": True},
                           {"jid": "2@lid", "name": "", "guest": True},
                           {"jid": "3@lid", "name": ""}, {"name": "sem endereço"}]}
    people = cl.waiting_people(response)
    assert [p["jid"] for p in people] == ["1@lid", "2@lid", "3@lid"]
    i18n = I18n()
    assert [cl.person_label(i18n, p) for p in people] == ["Pedro", "Um convidado", "Alguém"]
    assert cl.newly_waiting(set(), people[:1]) == people[:1]
    assert cl.newly_waiting(cl.waiting_jids(people[:1]), people[:1]) == []
    assert cl.arrivals_announcement(i18n, people[:1]) == "Pedro quer entrar."
    assert cl.arrivals_announcement(i18n, people) == "3 querem entrar."
    assert cl.arrivals_announcement(i18n, []) == ""
    assert cl.waiting_people(None) == [] and cl.waiting_people({"people": "x"}) == []


class Button:
    def __init__(self):
        self.label = ""

    def GetLabel(self):
        return self.label

    def SetLabel(self, label):
        self.label = label


class Stub(CallLinksMixin):
    def __init__(self, *, neonize=False, flags=()):
        self.i18n = I18n()
        self.spoken = []
        self.started = []
        self.events = []
        self.synced = 0
        self._active_voice_call = None
        self._neonize = neonize
        for flag in flags:
            setattr(self, flag, True)
        self.voice_call_window_waiting_room_button = Button()

    def _uses_neonize(self):
        return self._neonize

    def output(self, text, interrupt=False):
        self.spoken.append(text)

    def _start_call(self, *args, **kwargs):
        self.started.append((args, kwargs))

    def on_voice_call_state_event(self, event):
        self.events.append(event)

    def _sync_call_link_controls(self):
        self.synced += 1


def test_call_links_need_the_bridge_to_say_so_on_neonize():
    assert Stub()._call_links_supported() is True
    assert Stub(neonize=True)._call_links_supported() is False
    assert Stub(neonize=True, flags=("_neonize_calls",))._call_links_supported() is False
    assert Stub(neonize=True, flags=("_neonize_calls", "_neonize_call_links"))._call_links_supported() is True


def test_joining_a_link_starts_a_group_call_with_the_canonical_link():
    stub = Stub()
    assert stub.join_call_link("https://web.whatsapp.com/call/video/" + TOKEN) is True
    assert stub.started == [(("", "call_link_call_name"),
                             {"is_video": True, "is_group": True, "call_link": VIDEO})]
    assert stub.join_call_link("https://example.com") is False
    assert stub.spoken == ["call_link_invalid"] and len(stub.started) == 1


def test_the_join_answer_replays_connected_unless_the_user_waits_to_be_let_in():
    stub = Stub()
    record = {"call_id": "L1", "call_link": VOICE, "is_video": False}
    stub._active_voice_call = record
    stub._on_call_link_joined(record, {"id": "L1", "admin": True, "mediaEpoch": "a" * 32})
    assert record["call_link_admin"] is True
    assert [(e["id"], e["state"], e["peerJid"]) for e in stub.events] == [("L1", "ACTIVE", "")]
    # the epoch the microphone and speaker bind to travels with the replay
    assert stub.events[0]["mediaEpoch"] == "a" * 32

    waiting = {"call_id": "L2", "call_link": VOICE}
    stub._active_voice_call = waiting
    bound = []
    stub._call_audio_session = types.SimpleNamespace(set_media_epoch=bound.append)
    stub._on_call_link_joined(waiting, {"waiting": True, "mediaEpoch": "b" * 32})
    assert len(stub.events) == 1 and stub.spoken == ["call_link_waiting_for_admin"]
    # waiting, the audio still binds to the call's epoch: an admission that
    # lands before this answer is dropped and nothing would replay it
    assert waiting["media_epoch"] == "b" * 32 and bound == ["b" * 32]

    # a record that is no longer the active call is left alone
    stub._on_call_link_joined(record, {"id": "L1"})
    assert len(stub.events) == 1


def test_the_admin_hears_each_arrival_once_and_the_button_counts_them():
    stub = Stub()
    stub._call_link_room_call = "L1"
    stub._call_link_announced = set()
    pedro = {"jid": "1@lid", "name": "Pedro", "guest": True}
    ana = {"jid": "2@lid", "name": "Ana", "guest": False}
    stub._active_voice_call = {"call_id": "L1", "call_link": VOICE, "call_link_admin": True}
    stub._apply_call_link_room("L1", [pedro], True)
    stub._apply_call_link_room("L1", [pedro], True)
    stub._apply_call_link_room("L1", [pedro, ana], True)
    assert stub.spoken == ["Pedro quer entrar.", "Ana quer entrar."]
    assert stub.voice_call_window_waiting_room_button.label == "Sala de &espera (2)"
    stub._apply_call_link_room("L1", [], True)
    assert stub.voice_call_window_waiting_room_button.label == "Sala de &espera"
    # a late answer for another call, or a failed read, changes nothing
    stub._apply_call_link_room("OLD", [pedro], True)
    stub._apply_call_link_room("L1", None, True)
    assert stub._call_link_waiting == [] and len(stub.spoken) == 2


def test_only_a_joined_link_call_has_a_waiting_room():
    stub = Stub()
    assert stub._active_call_link() is None
    stub._active_voice_call = {"call_id": "outgoing:" + VOICE, "call_link": VOICE}
    assert stub._active_call_link() is None
    stub._active_voice_call = {"call_id": "G1"}
    assert stub._active_call_link() is None
    stub._active_voice_call = {"call_id": "L1", "call_link": VOICE}
    assert stub._active_call_link() is stub._active_voice_call


def test_a_member_who_is_not_the_admin_hears_no_arrivals_and_sees_no_room():
    stub = Stub()
    stub._call_link_room_call = "L1"
    stub._call_link_announced = set()
    record = stub._active_voice_call = {"call_id": "L1", "call_link": VOICE, "call_link_admin": True}
    pedro = {"jid": "1@lid", "name": "Pedro", "guest": True}
    stub._apply_call_link_room("L1", [pedro], False)
    assert stub.spoken == [] and stub._call_link_waiting == []
    assert record["call_link_admin"] is False and stub.synced == 1
    # the answer is read each time: becoming admin brings the room back
    stub._apply_call_link_room("L1", [pedro], True)
    assert record["call_link_admin"] is True and stub.spoken == ["Pedro quer entrar."]


def test_the_waiting_room_answer_says_whether_this_user_is_the_admin():
    assert cl.waiting_room_admin({"admin": True, "people": []}) is True
    assert cl.waiting_room_admin({"people": []}) is False
    assert cl.waiting_room_admin({"admin": "yes"}) is False
    assert cl.waiting_room_admin(None) is False


class Control:
    def __init__(self, shown=False):
        self.shown, self.layouts = shown, 0

    def IsShown(self):
        return self.shown

    def Show(self, shown):
        self.shown = shown

    def GetParent(self):
        return self

    def Layout(self):
        self.layouts += 1


class Timer(Control):
    def __init__(self):
        super().__init__()
        self.running = False

    def IsRunning(self):
        return self.running

    def Start(self, ms):
        self.running = True

    def Stop(self):
        self.running = False


def test_the_waiting_room_button_is_only_the_admins_and_the_room_is_read_for_every_link_call():
    stub = Stub()
    stub.voice_call_window_waiting_room_button = button = Control()
    stub._call_link_room_timer = timer = Timer()
    stub._active_voice_call = {"call_id": "L1", "call_link": VOICE, "call_link_admin": False}
    CallLinksMixin._sync_call_link_controls(stub)
    assert button.shown is False and timer.running and stub._call_link_room_call == "L1"
    stub._active_voice_call["call_link_admin"] = True
    CallLinksMixin._sync_call_link_controls(stub)
    assert button.shown is True
    stub._active_voice_call = None
    CallLinksMixin._sync_call_link_controls(stub)
    assert button.shown is False and not timer.running and stub._call_link_room_call == ""


class Response:
    def __init__(self, body, status=200):
        self._body, self.status_code = body, status

    def json(self):
        return self._body


class Inline:
    """threading.Thread that runs its target at once."""
    def __init__(self, target, **_kw):
        self._target = target

    def start(self):
        self._target()


def run_inline(monkeypatch):
    from main_window import call_links
    monkeypatch.setattr(call_links.threading, "Thread", Inline)
    monkeypatch.setattr(call_links.wx, "CallAfter", lambda fn, *a: fn(*a))


def test_polling_the_room_reads_the_people_and_whether_this_user_is_admin(monkeypatch):
    run_inline(monkeypatch)
    stub = Stub()
    stub._active_voice_call = {"call_id": "L1", "call_link": VOICE, "call_link_admin": True}
    stub._call_link_room_call = "L1"
    stub._call_link_announced = set()
    posts = []
    answer = {"response": {"id": "L1", "admin": True,
                           "people": [{"jid": "1@lid", "name": "Pedro", "guest": True}]}}
    stub._post_call_control = lambda action, body: posts.append((action, body)) or Response(answer)
    stub._poll_call_link_room()
    assert posts == [("link/waiting-room", {"callId": "L1"})]
    assert stub.spoken == ["Pedro quer entrar."] and stub._call_link_room_busy is False
    # a failed read keeps the last state and frees the poll
    stub._post_call_control = lambda action, body: Response({}, 500)
    stub._poll_call_link_room()
    assert stub.spoken == ["Pedro quer entrar."] and stub._call_link_room_busy is False
    # nothing is read once the window is closing
    stub._shutting_down = True
    stub._post_call_control = lambda *a: posts.append("late")
    stub._poll_call_link_room()
    assert "late" not in posts


def test_creating_a_link_hands_back_the_canonical_link_or_a_speakable_error(monkeypatch):
    run_inline(monkeypatch)
    stub = Stub()
    stub._raise_for_call_response = lambda response, _what: response
    stub._call_error_text = lambda exc: "HTTP 500"
    done = []
    stub._post_call_control = lambda action, body, timeout=0: Response(
        {"response": {"link": VIDEO + "?x=1"}})
    stub.create_call_link(True, lambda link, error: done.append((link, error)))
    assert done == [(VIDEO, "")]
    stub._post_call_control = lambda action, body, timeout=0: Response({"response": {"link": "nope"}})
    stub.create_call_link(True, lambda link, error: done.append((link, error)))
    assert done[-1] == ("", "call_link_create_failed_generic")
    stub._post_call_control = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
    stub.create_call_link(False, lambda link, error: done.append((link, error)))
    assert done[-1] == ("", "HTTP 500")
    unsupported = Stub(neonize=True)
    unsupported.create_call_link(False, lambda link, error: done.append((link, error)))
    assert done[-1] == ("", "call_log_action_unavailable")


def test_the_waiting_room_opens_once_and_decisions_are_spoken_by_name(monkeypatch):
    run_inline(monkeypatch)
    stub = Stub()
    stub.show_call_waiting_room()
    assert stub.spoken == ["call_link_no_waiting_room"]
    posts, polled, done = [], [], []
    stub._raise_for_call_response = lambda response, _what: response
    stub._post_call_control = lambda action, body: posts.append((action, body)) or Response({})
    stub._poll_call_link_room = lambda: polled.append(1)
    person = {"jid": "1@lid", "name": "Pedro", "guest": True}
    stub.decide_call_link_person("L1", person, True, done.append)
    stub.decide_call_link_person("L1", person, False, done.append)
    assert posts == [("link/admit", {"callId": "L1", "jid": "1@lid", "admit": True}),
                     ("link/admit", {"callId": "L1", "jid": "1@lid", "admit": False})]
    assert stub.spoken[1:] == ["call_link_admitted", "call_link_denied"]
    assert done == [True, True] and len(polled) == 2
    stub._raise_for_call_response = lambda *a: (_ for _ in ()).throw(RuntimeError("no"))
    stub.decide_call_link_person("L1", person, True, done.append)
    assert stub.spoken[-1] == "call_link_decision_failed" and done[-1] is False


def test_a_link_join_with_no_call_id_is_a_failure():
    import pytest
    assert cl.joined_call_id({"id": "L1", "waiting": True}) == "L1"
    for body in ({}, {"id": ""}, None, "x"):
        with pytest.raises(ValueError):
            cl.joined_call_id(body)


def test_a_link_is_not_asked_about_while_a_call_is_active():
    stub = Stub()
    stub._active_voice_call = {"call_id": "G1"}
    assert stub.join_call_link(VOICE, confirm_parent=object()) is False
    assert stub.spoken == ["voice_call_already_active"] and stub.started == []
    stub._active_voice_call = None
    stub._active_incoming_calls = {"X": {}}
    assert stub.join_call_link(VOICE) is False and stub.started == []
