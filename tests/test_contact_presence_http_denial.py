"""HTTP privacy denial clears prior data without alias fallback or stale commits.

Real reader/coordinator/profile/renderer methods; fake HTTP, clock and UI queue.
No App, account, browser, physical worker or network request is created.
"""
from types import SimpleNamespace

import pytest

from core.contact_presence import cached, merge
from core.contact_presence_query import read_presence, read_snapshot
from main_window.identity import IdentityMixin
import main_window.contact_presence as coordinator
import main_window.profile_presence as profile
from tests.test_contact_presence_freshness import PN, LID
from tests.test_contact_presence_transition_acceptance import pipeline


DENIED = {"status": "success", "presence": {"status": "pending", "restricted": True}}


def http_loop(monkeypatch, payload=DENIED):
    loop, _, rendered, button = pipeline(monkeypatch)
    del loop.owner._fetch_contact_presence  # Execute the real typed HTTP reader.
    loop.owner._lid_to_phone[LID], loop.owner._phone_to_lid[PN] = PN, LID
    monkeypatch.setattr(profile, "time", SimpleNamespace(monotonic=lambda: loop.now))

    def request(*args, **kwargs):
        loop.reads.append((args, kwargs))
        return SimpleNamespace(status_code=200, json=lambda: payload)

    monkeypatch.setattr(coordinator, "api_get", request)
    return loop, rendered, button


@pytest.mark.parametrize("path,old", [("chat", "online"), ("chat", "seen"), ("profile", "seen")])
def test_http_denial_clears_cache_and_three_views_without_another_read(monkeypatch, path, old):
    loop, rendered, button = http_loop(monkeypatch)
    owner = loop.owner
    merge(owner, PN, {"isOnline": old == "online", "lastSeen": 1700000000 if old == "seen" else None})
    assert all("Online" in value if old == "online" else "Seen" in value for value in rendered())
    if path == "chat":
        loop.begin()
    else:
        assert IdentityMixin.get_last_seen(owner, LID) is None
        loop.drain()
    entry = cached(owner, PN, fresh=False)
    assert entry["restricted"] is True
    assert entry["isOnline"] is None and entry["lastSeen"] is None
    assert entry is cached(owner, LID, fresh=False)
    assert rendered() == ("Unknown", "Contact", "")
    assert len(loop.reads) == 1 and button.focuses == 0
    loop.advance(1000)
    owner._refresh_open_contact_presence()
    assert IdentityMixin.get_last_seen(owner, PN) is None
    loop.drain()
    assert len(loop.reads) == 1  # Even expired denial cannot authorize polling.
    assert not any(timer.active for timer in loop.timers)
    assert not owner._contact_presence_work.pending
    if path == "profile":
        assert not owner._profile_presence_work.pending


@pytest.mark.parametrize("first_pending", [False, True])
def test_denied_pending_is_a_clear_only_snapshot_and_stops_paired_identity(first_pending):
    calls = []
    def request(target, timeout):
        calls.append(target)
        payload = {"status": "success", "presence": {"status": "pending"}} if first_pending and target == PN else DENIED
        return SimpleNamespace(status_code=200, json=lambda: payload)
    result = read_presence((PN, LID), request, lambda: 100)
    assert result.outcome == "restricted"
    assert result.snapshot == {"restricted": True, "lastSeen": None}
    assert calls == ([PN, LID] if first_pending else [PN])
    assert read_snapshot((PN, LID), request, lambda: 100) == result.snapshot


@pytest.mark.parametrize("path", ["chat", "profile"])
@pytest.mark.parametrize("change", ["epoch", "visit", "mapping", "revision", "routing", "shutdown", "disconnect", "deadline"])
def test_queued_http_denial_rechecks_existing_acceptance_guards(monkeypatch, path, change):
    loop, _, _ = http_loop(monkeypatch)
    owner = loop.owner
    before = merge(owner, PN, {"isOnline": False, "lastSeen": 1700000000})
    if path == "chat":
        owner._refresh_open_contact_presence()
        loop.workers.pop(0)()  # HTTP finished; its real UI delivery is queued.
        owner._refresh_open_contact_presence = lambda: None  # No successor demand in this case.
    else:
        IdentityMixin.get_last_seen(owner, PN)
    if change == "epoch":
        owner._contact_presence_epoch = 1
    elif change == "visit":
        owner.conversations_panel._contact_presence_visit += 1
    elif change == "mapping":
        owner._phone_to_lid.clear()
        owner._lid_to_phone.clear()
    elif change == "revision":
        before = merge(owner, PN, {"isOnline": True, "restricted": False})
    elif change == "routing":
        owner.token = "synthetic-successor"
    elif change == "shutdown":
        owner._shutting_down = True
    elif change == "disconnect":
        owner._wa_connected = False
    else:
        loop.now = 130
    loop.drain()
    assert cached(owner, PN, fresh=False) is before
    assert not before.get("restricted", False)
    assert len(loop.reads) == 1


@pytest.mark.parametrize("permission", [None, 0, 1, "true", {}, []])
def test_untyped_pending_permission_does_not_become_a_denial(permission):
    answer = {"status": "success", "presence": {"status": "pending", "restricted": permission}}
    result = read_presence((PN, LID), lambda *args: SimpleNamespace(status_code=200, json=lambda: answer), lambda: 100)
    assert result.outcome == "malformed" and result.snapshot is None
