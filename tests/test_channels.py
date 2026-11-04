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
    ("https://chat.whatsapp.com/AbCdEfGhIjKlMnOpQrStUv", ""),
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


@pytest.mark.parametrize("args, body, key", [
    (("",), {"query": "", "limit": 30}, "channels_recommended"),
    (("", 30, "", "RECOMMENDED"), {"query": "", "limit": 30}, "channels_recommended"),
    (("", 30, "SPORTS", "TRENDING"), {"query": "", "limit": 30, "category": "SPORTS",
                                      "view": "TRENDING"}, "channels_found"),
    (("", 30, "", "NEW"), {"query": "", "limit": 30, "view": "NEW"}, "channels_found"),
    (("gol", 30, "SPORTS"), {"query": "gol", "limit": 30, "category": "SPORTS"}, "channels_found"),
    (("", 30, "SPECIAL_EVENTS", "bogus"), {"query": "", "limit": 30}, "channels_recommended"),
])
def test_a_category_and_a_view_narrow_the_directory(args, body, key):
    assert ch.directory_body(*args) == body
    query, _, category, view = (list(args) + ["", "", ""])[:4]
    assert ch.directory_announcement(query, category, view) == key


def test_pinned_updates_are_read_without_the_expired_ones():
    thread = {"pinned_messages": [{"message_id": "4977", "expiry_ts": "2000"},
                                  {"message_id": "12", "expiry_ts": "900"},
                                  {"message_id": "13"}, {"message_id": "4977"}, "x",
                                  {"message_id": "-4"}]}
    assert ch.pinned_ids(thread, now=1000) == (4977, 13)
    assert ch.pinned_ids({}, now=1000) == ()
    node = {"id": "1@newsletter", "thread_metadata": thread}
    assert ch.parse_channel(node).pinned == ch.pinned_ids(thread)


def test_the_overview_takes_the_loaded_updates_that_are_pinned():
    records = [{"key": {"id": "A"}, "serverId": 7}, {"key": {"id": "B"}, "serverId": 8},
               {"key": {"id": "C"}, "serverId": True}, {"key": {"id": "D"}}]
    found = ch.pinned_messages(records, (8, 1))
    assert found == [{"key": {"id": "B"}, "serverId": 8, "pinInChat": True}]
    assert "pinInChat" not in records[1]            # a copy, the row is untouched
    assert [ch.server_id_of(m) for m in records] == [7, 8, 0, 0]


@pytest.mark.parametrize("role, allowed", [("owner", True), ("admin", True),
                                           ("subscriber", False), ("guest", False), ("", False)])
def test_only_an_owner_or_an_admin_posts_and_pins(role, allowed):
    assert ch.admin_role(role) is allowed


def test_a_pin_names_the_update_by_its_server_id(monkeypatch):
    sent = []

    def post(url, json=None, headers=None, timeout=0):
        sent.append((url.rsplit("/api/tok/", 1)[1], json))
        return Response(200, {"status": "success", "response": {}})

    monkeypatch.setattr(ch, "api_post", post)
    ch.pin_update(CONNECTION, "1@newsletter", 4977, True)
    assert sent == [("channels/pin", {"id": "1@newsletter", "serverId": 4977, "pin": True})]
    monkeypatch.setattr(ch, "api_post", lambda *a, **k: Response(503, {}))
    with pytest.raises(ValueError, match="channel_pin_failed"):
        ch.pin_update(CONNECTION, "1@newsletter", 4977, False)


@pytest.mark.parametrize("jid, lids, target", [
    ("5511999998888@s.whatsapp.net", {}, "5511999998888@c.us"),
    ("5511999998888@s.whatsapp.net", {"5511999998888@s.whatsapp.net": "777@lid"}, "777@lid"),
    ("5511999998888@c.us", {"5511999998888@c.us": "777@lid"}, "777@lid"),
    ("777@lid", {}, "777@lid"),
    ("1203@g.us", {}, ""), ("status@broadcast", {}, ""), ("", {}, ""),
])
def test_an_admin_is_named_by_lid_when_known(jid, lids, target):
    assert ch.admin_target(jid, lids) == target


def test_the_admin_routes(monkeypatch):
    sent = []
    answers = {"channels/admins": {"status": "success", "response": {"adminCount": 2}}}

    def post(url, json=None, headers=None, timeout=0):
        route = url.rsplit("/api/tok/", 1)[1]
        sent.append((route, json))
        return Response(200, answers.get(route, {"status": "success", "response": {}}))

    monkeypatch.setattr(ch, "api_post", post)
    assert ch.admin_count(CONNECTION, "1@newsletter") == 2
    ch.invite_admin(CONNECTION, "1@newsletter", "777@lid", "Canal", "  oi  ")
    ch.revoke_admin_invite(CONNECTION, "1@newsletter", "777@lid")
    assert sent == [
        ("channels/admins", {"id": "1@newsletter"}),
        ("channels/admin-invite", {"id": "1@newsletter", "user": "777@lid", "name": "Canal",
                                   "message": "oi"}),
        ("channels/admin-revoke", {"id": "1@newsletter", "user": "777@lid"}),
    ]
    answers["channels/admins"] = {"status": "success", "response": {"adminCount": True}}
    assert ch.admin_count(CONNECTION, "1@newsletter") is None
    monkeypatch.setattr(ch, "api_post", lambda *a, **k: Response(503, {}))
    with pytest.raises(ValueError, match="channel_admin_invite_failed"):
        ch.invite_admin(CONNECTION, "1@newsletter", "777@lid")


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
    # the copy the dialog actually passes: the followed list's, role subscriber
    followed = ch.parse_channel({"id": "6@newsletter", "viewer_metadata": {"role": "SUBSCRIBER"},
                                 "thread_metadata": {"name": {"text": "Seguido"}}})
    assert channel_label(followed, I18n(), followed={"6@newsletter"}) == "Seguido, following"
    assert channel_label(followed, I18n()) == "Seguido"
    assert "newsletter" not in channel_label(guest, I18n())


def test_the_details_name_the_followers_role_and_link():
    from ui.dialogs.channels import channel_details
    text = channel_details(ch.parse_channel(NODE), I18n(), following=True)
    assert "followers 1520" in text and "you own it" in text
    assert "link https://whatsapp.com/channel/0029VaAbCdEfGhIjKlMnOp" in text
    plain = channel_details(ch.parse_channel({"id": "5@newsletter"}, default_role="guest"),
                            I18n(), following=False)
    assert plain == "you do not follow"
