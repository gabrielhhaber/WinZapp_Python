"""Tests for what get_remote_chats() writes, and what it sweeps.

`persist_full` used to gate two things that have nothing to do with each
other: the full clear-and-reimport save, which rewrites every message of
every chat, and the retroactive phantom-chat sweep, which is a plain
in-memory dict scan. The initial sync's retry loop needs the second and
cannot afford the first.

Measured on a 937-chat session: the loop called this with the save enabled up
to 30 times per round at roughly 4 s each, about 8 minutes of redundant
writes across the capture, for data that sync_chat_messages() then persists
incrementally chat by chat anyway. Splitting them is what makes the loop
cheap without losing the sweep.

MainWindow is a wx.Frame, so the method is bound to a stub carrying only the
attributes it reads — the same pattern the other main.py tests use.
"""

import json
import types

import pytest

import main
from main import MainWindow


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _DB:
    def __init__(self):
        self.metadata = {}

    def set_metadata_json(self, key, value):
        self.metadata[key] = value


class _Stub:
    """Minimum surface get_remote_chats() actually reads on the happy path."""

    _mute_state_jids = MainWindow._mute_state_jids

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
        self.save_data_calls = 0
        self.schedule_save_calls = 0

    # ── collaborators ────────────────────────────────────────────────
    def save_data(self, chats, contacts):
        self.save_data_calls += 1

    def _schedule_save(self, *a, **kw):
        self.schedule_save_calls += 1

    def _check_wa_connection_closed(self, response):
        return False

    def _fill_group_name(self, jid):
        return ""

    def _group_name_from_chat_dict(self, chat):
        return (chat.get("groupMetadata") or {}).get("subject", "") or chat.get("name", "")

    def _persist_locally_read_at(self):
        pass


def _make(chats=None):
    stub = _Stub(chats)
    for name in ("get_remote_chats", "_normalize_jid", "_lift_contact_identity",
                 "_last_received_jid"):
        # Read from __dict__, not getattr: accessing a staticmethod through
        # the class hands back the plain function, so `isinstance(...,
        # staticmethod)` is always False there and every one of them would be
        # re-bound as an instance method and receive `self` as its first
        # argument.
        raw = MainWindow.__dict__[name]
        if isinstance(raw, staticmethod):
            setattr(stub, name, raw.__func__)
        else:
            setattr(stub, name, types.MethodType(raw, stub))
    return stub


def _chat(jid, **extra):
    payload = {"id": {"_serialized": jid}, "t": 1700000000, "unreadCount": 0}
    payload.update(extra)
    return payload


@pytest.fixture
def post(monkeypatch):
    """Patch the one HTTP call, and hand back a slot for the payload."""
    box = {"payload": []}

    def _post(url, json=None, headers=None, timeout=None):
        return _Response(box["payload"])

    monkeypatch.setattr(main.requests, "post", _post)
    return box


class TestTheFullSaveIsOptional:
    def test_persist_full_false_does_not_rewrite_the_database(self, post):
        post["payload"] = [_chat("5511900000001@c.us")]
        stub = _make()
        result = stub.get_remote_chats({}, persist_full=False, notify_errors=False)
        assert result is not None
        assert stub.save_data_calls == 0
        assert stub.schedule_save_calls == 1

    def test_persist_full_true_still_rewrites(self, post):
        post["payload"] = [_chat("5511900000001@c.us")]
        stub = _make()
        stub.get_remote_chats({}, persist_full=True, notify_errors=False)
        assert stub.save_data_calls == 1

    def test_the_chat_list_is_still_merged_either_way(self, post):
        post["payload"] = [_chat("5511900000001@c.us"), _chat("5511900000002@c.us")]
        stub = _make()
        result = stub.get_remote_chats({}, persist_full=False, notify_errors=False)
        assert set(result) == {"5511900000001@s.whatsapp.net",
                               "5511900000002@s.whatsapp.net"}

    def test_mute_and_pin_metadata_is_written_regardless_of_the_flag(self, post):
        """The settle loop learns these from the same response, and they are
        written by their own set_metadata_json() — dropping the full save must
        not drop them."""
        post["payload"] = [_chat("5511900000001@c.us", muteExpiration=-1)]
        stub = _make()
        stub.get_remote_chats({}, persist_full=False, notify_errors=False)
        assert "muted_chats" in stub.db.metadata
        assert stub._muted_chats


class TestRemoteReadReconciliation:
    JID = "5511900000001@s.whatsapp.net"

    def _cached(self, *, archived=False, timestamp=1700000000):
        return {
            self.JID: {
                "remoteJid": self.JID,
                "t": timestamp,
                "unreadCount": 4,
                "archived": archived,
                "messages": {"messages": {"records": []}},
            }
        }

    @pytest.mark.parametrize("archived", [False, True])
    def test_current_zero_from_phone_clears_normal_and_archived_chats(
        self, post, archived
    ):
        cached = self._cached(archived=archived)
        post["payload"] = [
            _chat("5511900000001@c.us", unreadCount=0, archive=archived)
        ]
        stub = _make(cached)

        result = stub.get_remote_chats(
            dict(cached), persist_full=False, notify_errors=False
        )

        assert result[self.JID]["unreadCount"] == 0

    def test_older_snapshot_does_not_erase_a_newer_live_arrival(self, post):
        cached = self._cached(timestamp=1700000001)
        post["payload"] = [
            _chat("5511900000001@c.us", unreadCount=0, t=1700000000)
        ]
        stub = _make(cached)

        result = stub.get_remote_chats(
            dict(cached), persist_full=False, notify_errors=False
        )

        assert result[self.JID]["unreadCount"] == 4


class TestTheSweepIsIndependentOfTheSave:
    """A phantom one-to-one entry — in the cache, echoed by the server, with
    no messages and no activity — is what the sweep exists to remove."""

    def _phantom_cache(self):
        return {
            "5511900000009@s.whatsapp.net": {
                "remoteJid": "5511900000009@s.whatsapp.net",
                "messages": {"messages": {"records": []}},
                "t": 0,
                "unreadCount": 0,
            }
        }

    def test_the_sweep_runs_without_the_full_save(self, post):
        """The combination the settle loop now uses."""
        post["payload"] = [{"id": {"_serialized": "5511900000009@c.us"},
                            "t": 0, "unreadCount": 0}]
        stub = _make()
        result = stub.get_remote_chats(self._phantom_cache(), persist_full=False,
                                       prune_stale=True, notify_errors=False)
        assert "5511900000009@s.whatsapp.net" not in result
        assert stub.save_data_calls == 0

    def test_prune_stale_defaults_to_persist_full(self, post):
        """Every caller that does not pass the new flag keeps its old
        behaviour exactly — that is the whole point of the default."""
        post["payload"] = [{"id": {"_serialized": "5511900000009@c.us"},
                            "t": 0, "unreadCount": 0}]

        swept = _make().get_remote_chats(self._phantom_cache(), persist_full=True,
                                         notify_errors=False)
        assert "5511900000009@s.whatsapp.net" not in swept

        kept = _make().get_remote_chats(self._phantom_cache(), persist_full=False,
                                        notify_errors=False)
        assert "5511900000009@s.whatsapp.net" in kept

    def test_a_chat_with_activity_is_never_swept(self, post):
        post["payload"] = [_chat("5511900000009@c.us")]
        cache = self._phantom_cache()
        cache["5511900000009@s.whatsapp.net"]["t"] = 1700000000
        result = _make().get_remote_chats(cache, persist_full=False,
                                          prune_stale=True, notify_errors=False)
        assert "5511900000009@s.whatsapp.net" in result


class TestTheSnapshotCanBeHeldBackFromDisk:
    """`defer_chat_save` keeps a fresh list-chats snapshot memory-only.

    The snapshot carries the activity markers (`t`, lastReceivedKey,
    unreadCount) that the *next* launch reads to decide a chat needs no
    get-messages call at all. Persisting it before the message delta selected
    by those markers has succeeded is how a conversation loses a message
    permanently: the marker says "already seen", and nothing ever asks again.
    So every caller inside a sync round passes this flag and the round itself
    commits once, at the end, only on success.
    """

    def test_the_deferred_call_writes_nothing(self, post):
        post["payload"] = [_chat("5511900000001@c.us")]
        stub = _make()
        result = stub.get_remote_chats({}, persist_full=False, notify_errors=False,
                                       defer_chat_save=True)
        assert result is not None
        assert (stub.save_data_calls, stub.schedule_save_calls) == (0, 0)

    def test_the_merged_snapshot_is_still_returned_in_full(self, post):
        """Deferring the write must not defer the data — the message phase
        reads the returned dict, not the database."""
        post["payload"] = [_chat("5511900000001@c.us"), _chat("5511900000002@c.us")]
        result = _make().get_remote_chats({}, persist_full=False, notify_errors=False,
                                          defer_chat_save=True)
        assert set(result) == {"5511900000001@s.whatsapp.net",
                               "5511900000002@s.whatsapp.net"}

    def test_the_full_save_still_wins_over_it(self, post):
        """persist_full is the F5/first-pairing path, which has no marker to
        protect: it rebuilds every chat from scratch anyway."""
        post["payload"] = [_chat("5511900000001@c.us")]
        stub = _make()
        stub.get_remote_chats({}, persist_full=True, notify_errors=False,
                              defer_chat_save=True)
        assert stub.save_data_calls == 1
