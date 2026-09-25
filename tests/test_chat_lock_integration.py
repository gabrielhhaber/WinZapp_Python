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

    def set_all_chats(self, chats, names):
        pass

    def restore_selection(self):
        pass


class _PanelStub(_MainWindowStub):
    show_locked_chats_panel = MainWindow.show_locked_chats_panel
    on_alt_7 = MainWindow.on_alt_7
    unlock_chat_lock_vault = MainWindow.unlock_chat_lock_vault

    def touch_chat_lock_timeout(self):
        pass

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


def test_a_never_configured_vault_still_opens_an_empty_panel_without_a_pin():
    key = Fernet.generate_key()
    vault = ChatLockVault(key)
    mw = _PanelStub(key, vault)
    mw._chat_lock_unlocked = False
    mw.calls_panel.shown = True

    mw.on_alt_7(None)

    assert mw.locked_conversations_panel.shown
    assert not mw.calls_panel.shown
    assert mw._chat_lock_unlocked is False


def test_navigation_row_is_visible_while_the_vault_was_never_set_up():
    key = Fernet.generate_key()
    mw = _MainWindowStub(key, ChatLockVault(key))

    assert mw.chat_lock_navigation_visible()


class _SettingsUnlockStub:
    unlock_chat_lock_settings = MainWindow.unlock_chat_lock_settings

    def __init__(self, vault):
        self._chat_lock_vault = vault
        self._chat_lock_unlocked = False
        self.configure_result = True
        self.configured = 0
        self.unlock_calls = []
        self.refreshed = 0
        self.armed = 0

    def _chat_lock_error(self, key):
        raise AssertionError(key)

    def _configure_chat_lock_vault(self):
        self.configured += 1
        if self.configure_result:
            self._chat_lock_vault.configure("246810", "gizli-kod")
        return self.configure_result

    def unlock_chat_lock_vault(self, *, show_panel=True):
        self.unlock_calls.append(show_panel)
        return True

    def _refresh_chat_lock_navigation(self):
        self.refreshed += 1

    def _arm_chat_lock_timeout(self):
        self.armed += 1


def test_settings_can_configure_the_vault_without_opening_the_chat_list():
    stub = _SettingsUnlockStub(ChatLockVault(Fernet.generate_key()))

    assert stub.unlock_chat_lock_settings()

    assert stub.configured == 1
    assert stub._chat_lock_unlocked
    assert stub.refreshed == 1
    assert stub.armed == 1
    assert stub.unlock_calls == []


def test_settings_authentication_does_not_open_the_chat_list():
    vault = ChatLockVault(Fernet.generate_key())
    vault.configure("246810", "gizli-kod")
    stub = _SettingsUnlockStub(vault)

    assert stub.unlock_chat_lock_settings()

    assert stub.unlock_calls == [False]


# ── Ctrl+Shift+T: lock the focused conversation from either list ────────────

from ui.chat_lock import LockedConversationsPanel
from ui.conversations import ArchivedConversationsPanel, ConversationsPanel


class _LockCaller:
    def __init__(self):
        self.locked = []

    def lock_chat(self, jid):
        self.locked.append(jid)


class _ListPanelStub:
    _on_accel_lock_list = ConversationsPanel._on_accel_lock_list
    _on_accel_lock = ArchivedConversationsPanel._on_accel_lock

    def __init__(self, chat):
        self.main_window = _LockCaller()
        self._chat = chat

    def _selected_chat_from_list(self):
        return self._chat


def test_ctrl_shift_t_locks_the_focused_conversation():
    panel = _ListPanelStub({"remoteJid": "1234567890@s.whatsapp.net"})
    panel._on_accel_lock_list(None)
    assert panel.main_window.locked == ["1234567890@s.whatsapp.net"]


def test_ctrl_shift_t_locks_the_focused_archived_conversation():
    panel = _ListPanelStub({"remoteJid": "another@g.us"})
    panel._on_accel_lock(None)
    assert panel.main_window.locked == ["another@g.us"]


def test_ctrl_shift_t_with_nothing_focused_does_nothing():
    panel = _ListPanelStub(None)
    panel._on_accel_lock_list(None)
    panel._on_accel_lock(None)
    assert panel.main_window.locked == []


# ── The empty locked-chats list says so ─────────────────────────────────────

class _Field:
    def GetValue(self):
        return ""


class _ListCtrl:
    def __init__(self):
        self.rows = []
        self.focused = -1

    def GetFocusedItem(self):
        return self.focused

    def Freeze(self):
        pass

    def Thaw(self):
        pass

    def DeleteAllItems(self):
        self.rows = []

    def Append(self, row):
        self.rows.append(row[0])

    def Focus(self, index):
        self.focused = index

    def Select(self, index):
        pass


class _EmptyListMainWindow:
    class i18n:
        @staticmethod
        def t(key):
            return f"[{key}]"

    def _search_normalization_mode(self):
        return False

    def _last_msg_preview(self, chat):
        return ""


class _LockedPanelListStub:
    refresh = LockedConversationsPanel.refresh

    def __init__(self, chats=()):
        self.main_window = _EmptyListMainWindow()
        self.search_field = _Field()
        self.conversations_list = _ListCtrl()
        self._all_chats_list = list(chats)
        self._all_chat_names = ["Maria" for _ in chats]
        self.chats_list = []
        self.chat_names = []


def test_an_empty_locked_list_shows_the_no_locked_chats_notice():
    panel = _LockedPanelListStub()

    panel.refresh()

    assert panel.conversations_list.rows == ["[chat_lock_none]"]
    assert panel.conversations_list.focused == 0
    # The notice is not a chat: activating it must open nothing.
    assert panel.chats_list == []


def test_a_locked_chat_replaces_the_notice():
    panel = _LockedPanelListStub([{"remoteJid": "1@s.whatsapp.net"}])

    panel.refresh()

    assert "[chat_lock_none]" not in panel.conversations_list.rows
    assert len(panel.conversations_list.rows) == 1
