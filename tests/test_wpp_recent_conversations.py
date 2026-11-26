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
        return {"response": {"requested": True}}


def _window(chats):
    return SimpleNamespace(
        chats={i: {} for i in range(chats)}, _wa_connected=True, token="t",
        wpp_server="http://127.0.0.1", wpp_port=21465,
    )


def test_ask_once_posts_and_latches(monkeypatch):
    monkeypatch.setattr(engines, "loaded_engine", lambda mw: engines.WPPCONNECT)
    seen = []

    def post(url, **kw):
        seen.append((url, kw["json"]))
        return _Response()

    mw = _window(413)
    assert rc.ask_once(mw, post=post) is True
    assert seen[0][0].endswith("/api/t/request-recent-conversations")
    assert seen[0][1]["conversationCount"] == rc.CONVERSATIONS
    assert rc.ask_once(mw, post=post) is False
    assert len(seen) == 1


def test_ask_once_skips_a_short_list(monkeypatch):
    monkeypatch.setattr(engines, "loaded_engine", lambda mw: engines.WPPCONNECT)
    assert rc.ask_once(_window(50), post=lambda *a, **k: 1 / 0) is False


def test_route_and_controller_exist():
    routes = (ROOT / "client/api_patches/src/routes/index.ts").read_text(encoding="utf-8")
    ctl = (ROOT / "client/api_patches/src/controller/deviceController.ts").read_text(encoding="utf-8")
    assert "/api/:session/request-recent-conversations" in routes
    assert "DeviceController.requestRecentConversations" in routes
    assert "export async function requestRecentConversations" in ctl
    assert "RECENT_CONVERSATIONS_ON_DEMAND_REQUEST" in ctl
