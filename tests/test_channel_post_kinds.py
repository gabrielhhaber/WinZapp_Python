"""What WinZapp posts in a channel: text always, photos, videos and polls when
the engine does (WPPConnect through wa-js's newsletter job; the Neonize bridge
says so in /send-capabilities and does not yet)."""

import types

from core.channels import channel_post_kinds
from core.polls import poll_send_payload
from ui.conversation_panel.channel_conversation import (
    refuse_attachments_in_channel, refuse_in_channel)

CHANNEL = "120363000000000001@newsletter"


def test_wppconnect_states_nothing_and_posts_everything():
    assert channel_post_kinds({"compatible": True}) == {"photo_video", "poll"}


def test_the_bridge_states_what_it_posts():
    assert channel_post_kinds({"channelMedia": False, "channelPolls": False}) == set()
    assert channel_post_kinds({"channelMedia": True, "channelPolls": False}) == {"photo_video"}
    assert channel_post_kinds(None) is None


def _panel(jid, kinds):
    said = []
    window = types.SimpleNamespace(_channel_post_kinds=kinds, output=said.append,
                                   i18n=types.SimpleNamespace(t=lambda k: k))
    return types.SimpleNamespace(conversation={"remoteJid": jid}, main_window=window), said


def test_a_photo_or_a_poll_goes_to_a_channel_when_the_engine_posts_it():
    panel, said = _panel(CHANNEL, {"photo_video", "poll"})
    assert refuse_in_channel(panel, "photo_video") is False
    assert refuse_in_channel(panel, "poll") is False
    assert refuse_in_channel(panel) is True and said == ["channel_post_kinds"]


def test_an_engine_that_posts_text_only_says_text_only():
    panel, said = _panel(CHANNEL, set())
    assert refuse_in_channel(panel, "photo_video") is True
    assert said == ["channel_text_only"]


def test_a_batch_with_a_document_is_refused_in_a_channel():
    panel, said = _panel(CHANNEL, {"photo_video", "poll"})
    assert refuse_attachments_in_channel(panel, [{"media_type": "image"}, {"media_type": "video"}]) is False
    assert refuse_attachments_in_channel(panel, [{"media_type": "image"}, {"media_type": "document"}]) is True


def test_a_chat_is_never_refused():
    panel, said = _panel("5511999999999@s.whatsapp.net", set())
    assert refuse_in_channel(panel) is False and said == []


def test_a_poll_to_a_channel_says_it_is_a_channel():
    poll = {"name": "q", "options": ["a", "b"]}
    assert poll_send_payload(CHANNEL, poll, is_group=False, is_lid=False)["isNewsletter"] is True
    assert "isNewsletter" not in poll_send_payload("1@g.us", poll, is_group=True, is_lid=False)
