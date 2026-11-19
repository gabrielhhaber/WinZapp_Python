"""Scrolling up in a channel: its older updates come from WhatsApp's server.

Reported on both engines: a channel's older updates did not load. Measured on
2026-11-08: WhatsApp Web held 16 of a followed channel's 120 updates and every
`before` page of get-messages answered empty, so the client asked the PHONE
for older history (a channel has none there), waited, and wrote the channel
off. The page now pulls them from the server first (pullChannelHistory in
channelsPanelController.ts); on Neonize the bridge forgot the anchor's server
id on every restart, so the client sends the one it stored.
"""

import main
from core import channels as ch

from tests.test_exhausted_chats_durability import _make

CHANNEL = "120363000000000001@newsletter"
ANCHOR = {"key": {"id": "3A9F", "remoteJid": CHANNEL, "_serialized": f"false_{CHANNEL}_3A9F"},
          "messageTimestamp": 1_793_059_670, "serverId": 4929}


class _Empty:
    status_code = 200

    @staticmethod
    def json():
        return {"response": []}


def _capture(monkeypatch):
    urls = []

    def get(url, *args, **kwargs):
        urls.append(url)
        return _Empty()
    monkeypatch.setattr(main.requests, "get", get)
    return urls


def test_the_anchor_server_id_goes_with_a_channel_page_only():
    assert ch.older_page_query(CHANNEL, ANCHOR) == "&serverId=4929"
    assert ch.older_page_query(CHANNEL, {"key": {"id": "x"}}) == ""
    assert ch.older_page_query("5511999999999@s.whatsapp.net", ANCHOR) == ""
    assert ch.older_page_query(CHANNEL, {"serverId": True}) == ""


def test_only_a_channel_ends_on_the_server():
    assert ch.history_ends_on_server(CHANNEL)
    assert not ch.history_ends_on_server("1203@g.us")
    assert not ch.history_ends_on_server(None)


def test_an_empty_channel_page_never_asks_the_phone(monkeypatch):
    urls = _capture(monkeypatch)
    stub = _make()
    assert stub.fetch_older_messages(CHANNEL, ANCHOR) == []
    assert stub.asks == []
    assert CHANNEL in stub._exhausted_chats
    assert "exhausted_chats" not in stub.db.metadata      # this session only
    assert urls and urls[0].endswith("&serverId=4929")
    assert f"id=false_{CHANNEL}_3A9F" in urls[0]


def test_scrolling_up_again_asks_the_server_again(monkeypatch):
    urls = _capture(monkeypatch)
    stub = _make()
    stub.fetch_older_messages(CHANNEL, ANCHOR)
    stub.fetch_older_messages(CHANNEL, ANCHOR)
    assert len(urls) == 2 and stub.asks == []
