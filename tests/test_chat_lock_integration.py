"""Boundary tests for MainWindow's local locked-chat identity handling.

The methods are exercised on a lightweight stub so no wx window, WPPConnect
session, browser profile or real WhatsApp data is opened.
"""

from cryptography.fernet import Fernet

from core.chat_lock_vault import ChatLockVault, jid_fingerprint
from main import MainWindow


class _Database:
    def __init__(self):
        self.json_writes = []

    def set_metadata_json(self, key, value):
        self.json_writes.append((key, value))


class _MainWindowStub:
    _CHAT_LOCK_INDEX_KEY = MainWindow._CHAT_LOCK_INDEX_KEY
    _normalize_jid = staticmethod(MainWindow._normalize_jid)
    _chat_lock_candidates = MainWindow._chat_lock_candidates
    is_chat_locked = MainWindow.is_chat_locked
    chat_lock_navigation_visible = MainWindow.chat_lock_navigation_visible
    _forget_chat_lock = MainWindow._forget_chat_lock

    def __init__(self, key, vault=None):
        self.key = key
        self._chat_lock_vault = vault
        self._chat_lock_fingerprints = set()
        self._lid_to_phone = {}
        self._phone_to_lid = {}
        self.chats = {}
        self.db = _Database()
        self.persist_calls = 0

    def _persist_chat_lock_vault(self):
        self.persist_calls += 1


def _vault_with_chat(key, jid):
    vault = ChatLockVault(key)
    vault.configure("246810", "gizli-kod")
    vault.lock_chat(jid)
    return vault


def test_fingerprint_index_still_hides_chat_after_interrupted_token_write():
    key = Fernet.generate_key()
    jid = "1234567890@s.whatsapp.net"
    vault = ChatLockVault(key)
    vault.configure("246810", "gizli-kod")
    mw = _MainWindowStub(key, vault)
    mw._chat_lock_fingerprints = {jid_fingerprint(key, jid)}

    assert not vault.is_locked(jid)
    assert mw.is_chat_locked(jid)


def test_hidden_navigation_stays_hidden_even_while_vault_is_unlocked():
    key = Fernet.generate_key()
    vault = ChatLockVault(key)
    vault.configure("246810", "gizli-kod")
    mw = _MainWindowStub(key, vault)
    mw._chat_lock_unlocked = True

    assert not mw.chat_lock_navigation_visible()

    vault.set_hide_navigation(False)
    assert mw.chat_lock_navigation_visible()


def test_corrupt_vault_fingerprint_does_not_expose_navigation_row():
    key = Fernet.generate_key()
    mw = _MainWindowStub(key, vault=None)
    mw._chat_lock_fingerprints = {
        jid_fingerprint(key, "private@s.whatsapp.net")
    }

    assert not mw.chat_lock_navigation_visible()


def test_forgetting_deleted_chat_removes_phone_and_lid_vault_identities():
    key = Fernet.generate_key()
    phone = "1234567890@s.whatsapp.net"
    lid = "99887766@lid"
    vault = _vault_with_chat(key, phone)
    vault.lock_chat(lid)
    mw = _MainWindowStub(key, vault)
    mw._phone_to_lid = {phone: lid}
    mw._lid_to_phone = {lid: phone}

    mw._forget_chat_lock(phone)

    assert vault.locked_jids == set()
    assert mw.persist_calls == 1


def test_forgetting_deleted_chat_cleans_corruption_fallback_index():
    key = Fernet.generate_key()
    jid = "1234567890@s.whatsapp.net"
    other = "another@g.us"
    mw = _MainWindowStub(key, vault=None)
    mw._chat_lock_fingerprints = {
        jid_fingerprint(key, jid),
        jid_fingerprint(key, other),
    }

    mw._forget_chat_lock(jid)

    assert mw._chat_lock_fingerprints == {jid_fingerprint(key, other)}
    assert mw.db.json_writes == [
        (mw._CHAT_LOCK_INDEX_KEY, [jid_fingerprint(key, other)])
    ]
