"""Call links on the client: reading a link, the waiting room, and joining
(core/call_links.py, main_window/call_links.py)."""

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
    # only ever handed over by Windows, never taken from a message
    assert cl.call_link_in_text("veja whatsapp://call/voice/" + TOKEN) == ""


def test_a_message_carries_its_first_call_link():
    assert cl.call_link_in_text("Entra aí: " + VIDEO + " até já") == VIDEO
    assert cl.call_link_in_text("https://call.whatsapp.com/voice/short " + VOICE) == VOICE
    assert cl.call_link_in_text("sem link") == ""


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
    stub._on_call_link_joined(record, {"id": "L1", "admin": True})
    assert record["call_link_admin"] is True
    assert [(e["id"], e["state"], e["peerJid"]) for e in stub.events] == [("L1", "ACTIVE", "")]

    waiting = {"call_id": "L2", "call_link": VOICE}
    stub._active_voice_call = waiting
    stub._on_call_link_joined(waiting, {"waiting": True})
    assert len(stub.events) == 1 and stub.spoken == ["call_link_waiting_for_admin"]

    # a record that is no longer the active call is left alone
    stub._on_call_link_joined(record, {"id": "L1"})
    assert len(stub.events) == 1


def test_the_admin_hears_each_arrival_once_and_the_button_counts_them():
    stub = Stub()
    stub._call_link_room_call = "L1"
    stub._call_link_announced = set()
    pedro = {"jid": "1@lid", "name": "Pedro", "guest": True}
    ana = {"jid": "2@lid", "name": "Ana", "guest": False}
    stub._apply_call_link_room("L1", [pedro])
    stub._apply_call_link_room("L1", [pedro])
    stub._apply_call_link_room("L1", [pedro, ana])
    assert stub.spoken == ["Pedro quer entrar.", "Ana quer entrar."]
    assert stub.voice_call_window_waiting_room_button.label == "Sala de &espera (2)"
    stub._apply_call_link_room("L1", [])
    assert stub.voice_call_window_waiting_room_button.label == "Sala de &espera"
    # a late answer for another call, or a failed read, changes nothing
    stub._apply_call_link_room("OLD", [pedro])
    stub._apply_call_link_room("L1", None)
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
