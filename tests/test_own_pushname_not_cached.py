"""An outgoing message carries OUR pushName; it must never be cached as the
recipient's name (the conversation then showed our own name instead of the
unsaved contact's phone number)."""

from tests.test_lid_mapping_thread_safety import _Stub as _BaseStub


class _Stub(_BaseStub):
    def _needs_sender_resolution(self, jid):
        return False


def _msg(from_me, push="Meu Nome"):
    return {
        "key": {"remoteJid": "5511999990000@s.whatsapp.net", "fromMe": from_me},
        "pushName": push,
    }


def test_outgoing_pushname_is_not_cached():
    stub = _Stub()
    stub._extract_lid_mapping(_msg(True))
    assert stub._message_pushname_cache == {}


def test_incoming_pushname_is_cached():
    stub = _Stub()
    stub._extract_lid_mapping(_msg(False, "Maria"))
    assert stub._message_pushname_cache == {"5511999990000@s.whatsapp.net": "Maria"}
