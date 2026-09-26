"""Accessible incoming-call alert lifecycle."""

from types import SimpleNamespace

import wx

from core.call_logic import incoming_call_can_answer
from core.websocket_client import WebSocketClient
from main import MainWindow


class _Sound:
    def __init__(self):
        self.play_calls = 0
        self.stop_calls = 0

    def play(self):
        self.play_calls += 1

    def stop(self):
        self.stop_calls += 1


class _Dialog:
    def __init__(self):
        self.closed = False
        self.refreshed_messages = []

    def close_from_call_lifecycle(self):
        self.closed = True

    def refresh_labels(self, message=None):
        self.refreshed_messages.append(message)


class _Bar:
    def __init__(self):
        self.shown = False

    def Show(self):
        self.shown = True

    def Hide(self):
        self.shown = False

    def IsShown(self):
        return self.shown


class _Label:
    def __init__(self):
        self.text = ""

    def SetLabel(self, text):
        self.text = text


class _Button(_Label):
    def __init__(self):
        super().__init__()
        self.enabled = True

    def Enable(self, enable=True):
        self.enabled = bool(enable)


class _I18n:
    def t(self, key):
        return {
            "incoming_call_announcement": "{name} está te ligando.",
            "incoming_video_call_announcement": "{name} está te ligando por vídeo.",
            "incoming_group_call_announcement": "Chamada em grupo recebida no grupo {name}.",
            "unknown_contact": "Contato desconhecido",
            "unknown_group": "Grupo sem nome",
            "incoming_call_answer_button": "Atender",
            "incoming_call_reject_button": "Recusar",
            "incoming_call_silence_button": "Silenciar alerta",
            "incoming_call_answered": "Ligação atendida.",
            "incoming_call_answer_failed": "Falha ao atender: {error}",
            "incoming_call_reject_failed": "Falha ao recusar: {error}",
            "incoming_call_end_failed": "Falha ao desligar: {error}",
            "voice_call_active_label": "Ligação de voz com {name}.",
            "voice_call_individual_only": "Apenas individual.",
            "voice_call_already_active": "Já em ligação.",
            "voice_call_starting": "Ligando para {name}.",
            "voice_call_start_failed": "Falha ao ligar: {error}",
            "voice_call_connected": "Ligação conectada.",
            "voice_call_ended": "Ligação encerrada.",
        }.get(key, key)


class _MainStub:
    _CALL_RINGING_STATES = MainWindow._CALL_RINGING_STATES
    _CALL_EVENT_START_GRACE_SECONDS = MainWindow._CALL_EVENT_START_GRACE_SECONDS
    _normalize_jid = staticmethod(MainWindow._normalize_jid)
    _group_name_from_chat_dict = staticmethod(MainWindow._group_name_from_chat_dict)
    on_incoming_call_event = MainWindow.on_incoming_call_event
    _expire_incoming_call_alert = MainWindow._expire_incoming_call_alert
    stop_incoming_call_alert = MainWindow.stop_incoming_call_alert
    stop_all_incoming_call_alerts = MainWindow.stop_all_incoming_call_alerts
    _close_incoming_call_dialog = MainWindow._close_incoming_call_dialog
    _sync_incoming_call_bar = MainWindow._sync_incoming_call_bar
    _refresh_call_language_surfaces = MainWindow._refresh_call_language_surfaces
    _first_incoming_call_identity = MainWindow._first_incoming_call_identity
    _call_control_payload = MainWindow._call_control_payload
    _stop_active_voice_call_if_matches = MainWindow._stop_active_voice_call_if_matches
    on_call_remote_audio = MainWindow.on_call_remote_audio
    on_voice_call_state_event = MainWindow.on_voice_call_state_event
    _stop_incoming_call_audio_monitor = MainWindow._stop_incoming_call_audio_monitor
    _has_answerable_incoming_call = MainWindow._has_answerable_incoming_call
    # The real bridged comparison, not string equality: the whole point of
    # core/call_matching.py is that one call's events do not agree on whether
    # the peer is an @lid or a phone JID.
    _chat_jids_equivalent = MainWindow._chat_jids_equivalent
    _jid_address_forms = MainWindow._jid_address_forms

    _end_active_call_locally = MainWindow._end_active_call_locally

    def _watch_ended_call_log(self, call_id, peer_jid, outgoing):
        self.watched_call_logs.append((call_id, peer_jid, outgoing))

    def _confirm_call_ended(self, active, call_id, peer_jid, *, upgraded_to_video=False):
        # The page is asked first in production (tests/test_call_video_upgrade.py);
        # here it has already answered "gone".
        self._end_active_call_locally(active, call_id, peer_jid)

    def _ensure_page_call_ended(self, call_id):
        self.ensured_ended = getattr(self, "ensured_ended", []) + [call_id]

    def __init__(self):
        self._active_incoming_calls = {}
        self._incoming_call_details = {}
        self._incoming_call_watchdogs = {}
        self._incoming_call_dialogs = {}
        self._active_voice_call = None
        self._call_audio_session = None
        self._call_ring_audio_session = None
        self.ring_monitor_starts = []
        self.ring_monitor_stops = 0
        self.call_incoming_sound = _Sound()
        self.settings = {"calls": {"alerts_enabled": True, "popup_enabled": True}}
        self.i18n = _I18n()
        self.announcements = []
        self.watched_call_logs = []
        self.armed_watchdogs = []
        self.cancelled_watchdogs = []
        self.chats = {}
        self._lid_to_phone = {}
        self._phone_to_lid = {}
        self._group_name_cache = {}
        self.popups = []
        self.incoming_call_bar = _Bar()
        self.incoming_call_label = _Label()
        self.incoming_call_answer_button = _Button()
        self.incoming_call_reject_button = _Button()
        self.incoming_call_stop_button = _Button()
        self.voice_call_bar = _Bar()
        self.voice_call_label = _Label()
        self.layout_calls = 0
        self.wpp_server = "http://127.0.0.1"
        self.wpp_port = 6300
        self.token = "session-token"
        self._wa_startup_time = 2_000_000_000
        self._voice_call_last_announced_state = ""

    def _arm_incoming_call_watchdog(self, identity):
        self.armed_watchdogs.append(identity)

    def _cancel_incoming_call_watchdog(self, identity):
        self.cancelled_watchdogs.append(identity)

    def _preview_sender_from_jid(self, jid):
        return "Fulano" if jid else ""

    def output(self, text, interrupt=False):
        self.announcements.append((text, interrupt))

    def _show_incoming_call_dialog(self, identity, message):
        self.popups.append((identity, message))

    def Layout(self):
        self.layout_calls += 1

    def _start_incoming_call_audio_monitor(self, identity):
        self.ring_monitor_starts.append(identity)
        return True

    def _stop_incoming_call_audio_monitor(self):
        self.ring_monitor_stops += 1
        self._call_ring_audio_session = None


def _offer(call_id="call-1", peer="5511999999999@s.whatsapp.net"):
    return {
        "event": "offer",
        "state": "INCOMING_RING",
        "id": call_id,
        "peerJid": peer,
    }


def test_offer_announces_and_starts_loop_only_once():
    stub = _MainStub()

    stub.on_incoming_call_event(_offer())
    stub.on_incoming_call_event(_offer())

    assert stub.announcements == [("Fulano está te ligando.", True)]
    assert stub.call_incoming_sound.play_calls == 1
    assert stub.armed_watchdogs == ["call-1"]
    assert stub._active_incoming_calls == {
        "call-1": "5511999999999@s.whatsapp.net"
    }
    assert stub.popups == [("call-1", "Fulano está te ligando.")]
    assert stub.ring_monitor_starts == ["call-1"]


def test_disabled_call_alerts_ignore_new_offer():
    stub = _MainStub()
    stub.settings["calls"]["alerts_enabled"] = False

    stub.on_incoming_call_event(_offer())

    assert stub.announcements == []
    assert stub.call_incoming_sound.play_calls == 0
    assert stub._active_incoming_calls == {}


def test_offer_from_before_current_session_is_ignored():
    stub = _MainStub()
    event = _offer()
    event["timestamp"] = int(stub._wa_startup_time) - 60

    stub.on_incoming_call_event(event)

    assert stub.announcements == []
    assert stub.call_incoming_sound.play_calls == 0
    assert stub._active_incoming_calls == {}


def test_offer_inside_startup_grace_is_still_live():
    stub = _MainStub()
    event = _offer()
    event["timestamp"] = int(stub._wa_startup_time) - 3

    stub.on_incoming_call_event(event)

    assert stub.call_incoming_sound.play_calls == 1
    assert set(stub._active_incoming_calls) == {"call-1"}


def test_offer_stores_call_details_for_real_answer_or_reject():
    stub = _MainStub()

    stub.on_incoming_call_event(_offer())

    assert stub._incoming_call_details["call-1"] == {
        "call_id": "call-1",
        "peer_jid": "5511999999999@s.whatsapp.net",
        "group_jid": "",
        "is_video": False,
        "is_group": False,
        "name": "Fulano",
        "message": "Fulano está te ligando.",
    }
    assert stub._call_control_payload("call-1") == {"callId": "call-1"}


def test_call_control_payload_keeps_real_whatsapp_ids_with_at_sign():
    stub = _MainStub()
    call_id = "false_5511999999999@s.whatsapp.net_ABC123"

    stub.on_incoming_call_event(_offer(call_id=call_id))

    assert stub._call_control_payload(call_id) == {"callId": call_id}


def test_call_control_payload_does_not_send_peer_jid_as_call_id():
    stub = _MainStub()
    stub._incoming_call_details["5511999999999@s.whatsapp.net"] = {"peer_jid": "5511999999999@s.whatsapp.net"}

    assert stub._call_control_payload("5511999999999@s.whatsapp.net") == {}


def test_offer_received_while_offline_is_ignored_even_if_recent():
    stub = _MainStub()
    event = _offer()
    event.update({
        "timestamp": int(stub._wa_startup_time) + 1,
        "receivedWhileOffline": True,
    })

    stub.on_incoming_call_event(event)

    assert stub.call_incoming_sound.play_calls == 0
    assert stub._active_incoming_calls == {}


def test_popup_can_be_disabled_without_disabling_spoken_and_sound_alert():
    stub = _MainStub()
    stub.settings["calls"]["popup_enabled"] = False

    stub.on_incoming_call_event(_offer())

    assert stub.announcements == [("Fulano está te ligando.", True)]
    assert stub.call_incoming_sound.play_calls == 1
    assert stub.popups == []
    assert stub.incoming_call_bar.shown is True
    assert stub.incoming_call_label.text == "Fulano está te ligando."


def test_in_window_stop_button_clears_non_popup_call_surface():
    stub = _MainStub()
    stub.settings["calls"]["popup_enabled"] = False
    stub.on_incoming_call_event(_offer())

    stub.stop_all_incoming_call_alerts()

    assert stub._active_incoming_calls == {}
    assert stub.incoming_call_bar.shown is False
    assert stub.call_incoming_sound.stop_calls == 1


def test_group_offer_is_announced_by_group_name_but_cannot_be_answered():
    """REGRESSION: the group offer used to be dropped before the announcement,
    the ring tone and even the log line. WhatsApp Web's own ringtone is muted
    by callMediaBridge, so a blind user got NO signal at all that their phone
    was ringing, and log.log had nothing to explain it afterwards.

    WPPConnect still cannot answer a group call, so the offer is announced and
    shown like any other -- only `is_group` keeps the Answer button disabled.
    """
    stub = _MainStub()
    group_jid = "120363427511142886@g.us"
    stub.chats[group_jid] = {
        "remoteJid": group_jid,
        "groupMetadata": {"subject": "Família"},
    }

    event = _offer(peer="5511888888888@lid")
    event.update({"isGroup": True, "groupJid": group_jid})
    stub.on_incoming_call_event(event)

    assert stub.announcements == [("Chamada em grupo recebida no grupo Família.", True)]
    assert stub.call_incoming_sound.play_calls == 1
    assert stub.popups == [("call-1", "Chamada em grupo recebida no grupo Família.")]
    assert stub._active_incoming_calls != {}
    details = stub._incoming_call_details["call-1"]
    assert details["is_group"] is True
    assert details["name"] == "Família"
    assert incoming_call_can_answer(details) is False
    # The receive-only monitor exists so ANSWERING can promote the same session
    # to full duplex. A group offer never gets that far, so opening an output
    # stream for it would hold the device for nothing.
    assert stub.ring_monitor_starts == []


def test_group_offer_still_obeys_the_calls_alert_setting():
    """The group alert is an ordinary incoming-call alert, so "allow incoming
    call alerts" turns it off exactly like a one-to-one one."""
    stub = _MainStub()
    stub.settings["calls"] = {"alerts_enabled": False}
    group_jid = "120363427511142886@g.us"
    stub.chats[group_jid] = {
        "remoteJid": group_jid,
        "groupMetadata": {"subject": "Família"},
    }

    event = _offer(peer="5511888888888@lid")
    event.update({"isGroup": True, "groupJid": group_jid})
    stub.on_incoming_call_event(event)

    assert stub.announcements == []
    assert stub._active_incoming_calls == {}


def test_video_offer_announces_and_can_be_answered():
    stub = _MainStub()
    event = _offer()
    event["isVideo"] = True

    stub.on_incoming_call_event(event)

    assert stub.announcements == [("Fulano está te ligando por vídeo.", True)]
    assert stub._incoming_call_details["call-1"]["is_video"] is True


def test_answered_or_ended_state_stops_the_tone():
    stub = _MainStub()
    stub.on_incoming_call_event(_offer())
    dialog = _Dialog()
    stub._incoming_call_dialogs["call-1"] = dialog

    stub.on_incoming_call_event({"event": "state", "state": "HANDLED_REMOTELY", "id": "call-1"})

    assert stub._active_incoming_calls == {}
    assert stub.call_incoming_sound.stop_calls == 1
    assert stub.cancelled_watchdogs == ["call-1"]
    assert dialog.closed is True
    assert stub._incoming_call_dialogs == {}
    assert stub.ring_monitor_stops == 1


def test_ringing_state_update_does_not_stop_the_tone():
    stub = _MainStub()
    stub.on_incoming_call_event(_offer())

    stub.on_incoming_call_event({
        "event": "state", "state": "INCOMING_RING", "id": "call-1"
    })

    assert set(stub._active_incoming_calls) == {"call-1"}
    assert stub.call_incoming_sound.stop_calls == 0


def test_numeric_received_call_state_keeps_the_tone_playing():
    stub = _MainStub()
    stub.on_incoming_call_event(_offer())

    stub.on_incoming_call_event({
        "event": "state", "state": "3", "id": "call-1"
    })

    assert set(stub._active_incoming_calls) == {"call-1"}
    assert stub.call_incoming_sound.stop_calls == 0


def test_watchdog_stops_a_call_when_no_terminal_event_arrives():
    stub = _MainStub()
    stub.on_incoming_call_event(_offer())

    stub._expire_incoming_call_alert("call-1")

    assert stub._active_incoming_calls == {}
    assert stub.call_incoming_sound.stop_calls == 1


def test_one_ended_call_does_not_silence_another_ringing_call():
    stub = _MainStub()
    stub.on_incoming_call_event(_offer("call-1"))
    stub.on_incoming_call_event(_offer("call-2", "5511888888888@s.whatsapp.net"))

    stub.on_incoming_call_event({"event": "ended", "state": "ENDED", "id": "call-1"})

    assert set(stub._active_incoming_calls) == {"call-2"}
    assert stub.call_incoming_sound.stop_calls == 0


def test_stop_button_only_stops_the_local_alert():
    stub = _MainStub()
    stub._active_incoming_calls["call-1"] = "peer@s.whatsapp.net"

    stub.stop_incoming_call_alert("call-1")

    assert stub._active_incoming_calls == {}
    assert stub.call_incoming_sound.stop_calls == 1
    assert stub.announcements == []


def test_remote_call_audio_is_forwarded_to_python_session():
    stub = _MainStub()
    received = []
    stub._call_audio_session = SimpleNamespace(
        enqueue_remote_audio=lambda pcm, sample_rate: received.append((pcm, sample_rate))
    )

    stub.on_call_remote_audio(b"\x01\x02", 48000)

    assert received == [(b"\x01\x02", 48000)]


def test_websocket_normalizes_nested_call_payload(monkeypatch):
    delivered = []
    monkeypatch.setattr(wx, "CallAfter", lambda fn, *args: fn(*args))
    stub = SimpleNamespace(
        instance_name="session-a",
        main_window=SimpleNamespace(on_incoming_call_event=delivered.append),
    )
    stub._belongs_to_this_session = WebSocketClient._belongs_to_this_session.__get__(stub)
    stub._clean_jid = WebSocketClient._clean_jid.__get__(stub)
    stub._call_timestamp_seconds = WebSocketClient._call_timestamp_seconds

    WebSocketClient.on_wpp_incoming_call(stub, {
        "session": "session-a",
        "data": {
            "event": "offer",
            "state": "INCOMING_RING",
            "id": "abc",
            "peerJid": {"_serialized": "5511999999999@c.us"},
            "groupJid": {"_serialized": "120363427511142886@g.us"},
            "isVideo": True,
            "offerTime": 2_000_000_001_000,
            "observedAt": 2_000_000_002,
        },
    })

    assert delivered == [{
        "event": "offer",
        "state": "INCOMING_RING",
        "id": "abc",
        "peerJid": "5511999999999@s.whatsapp.net",
        "groupJid": "120363427511142886@g.us",
        "isVideo": True,
        "isGroup": False,
        "timestamp": 2_000_000_001,
        "observedAt": 2_000_000_002,
        "receivedWhileOffline": False,
    }]


def test_call_timestamp_normalizer_handles_seconds_millis_and_micros():
    normalize = WebSocketClient._call_timestamp_seconds

    assert normalize(2_000_000_001) == 2_000_000_001
    assert normalize(2_000_000_001_000) == 2_000_000_001
    assert normalize(2_000_000_001_000_000) == 2_000_000_001
    assert normalize("invalid") == 0
    assert normalize(True) == 0


def test_websocket_normalizes_call_state_payload(monkeypatch):
    delivered = []
    monkeypatch.setattr(wx, "CallAfter", lambda fn, *args: fn(*args))
    stub = SimpleNamespace(
        instance_name="session-a",
        main_window=SimpleNamespace(on_voice_call_state_event=delivered.append),
    )
    stub._belongs_to_this_session = WebSocketClient._belongs_to_this_session.__get__(stub)

    WebSocketClient.on_wpp_call_state(stub, {
        "session": "session-a",
        "data": {
            "event": "state",
            "state": "active",
            "id": "call-1",
            "peer_jid": "5511999999999@s.whatsapp.net",
        },
    })

    # peer_jid is folded into the one canonical peerJid key, not kept
    # alongside it — see on_wpp_call_state()'s own comment on why a second
    # place to read the peer from is worse than none.
    assert delivered == [{
        "event": "state",
        "state": "ACTIVE",
        "id": "call-1",
        "peerJid": "5511999999999@s.whatsapp.net",
        "outgoing": False,
        "isVideo": False,
        "isGroup": False,
    }]


def test_stale_terminal_event_for_same_peer_does_not_end_current_call(monkeypatch):
    stub = _MainStub()
    stub._active_voice_call = {
        "identity": "outgoing:5511999999999@s.whatsapp.net",
        "call_id": "current-call",
        "peer_jid": "5511999999999@s.whatsapp.net",
        "name": "Fulano",
    }
    stopped = []
    stub._stop_voice_call_audio = lambda: stopped.append(True)
    stub._sync_voice_call_bar = lambda: None

    stub.on_voice_call_state_event({
        "event": "timeout",
        "state": "NOT_ANSWERED",
        "id": "old-call",
        "peerJid": "5511999999999@s.whatsapp.net",
    })

    assert stopped == []
    assert stub._active_voice_call["call_id"] == "current-call"


def test_websocket_forwards_remote_call_audio_to_main_window():
    delivered = []
    stub = SimpleNamespace(
        instance_name="session-a",
        main_window=SimpleNamespace(
            on_call_remote_audio=lambda pcm, sample_rate: delivered.append((pcm, sample_rate))
        ),
    )
    stub._belongs_to_this_session = WebSocketClient._belongs_to_this_session.__get__(stub)

    WebSocketClient.on_call_audio_remote(stub, {
        "session": "session-a",
        "sampleRate": "48000",
        "pcm": {"type": "Buffer", "data": [1, 2, 3]},
    })

    assert delivered == [(b"\x01\x02\x03", 48000)]


def test_websocket_forwards_remote_call_video_and_counts_received_frames():
    delivered = []
    stub = SimpleNamespace(
        instance_name="session-a",
        _call_remote_video_frames_received=0,
        main_window=SimpleNamespace(on_call_remote_video=delivered.append),
    )
    stub._belongs_to_this_session = WebSocketClient._belongs_to_this_session.__get__(stub)

    WebSocketClient.on_call_video_remote(stub, {
        "session": "session-a",
        "jpeg": {"type": "Buffer", "data": [1, 2, 3]},
    })

    assert delivered == [b"\x01\x02\x03"]
    assert stub._call_remote_video_frames_received == 1


def test_remote_call_audio_uses_ringing_monitor_before_answer():
    stub = _MainStub()
    received = []
    stub._call_ring_audio_session = SimpleNamespace(
        enqueue_remote_audio=lambda pcm, sample_rate: received.append((pcm, sample_rate))
    )

    stub.on_call_remote_audio(b"\x03\x04", 48000)

    assert received == [(b"\x03\x04", 48000)]


def test_language_change_retranslates_an_already_ringing_call():
    stub = _MainStub()
    stub._active_incoming_calls["call-1"] = "5511999999999@s.whatsapp.net"
    stub._incoming_call_details["call-1"] = {
        "call_id": "call-1",
        "peer_jid": "5511999999999@s.whatsapp.net",
        "is_video": True,
        "name": "Fulano",
        "message": "OLD LANGUAGE",
    }
    dialog = _Dialog()
    stub._incoming_call_dialogs["call-1"] = dialog

    stub._refresh_call_language_surfaces()

    expected = "Fulano está te ligando por vídeo."
    assert stub._incoming_call_details["call-1"]["message"] == expected
    assert dialog.refreshed_messages == [expected]


def test_isgroup_without_a_group_jid_is_treated_as_an_individual_call():
    """REGRESSION: the Node side now infers isGroup from participant count
    (groupParticipantCountOf(call) > 1), so the flag can be asserted for a
    real one-to-one call. Trusting it alone announced "incoming group call in
    Unnamed group" instead of the caller's name AND disabled the Answer button
    via incoming_call_can_answer() -- the user simply could not answer a call
    from a friend. CLAUDE.md's rule applies in both directions: a @g.us is not
    trustworthy alone, and neither is a group claim with no @g.us behind it.
    """
    stub = _MainStub()

    event = _offer()
    event["isGroup"] = True  # asserted, but no groupJid and a phone peerJid

    stub.on_incoming_call_event(event)

    assert stub.announcements == [("Fulano está te ligando.", True)]
    details = stub._incoming_call_details["call-1"]
    assert details["is_group"] is False
    assert incoming_call_can_answer(details) is True
    # A real one-to-one call still gets its receive-only monitor.
    assert stub.ring_monitor_starts == ["call-1"]


def test_dismissing_the_only_answerable_call_releases_the_speaker():
    """REGRESSION: the receive-only monitor is keyed to a call that can be
    ANSWERED, but it was released only when _active_incoming_calls emptied.
    Group offers now sit in that dictionary too -- announced, never answerable,
    and they start no monitor -- so dismissing the one-to-one call left the
    speaker held open for as long as a group offer kept ringing beside it."""
    stub = _MainStub()
    group_jid = "120363427511142886@g.us"
    stub.chats[group_jid] = {"remoteJid": group_jid, "groupMetadata": {"subject": "Família"}}

    stub.on_incoming_call_event(_offer(call_id="one-to-one"))
    group_event = _offer(call_id="group-call", peer="5511888888888@lid")
    group_event.update({"isGroup": True, "groupJid": group_jid})
    stub.on_incoming_call_event(group_event)

    assert stub.ring_monitor_starts == ["one-to-one"]
    assert stub.ring_monitor_stops == 0

    stub.stop_incoming_call_alert("one-to-one")

    # The group offer is still ringing, so the alert list is not empty -- but
    # nothing answerable remains, so the speaker must be released.
    assert stub._active_incoming_calls == {"group-call": "5511888888888@lid"}
    assert stub.ring_monitor_stops == 1


def test_dismissing_one_of_two_answerable_calls_keeps_the_speaker():
    """The other half: a second answerable call still needs the monitor."""
    stub = _MainStub()
    stub.on_incoming_call_event(_offer(call_id="first"))
    stub.on_incoming_call_event(_offer(call_id="second", peer="5511777777777@s.whatsapp.net"))

    stub.stop_incoming_call_alert("first")

    assert stub.ring_monitor_stops == 0


def _ringing_group_offer(stub, call_id="group-call"):
    group_jid = "120363427511142886@g.us"
    stub.chats[group_jid] = {"remoteJid": group_jid, "groupMetadata": {"subject": "Família"}}
    event = _offer(call_id=call_id, peer="5511888888888@lid")
    event.update({"isGroup": True, "groupJid": group_jid})
    stub.on_incoming_call_event(event)


def test_the_watchdog_timeout_also_releases_the_speaker():
    """REGRESSION: the answerable-call gate was applied only in
    stop_incoming_call_alert(). The watchdog path kept the old emptiness test,
    so a one-to-one call expiring beside a still-ringing group offer left the
    speaker held -- and with exclusive_output on, that silences the screen
    reader with nothing on screen to explain it."""
    stub = _MainStub()
    stub.on_incoming_call_event(_offer(call_id="one-to-one"))
    _ringing_group_offer(stub)
    assert stub.ring_monitor_starts == ["one-to-one"]

    stub._expire_incoming_call_alert("one-to-one")

    assert "group-call" in stub._active_incoming_calls
    assert stub.ring_monitor_stops == 1


def test_a_terminal_call_event_also_releases_the_speaker():
    """Same gap in the non-ringing branch of on_incoming_call_event()."""
    stub = _MainStub()
    stub.on_incoming_call_event(_offer(call_id="one-to-one"))
    _ringing_group_offer(stub)

    stub.on_incoming_call_event({
        "event": "callstate",
        "state": "ENDED",
        "id": "one-to-one",
        "peerJid": "5511999999999@s.whatsapp.net",
    })

    assert "group-call" in stub._active_incoming_calls
    assert stub.ring_monitor_stops == 1


class TestIncomingCallPopupDoesNotPinItselfOnTop:
    """Reported live: while a call rang, Alt+Tab to WinZapp's main window
    always landed back on the incoming-call popup -- the same defect fixed for
    the call window in 7f50df41, from the same causes. Checked statically, as
    that fix was: constructing a real wx.Dialog opens a window on whoever runs
    the suite (CLAUDE.md rule 1)."""

    SOURCE = (
        __import__("pathlib").Path(__file__).parents[1]
        / "client" / "ui" / "dialogs" / "incoming_call.py"
    ).read_text(encoding="utf-8")

    def _init_call(self):
        start = self.SOURCE.index("super().__init__(")
        return self.SOURCE[start:self.SOURCE.index(")", self.SOURCE.index("style=", start)) + 1]

    def test_the_popup_is_not_an_owned_window(self):
        """An owned window is kept above its owner by Windows itself, so no
        focus code could ever put MainWindow in front of it."""
        assert "super().__init__(\n            None," in self.SOURCE
        # wxWidgets still makes the app's top-level window the owner of a
        # NULL-parent dialog unless this style is set.
        assert "wx.DIALOG_NO_PARENT" in self._init_call()

    def test_the_popup_is_not_created_stay_on_top(self):
        assert "wx.STAY_ON_TOP" not in self._init_call()

    def test_raising_the_popup_drops_topmost_straight_after(self):
        """It must still appear over whatever app the user is in when the call
        arrives -- that is how a blind user learns of it -- but only once:
        HWND_TOPMOST (-1) followed by HWND_NOTOPMOST (-2)."""
        topmost = self.SOURCE.index("wintypes.HWND(-1)")
        notopmost = self.SOURCE.index("wintypes.HWND(-2)")
        assert topmost < notopmost
        assert self.SOURCE.count("wintypes.HWND(-1)") == 1


def test_an_offer_that_stops_ringing_has_its_call_record_looked_up():
    """A missed or declined call shows in the conversation through the record
    WhatsApp writes for it (core/call_log.py); the lookup starts here."""
    stub = _MainStub()
    stub.on_incoming_call_event(_offer(call_id="ABC"))
    assert stub.watched_call_logs == []

    stub.on_incoming_call_event({"event": "state", "state": "HANDLED_REMOTELY",
                                 "id": "ABC", "peerJid": "5511999999999@s.whatsapp.net"})

    assert stub.watched_call_logs == [("ABC", "5511999999999@s.whatsapp.net", False)]


def test_an_ended_outgoing_call_has_its_call_record_looked_up():
    stub = _MainStub()
    stub._active_voice_call = {
        "identity": "outgoing:5511999999999@s.whatsapp.net",
        "call_id": "WA-CALL",
        "peer_jid": "5511999999999@s.whatsapp.net",
        "name": "Fulano",
        "outgoing": True,
    }
    stub._stop_voice_call_audio = lambda grace_seconds=0: None
    stub._sync_voice_call_bar = lambda: None

    stub.on_voice_call_state_event({
        "event": "state", "state": "ENDED", "id": "WA-CALL",
        "peerJid": "5511999999999@s.whatsapp.net",
    })

    assert stub.watched_call_logs == [("WA-CALL", "5511999999999@s.whatsapp.net", True)]
