"""core/channels.py: the channels window's reading and actions."""
import pytest

from core import channels as ch

NODE = {
    "id": "120363000000000001@newsletter",
    "state": {"type": "ACTIVE"},
    "thread_metadata": {"name": {"text": " Notícias "}, "description": {"text": "Todo dia"},
                        "invite": "0029VaAbCdEfGhIjKlMnOp", "subscribers_count": "1520",
                        "verification": "VERIFIED", "creation_time": "1700000000"},
    "viewer_metadata": {"role": "OWNER", "mute": "OFF"},
}


def test_a_node_becomes_a_channel():
    c = ch.parse_channel(NODE)
    assert (c.jid, c.name, c.description, c.followers, c.verified) == (
        NODE["id"], "Notícias", "Todo dia", 1520, True)
    assert (c.role, c.muted, c.created) == ("owner", False, 1700000000)
    assert c.following and c.owner and c.admin
    assert c.link == "https://whatsapp.com/channel/0029VaAbCdEfGhIjKlMnOp"


def test_what_the_answer_does_not_say_stays_unknown():
    c = ch.parse_channel({"id": "1@newsletter"}, default_role="guest")
    assert (c.name, c.followers, c.muted, c.role, c.link) == ("", None, None, "guest", "")
    assert not c.following


@pytest.mark.parametrize("node", [
    {"id": "1@g.us"}, {"id": "x"}, None, "text",
    {"id": "1@newsletter", "state": {"type": "DELETED"}},
    {"id": "1@newsletter", "state": {"type": "NON_EXISTING"}},
])
def test_anything_else_is_not_a_channel(node):
    assert ch.parse_channel(node) is None


def test_a_list_answer_drops_repeats_and_strangers():
    body = {"status": "success", "response": [NODE, NODE, {"id": "5511@c.us"}]}
    assert [c.jid for c in ch.parse_channels(body)] == [NODE["id"]]


@pytest.mark.parametrize("body", [{"status": "error"}, {"status": "success", "response": {}}, None])
def test_a_refused_answer_is_an_error(body):
    with pytest.raises(ValueError):
        ch.parse_channels(body)


@pytest.mark.parametrize("text, code", [
    ("https://whatsapp.com/channel/0029VaAbCdEfGhIjKlMnOp", "0029VaAbCdEfGhIjKlMnOp"),
    ("  0029VaAbCdEfGhIjKlMnOp/ ", "0029VaAbCdEfGhIjKlMnOp"),
    ("short", ""),
    (None, ""),
])
def test_an_invite_code_is_read_from_a_link(text, code):
    assert ch.invite_code(text) == code


def test_channels_sort_by_name():
    a = ch.parse_channel({**NODE, "id": "2@newsletter", "thread_metadata": {"name": {"text": "beta"}}})
    b = ch.parse_channel({**NODE, "id": "3@newsletter", "thread_metadata": {"name": {"text": "Alfa"}}})
    assert [c.name for c in ch.sorted_channels([a, b])] == ["Alfa", "beta"]


class Response:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


CONNECTION = ("http://127.0.0.1", 6300, "tok")


def test_requests_go_to_the_engines_routes(monkeypatch):
    sent = []

    def post(url, json=None, headers=None, timeout=0):
        sent.append((url.rsplit("/api/tok/", 1)[1], json))
        if url.endswith("newsletter"):
            return Response(201, {"idJid": "9@newsletter"})
        if url.endswith("channels/info"):
            return Response(200, {"status": "success", "response": NODE})
        return Response(200, {"status": "success", "response": [NODE]})

    monkeypatch.setattr(ch, "api_post", post)
    assert [c.jid for c in ch.list_channels(CONNECTION)] == [NODE["id"]]
    found = ch.directory(CONNECTION, "futebol")
    assert found[0].role == "owner"           # the node said so
    assert ch.channel_info(CONNECTION, invite="0029VaAbCdEfGhIjKlMnOp").jid == NODE["id"]
    ch.follow(CONNECTION, "1@newsletter")
    ch.set_muted(CONNECTION, "1@newsletter", True)
    assert ch.create_channel(CONNECTION, "Meu canal", "sobre") == "9@newsletter"
    assert sent == [
        ("channels/list", {}), ("channels/directory", {"query": "futebol", "limit": 30}),
        ("channels/info", {"invite": "0029VaAbCdEfGhIjKlMnOp"}),
        ("channels/follow", {"id": "1@newsletter"}), ("channels/mute", {"id": "1@newsletter", "mute": True}),
        ("newsletter", {"name": "Meu canal", "options": {"description": "sobre"}}),
    ]


def test_a_refused_action_raises_its_code(monkeypatch):
    monkeypatch.setattr(ch, "api_post", lambda *a, **k: Response(503, {"code": "x"}))
    with pytest.raises(ValueError, match="channel_follow_failed"):
        ch.follow(CONNECTION, "1@newsletter")


def test_edit_and_delete_use_put_and_delete(monkeypatch):
    sent = []

    def request(method, url, json=None, headers=None, timeout=0):
        sent.append((method, url.rsplit("/api/tok/", 1)[1], json))
        return Response(201, False if method == "DELETE" and "2@" in url else True)

    monkeypatch.setattr(ch, "api_request", request)
    ch.edit_channel(CONNECTION, "1@newsletter", "Novo", "desc")
    ch.delete_channel(CONNECTION, "1@newsletter")
    with pytest.raises(ValueError, match="channel_delete_failed"):
        ch.delete_channel(CONNECTION, "2@newsletter")
    assert sent[0] == ("PUT", "newsletter/1@newsletter", {"name": "Novo", "description": "desc"})
    assert sent[1][:2] == ("DELETE", "newsletter/1@newsletter")


class I18n:
    def t(self, key):
        return {"channels_unnamed": "unnamed", "channels_verified": "verified",
                "channels_role_owner": "yours", "channels_role_admin": "admin",
                "channels_following": "following", "channels_muted": "muted",
                "channels_followers": "followers {count}", "channels_you_are_owner": "you own it",
                "channels_you_are_admin": "you admin it", "channels_you_follow": "you follow",
                "channels_you_do_not_follow": "you do not follow",
                "channels_created_on": "created {date}", "channels_link_line": "link {link}"}[key]


def test_a_row_says_what_sets_the_channel_apart_never_its_id():
    from ui.dialogs.channels import channel_label
    owner = ch.parse_channel(NODE)
    guest = ch.parse_channel({"id": "5@newsletter", "viewer_metadata": {"mute": "ON"}},
                             default_role="guest")
    assert channel_label(owner, I18n()) == "Notícias, verified, yours"
    assert channel_label(guest, I18n(), followed={"5@newsletter"}) == "unnamed, following, muted"
    assert "newsletter" not in channel_label(guest, I18n())


def test_the_details_name_the_followers_role_and_link():
    from ui.dialogs.channels import channel_details
    text = channel_details(ch.parse_channel(NODE), I18n(), following=True)
    assert "followers 1520" in text and "you own it" in text
    assert "link https://whatsapp.com/channel/0029VaAbCdEfGhIjKlMnOp" in text
    plain = channel_details(ch.parse_channel({"id": "5@newsletter"}, default_role="guest"),
                            I18n(), following=False)
    assert plain == "you do not follow"
