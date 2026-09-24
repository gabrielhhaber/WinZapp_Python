"""A local contact is shown everywhere at once, not after a restart.

Reported 2026-09-23: adding a local contact ("Adicionar contato local" in the
conversation data dialog) left the message list showing the person's original
WhatsApp name, even after closing and reopening the conversation; only a
restart fixed it.

The name was stored only under the phone JID. The chat and its messages are
usually keyed by the person's @lid, whose parallel contact record still held
the WhatsApp name, and the message list's name lookups (_sender_label for
one-to-one rows, _get_participant_name for group rows) tried the @lid record
first. MainWindow._resolve_contact_name() already put the phone record first
for exactly this reason; the panel's lookups did not. At startup,
register_jid_mapping() rebuilt the @lid record from the phone one, which is
why a restart hid the bug.

Two fixes, each sufficient for the reported case: the panel's lookups now try
the phone record first, and save_local_contact() mirrors the entry onto the
@lid record (persisted), then redraws the chat list and the open rows.
"""

from main import MainWindow
from ui.conversations import ConversationsPanel

PHONE = "5511999999999@s.whatsapp.net"
PHONE_8 = "551199999999@s.whatsapp.net"
LID = "123456789012345@lid"


class _Mw:
    _is_bad_contact_name = staticmethod(MainWindow._is_bad_contact_name)
    get_chat = MainWindow.get_chat
    save_local_contact = MainWindow.save_local_contact
    remove_local_contact = MainWindow.remove_local_contact
    _lid_for_local_contact = MainWindow._lid_for_local_contact
    _refresh_views_after_contact_change = MainWindow._refresh_views_after_contact_change
    _phone_digits_equivalent = staticmethod(MainWindow._phone_digits_equivalent)
    _normalize_jid = staticmethod(MainWindow._normalize_jid)

    def __init__(self, phone=PHONE):
        self.contacts = {LID: {"id": LID, "remoteJid": LID,
                               "name": "Original", "pushName": "Original"}}
        self.chats = {phone: {"remoteJid": phone, "name": "Original"}}
        self._lid_to_phone = {LID: phone}
        self._phone_to_lid = {phone: LID}
        self._presence_pushname_map = {}
        self.db = _Db()
        self.set_chats = 0
        self.conversations_panel = None

    def _is_self_jid(self, jid):
        return False

    def self_reference_label(self):
        return "Eu"

    def _schedule_set_chats(self):
        self.set_chats += 1


class _Db:
    def __init__(self):
        self.upserted = {}
        self.deleted = []

    def upsert_contacts_batch(self, contacts):
        self.upserted.update(contacts)

    def delete_contact(self, jid):
        self.deleted.append(jid)


class _Panel:
    _sender_label = ConversationsPanel._sender_label
    _get_participant_name = ConversationsPanel._get_participant_name
    _is_separator = ConversationsPanel._is_separator

    def __init__(self, mw, conversation=None, rows=()):
        self.main_window = mw
        self.conversation = conversation
        self._sorted_messages = list(rows)
        self._group_participants_cache = []
        self.repainted = []

    def _repaint_or_repopulate(self, ids):
        self.repainted.append(list(ids))


def _incoming(remote, participant=None, mid="M1"):
    key = {"remoteJid": remote, "fromMe": False, "id": mid}
    if participant:
        key["participant"] = participant
    return {"key": key, "messageType": "conversation",
            "message": {"conversation": "oi"}, "pushName": "Original"}


class TestTheLookupsPreferThePhoneRecord:
    """Even with the @lid record left stale, the phone record wins."""

    def _mw_with_only_phone_updated(self):
        mw = _Mw()
        mw.contacts[PHONE] = {"remoteJid": PHONE, "name": "Novo Nome", "isSaved": True}
        return mw

    def test_a_one_to_one_row_shows_the_local_contact(self):
        panel = _Panel(self._mw_with_only_phone_updated(), {"remoteJid": PHONE})
        assert panel._sender_label(_incoming(LID)) == "Novo Nome"

    def test_a_group_row_shows_the_local_contact(self):
        panel = _Panel(self._mw_with_only_phone_updated())
        assert panel._get_participant_name(LID, resolve_missing=False) == "Novo Nome"

    def test_without_a_phone_record_the_lid_record_still_answers(self):
        panel = _Panel(_Mw(), {"remoteJid": PHONE})
        assert panel._sender_label(_incoming(LID)) == "Original"


class TestSaveLocalContact:
    def _entry(self, name="Novo Nome", jid=PHONE):
        return {"remoteJid": jid, "name": name, "pushName": name, "isSaved": True}

    def test_the_lid_record_gets_the_same_name(self):
        mw = _Mw()
        mw.save_local_contact(PHONE, self._entry())
        assert mw.contacts[PHONE]["name"] == "Novo Nome"
        assert mw.contacts[LID]["name"] == "Novo Nome"
        assert mw.contacts[LID]["remoteJid"] == LID

    def test_both_records_are_persisted(self):
        mw = _Mw()
        mw.save_local_contact(PHONE, self._entry())
        assert set(mw.db.upserted) == {PHONE, LID}

    def test_a_ninth_digit_variant_still_finds_the_lid(self):
        """The mapping was learned for the 8-digit form, the user typed 9."""
        mw = _Mw(phone=PHONE_8)
        mw.save_local_contact(PHONE, self._entry())
        assert mw.contacts[LID]["name"] == "Novo Nome"

    def test_no_known_lid_stores_the_phone_record_only(self):
        mw = _Mw()
        mw._phone_to_lid = {}
        mw.save_local_contact(PHONE, self._entry())
        assert set(mw.db.upserted) == {PHONE}
        assert mw.contacts[LID]["name"] == "Original"

    def test_the_chat_list_and_the_open_rows_are_redrawn(self, monkeypatch):
        import main
        monkeypatch.setattr(main.wx, "CallAfter", lambda fn, *a, **k: fn(*a, **k))
        mw = _Mw()
        rows = [_incoming(LID, mid="A"), {"_type": "unread_separator"},
                _incoming(LID, mid="B")]
        mw.conversations_panel = _Panel(mw, {"remoteJid": PHONE}, rows)
        mw.save_local_contact(PHONE, self._entry())
        assert mw.set_chats == 1
        assert mw.conversations_panel.repainted == [["A", "B"]]

    def test_after_saving_the_open_row_reads_the_new_name(self):
        mw = _Mw()
        mw.save_local_contact(PHONE, self._entry())
        panel = _Panel(mw, {"remoteJid": PHONE})
        assert panel._sender_label(_incoming(LID)) == "Novo Nome"


class TestRemoveLocalContact:
    def test_the_lid_copy_goes_with_it(self):
        mw = _Mw()
        mw.save_local_contact(PHONE, {"remoteJid": PHONE, "name": "Novo Nome", "isSaved": True})
        mw.remove_local_contact(PHONE)
        assert PHONE not in mw.contacts and LID not in mw.contacts
        assert set(mw.db.deleted) == {PHONE, LID}

    def test_a_lid_record_whatsapp_renamed_since_is_kept(self):
        mw = _Mw()
        mw.save_local_contact(PHONE, {"remoteJid": PHONE, "name": "Novo Nome", "isSaved": True})
        mw.contacts[LID]["name"] = "Nome do WhatsApp"
        mw.remove_local_contact(PHONE)
        assert mw.contacts[LID]["name"] == "Nome do WhatsApp"
        assert mw.db.deleted == [PHONE]
