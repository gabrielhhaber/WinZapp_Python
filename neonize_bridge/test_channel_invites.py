"""A channel invitation received in a chat (to follow, or to be an admin)
reaches the client in the shape WhatsApp Web's model gives it."""
from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import (
    Message, NewsletterAdminInviteMessage, NewsletterFollowerInviteMessage)

import msgmap


def fields_of(message):
    """What msgmap.build sends the client for *message* received in a chat:
    the dispatch itself, so a lost CHANNEL_INVITES branch fails here."""
    out = msgmap.build(chat="5511999998888@s.whatsapp.net", from_me=False, id="X",
                       sender="5511999998888@s.whatsapp.net", ts=1, message=message)
    assert out is not None
    return out


def test_an_invitation_to_follow():
    m = Message(newsletterFollowerInviteMessageV2=NewsletterFollowerInviteMessage(
        newsletterJID="120363000000000001@newsletter", newsletterName="Canal",
        caption="Siga!"))
    out = fields_of(m)
    assert out["type"] == "newsletter_follower_invite"
    assert out["newsletterFollowerInviteInfo"] == {
        "newsletterId": "120363000000000001@newsletter", "newsletterName": "Canal",
        "inviteMessage": "Siga!"}


def test_an_invitation_to_be_admin_carries_its_expiration():
    m = Message(newsletterAdminInviteMessage=NewsletterAdminInviteMessage(
        newsletterJID="1@newsletter", newsletterName="C", caption="", inviteExpiration=1900000000))
    out = fields_of(m)
    assert out["type"] == "newsletter_admin_invite"
    assert out["newsletterAdminInviteInfo"]["inviteExpiration"] == 1900000000
