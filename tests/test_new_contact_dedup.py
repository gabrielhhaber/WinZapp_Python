"""New local contact (NewContactDialog, via "Nova conversa" > "Novo contato")
built its JID straight from the digits the user typed, then
NewConversationDialog._on_new_contact() registered a chat under that exact
raw JID with no normalization and no lookup against chats that might already
exist for the same person under a different-but-equivalent JID.

Reported live 2026-09-03: a contact was added by typing the number with its
Brazilian 9th mobile digit, but WhatsApp's own pn-lid resolution for that
number returned the 8-digit form bridged to a @lid. Two chats existed for
the same person from then on — the manually-typed one was a dead end
nothing ever routed a message to, so its first message stayed "not
confirmed" forever, while the real conversation lived under the other JID.

_find_existing_chat() is the fix: same normalize + get_chat() lookup
_open_conversation() already did for existing search results, plus a
contact_dedup_key() scan over main_window.chats so an 8/9-digit variant or
an already-bridged @lid is recognized as the same person before a new chat
entry is created. It's a @staticmethod taking only (mw, jid), so it can be
exercised directly without instantiating the wx.Dialog.
"""

from ui.dialogs.new_conversation import NewConversationDialog


class _FakeMw:
    def __init__(self, chats=None, lid_to_phone=None):
        self.chats = dict(chats or {})
        self._lid_to_phone = dict(lid_to_phone or {})

    def _normalize_jid(self, jid):
        if not jid:
            return jid
        if ":" in jid and "@" in jid:
            base, rest = jid.split("@", 1)
            jid = f"{base.split(':', 1)[0]}@{rest}"
        if jid.endswith("@c.us"):
            return jid[:-5] + "@s.whatsapp.net"
        return jid

    def get_chat(self, jid):
        chat = self.chats.get(jid)
        if chat is not None:
            return chat
        if jid.endswith("@lid"):
            alt = self._lid_to_phone.get(jid, "")
        else:
            alt = {v: k for k, v in self._lid_to_phone.items()}.get(jid, "")
        return self.chats.get(alt) if alt else None


class TestFindExistingChatForNewContact:
    def test_brazilian_9th_digit_variant_reuses_the_existing_chat(self):
        existing_jid = "551199999999@s.whatsapp.net"
        existing_chat = {"remoteJid": existing_jid, "pushName": "Arthur"}
        mw = _FakeMw(chats={existing_jid: existing_chat})

        norm_jid, found = NewConversationDialog._find_existing_chat(
            mw, "5511999999999@s.whatsapp.net"
        )

        assert found is existing_chat
        assert norm_jid == "5511999999999@s.whatsapp.net"

    def test_already_bridged_lid_reuses_the_existing_chat(self):
        lid = "84563963461698@lid"
        phone = "555196664076@s.whatsapp.net"
        lid_chat = {"remoteJid": lid, "pushName": "Arthur"}
        mw = _FakeMw(chats={lid: lid_chat}, lid_to_phone={lid: phone})

        norm_jid, found = NewConversationDialog._find_existing_chat(mw, phone)

        assert found is lid_chat

    def test_cus_and_net_forms_reuse_the_existing_chat(self):
        existing_jid = "5511999999999@s.whatsapp.net"
        existing_chat = {"remoteJid": existing_jid, "pushName": "Bia"}
        mw = _FakeMw(chats={existing_jid: existing_chat})

        norm_jid, found = NewConversationDialog._find_existing_chat(
            mw, "5511999999999@c.us"
        )

        assert found is existing_chat
        assert norm_jid == existing_jid

    def test_truly_new_contact_finds_nothing(self):
        mw = _FakeMw(chats={"5511999999999@s.whatsapp.net": {}})

        norm_jid, found = NewConversationDialog._find_existing_chat(
            mw, "5521988887777@s.whatsapp.net"
        )

        assert found is None
        assert norm_jid == "5521988887777@s.whatsapp.net"

    def test_group_chats_are_never_matched_as_the_same_person(self):
        mw = _FakeMw(chats={"120363000000000001@g.us": {"remoteJid": "120363000000000001@g.us"}})

        norm_jid, found = NewConversationDialog._find_existing_chat(
            mw, "5511999999999@s.whatsapp.net"
        )

        assert found is None
