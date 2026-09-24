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


# ── Alt+7 / show_locked_chats_panel and the Calls tab ───────────────────────
#
# The Calls tab (Alt+6, PR #292) landed after this vault branch, so nothing
# in it knew the vault existed. show_locked_chats_panel() (and therefore
# Alt+7, its keyboard shortcut) must hide calls_panel like it already hides
# every other top-level panel, or the Calls tab stays visible underneath.

class _Shown:
    def __init__(self):
        self.shown = False

    def Show(self):
        self.shown = True

    def Hide(self):
        self.shown = False


class _LockedPanel(_Shown):
    def __init__(self):
        super().__init__()
        self.hide_navigation = type("_CB", (), {"SetValue": lambda self, v: None})()

    def set_all_chats(self, chats, names):
        pass

    def restore_selection(self):
        pass


class _PanelStub(_MainWindowStub):
    show_locked_chats_panel = MainWindow.show_locked_chats_panel
    on_alt_7 = MainWindow.on_alt_7
    unlock_chat_lock_vault = MainWindow.unlock_chat_lock_vault

    def __init__(self, key, vault):
        super().__init__(key, vault)
        self._chat_lock_unlocked = True
        self._locked_chat_rows = ([], [])
        self.conversations_panel = _Shown()
        self.archived_conversations_panel = _Shown()
        self.status_panel = _Shown()
        self.calls_panel = _Shown()
        self.locked_conversations_panel = _LockedPanel()
        self.content_panel = type("_L", (), {"Layout": lambda self: None})()


def test_showing_the_locked_chats_panel_hides_the_calls_tab():
    key = Fernet.generate_key()
    vault = ChatLockVault(key)
    vault.configure("246810", "gizli-kod")
    mw = _PanelStub(key, vault)
    mw.calls_panel.shown = True

    mw.show_locked_chats_panel()

    assert mw.locked_conversations_panel.shown
    assert not mw.calls_panel.shown


def test_alt_7_opens_the_vault_and_hides_the_calls_tab():
    key = Fernet.generate_key()
    vault = ChatLockVault(key)
    vault.configure("246810", "gizli-kod")
    mw = _PanelStub(key, vault)
    mw.calls_panel.shown = True

    mw.on_alt_7(None)

    assert mw.locked_conversations_panel.shown
    assert not mw.calls_panel.shown


def test_alt_7_does_nothing_when_the_vault_was_never_configured():
    key = Fernet.generate_key()
    vault = ChatLockVault(key)
    mw = _PanelStub(key, vault)
    mw._chat_lock_unlocked = False
    mw.calls_panel.shown = True

    mw.on_alt_7(None)

    assert not mw.locked_conversations_panel.shown
    assert mw.calls_panel.shown
