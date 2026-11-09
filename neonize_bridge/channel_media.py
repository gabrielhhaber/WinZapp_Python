"""Photos, videos and polls posted to a channel (newsletter) the admin runs.

A channel's media is not end-to-end encrypted: whatsmeow's UploadNewsletter
sends the plain file to /newsletter/newsletter-<type>/ and answers a handle,
and the post goes out with that handle as the message node's media_id
(whatsmeow sendNewsletter). The message carries the plain file's SHA-256 and
length, but no media key and no encrypted hash: a channel follower reads the
file as it was uploaded. WinZapp's neonize build passes the handle on
(`send_newsletter_message`, neonize patch 0018); without it the channel
refuses the media.

The message itself is built by the same code as for a chat (neonize's
build_image_message, video_message.build): `NewsletterUploads` stands in for
the client with `upload` swapped for the newsletter upload, keeps the handle of
the file and the message then loses the fields encryption added.

A poll goes as WhatsApp Web posts one (WAWebPollsGeneratePollCreation
MessageProto, read on 2026-11-08): pollCreationMessageV3 for a single answer,
pollCreationMessage for several, and no message secret (a channel's votes are
not encrypted). WinZapp's whatsmeow (patch 0012) sends it as type "poll" with
its <meta polltype="creation">; the same build carries both patches.

Tested in test_channel_media.py.
"""
import inspect
import types

#: Message field of each kind a channel takes
FIELDS = {"image": "imageMessage", "video": "videoMessage"}
#: fields of an encrypted upload a channel's message does not carry
ENCRYPTION_FIELDS = ("mediaKey", "fileEncSHA256", "thumbnailEncSHA256", "mediaKeyTimestamp")


class ChannelMediaError(Exception):
    pass


def is_channel(destination) -> bool:
    return getattr(destination, "Server", "") == "newsletter"


def can_post_media(client=None) -> bool:
    """Does this neonize build send a channel's media handle? Without a
    client (not connected yet), asks neonize's client class."""
    if client is None:
        try:
            from neonize.client import NewClient as client
        except ImportError:
            return False
    return callable(getattr(client, "send_newsletter_message", None)) \
        and callable(getattr(client, "upload_newsletter", None))


class NewsletterUploads:
    """The client, with every upload going to the newsletter media servers.
    The handle of the largest upload (the file, not a thumbnail) is kept."""

    def __init__(self, client):
        self._client = client
        self.handle, self._size = "", -1

    def upload(self, binary, media_type=None):
        from neonize.utils.enum import MediaType
        if media_type is None:
            media_type = MediaType.from_magic(binary)
        answer = self._client.upload_newsletter(binary, media_type)
        if len(binary) > self._size:
            self.handle, self._size = getattr(answer, "Handle", "") or "", len(binary)
        return answer

    def __getattr__(self, name):
        # a method of the client's class runs with this object as self, so the
        # builder's self.upload() is the newsletter upload above
        func = getattr(type(self._client), name, None)
        if inspect.isfunction(func):
            return types.MethodType(func, self)
        return getattr(self._client, name)


def without_encryption(message, field):
    """The message as a channel takes it: no media key, no encrypted hashes."""
    media = getattr(message, field)
    for name in ENCRYPTION_FIELDS:
        if name in media.DESCRIPTOR.fields_by_name:
            media.ClearField(name)
    return message


def build(client, kind, file, caption, video_builder=None):
    """(Message, handle) of a photo or video for a channel. Blocking."""
    field = FIELDS.get(kind)
    if field is None:
        raise ChannelMediaError(f"a channel does not take a {kind or 'file'}")
    uploads = NewsletterUploads(client)
    if kind == "image":
        message = uploads.build_image_message(file, caption=caption or None)
    else:
        if video_builder is None:
            import video_message
            video_builder = video_message.build
        message = video_builder(uploads, file, caption or None)
    if not uploads.handle:
        raise ChannelMediaError("the channel's upload gave no media handle")
    return without_encryption(message, field), uploads.handle


def send(client, destination, kind, file, caption, video_builder=None):
    """Posts the photo or video to the channel; neonize's SendResponse."""
    if not can_post_media(client):
        raise ChannelMediaError("this neonize build cannot post media to a channel")
    message, handle = build(client, kind, file, caption, video_builder)
    return client.send_newsletter_message(destination, message, handle)


def poll_message(name, choices, multiple):
    """The Message of a channel poll: V3 with one answer, V1 with several."""
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message, PollCreationMessage
    options = [str(choice) for choice in choices if str(choice or "").strip()]
    if not str(name or "").strip() or len(options) < 2:
        raise ChannelMediaError("a poll needs a question and two options")
    poll = PollCreationMessage(
        name=str(name), selectableOptionsCount=0 if multiple else 1,
        options=[PollCreationMessage.Option(optionName=option) for option in options])
    return Message(pollCreationMessage=poll) if multiple else Message(pollCreationMessageV3=poll)


def send_poll(client, destination, name, choices, multiple):
    """Posts the poll to the channel; neonize's SendResponse."""
    if not can_post_media(client):
        raise ChannelMediaError("this neonize build cannot post a poll to a channel")
    return client.send_newsletter_message(destination, poll_message(name, choices, multiple))
