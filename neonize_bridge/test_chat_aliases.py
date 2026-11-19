"""A chat stored under the number or the LID is found through both (chat_aliases.py)."""
from types import SimpleNamespace as N

import chat_aliases as ca
from store import Store

PN = "5511999990000@s.whatsapp.net"
LID = "111222333@lid"


def _exists(*stored):
    return lambda key: key in stored


def test_keys_find_the_chat_through_the_other_address():
    mapping = {PN: LID, LID: PN}
    # stored by the LID, asked by the number (and the opposite)
    assert ca.chat_keys(_exists(LID), PN, lambda j: mapping.get(j, "")) == [LID]
    assert ca.chat_keys(_exists(PN), LID, lambda j: mapping.get(j, "")) == [PN]


def test_keys_return_both_addresses_with_the_requested_first():
    mapping = {PN: LID, LID: PN}
    found = ca.chat_keys(_exists(PN, LID), LID, lambda j: mapping.get(j, ""))
    assert found == [LID, PN]


def test_keys_accept_the_c_us_form_and_the_stored_legacy_form():
    assert ca.chat_keys(_exists(PN), "5511999990000@c.us", lambda j: "") == [PN]
    assert ca.chat_keys(_exists("5511999990000@c.us"), PN, lambda j: "") == [
        "5511999990000@c.us"]


def test_keys_without_a_chat_or_without_a_mapping_is_empty():
    assert ca.chat_keys(_exists(), PN, lambda j: LID) == []
    assert ca.chat_keys(_exists(LID), PN, lambda j: "") == []


def test_group_does_not_ask_for_the_other_address():
    questions = []
    assert ca.chat_keys(_exists("1203@g.us"), "1203@g.us",
                        lambda j: questions.append(j) or "") == ["1203@g.us"]
    assert questions == []


class _Jid:
    def __init__(self, user, server):
        self.User, self.Server = user, server


class _Neonize:
    def get_lid_from_pn(self, jid):
        return _Jid("111222333", "lid") if jid.User == "5511999990000" else _Jid("", "")

    def get_pn_from_lid(self, jid):
        return _Jid("5511999990000", "s.whatsapp.net") if jid.User == "111222333" else _Jid("", "")


class _Session(ca.ChatAlias):
    def __init__(self, db, client, status="CONNECTED"):
        self.db, self.client, self.status = db, client, status


def test_session_finds_the_chat_through_neonizes_mapping(tmp_path):
    db = Store(str(tmp_path / "x.db"))
    db.chat(LID, name="Ana")
    s = _Session(db, _Neonize())
    assert s.existing_chat("5511999990000@c.us") == LID
    assert s.chat_keys(PN) == [LID]
    assert s.existing_chat("999@s.whatsapp.net") == ""


def test_disconnected_session_only_finds_the_exact_key(tmp_path):
    db = Store(str(tmp_path / "x.db"))
    db.chat(LID)
    assert _Session(db, None, "CLOSED").existing_chat(PN) == ""
    assert _Session(db, None, "CLOSED").existing_chat(LID) == LID


def test_a_mutation_names_a_persons_chat_by_its_lid():
    """WhatsApp Web indexes clearChat, archive, pin... by the chat's LID; the
    phone did not clear a chat named by its number."""
    mapping = {PN: LID}
    assert ca.mutation_index_jid(PN, lambda j: mapping.get(j, "")) == LID
    assert ca.mutation_index_jid("5511999990000@c.us", lambda j: mapping.get(j, "")) == LID
    assert ca.mutation_index_jid(LID, lambda j: PN) == LID
    # LID unknown: the number, as before
    assert ca.mutation_index_jid(PN, lambda j: "") == PN
    asked = []
    assert ca.mutation_index_jid("1203630@g.us", lambda j: asked.append(j) or LID) == "1203630@g.us"
    assert asked == []
