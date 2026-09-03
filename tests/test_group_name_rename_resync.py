"""A group renamed on WhatsApp never picked up its new name in WinZapp, even
after a full F5 resync.

get_remote_chats() merges each list-chats round into self.chats. For an
already-known group, its "name" field was only ever recomputed from the
fresh chat.groupMetadata.subject when the CURRENT stored name was blank
(the per-key merge loop) or, failing that, when self.chats[jid]["name"]
was still blank (the end-of-branch fallback). Once a group had been named
once, both guards were permanently false, so a rename could never reach
chats[jid]["name"] through this path — only a live "gp2" subject-change
system message could (_apply_group_subject_change), and that message isn't
always delivered/retained, isn't replayed by a resync, and needs the app to
have been connected at the exact moment of the rename.

_group_name_from_chat_dict() itself compounded this: it read the flat
chat["name"]/chat["subject"] fields BEFORE groupMetadata.subject, so even
where the merge loop did call it, a non-empty but stale flat name still won
over the fresh groupMetadata value list-chats had just delivered in the same
response (see the "group metadata shape" log line in get_remote_chats()).

Fixed by (1) making groupMetadata.subject the first thing
_group_name_from_chat_dict() checks, and (2) removing the "only when
currently blank" gates in get_remote_chats()'s merge, so an already-named
group's name is refreshed from groupMetadata.subject on every resync round,
not just the first one.

MainWindow is a wx.Frame; get_remote_chats and _group_name_from_chat_dict
are bound to a plain stub, same pattern as
tests/test_get_remote_chats_persistence.py.
"""

import json
import types

import pytest

import main
from main import MainWindow

GROUP_JID = "120363000000000001@g.us"


class _Response:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _DB:
    def __init__(self):
        self.metadata = {}

    def set_metadata_json(self, key, value):
        self.metadata[key] = value


class _Stub:
    def __init__(self, chats=None):
        self.wpp_server = "http://127.0.0.1"
        self.wpp_port = 6300
        self.token = "tok"
        self.chats = chats or {}
        self.contacts = {}
        self.settings = {"cleared_chats": {}}
        self.db = _DB()
        self._deleted_chats = set()
        self._muted_chats = {}
        self._pinned_chats = set()
        self._archived_chats = set()
        self._lid_to_phone = {}
        self._phone_to_lid = {}
        self._group_name_cache = {}
        self._locally_read_at = {}
        self.conversations_panel = None

    def save_data(self, chats, contacts):
        pass

    def _schedule_save(self, *a, **kw):
        pass

    def _check_wa_connection_closed(self, response):
        return False

    def _fill_group_name(self, jid):
        return ""

    def _persist_locally_read_at(self):
        pass


def _make(chats=None):
    stub = _Stub(chats)
    for name in ("get_remote_chats", "_normalize_jid", "_lift_contact_identity",
                 "_last_received_jid", "_group_name_from_chat_dict"):
        raw = MainWindow.__dict__[name]
        if isinstance(raw, staticmethod):
            setattr(stub, name, raw.__func__)
        else:
            setattr(stub, name, types.MethodType(raw, stub))
    return stub


def _group_chat(jid, subject, **extra):
    payload = {
        "id": {"_serialized": jid},
        "t": 1700000000,
        "unreadCount": 0,
        "isGroup": True,
        "groupMetadata": {"subject": subject},
    }
    payload.update(extra)
    return payload


@pytest.fixture
def post(monkeypatch):
    box = {"payload": []}

    def _post(url, json=None, headers=None, timeout=None):
        return _Response(box["payload"])

    monkeypatch.setattr(main.requests, "post", _post)
    return box


class TestGroupRenamePicksUpOnResync:
    def test_rename_replaces_a_stale_but_present_flat_name(self, post):
        """The exact failure reported live: the raw list-chats entry's own
        flat "name" field still carries the OLD name (a non-empty but stale
        WhatsApp Web chat-store cache), while groupMetadata.subject already
        has the new one in the very same response."""
        cached = {
            GROUP_JID: {
                "remoteJid": GROUP_JID,
                "name": "Nome Antigo",
                "t": 1699999999,
                "messages": {"messages": {"records": []}},
            }
        }
        post["payload"] = [
            _group_chat(GROUP_JID, "Nome Novo", name="Nome Antigo")
        ]
        stub = _make(cached)

        result = stub.get_remote_chats(dict(cached), persist_full=False, notify_errors=False)

        assert result[GROUP_JID]["name"] == "Nome Novo"

    def test_rename_is_picked_up_even_with_no_flat_name_key_at_all(self, post):
        """The other documented shape: the raw entry carries no top-level
        "name" key whatsoever, so the per-key merge loop never visits it —
        only the end-of-branch fallback can catch this one."""
        cached = {
            GROUP_JID: {
                "remoteJid": GROUP_JID,
                "name": "Nome Antigo",
                "t": 1699999999,
                "messages": {"messages": {"records": []}},
            }
        }
        raw = _group_chat(GROUP_JID, "Nome Novo")
        del raw["id"]
        raw["id"] = {"_serialized": GROUP_JID}
        post["payload"] = [raw]
        stub = _make(cached)

        result = stub.get_remote_chats(dict(cached), persist_full=False, notify_errors=False)

        assert result[GROUP_JID]["name"] == "Nome Novo"

    def test_first_time_naming_of_a_new_group_still_works(self, post):
        """Sanity check: the priority-order fix must not regress the
        ordinary case of a brand-new group getting named for the first
        time."""
        post["payload"] = [_group_chat(GROUP_JID, "Grupo Recem Criado")]
        stub = _make()

        result = stub.get_remote_chats({}, persist_full=False, notify_errors=False)

        assert result[GROUP_JID]["name"] == "Grupo Recem Criado"
