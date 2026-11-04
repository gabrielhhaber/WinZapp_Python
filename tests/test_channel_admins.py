"""The channel admins window (ui/dialogs/channel_admins.py), without a window:
its pure helper and its handlers called unbound on a stub."""

from types import SimpleNamespace

from core import channels as ch
from ui.dialogs import channel_admins as ca
from ui.dialogs.channel_admins import ChannelAdminsDialog

I18N = SimpleNamespace(t=lambda key: {"unknown_contact": "unknown"}.get(key, key))


def channel(role):
    return ch.Channel(jid="1@newsletter", name="Canal", description="", followers=None,
                      verified=False, role=role, muted=None, invite="", created=0)


def test_the_picked_contact_is_said_by_name_never_by_jid():
    assert ca.contact_name({"name": " Ana ", "remoteJid": "1@lid"}, I18N) == "Ana"
    assert ca.contact_name({"pushName": "Bia", "remoteJid": "1@lid"}, I18N) == "Bia"
    assert ca.contact_name({"remoteJid": "1@lid"}, I18N) == "unknown"


class Stub:
    _target = ChannelAdminsDialog._target
    _on_invite = ChannelAdminsDialog._on_invite
    _on_revoke = ChannelAdminsDialog._on_revoke

    def __init__(self, role, picked=None):
        self._channel = channel(role)
        self._busy = False
        self._i18n = I18N
        self._mw = SimpleNamespace(_phone_to_lid={"5511999998888@s.whatsapp.net": "777@lid"})
        self.picked, self.asked, self.sent = picked, [], []

    def _current(self):
        return True

    def _pick(self, title_key, ok_key):
        self.asked.append(title_key)
        return self.picked

    def _send(self, work, done, failed):
        self.sent.append((done, failed))


def test_an_admin_who_is_not_the_owner_invites_nobody():
    stub = Stub("admin", {"remoteJid": "5511999998888@s.whatsapp.net"})
    stub._on_invite()
    stub._on_revoke()
    assert stub.asked == [] and stub.sent == []


def test_a_group_picked_by_mistake_is_never_invited():
    stub = Stub("owner", {"remoteJid": "1203@g.us", "name": "Grupo"})
    stub._on_invite()
    assert stub.asked == ["channel_admin_invite_pick"] and stub.sent == []


def test_the_invitee_is_named_by_lid_when_the_window_knows_it():
    stub = Stub("owner")
    assert stub._target({"remoteJid": "5511999998888@s.whatsapp.net"}) == "777@lid"
    assert stub._target({"remoteJid": "5511000001111@s.whatsapp.net"}) == "5511000001111@c.us"
    assert stub._target(None) == ""


class CountStub:
    _finish_count = ChannelAdminsDialog._finish_count

    def __init__(self, role):
        self._channel = channel(role)
        self._i18n = SimpleNamespace(t=lambda key: key)
        self.said = []

    def _announce(self, text):
        self.said.append(text)

    def Layout(self):
        pass


def test_the_count_is_spoken_and_an_admin_hears_who_invites():
    owner, admin = CountStub("owner"), CountStub("admin")
    owner._finish_count(3)
    admin._finish_count(None)
    assert owner.said == ["channel_admins_count"]
    assert admin.said == ["channel_admins_unavailable channel_admins_owner_only"]


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
