"""Channel invitations received in a chat (core/channel_invites.py): they
used to show as nothing; now as a row naming the channel, and Enter opens it."""
import types

from core import channel_invites as ci


def wpp(kind="newsletter_follower_invite", info=None):
    field = ci.WPP_TYPES[kind][1]
    return {"type": kind, field: info if info is not None else {
        "newsletterId": {"server": "newsletter", "user": "120363000000000001",
                         "_serialized": "120363000000000001@newsletter"},
        "newsletterName": "Canal", "inviteMessage": "Siga!"}}


def test_wpp_and_neonize_shapes_both_become_the_clients_message():
    client_type, content = ci.from_wpp("newsletter_follower_invite", wpp())
    assert client_type == ci.FOLLOWER
    assert content == {ci.FOLLOWER: {"newsletterJid": "120363000000000001@newsletter",
                                     "newsletterName": "Canal", "caption": "Siga!"}}
    # the bridge gives the id as text, and an admin one carries its expiration
    _t, admin = ci.from_wpp("newsletter_admin_invite", wpp("newsletter_admin_invite", {
        "newsletterId": "1@newsletter", "newsletterName": "C", "inviteMessage": "",
        "inviteExpiration": 1900000000}))
    assert admin[ci.ADMIN]["inviteExpiration"] == 1900000000


def test_an_invitation_with_no_channel_is_not_one():
    assert ci.from_wpp("newsletter_follower_invite", wpp(info={"newsletterId": "5511@c.us"})) is None
    assert ci.from_wpp("chat", {"body": "oi"}) is None


def i18n():
    return types.SimpleNamespace(t=lambda key: {
        "channel_invite_follow_row": "Convite para seguir o canal {name}",
        "channel_invite_admin_row": "Convite para ser administrador do canal {name}",
        "channels_unnamed": "Canal sem nome"}.get(key, key))


def message(kind, content):
    return {"messageType": kind, "message": content}


def test_the_row_names_the_offer_the_channel_and_the_words():
    _t, content = ci.from_wpp("newsletter_follower_invite", wpp())
    msg = message(ci.FOLLOWER, content)
    assert ci.row_text(i18n(), msg) == "Convite para seguir o canal Canal. Siga!"
    assert ci.channel_of(msg) == ("120363000000000001@newsletter", "Canal")
    unnamed = message(ci.ADMIN, {ci.ADMIN: {"newsletterJid": "1@newsletter", "newsletterName": ""}})
    assert ci.row_text(i18n(), unnamed) == "Convite para ser administrador do canal Canal sem nome"


def test_the_normalizer_keeps_it():
    from core.websocket_client import WebSocketClient
    import inspect
    source = inspect.getsource(WebSocketClient._normalize_wpp_message)
    assert "channel_invites.from_wpp" in source


def test_it_counts_as_a_message_and_has_a_notification_text():
    from main_window.chat_list import ChatListMixin
    from core.notification_manager import format_notification_body
    _t, content = ci.from_wpp("newsletter_follower_invite", wpp())
    msg = {**message(ci.FOLLOWER, content), "key": {"id": "X"}}
    assert ChatListMixin._counts_as_last_message(msg)
    window = types.SimpleNamespace(settings={})
    assert format_notification_body(msg, window, i18n()) == "Convite para seguir o canal Canal. Siga!"
