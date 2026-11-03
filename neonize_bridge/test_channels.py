"""Channel routes: the pure functions that build what the bridge answers."""
import base64
from types import SimpleNamespace as N

import pytest

import channels


def jid_text(jid):
    return "%s@%s" % (jid.User, jid.Server)


def meta(mute=2, verified=2):
    return N(ID=N(User="120363", Server="newsletter"),
             ThreadMeta=N(Name=N(Text="News"), Description=N(Text="Summary"),
                          InviteCode="abc", CreationTime=1700000000,
                          SubscriberCount=42, VerificationState=verified),
             ViewerMeta=N(Mute=mute))


class _Store:
    def __init__(self, *jids):
        self.names = dict.fromkeys(jids, "")

    def channels(self):
        return list(self.names)

    def channel_name(self, jid, name):
        self.names[jid] = name


class _NamesClient:
    def __init__(self, **names):
        self.names = names

    def get_newsletter_info(self, jid):
        name = self.names[jid]
        if isinstance(name, Exception):
            raise name
        return N(ThreadMeta=N(Name=N(Text=name)))


def test_a_stored_channel_gets_the_name_whatsapp_gives_it():
    store = _Store("120363@newsletter")
    channels.load_names(_NamesClient(u120363="Canal"), store, lambda user, _server: "u" + user)
    assert store.names == {"120363@newsletter": "Canal"}


def test_a_channel_whatsapp_refuses_does_not_stop_the_next_one():
    store = _Store("1@newsletter", "2@newsletter")
    client = _NamesClient(u1=RuntimeError("not found"), u2="Canal")
    channels.load_names(client, store, lambda user, _server: "u" + user)
    assert store.names == {"1@newsletter": "", "2@newsletter": "Canal"}


def test_a_blank_answer_keeps_the_name_already_known():
    store = _Store("1@newsletter")
    store.names["1@newsletter"] = "Canal"
    channels.load_names(_NamesClient(u1=""), store, lambda user, _server: "u" + user)
    assert store.names["1@newsletter"] == "Canal"


def test_channel_id_accepts_with_and_without_server():
    assert channels.channel_id("120363") == "120363@newsletter"
    assert channels.channel_id(" 120363@newsletter ") == "120363@newsletter"


@pytest.mark.parametrize("text", ["", None, "@newsletter", "5511@c.us", "5511@g.us"])
def test_channel_id_refuses_what_is_not_a_channel(text):
    assert channels.channel_id(text) == ""


def test_image_from_text_decodes_base64_and_data_url():
    raw = base64.b64encode(b"\x89PNGdata").decode()
    assert channels.image_from_text(raw) == b"\x89PNGdata"
    assert channels.image_from_text("data:image/png;base64," + raw) == b"\x89PNGdata"


def test_empty_image_from_text_is_empty_bytes():
    assert channels.image_from_text(None) == b""
    assert channels.image_from_text("  ") == b""


@pytest.mark.parametrize("text", ["/etc/passwd", "https://example.com/a.png", "C:\\x.png"])
def test_image_that_is_not_base64_is_refused_instead_of_becoming_a_path(text):
    with pytest.raises(ValueError):
        channels.image_from_text(text)


def test_creation_request_reads_the_flat_body():
    photo = base64.b64encode(b"x").decode()
    assert channels.creation_request(
        {"name": " My channel ", "description": "d", "picture": photo}
    ) == {"name": "My channel", "description": "d", "picture": b"x"}


def test_creation_request_reads_the_wppconnect_options():
    photo = base64.b64encode(b"y").decode()
    request = channels.creation_request(
        {"name": "C", "options": {"description": "from the option", "picture": photo}})
    assert request == {"name": "C", "description": "from the option", "picture": b"y"}


def test_creation_request_without_a_name_is_refused():
    with pytest.raises(ValueError):
        channels.creation_request({"description": "x"})
    with pytest.raises(ValueError):
        channels.creation_request(None)


def test_creation_request_without_description_or_image():
    assert channels.creation_request({"name": "C"}) == {
        "name": "C", "description": "", "picture": b""}


def test_mute_from_accepts_the_names_and_the_types():
    assert channels.mute_from({"mute": False}) is False
    assert channels.mute_from({"value": "false"}) is False
    assert channels.mute_from({"muted": True}) is True
    assert channels.mute_from({"value": "true"}) is True


def test_mute_from_with_nothing_mutes():
    assert channels.mute_from({}) is True
    assert channels.mute_from(None) is True


def test_channel_to_wpp_builds_the_response():
    assert channels.channel_to_wpp(meta(), jid_text) == {
        "id": "120363@newsletter", "name": "News", "description": "Summary",
        "inviteCode": "abc", "creationTime": 1700000000, "subscribersCount": 42,
        "verified": False, "muted": False}


def test_channel_to_wpp_marks_muted_and_verified():
    response = channels.channel_to_wpp(meta(mute=1, verified=1), jid_text)
    assert response["muted"] is True and response["verified"] is True


def test_channel_not_found_recognizes_whatsapps_error():
    assert channels.channel_not_found(Exception("item-not-found"))
    assert channels.channel_not_found(Exception("newsletter not found"))
    assert not channels.channel_not_found(Exception("timeout"))
