"""resolve_self_lid() must keep the account's own @lid.

/contact/pn-lid answers with the phone under "phoneNumber". The parser did not
read that key, dropped the whole answer and left my_lid empty, so every group
listing this account by @lid (most of them) stopped recognizing it as a member:
an admin found Add Member dimmed. The payload below is the real shape.
"""

import threading
import types

from main import MainWindow
from main_window import identity
from tests.god_modules import patch_main_global


class _Db:
    def __init__(self):
        self.metadata = {}
        self.mappings = {}

    def set_metadata(self, key, value):
        self.metadata[key] = value

    def set_lid_mapping(self, lid, phone):
        self.mappings[lid] = phone

    def delete_lid_mapping(self, lid):
        self.mappings.pop(lid, None)


class _Window:
    resolve_self_lid = MainWindow.resolve_self_lid

    def __init__(self):
        self.my_jid = "15550001111@c.us"
        self.my_lid = ""
        self.wpp_server, self.wpp_port, self.token = "http://127.0.0.1", 6300, "tok"
        self.db = _Db()
        self._lid_mapping_lock = threading.Lock()
        self._lid_to_phone = {}
        self._phone_to_lid = {}
        self.registered = []

    def _normalize_jid(self, jid):
        return jid.replace("@c.us", "@s.whatsapp.net")

    def register_jid_mapping(self, lid, phone):
        self.registered.append((lid, phone))


def _resolve(monkeypatch, payload):
    class _Inline:
        def __init__(self, target=None, **_kw):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(identity.threading, "Thread", _Inline)
    patch_main_global(monkeypatch, "api_get", lambda *a, **kw: types.SimpleNamespace(
        status_code=200, json=lambda: payload))
    window = _Window()
    window.resolve_self_lid()
    return window


def test_the_phone_number_key_of_the_real_answer_is_read(monkeypatch):
    window = _resolve(monkeypatch, {
        "lid": {"id": "9999", "server": "lid", "_serialized": "9999@lid"},
        "phoneNumber": {"id": "15550001111", "server": "c.us",
                        "_serialized": "15550001111@c.us"},
        "contact": {"name": "Me"},
    })

    assert window.my_lid == "9999@lid"
    assert window.my_jid == "15550001111@s.whatsapp.net"
    assert window.db.metadata["my_lid"] == "9999@lid"
    assert window.registered == [("9999@lid", "15550001111@s.whatsapp.net")]


def test_the_older_phone_key_still_works(monkeypatch):
    window = _resolve(monkeypatch, {"lid": "9999@lid", "phone": "15550001111@c.us"})

    assert window.my_lid == "9999@lid"


def test_an_answer_without_a_lid_changes_nothing(monkeypatch):
    window = _resolve(monkeypatch, {"phoneNumber": "15550001111@c.us"})

    assert window.my_lid == ""
    assert window.db.metadata == {}
