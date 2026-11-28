"""WPPConnect asks the phone for the chats past its 399-chat first batch."""

import pathlib
from types import SimpleNamespace

from core import connection_engine as engines
from main_window import wpp_recent_conversations as rc

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_asks_only_near_the_cap_on_wppconnect_once():
    assert rc.should_ask(engines.WPPCONNECT, 413, False)
    assert rc.should_ask(engines.WPPCONNECT, 390, False)
    assert not rc.should_ask(engines.WPPCONNECT, 120, False)
    assert not rc.should_ask(engines.WPPCONNECT, 413, True)
    assert not rc.should_ask(engines.NEONIZE, 413, False)


class _Response:
    status_code = 200

    def json(self):
        return {"response": {"requested": True, "primarySupports": True}}


def _window(chats):
    return SimpleNamespace(
        chats={i: {} for i in range(chats)}, _wa_connected=True, token="t",
        wpp_server="http://127.0.0.1", wpp_port=21465,
    )


def _run(monkeypatch, store_reads, chats=413):
    monkeypatch.setattr(engines, "loaded_engine", lambda mw: engines.WPPCONNECT)
    seen = []
    reads = iter(store_reads)

    def post(url, **kw):
        seen.append((url, kw["json"]))
        return _Response()

    mw = _window(chats)
    sent = rc.ask_once(mw, post=post, sleep=lambda s: None,
                       chats_in_store=lambda m: next(reads))
    return mw, seen, sent


def test_every_variant_is_tried_while_the_store_does_not_grow(monkeypatch):
    mw, seen, sent = _run(monkeypatch, [472] * 6)
    assert sent == len(rc.VARIANTS) == len(seen)
    assert [j["conversationCount"] for _u, j in seen] == [c for c, _m in rc.VARIANTS]
    assert seen[0][0].endswith("/api/t/request-recent-conversations")
    assert seen[0][1] == {"conversationCount": 10, "messagesPerConversation": 10}


def test_it_stops_at_the_first_variant_the_phone_answers(monkeypatch):
    _mw, seen, sent = _run(monkeypatch, [472, 472 + rc.GAIN_TO_STOP])
    assert sent == 1 == len(seen)


def test_it_asks_once_per_launch(monkeypatch):
    mw, seen, _sent = _run(monkeypatch, [472] * 6)
    assert rc.ask_once(mw, post=lambda *a, **k: 1 / 0, sleep=lambda s: None) == 0


def test_a_refused_attempt_ends_the_series(monkeypatch):
    monkeypatch.setattr(engines, "loaded_engine", lambda mw: engines.WPPCONNECT)

    class _Refused:
        status_code = 500

        def json(self):
            return {}

    calls = []
    sent = rc.ask_once(_window(413), post=lambda *a, **k: calls.append(1) or _Refused(),
                       sleep=lambda s: None, chats_in_store=lambda m: 472)
    assert sent == 0 and len(calls) == 1


def test_ask_once_skips_a_short_list(monkeypatch):
    monkeypatch.setattr(engines, "loaded_engine", lambda mw: engines.WPPCONNECT)
    assert rc.ask_once(_window(50), post=lambda *a, **k: 1 / 0) == 0


def test_gain_needs_both_reads():
    assert rc.gained(400, 400 + rc.GAIN_TO_STOP)
    assert not rc.gained(None, 500) and not rc.gained(400, None)
    assert not rc.gained(400, 401)


def test_route_and_controller_exist():
    routes = (ROOT / "client/api_patches/src/routes/index.ts").read_text(encoding="utf-8")
    ctl = (ROOT / "client/api_patches/src/controller/deviceController.ts").read_text(encoding="utf-8")
    assert "/api/:session/request-recent-conversations" in routes
    assert "DeviceController.requestRecentConversations" in routes
    assert "export async function requestRecentConversations" in ctl
    assert "RECENT_CONVERSATIONS_ON_DEMAND_REQUEST" in ctl


def test_start_hands_the_thread_a_bare_callable(monkeypatch):
    """Sync tests replace threading.Thread with a fake that calls target()."""
    made = []

    class _Thread:
        def __init__(self, target=None, **kw):
            made.append(target)

        def start(self):
            made[-1]()

    monkeypatch.setattr(rc.threading, "Thread", _Thread)
    monkeypatch.setattr(engines, "loaded_engine", lambda mw: engines.NEONIZE)
    rc.start(_window(413))
    assert len(made) == 1
