from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message

import msgmap


def test_private_text_received():
    m = msgmap.build(chat="5551999990000@s.whatsapp.net", from_me=False, id="ABC",
                     sender="5551999990000@s.whatsapp.net", ts=1700000000,
                     message=Message(conversation="oi"))
    assert m["type"] == "chat" and m["body"] == "oi" and not m["fromMe"]
    assert m["id"]["_serialized"] == "false_5551999990000@c.us_ABC"
    assert m["from"] == "5551999990000@c.us" and m["t"] == 1700000000


def test_interactive_proto_is_no_longer_an_unknown_message():
    msg = Message()
    msg.buttonsMessage.contentText = "Choose a trip"
    msg.buttonsMessage.buttons.add(buttonID="one").buttonText.displayText = "First trip"
    result = msgmap.build(chat="5551999990000@s.whatsapp.net", from_me=False,
                          id="B", sender="5551999990000@s.whatsapp.net", ts=5,
                          message=msg)
    assert result["type"] == "buttons"
    assert result["body"] == "Choose a trip"
    assert result["dynamicReplyButtons"][0]["buttonText"]["displayText"] == "First trip"


def test_group_carries_the_author_in_the_id():
    m = msgmap.build(chat="1203@g.us", from_me=False, id="X",
                     sender="5551:12@s.whatsapp.net", ts=1700000000000,
                     message=Message(conversation="a"))
    assert m["id"]["_serialized"] == "false_1203@g.us_X_5551@c.us"
    assert m["author"] == "5551@c.us" and m["t"] == 1700000000


def test_image_with_a_caption():
    msg = Message()
    msg.imageMessage.caption = "photo"
    msg.imageMessage.mediaKey = b"\x01\x02"
    m = msgmap.build(chat="1@s.whatsapp.net", from_me=True, id="I",
                     sender="2@s.whatsapp.net", ts=5, message=msg)
    assert m["type"] == "image" and m["caption"] == "photo" and m["fromMe"]
    assert m["mediaKey"] == "AQI="


def test_reaction_does_not_become_a_message():
    msg = Message()
    msg.reactionMessage.text = "x"
    assert msgmap.build(chat="1@s.whatsapp.net", from_me=False, id="R",
                        sender="1@s.whatsapp.net", ts=1, message=msg) is None


def test_jids():
    assert msgmap.jid_wpp("a:3@s.whatsapp.net") == "a@c.us"
    assert msgmap.jid_whatsmeow("a@c.us") == "a@s.whatsapp.net"
    assert msgmap.jid_whatsmeow("a@lid") == "a@lid"


def test_media_record_keeps_keys_and_drops_thumbnail_and_caption():
    msg = Message()
    msg.imageMessage.caption = "photo"
    msg.imageMessage.mediaKey = b"\x01\x02"
    msg.imageMessage.directPath = "/v/t62/x"
    msg.imageMessage.JPEGThumbnail = b"\xff\xd8"
    record = msgmap.media_record_of(msg)
    back = Message.FromString(record)
    assert back.imageMessage.mediaKey == b"\x01\x02"
    assert back.imageMessage.directPath == "/v/t62/x"
    assert not back.imageMessage.caption and not back.imageMessage.JPEGThumbnail


def test_text_has_no_record():
    assert msgmap.media_record_of(Message(conversation="oi")) is None


def test_quoted_of_text_and_of_image():
    assert msgmap.quoted({"type": "chat", "body": "oi"}).conversation == "oi"
    img = msgmap.quoted({"type": "image", "caption": "photo", "mimetype": "image/png"})
    assert img.imageMessage.caption == "photo" and img.imageMessage.mimetype == "image/png"
    assert msgmap.quoted({"type": "ptt"}).audioMessage.PTT is True


def test_quoted_participant():
    assert msgmap.quoted_participant({"fromMe": True}, "me@s.whatsapp.net") == "me@s.whatsapp.net"
    assert msgmap.quoted_participant({"author": "5@c.us"}, "x") == "5@s.whatsapp.net"
    assert msgmap.quoted_participant({"from": "9@lid"}, "x") == "9@lid"


def test_reaction_is_recognized_and_does_not_become_a_message():
    msg = Message()
    msg.reactionMessage.key.ID = "TARGET"
    msg.reactionMessage.key.fromMe = True
    msg.reactionMessage.key.participant = "5551:3@s.whatsapp.net"
    msg.reactionMessage.text = "\U0001F44D"
    assert msgmap.reaction(msg) == ("TARGET", "\U0001F44D", True, "5551@c.us")
    assert msgmap.build(chat="1@s.whatsapp.net", from_me=False, id="R",
                        sender="1@s.whatsapp.net", ts=1, message=msg) is None
    msg.reactionMessage.text = ""
    assert msgmap.reaction(msg)[1] == ""          # reaction removed
    assert msgmap.reaction(Message(conversation="oi")) is None


def test_poll_carries_position_and_option_limit():
    msg = Message()
    msg.pollCreationMessage.name = "Lunch?"
    msg.pollCreationMessage.selectableOptionsCount = 1
    for name in ("Yes", "No"):
        msg.pollCreationMessage.options.add().optionName = name
    m = msgmap.build(chat="1@g.us", from_me=False, id="P", sender="2@s.whatsapp.net",
                     ts=5, message=msg)
    assert m["type"] == "poll_creation" and m["pollSelectableOptionsCount"] == 1
    assert m["pollOptions"] == [{"name": "Yes", "localId": 0}, {"name": "No", "localId": 1}]


def test_vote_is_recognized_and_the_options_come_from_the_hash():
    import hashlib
    msg = Message()
    msg.pollUpdateMessage.pollCreationMessageKey.ID = "P"
    assert msgmap.vote(msg) == "P" and msgmap.vote(Message(conversation="x")) is None
    hashes = [hashlib.sha256(n.encode()).digest() for n in ("No", "Maybe")]
    assert msgmap.vote_indexes(["Yes", "No", "Maybe"], hashes) == [1, 2]
    assert msgmap.vote_indexes(["Yes"], [b"other"]) == []


def test_edit_notice_is_recognized_and_does_not_become_a_message():
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import ProtocolMessage
    msg = Message()
    msg.protocolMessage.type = ProtocolMessage.MESSAGE_EDIT
    msg.protocolMessage.key.ID = "ORIG"
    msg.protocolMessage.editedMessage.conversation = "new text"
    target, new = msgmap.edit(msg)
    assert target == "ORIG" and new.conversation == "new text"
    assert msgmap.edit(Message(conversation="oi")) is None
    assert msgmap.build(chat="1@s.whatsapp.net", from_me=False, id="E",
                        sender="1@s.whatsapp.net", ts=1, message=msg) is None


def test_media_field():
    msg = Message()
    msg.imageMessage.caption = "x"
    assert msgmap.media_field(msg) == "imageMessage"
    assert msgmap.media_field(Message(conversation="oi")) == ""


def test_pin_and_unpin_message_notice():
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import PinInChatMessage
    msg = Message()
    msg.pinInChatMessage.key.ID = "TARGET"
    msg.pinInChatMessage.type = PinInChatMessage.PIN_FOR_ALL
    msg.messageContextInfo.messageAddOnDurationInSecs = 604800
    assert msgmap.pin(msg) == ("TARGET", True, 604800)
    msg.pinInChatMessage.type = PinInChatMessage.UNPIN_FOR_ALL
    assert msgmap.pin(msg)[1] is False
    assert msgmap.pin(Message(conversation="oi")) is None


def test_status_carries_who_posted_as_origin_and_author():
    m = msgmap.build(chat="status@broadcast", from_me=False, id="S",
                     sender="5551:7@s.whatsapp.net", ts=5,
                     message=Message(conversation="good morning"))
    assert m["isStatus"] and m["from"] == "5551@c.us" and m["author"] == "5551@c.us"
    mine = msgmap.build(chat="status@broadcast", from_me=True, id="T",
                        sender="1@s.whatsapp.net", ts=5,
                        message=Message(conversation="oi"))
    assert mine["fromMe"] and mine["from"] == ""


def test_status_color():
    assert msgmap.argb_color("#25D366") == 0xFF25D366
    assert msgmap.argb_color("junk") == 0xFF1B8755 and msgmap.argb_color(None) == 0xFF1B8755


def _build(msg, chat="1203@g.us"):
    return msgmap.build(chat=chat, from_me=False, id="M",
                        sender="5551@s.whatsapp.net", ts=5, message=msg)


def test_mention_forwarded_and_text_reply():
    msg = Message()
    e = msg.extendedTextMessage
    e.text = "oi @a @b"
    e.contextInfo.mentionedJID.extend(["5551:3@s.whatsapp.net", "777@lid"])
    e.contextInfo.isForwarded = True
    e.contextInfo.stanzaID = "ORIG"
    e.contextInfo.participant = "5552@s.whatsapp.net"
    e.contextInfo.quotedMessage.conversation = "quoted text"
    m = _build(msg)
    assert m["mentionedJidList"] == ["5551@c.us", "777@lid"]
    assert m["isForwarded"] is True
    assert m["quotedStanzaID"] == "ORIG" and m["quotedParticipant"] == "5552@c.us"
    assert m["quotedMsg"] == {"type": "chat", "body": "quoted text"}


def test_without_context_it_carries_no_context_fields():
    m = _build(Message(conversation="oi"))
    for key in ("mentionedJidList", "isForwarded", "quotedMsg", "quotedStanzaID"):
        assert key not in m


def test_only_forwarded_or_only_mention_without_a_reply():
    msg = Message()
    msg.imageMessage.contextInfo.isForwarded = True
    m = _build(msg)
    assert m["isForwarded"] is True and "quotedMsg" not in m and "quotedStanzaID" not in m
    msg = Message()
    msg.extendedTextMessage.text = "@x"
    msg.extendedTextMessage.contextInfo.mentionedJID.append("9@lid")
    m = _build(msg)
    assert m["mentionedJidList"] == ["9@lid"] and "isForwarded" not in m


def test_quoted_media_carries_type_caption_and_name_without_keys():
    msg = Message()
    msg.imageMessage.caption = "reply"
    c = msg.imageMessage.contextInfo
    c.stanzaID = "Q"
    c.quotedMessage.documentMessage.fileName = "a.pdf"
    c.quotedMessage.documentMessage.caption = "contract"
    c.quotedMessage.documentMessage.mediaKey = b"\x01"
    c.quotedMessage.documentMessage.JPEGThumbnail = b"\xff"
    q = _build(msg)["quotedMsg"]
    assert q == {"type": "document", "caption": "contract", "filename": "a.pdf"}


def test_quoted_voice_note_sticker_round_video_contact_and_poll():
    def quoting(fill):
        msg = Message()
        msg.extendedTextMessage.contextInfo.stanzaID = "Q"
        fill(msg.extendedTextMessage.contextInfo.quotedMessage)
        return _build(msg)["quotedMsg"]
    assert quoting(lambda q: setattr(q.audioMessage, "PTT", True)) == {
        "type": "ptt", "caption": "", "isPtt": True}
    assert quoting(lambda q: setattr(q.stickerMessage, "isAnimated", False)) == {
        "type": "sticker", "caption": ""}
    assert quoting(lambda q: setattr(q.ptvMessage, "seconds", 3))["type"] == "video"

    def contact(q):
        q.contactMessage.displayName = "Ana"
    assert quoting(contact) == {"type": "contact", "caption": "Ana"}

    def poll(q):
        q.pollCreationMessage.name = "Lunch?"
    assert quoting(poll) == {"type": "chat", "body": "Lunch?"}


def test_quoted_inside_a_wrapper_and_with_its_own_mentions():
    msg = Message()
    msg.extendedTextMessage.contextInfo.stanzaID = "Q"
    inner = msg.extendedTextMessage.contextInfo.quotedMessage
    e = inner.ephemeralMessage.message.extendedTextMessage
    e.text = "oi @x"
    e.contextInfo.mentionedJID.append("5@s.whatsapp.net")
    assert _build(msg)["quotedMsg"] == {
        "type": "chat", "body": "oi @x", "mentionedJidList": ["5@c.us"]}


def test_quoting_a_reaction_does_not_become_quotedmsg():
    msg = Message()
    msg.extendedTextMessage.contextInfo.stanzaID = "Q"
    msg.extendedTextMessage.contextInfo.quotedMessage.reactionMessage.text = "x"
    m = _build(msg)
    assert m["quotedStanzaID"] == "Q" and "quotedMsg" not in m


def test_round_video_becomes_ptv_with_the_video_fields():
    msg = Message()
    msg.ptvMessage.seconds = 7
    msg.ptvMessage.mediaKey = b"\x01\x02"
    msg.ptvMessage.directPath = "/v/x"
    msg.ptvMessage.contextInfo.isForwarded = True
    m = _build(msg)
    assert m["type"] == "ptv" and m["duration"] == 7 and m["mediaKey"] == "AQI="
    assert m["mimetype"] == "video/mp4" and m["clientUrl"] == "/v/x" and m["isGif"] is False
    assert m["isForwarded"] is True


def test_live_location_becomes_liveLocation():
    msg = Message()
    msg.liveLocationMessage.degreesLatitude = -30.5
    msg.liveLocationMessage.degreesLongitude = -51.25
    msg.liveLocationMessage.caption = "Arrived"
    m = _build(msg)
    assert m["type"] == "liveLocation" and m["lat"] == -30.5 and m["lng"] == -51.25
    assert m["loc"] == "Arrived" and m["body"] == "Arrived"


def test_several_contacts_become_one_card_with_the_name_of_the_set():
    msg = Message()
    msg.contactsArrayMessage.displayName = "Ana and 1 more"
    for name in ("Ana", "Bob"):
        c = msg.contactsArrayMessage.contacts.add()
        c.displayName = name
        c.vcard = f"BEGIN:VCARD\nFN:{name}\nEND:VCARD"
    m = _build(msg)
    assert m["type"] == "vcard" and m["displayName"] == "Ana and 1 more"
    assert m["body"].count("BEGIN:VCARD") == 2
    msg.contactsArrayMessage.displayName = ""
    assert _build(msg)["displayName"] == "Ana"


def test_protocol_that_is_not_a_revocation_still_gives_no_message():
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import ProtocolMessage
    msg = Message()
    msg.protocolMessage.type = ProtocolMessage.EPHEMERAL_SETTING
    assert _build(msg) is None



def _rich(*texts, unified=b""):
    m = Message()
    rich = m.richResponseMessage
    for text in texts:
        rich.submessages.add(messageText=text)
    if unified:
        rich.unifiedResponse.data = unified
    return m


def _meta_ai_reply(id, message):
    return msgmap.build(chat="13135550002@bot", from_me=False, id=id,
                        sender="13135550002@bot", ts=1, message=message)


def test_a_meta_ai_reply_carries_its_text_as_whatsapp_web_does():
    out = _meta_ai_reply("MSG1", _rich("Hello", "", "there"))
    assert out["type"] == "rich_response" and out["body"] == ""
    assert out["richResponse"] == {"fragments": [
        {"type": "Text", "text": "Hello"}, {"type": "Text", "text": "there"}]}
    assert "unifiedResponse" not in out


def test_a_meta_ai_reply_in_the_newer_layout_carries_its_unified_response():
    unified = b'{"sections": [{"view_model": {"primitive": {"text": "Hi"}}}]}'
    out = _meta_ai_reply("MSG2", _rich(unified=unified))
    assert out["unifiedResponse"]["sections"][0]["view_model"]["primitive"]["text"] == "Hi"


def test_a_unified_response_that_is_not_json_is_left_out():
    out = _meta_ai_reply("M3", _rich("Hi", unified=b"\xff not json"))
    assert out["type"] == "rich_response" and "unifiedResponse" not in out



def test_a_status_add_yours_wrapper_shows_its_text():
    msg = Message()
    msg.statusAddYours.message.extendedTextMessage.text = "add yours"
    m = _build(msg)
    assert m["type"] == "chat" and m["body"] == "add yours"


def test_a_question_status_wrapper_shows_its_text():
    msg = Message()
    msg.questionMessage.message.extendedTextMessage.text = "ask me anything"
    assert _build(msg)["body"] == "ask me anything"
