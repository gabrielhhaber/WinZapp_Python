"""The channel's admins and followers window (ui/dialogs/channel_admins.py),
without a window: its pure helpers and its handlers called unbound on a stub."""

from types import SimpleNamespace

from core import channels as ch
from ui.dialogs import channel_admins as ca
from ui.dialogs.channel_admins import ChannelAdminsDialog

I18N = SimpleNamespace(t=lambda key: {"unknown_contact": "unknown"}.get(key, key))
ME_LID, ME_PHONE = "99999@lid", "5500000000000@s.whatsapp.net"


def channel(role):
    return ch.Channel(jid="1@newsletter", name="Canal", description="", followers=None,
                      verified=False, role=role, muted=None, invite="", created=0)


def test_the_picked_contact_is_said_by_name_never_by_jid():
    assert ca.contact_name({"name": " Ana ", "remoteJid": "1@lid"}, I18N) == "Ana"
    assert ca.contact_name({"pushName": "Bia", "remoteJid": "1@lid"}, I18N) == "Bia"
    assert ca.contact_name({"remoteJid": "1@lid"}, I18N) == "unknown"


def test_a_part_of_the_read_that_fails_leaves_the_others(monkeypatch):
    monkeypatch.setattr(ch, "admin_count", lambda c, j: 2)
    monkeypatch.setattr(ch, "followers", lambda c, j: (_ for _ in ()).throw(ValueError("x")))
    monkeypatch.setattr(ch, "pending_admins", lambda c, j: ["55555@lid"])
    assert ca.read_people(None, channel("owner")) == (2, None, ["55555@lid"])
    assert ca.read_people(None, channel("admin")) == (2, None, [])     # only the owner sees invites


class Stub:
    _me = ChannelAdminsDialog._me
    _is_me = ChannelAdminsDialog._is_me
    _name = ChannelAdminsDialog._name
    _row_text = ChannelAdminsDialog._row_text
    _target = ChannelAdminsDialog._target
    _on_invite = ChannelAdminsDialog._on_invite
    _on_make_admin = ChannelAdminsDialog._on_make_admin
    _on_demote = ChannelAdminsDialog._on_demote
    _on_make_owner = ChannelAdminsDialog._on_make_owner
    _member_action = ChannelAdminsDialog._member_action
    _set_my_role = ChannelAdminsDialog._set_my_role
    _refused_while_busy = ChannelAdminsDialog._refused_while_busy

    def __init__(self, role, picked=None, selected=None, confirm=True):
        self._channel = channel(role)
        self._busy = False
        self._i18n = I18N
        self.changed = False
        self._mw = SimpleNamespace(
            my_lid=ME_LID, my_jid=ME_PHONE,
            _phone_to_lid={"5511999998888@s.whatsapp.net": "777@lid"},
            # as the real one: a saved name, else the pushName, else a placeholder
            chat_display_name=lambda chat: {"5511999998888@s.whatsapp.net": "Ana"}.get(
                chat["remoteJid"]) or chat.get("pushName") or "unknown")
        self.picked, self.selected, self.confirm = picked, selected, confirm
        self.asked, self.sent, self.said, self.invited = [], [], [], []

    def _current(self):
        return True

    def _selected(self):
        return self.selected

    def _pick(self, title_key, ok_key):
        self.asked.append(title_key)
        return self.picked

    def _confirm(self, key, name):
        self.asked.append((key, name))
        return self.confirm

    def _invite_person(self, user, name):
        self.invited.append((user, name))

    def _send(self, work, done, failed):
        self.sent.append(work)
        self.said.append(done(None))

    def _announce(self, text):
        self.said.append(text)


def test_rows_say_names_and_roles_and_this_account_is_you():
    stub = Stub("owner")
    ana = ch.Member("11111@lid", "5511999998888@s.whatsapp.net", "Ana WA", "subscriber")
    bia = ch.Member("22222@lid", "", "Bia", "admin")
    nobody = ch.Member("33333@lid", "", "", "pending")
    me = ch.Member(ME_LID, "", "", "owner")
    assert [stub._row_text(m) for m in (ana, bia, nobody, me)] == [
        "Ana, channel_role_follower", "Bia, channel_role_admin",            # Bia: WhatsApp's name
        "unknown, channel_role_pending", "channel_member_you, channel_role_owner"]


def test_an_admin_who_is_not_the_owner_invites_nobody():
    stub = Stub("admin", {"remoteJid": "5511999998888@s.whatsapp.net"},
                selected=ch.Member("11111@lid", "", "", "subscriber"))
    stub._on_invite()
    stub._on_make_admin()
    assert stub.asked == [] and stub.invited == []


def test_the_owner_invites_a_contact_or_the_selected_follower_by_lid():
    stub = Stub("owner", {"remoteJid": "5511999998888@s.whatsapp.net", "name": "Ana"},
                selected=ch.Member("11111@lid", "", "Carlos", "subscriber"))
    stub._on_invite()
    stub._on_make_admin()
    assert stub.invited == [("777@lid", "Ana"), ("11111@lid", "Carlos")]


def test_a_group_picked_by_mistake_is_never_invited():
    stub = Stub("owner", {"remoteJid": "1203@g.us", "name": "Grupo"})
    stub._on_invite()
    assert stub.asked == ["channel_admin_invite_pick"] and stub.invited == []


def test_removing_an_admin_asks_first_and_a_no_does_nothing():
    bia = ch.Member("22222@lid", "", "Bia", "admin")
    yes, no = Stub("owner", selected=bia), Stub("owner", selected=bia, confirm=False)
    yes._on_demote()
    no._on_demote()
    assert yes.asked == [("channel_admin_demote_confirm", "Bia")] and len(yes.sent) == 1
    assert no.sent == []


def test_handing_the_channel_over_makes_this_account_an_admin():
    stub = Stub("owner", selected=ch.Member("22222@lid", "", "Bia", "admin"))
    stub._on_make_owner()
    assert stub._channel.role == "admin" and stub.changed
    assert stub._mw._channel_roles == {"1@newsletter": "admin"}
    assert stub.said == ["channel_owner_made"]


def test_an_admin_cannot_remove_another_admin():
    stub = Stub("admin", selected=ch.Member("22222@lid", "", "Bia", "admin"))
    stub._on_demote()
    assert stub.asked == [] and stub.sent == []


class BusyStub:
    from ui.dialogs.channels import ChannelsDialog
    _refused_while_busy = ChannelsDialog._refused_while_busy
    _on_delete = ChannelsDialog._on_delete

    def __init__(self):
        self._busy = True
        self._i18n = SimpleNamespace(t=lambda key: key)
        self.said = []

    def _mine_version(self, selected):
        return channel("owner")

    def _selected(self):
        return channel("owner")

    def _announce(self, text):
        self.said.append(text)


def test_delete_while_busy_is_refused_before_asking():
    stub = BusyStub()
    stub._on_delete()          # no confirmation dialog is built: it would raise here
    assert stub.said == ["loading"]
