"""Regression coverage for own-status cache reconciliation.

An empty /statuses response used to be ambiguous: it could mean either
"WhatsApp has no own statuses" or "the status store is not ready yet". The
API now exposes myStatusReady so an authoritative empty snapshot can clear
SQLite/memory without reintroducing the reconnect cache-wipe bug.
"""

from status_panel import StatusPanel


class _FakeI18n:
    def t(self, key):
        return key


class _FakeMainWindow:
    def __init__(self, status_updates):
        self.i18n = _FakeI18n()
        self._status_updates = status_updates
        self.removed_ids = []
        self.settings = {}

    def remove_failed_status_update(self, message_id, refresh=False):
        self.removed_ids.append(message_id)
        for participant, bucket in list(self._status_updates.items()):
            kept = [m for m in bucket if m.get("key", {}).get("id") != message_id]
            if kept:
                self._status_updates[participant] = kept
            else:
                self._status_updates.pop(participant, None)


class _Stub:
    _reconcile_my_status_cache = StatusPanel._reconcile_my_status_cache
    _parse_statuses            = StatusPanel._parse_statuses
    _is_self_jid                = lambda self, jid: jid == "me@s.whatsapp.net"

    def __init__(self, status_updates):
        self.main_window = _FakeMainWindow(status_updates)


def _own_status(msg_id, ts=1700000000):
    return {
        "key": {"id": msg_id, "fromMe": True, "remoteJid": "status@broadcast"},
        "messageType": "conversation",
        "message": {"conversation": "oi"},
        "messageTimestamp": ts,
    }


class TestEmptyRemoteListReadiness:
    def test_empty_remote_list_without_ready_marker_deletes_nothing(self):
        stub = _Stub(status_updates={"me": [_own_status("s1"), _own_status("s2")]})

        stub._reconcile_my_status_cache([])

        assert stub.main_window.removed_ids == []
        assert stub.main_window._status_updates == {"me": [_own_status("s1"), _own_status("s2")]}

    def test_authoritative_empty_remote_list_deletes_all_local_own_statuses(self):
        stub = _Stub(status_updates={"me": [_own_status("s1"), _own_status("s2")]})

        stub._reconcile_my_status_cache([], authoritative_empty=True)

        assert set(stub.main_window.removed_ids) == {"s1", "s2"}
        assert stub.main_window._status_updates == {}

    def test_a_nonempty_remote_list_still_removes_genuinely_stale_ids(self):
        """The one real case this is meant to still catch: the remote list
        legitimately no longer includes an id the local cache has."""
        stub = _Stub(status_updates={"me": [_own_status("s1"), _own_status("s2")]})

        stub._reconcile_my_status_cache([_own_status("s1")])

        assert stub.main_window.removed_ids == ["s2"]

    def test_a_nonempty_remote_list_matching_everything_removes_nothing(self):
        stub = _Stub(status_updates={"me": [_own_status("s1")]})

        stub._reconcile_my_status_cache([_own_status("s1")])

        assert stub.main_window.removed_ids == []
