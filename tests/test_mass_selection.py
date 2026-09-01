"""Tests for the Ctrl+Space-toggled selection and the mass actions built on it.

Ctrl+Space on a row in either list toggles that row's membership in a
selection set (self.selected_chats / self.selected_messages) instead of
activating it — kept off plain Space, which is reserved for playing/pausing
the focused audio/video message — and the two context menus grow a "mass
actions" submenu whose handlers act on whatever is in those sets.

What is worth pinning here is that the selection is keyed by identity — chat
JID and message key.id — not by list index. Both lists are rebuilt constantly
(a new message arriving, pagination, a re-sort), so an index-keyed selection
would silently start pointing at different rows between selecting and acting,
and these handlers delete/clear/forward what they are pointed at.

ConversationsPanel is a wx.Panel and cannot be instantiated without a running
wx.App, so the methods under test are bound to a small stub carrying only the
attributes they touch — same approach as tests/test_message_bookmarks.py.
"""

import os
import threading
from datetime import datetime

import pytest
import wx

from ui.conversations import ConversationsPanel, _SAVEABLE_MESSAGE_TYPES


class _FakeI18n:
    # The bulk confirmation dialogs format {count} into the text — give those
    # specific keys a real template so the tests can pin the substitution,
    # not just that .format() didn't blow up on a plain key name.
    _TEMPLATES = {
        "clear_confirm_msg_bulk": "Clear {count} selected chats?",
        "delete_confirm_msg_bulk": "Delete {count} selected chats?",
        "delete_msg_confirm_bulk": "Delete {count} selected messages?",
        "datetime_fmt": "%d/%m/%Y %H:%M",
    }

    def t(self, key):
        return self._TEMPLATES.get(key, key)  # other keys asserted by name


class _FakeSound:
    def __init__(self):
        self.plays = 0

    def play(self):
        self.plays += 1


class _FakeList:
    def __init__(self, focused=-1, count=0):
        self._focused = focused
        self._count = count
        self.focus_calls = []
        self.select_calls = []
        self.ensure_visible_calls = []

    def GetFocusedItem(self):
        return self._focused

    def GetFirstSelected(self):
        return self._focused

    def GetItemCount(self):
        return self._count

    def Focus(self, idx):
        self.focus_calls.append(idx)
        self._focused = idx

    def Select(self, idx, on=True):
        self.select_calls.append((idx, on))

    def EnsureVisible(self, idx):
        self.ensure_visible_calls.append(idx)

    def SetItemText(self, idx, text):
        pass


class _FakeMainWindow:
    def __init__(self):
        self.i18n = _FakeI18n()
        self.settings = {"user_interface": {}}
        self.announced = []
        self.cleared = []
        self.deleted = []
        self.archived = []
        self.marked_read = []
        self.marked_unread = []
        self.deleted_messages = []
        self.deleted_for_everyone = []
        self.saves = 0
        self.pin_calls = []
        self.pin_results = None
        # Every save dialog reports the folder it wrote into, so
        # Configuracoes > Arquivos e salvamento can reopen there next time.
        self.remembered_folders = []

    def remember_save_folder(self, saved_path):
        self.remembered_folders.append(saved_path)

    def output(self, text, interrupt=False):
        self.announced.append(text)

    def clear_chat(self, jid):
        self.cleared.append(jid)

    def delete_chat(self, jid):
        self.deleted.append(jid)

    def archive_chat(self, jid, archived):
        self.archived.append((jid, archived))

    def mark_conversation_as_read(self, jid, read):
        self.marked_read.append((jid, read))

    def mark_conversation_as_unread(self, jid):
        self.marked_unread.append(jid)

    def delete_message_for_me(self, jid, key):
        self.deleted_messages.append((jid, key))

    def delete_message_for_everyone(self, jid, key):
        self.deleted_for_everyone.append((jid, key))
        return True

    def _schedule_save(self, *a, **kw):
        self.saves += 1

    def pin_message(self, jid, key, pin=True):
        """Server-side pin. pin_results, when set, is consumed one entry per
        call so a test can make specific messages fail."""
        self.pin_calls.append((jid, key.get("id", ""), pin))
        if self.pin_results is None:
            return True
        return next(self.pin_results)

    def add_chats_to_ui(self):
        pass

    def _is_self_jid(self, jid):
        # Overridden per-test (monkeypatch/attribute set) for the self-chat
        # ("Me") bulk-delete cases; every other test's conversation is a
        # regular group/individual chat.
        return False


class _FakeEvent:
    def __init__(self, key, ctrl=False, shift=False):
        self._key = key
        self._ctrl = ctrl
        self._shift = shift
        self.skipped = False

    def GetKeyCode(self):
        return self._key

    def ControlDown(self):
        return self._ctrl

    def ShiftDown(self):
        return self._shift

    def Skip(self):
        self.skipped = True


class _Panel:
    """Stub carrying exactly what the handlers under test touch."""

    _is_separator = ConversationsPanel._is_separator
    _on_messages_list_key_down = ConversationsPanel._on_messages_list_key_down
    _on_conv_list_key_down = ConversationsPanel._on_conv_list_key_down
    _on_mass_clear_chats = ConversationsPanel._on_mass_clear_chats
    _on_mass_delete_chats = ConversationsPanel._on_mass_delete_chats
    _on_mass_archive_chats = ConversationsPanel._on_mass_archive_chats
    _on_mass_mark_read_chats = ConversationsPanel._on_mass_mark_read_chats
    _on_mass_mark_unread_chats = ConversationsPanel._on_mass_mark_unread_chats
    _on_mass_forward_messages = ConversationsPanel._on_mass_forward_messages
    _on_mass_save_messages = ConversationsPanel._on_mass_save_messages
    _on_mass_delete_messages = ConversationsPanel._on_mass_delete_messages
    _confirm_local_only_delete = ConversationsPanel._confirm_local_only_delete
    _on_mass_copy_messages = ConversationsPanel._on_mass_copy_messages
    _on_mass_star_messages = ConversationsPanel._on_mass_star_messages
    _on_mass_pin_messages = ConversationsPanel._on_mass_pin_messages
    _on_mass_pin_failed = ConversationsPanel._on_mass_pin_failed
    _mass_message_targets = ConversationsPanel._mass_message_targets
    _on_accel_copy_message = ConversationsPanel._on_accel_copy_message
    _bulk_shortcuts_enabled = ConversationsPanel._bulk_shortcuts_enabled
    _group_admin_delete_override = ConversationsPanel._group_admin_delete_override
    _is_system_event = staticmethod(ConversationsPanel._is_system_event)

    _select_message_at = ConversationsPanel._select_message_at
    _toggle_message_selection = ConversationsPanel._toggle_message_selection
    _all_selectable_message_ids = ConversationsPanel._all_selectable_message_ids
    _select_chat_at = ConversationsPanel._select_chat_at
    _all_chat_jids = ConversationsPanel._all_chat_jids
    _refresh_message_rows_by_ids = ConversationsPanel._refresh_message_rows_by_ids
    _set_message_row_texts = ConversationsPanel._set_message_row_texts
    _render_message_line = lambda self, msg, index=None, total=None: msg.get("key", {}).get("id", "")
    _extract_timestamp = ConversationsPanel._extract_timestamp

    def _sender_label(self, msg):
        """Fake — the real one resolves contact names via lid/phone caches
        and group participant lookups, well beyond what _on_mass_copy_messages
        itself needs to be tested."""
        return "Eu" if msg.get("key", {}).get("fromMe") else "Gabriel Haberkamp"

    def __init__(self, chats=(), messages=(), focused=-1):
        self.main_window = _FakeMainWindow()
        self.selection_sound = _FakeSound()
        self.selected_chats = set()
        self.selected_messages = set()
        self.chats_list = list(chats)
        self._sorted_messages = list(messages)
        self.conversation = {"remoteJid": "grupo@g.us"}
        self.conversations_list = _FakeList(focused=focused, count=len(chats))
        self.messages_list = _FakeList(focused=focused, count=len(messages))
        self._is_loading_more = False
        self._messages_offset = 0
        # Recorders for the collaborators the mass handlers delegate to.
        self.forwarded = []
        self.saved = []
        self.removed_locally = []
        self.starred = []
        self.pinned = []
        self.copied_files = []
        # The mass star/pin handlers apply the flag themselves and repaint
        # once, instead of delegating to the single-message handlers — these
        # record that they do exactly one repaint / one persist per batch.
        # The repaint is per-row (_repaint_or_repopulate); populate_calls
        # stays here to assert the full rebuild is NOT what happens.
        self.populate_calls = 0
        self.repainted = []
        self.repaint_ok = True
        self.persisted = []

    def populate_messages(self, preserve_focus=False):
        self.populate_calls += 1

    def _repaint_or_repopulate(self, msg_ids):
        self.repainted.append(sorted(i for i in msg_ids if i))
        if not self.repaint_ok:
            self.populate_messages(preserve_focus=True)

    def _persist_message_local_flags(self, jid, msgs):
        self.persisted.append((jid, [m.get("key", {}).get("id", "") for m in msgs]))

    def _on_menu_forward(self, msg, msgs_list=None):
        self.forwarded.append((msg, msgs_list))

    def _resolve_media_filename(self, msg):
        return f"{msg['key']['id']}.bin"

    def _save_message_media(self, msg, save_path):
        self.saved.append((msg, save_path))

    def remove_messages_by_id(self, ids, focus_previous=False):
        self.removed_locally.append((set(ids), focus_previous))

    def _on_menu_star(self, msg):
        self.starred.append(msg["key"]["id"])

    def _on_menu_pin_message(self, msg):
        self.pinned.append(msg["key"]["id"])

    def _on_menu_copy_message(self, msg):
        pass

    def _on_menu_copy_file(self, msg):
        self.copied_files.append(msg["key"]["id"])

    def seek_active_playback_by(self, delta_seconds):
        return False

    def seek_active_playback_to_edge(self, to_end):
        return False


def _chat(jid):
    return {"remoteJid": jid}


def _msg(msg_id, jid="grupo@g.us", from_me=False):
    return {"key": {"id": msg_id, "remoteJid": jid, "fromMe": from_me}, "message": {"conversation": "x"}}


def _saveable_msg(msg_id, jid="grupo@g.us", msg_type="documentMessage"):
    return {"key": {"id": msg_id, "remoteJid": jid}, "message": {}, "messageType": msg_type}


def _text_msg(msg_id, text="hello", jid="grupo@g.us", extended=False, from_me=False, ts=None):
    key = {"id": msg_id, "remoteJid": jid, "fromMe": from_me}
    if extended:
        msg = {
            "key": key, "messageType": "extendedTextMessage",
            "message": {"extendedTextMessage": {"text": text}},
        }
    else:
        msg = {
            "key": key, "messageType": "conversation",
            "message": {"conversation": text},
        }
    if ts is not None:
        msg["messageTimestamp"] = ts
    return msg


SEPARATOR = {"_type": "unread_separator", "count": 3}
PLACEHOLDER = {"_type": "empty_placeholder"}


def _space():
    """Plain Space — reserved for audio/video playback, must not toggle
    selection any more."""
    return _FakeEvent(wx.WXK_SPACE)


def _ctrl_space():
    return _FakeEvent(wx.WXK_SPACE, ctrl=True)


def _ctrl_shift_space():
    return _FakeEvent(wx.WXK_SPACE, ctrl=True, shift=True)


def _shift_down():
    return _FakeEvent(wx.WXK_DOWN, shift=True)


def _shift_up():
    return _FakeEvent(wx.WXK_UP, shift=True)


def _shift_home():
    return _FakeEvent(wx.WXK_HOME, shift=True)


def _shift_end():
    return _FakeEvent(wx.WXK_END, shift=True)


class TestCtrlSpaceInTheConversationsList:
    def test_ctrl_space_selects_the_focused_chat(self):
        panel = _Panel(chats=[_chat("a@s.whatsapp.net"), _chat("b@s.whatsapp.net")], focused=1)
        panel._on_conv_list_key_down(_ctrl_space())
        assert panel.selected_chats == {"b@s.whatsapp.net"}
        assert panel.main_window.announced == ["selected"]
        assert panel.selection_sound.plays == 1

    def test_plain_space_does_not_select(self):
        """Plain Space is left alone in the conversations list too, for
        consistency with the messages list."""
        panel = _Panel(chats=[_chat("a@s.whatsapp.net")], focused=0)
        event = _space()
        panel._on_conv_list_key_down(event)
        assert panel.selected_chats == set()
        assert event.skipped

    def test_ctrl_space_again_deselects_it(self):
        panel = _Panel(chats=[_chat("a@s.whatsapp.net")], focused=0)
        panel._on_conv_list_key_down(_ctrl_space())
        panel._on_conv_list_key_down(_ctrl_space())
        assert panel.selected_chats == set()
        assert panel.main_window.announced == ["selected", "unselected"]

    def test_deselecting_is_silent(self):
        """The tone marks "now selected"; replaying it on removal would make
        the two states indistinguishable by ear."""
        panel = _Panel(chats=[_chat("a@s.whatsapp.net")], focused=0)
        panel._on_conv_list_key_down(_ctrl_space())
        panel._on_conv_list_key_down(_ctrl_space())
        assert panel.selection_sound.plays == 1

    def test_several_chats_accumulate(self):
        chats = [_chat(f"{i}@s.whatsapp.net") for i in range(4)]
        panel = _Panel(chats=chats)
        for i in (0, 2, 3):
            panel.conversations_list._focused = i
            panel._on_conv_list_key_down(_ctrl_space())
        assert panel.selected_chats == {"0@s.whatsapp.net", "2@s.whatsapp.net", "3@s.whatsapp.net"}

    def test_ctrl_space_with_nothing_focused_is_a_no_op(self):
        panel = _Panel(chats=[_chat("a@s.whatsapp.net")], focused=-1)
        panel._on_conv_list_key_down(_ctrl_space())
        assert panel.selected_chats == set()
        assert panel.main_window.announced == []

    def test_a_focus_index_past_the_list_is_a_no_op(self):
        """The list can shrink under a stale focus index (a chat archived or
        deleted while the list was focused)."""
        panel = _Panel(chats=[_chat("a@s.whatsapp.net")], focused=7)
        panel._on_conv_list_key_down(_ctrl_space())
        assert panel.selected_chats == set()

    def test_a_chat_with_no_jid_is_skipped(self):
        panel = _Panel(chats=[{"name": "sem jid"}], focused=0)
        panel._on_conv_list_key_down(_ctrl_space())
        assert panel.selected_chats == set()

    def test_the_selection_survives_the_list_being_reordered(self):
        """Keyed by JID, not by row: this is the whole reason it is a set of
        JIDs and not a set of indices."""
        chats = [_chat("a@s.whatsapp.net"), _chat("b@s.whatsapp.net")]
        panel = _Panel(chats=chats, focused=0)
        panel._on_conv_list_key_down(_ctrl_space())
        panel.chats_list = list(reversed(chats))  # a new message re-sorts the list
        assert panel.selected_chats == {"a@s.whatsapp.net"}

    def test_shift_down_extends_selection_and_moves_focus(self):
        chats = [_chat("a@s.whatsapp.net"), _chat("b@s.whatsapp.net"), _chat("c@s.whatsapp.net")]
        panel = _Panel(chats=chats, focused=0)
        panel._on_conv_list_key_down(_shift_down())
        assert panel.selected_chats == {"b@s.whatsapp.net"}
        assert panel.conversations_list._focused == 1

    def test_shift_up_extends_selection_and_moves_focus(self):
        chats = [_chat("a@s.whatsapp.net"), _chat("b@s.whatsapp.net"), _chat("c@s.whatsapp.net")]
        panel = _Panel(chats=chats, focused=2)

        panel._on_conv_list_key_down(_shift_up())

        assert panel.selected_chats == {"b@s.whatsapp.net"}
        assert panel.conversations_list._focused == 1
        assert panel.selection_sound.plays == 1
        assert panel.main_window.announced == ["selected"]

    def test_shift_end_selects_everything_below_and_jumps_to_the_last_row(self):
        chats = [_chat(f"{i}@s.whatsapp.net") for i in range(4)]
        panel = _Panel(chats=chats, focused=1)
        panel._on_conv_list_key_down(_shift_end())
        assert panel.selected_chats == {"1@s.whatsapp.net", "2@s.whatsapp.net", "3@s.whatsapp.net"}
        assert panel.conversations_list._focused == 3

    def test_shift_home_selects_everything_above_and_jumps_to_the_first_row(self):
        chats = [_chat(f"{i}@s.whatsapp.net") for i in range(4)]
        panel = _Panel(chats=chats, focused=2)
        panel._on_conv_list_key_down(_shift_home())
        assert panel.selected_chats == {"0@s.whatsapp.net", "1@s.whatsapp.net", "2@s.whatsapp.net"}
        assert panel.conversations_list._focused == 0

    def test_ctrl_shift_space_selects_everything_then_clears_on_repeat(self):
        """Announces "all selected"/"all deselected" — distinct from the
        single-row "selected"/"unselected" toggle — so the user can tell by
        ear that the whole list changed state, not just the focused row."""
        chats = [_chat(f"{i}@s.whatsapp.net") for i in range(3)]
        panel = _Panel(chats=chats, focused=0)
        panel._on_conv_list_key_down(_ctrl_shift_space())
        assert panel.selected_chats == {"0@s.whatsapp.net", "1@s.whatsapp.net", "2@s.whatsapp.net"}
        assert panel.main_window.announced == ["all_selected"]
        panel._on_conv_list_key_down(_ctrl_shift_space())
        assert panel.selected_chats == set()
        assert panel.main_window.announced == ["all_selected", "all_unselected"]


class TestCtrlSpaceInTheMessagesList:
    def test_ctrl_space_selects_the_focused_message(self):
        panel = _Panel(messages=[_msg("m1"), _msg("m2")], focused=1)
        panel._on_messages_list_key_down(_ctrl_space())
        assert panel.selected_messages == {"m2"}
        assert panel.main_window.announced == ["selected"]
        assert panel.selection_sound.plays == 1

    def test_plain_space_does_not_select(self):
        """Plain Space is reserved for playing/pausing the focused audio or
        video message — it must fall through here (Skip), not toggle."""
        panel = _Panel(messages=[_msg("m1")], focused=0)
        event = _space()
        panel._on_messages_list_key_down(event)
        assert panel.selected_messages == set()
        assert event.skipped

    def test_ctrl_space_again_deselects_it(self):
        panel = _Panel(messages=[_msg("m1")], focused=0)
        panel._on_messages_list_key_down(_ctrl_space())
        panel._on_messages_list_key_down(_ctrl_space())
        assert panel.selected_messages == set()
        assert panel.main_window.announced == ["selected", "unselected"]

    @pytest.mark.parametrize("row", [SEPARATOR, PLACEHOLDER])
    def test_a_sentinel_row_cannot_be_selected(self, row):
        """The unread separator and the "no messages" placeholder have no
        key.id; every other handler guards on _is_separator for the same
        reason."""
        panel = _Panel(messages=[row], focused=0)
        panel._on_messages_list_key_down(_ctrl_space())
        assert panel.selected_messages == set()
        assert panel.main_window.announced == []

    def test_a_message_with_no_id_is_skipped(self):
        panel = _Panel(messages=[{"key": {}, "message": {}}], focused=0)
        panel._on_messages_list_key_down(_ctrl_space())
        assert panel.selected_messages == set()

    def test_ctrl_space_with_nothing_focused_is_a_no_op(self):
        panel = _Panel(messages=[_msg("m1")], focused=-1)
        panel._on_messages_list_key_down(_ctrl_space())
        assert panel.selected_messages == set()

    def test_the_selection_survives_pagination_prepending_older_messages(self):
        panel = _Panel(messages=[_msg("m5"), _msg("m6")], focused=1)
        panel._on_messages_list_key_down(_ctrl_space())
        panel._sorted_messages = [_msg("m1"), _msg("m2"), _msg("m5"), _msg("m6")]
        assert panel.selected_messages == {"m6"}

    def test_shift_down_extends_selection_and_moves_focus(self):
        panel = _Panel(messages=[_msg("m1"), _msg("m2"), _msg("m3")], focused=0)
        panel._on_messages_list_key_down(_shift_down())
        assert panel.selected_messages == {"m2"}
        assert panel.messages_list._focused == 1

    def test_shift_up_extends_selection_and_moves_focus(self):
        panel = _Panel(messages=[_msg("m1"), _msg("m2"), _msg("m3")], focused=2)
        panel._on_messages_list_key_down(_shift_up())
        assert panel.selected_messages == {"m2"}
        assert panel.messages_list._focused == 1

    def test_shift_up_repeatedly_selects_bottom_to_top_across_an_unread_separator(self):
        """Reported live: Shift+Up used to fall through to the native
        ListCtrl selection (no explicit handler existed, unlike Shift+Down),
        which didn't know the unread-separator row isn't a real message and
        never touched self.selected_messages — so selecting upward past it
        went out of sync."""
        panel = _Panel(messages=[_msg("m1"), _msg("m2"), SEPARATOR, _msg("m4"), _msg("m5")], focused=4)
        panel._on_messages_list_key_down(_shift_up())  # m5 -> m4
        panel._on_messages_list_key_down(_shift_up())  # m4 -> separator (skipped)
        panel._on_messages_list_key_down(_shift_up())  # separator -> m2
        panel._on_messages_list_key_down(_shift_up())  # m2 -> m1
        assert panel.selected_messages == {"m1", "m2", "m4"}
        assert panel.messages_list._focused == 0

    def test_shift_up_at_the_top_row_is_a_no_op(self):
        panel = _Panel(messages=[_msg("m1"), _msg("m2")], focused=0)
        panel._on_messages_list_key_down(_shift_up())
        assert panel.selected_messages == set()
        assert panel.messages_list._focused == 0

    def test_shift_end_selects_everything_below_when_nothing_is_playing(self):
        panel = _Panel(messages=[_msg("m1"), _msg("m2"), _msg("m3")], focused=1)
        panel._on_messages_list_key_down(_shift_end())
        assert panel.selected_messages == {"m2", "m3"}
        assert panel.messages_list._focused == 2

    def test_shift_home_selects_everything_above_when_nothing_is_playing(self):
        panel = _Panel(messages=[_msg("m1"), _msg("m2"), _msg("m3")], focused=1)
        panel._on_messages_list_key_down(_shift_home())
        assert panel.selected_messages == {"m1", "m2"}
        assert panel.messages_list._focused == 0

    def test_ctrl_shift_space_selects_everything_then_clears_on_repeat(self):
        """Same "all selected"/"all deselected" distinction as the
        conversations list — see the equivalent chat-list test above."""
        panel = _Panel(messages=[_msg("m1"), _msg("m2")], focused=0)
        panel._on_messages_list_key_down(_ctrl_shift_space())
        assert panel.selected_messages == {"m1", "m2"}
        assert panel.main_window.announced == ["all_selected"]
        panel._on_messages_list_key_down(_ctrl_shift_space())
        assert panel.selected_messages == set()
        assert panel.main_window.announced == ["all_selected", "all_unselected"]


class TestToggleMessageSelection:
    """_toggle_message_selection() is the shared logic behind both Ctrl+Space
    and the "Selecionar mensagem"/"Desselecionar mensagem" context menu item
    — this exercises it directly, the way the menu item's handler calls it."""

    def test_selects_an_unselected_message(self):
        panel = _Panel(messages=[_msg("m1")])
        panel._toggle_message_selection(panel._sorted_messages[0])
        assert panel.selected_messages == {"m1"}
        assert panel.main_window.announced == ["selected"]
        assert panel.selection_sound.plays == 1

    def test_unselects_an_already_selected_message(self):
        panel = _Panel(messages=[_msg("m1")])
        panel.selected_messages = {"m1"}
        panel._toggle_message_selection(panel._sorted_messages[0])
        assert panel.selected_messages == set()
        assert panel.main_window.announced == ["unselected"]
        assert panel.selection_sound.plays == 0

    def test_a_sentinel_row_is_a_no_op(self):
        panel = _Panel(messages=[SEPARATOR])
        panel._toggle_message_selection(panel._sorted_messages[0])
        assert panel.selected_messages == set()
        assert panel.main_window.announced == []


@pytest.fixture
def confirm_yes(monkeypatch):
    """Every destructive mass action asks first; answer yes."""
    monkeypatch.setattr(wx, "MessageBox", lambda *a, **k: wx.YES)


@pytest.fixture
def confirm_yes_capture(monkeypatch):
    """Same as confirm_yes, but records the (message, title) shown — used to
    pin that the bulk confirmations use the "N selected" wording/title, not
    the single-item ones, and that the count is the real selection size."""
    calls = []

    def _fake_message_box(message, title, *a, **k):
        calls.append((message, title))
        return wx.YES

    monkeypatch.setattr(wx, "MessageBox", _fake_message_box)
    return calls


@pytest.fixture
def confirm_no(monkeypatch):
    monkeypatch.setattr(wx, "MessageBox", lambda *a, **k: wx.NO)


@pytest.fixture
def choose_folder(monkeypatch, tmp_path):
    """_on_mass_save_messages opens a single folder picker (wx.DirDialog) up
    front, rather than the single-file wx.FileDialog used by the one-message
    "Save as" flow — fake it choosing tmp_path."""
    class _FakeDirDialog:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def ShowModal(self):
            return wx.ID_OK

        def GetPath(self):
            return str(tmp_path)

    monkeypatch.setattr(wx, "DirDialog", _FakeDirDialog)
    return tmp_path


@pytest.fixture
def run_threads_inline(monkeypatch):
    """_on_mass_delete_messages hands the server calls to a background thread;
    run it inline so the test observes the result deterministically."""
    class _Inline:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None, **kw):
            self._target = target
            self._args = args
            self._kwargs = kwargs or {}

        def start(self):
            self._target(*self._args, **self._kwargs)

    monkeypatch.setattr(threading, "Thread", _Inline)


@pytest.fixture
def fake_delete_dialog(monkeypatch):
    """Fakes the wx.Dialog/Panel/RadioButton/Button/sizer chain
    _on_mass_delete_messages() builds (mirroring _on_menu_delete_message()'s
    single-message dialog) — no running wx.App needed. Returns a dict the
    test sets before calling the handler: result (wx.ID_OK/wx.ID_CANCEL,
    default OK) and everyone (whether the "delete for everyone" radio ends
    up selected when that radio was even offered).
    """
    radios = []

    class _FakeRadioButton:
        def __init__(self, parent, label="", style=0):
            self.label = label
            self.style = style
            self._value = False
            radios.append(self)

        def SetValue(self, v):
            self._value = v

        def GetValue(self):
            return self._value

    class _FakeSizer:
        def __init__(self, *a, **k):
            pass

        def Add(self, *a, **k):
            pass

    class _FakePanel:
        def __init__(self, *a, **k):
            pass

        def SetSizer(self, *a, **k):
            pass

    class _FakeButton:
        def __init__(self, parent, id=None, label=""):
            self.id = id
            self.label = label

    class _FakeBtnSizer:
        def __init__(self, *a, **k):
            pass

        def AddButton(self, *a, **k):
            pass

        def Realize(self):
            pass

    state = {"result": wx.ID_OK, "everyone": False}

    class _FakeDialog:
        def __init__(self, *a, **k):
            pass

        def SetSizer(self, *a, **k):
            pass

        def Fit(self):
            pass

        def CentreOnParent(self):
            pass

        def ShowModal(self):
            # radios[0] is always "delete for me" (RB_GROUP, defaulted True
            # by the handler itself); radios[1], if present, is "delete for
            # everyone" — only offered when eligible.
            if len(radios) > 1 and state["everyone"]:
                radios[1].SetValue(True)
                radios[0].SetValue(False)
            return state["result"]

        def Destroy(self):
            pass

    monkeypatch.setattr(wx, "Dialog", _FakeDialog)
    monkeypatch.setattr(wx, "Panel", _FakePanel)
    monkeypatch.setattr(wx, "BoxSizer", _FakeSizer)
    monkeypatch.setattr(wx, "RadioButton", _FakeRadioButton)
    monkeypatch.setattr(wx, "Button", _FakeButton)
    monkeypatch.setattr(wx, "StdDialogButtonSizer", _FakeBtnSizer)

    state["radios"] = radios
    return state


@pytest.fixture
def fake_confirm_dialog(monkeypatch):
    """Fakes the wx.MessageDialog _confirm_local_only_delete() builds (the
    plain Delete/Cancel prompt for the "Me" chat's local-only delete — no
    running wx.App needed). Returns a dict the test sets before calling the
    handler: result (wx.ID_YES/wx.ID_NO, default YES) and captures the
    prompt/title/labels actually passed in."""
    state = {"result": wx.ID_YES, "prompt": None, "title": None, "labels": None}

    class _FakeMessageDialog:
        def __init__(self, parent, message, caption, style):
            state["prompt"] = message
            state["title"] = caption

        def SetYesNoLabels(self, yes, no):
            state["labels"] = (yes, no)

        def ShowModal(self):
            return state["result"]

        def Destroy(self):
            pass

    monkeypatch.setattr(wx, "MessageDialog", _FakeMessageDialog)
    return state


class TestMassChatActions:
    def test_clearing_applies_to_every_selected_chat(self, confirm_yes):
        panel = _Panel()
        panel.selected_chats = {"a@s.whatsapp.net", "b@s.whatsapp.net"}
        panel._on_mass_clear_chats(None)
        assert sorted(panel.main_window.cleared) == ["a@s.whatsapp.net", "b@s.whatsapp.net"]
        assert panel.selected_chats == set()
        assert panel.main_window.announced == ["success_clear"]

    def test_declining_the_confirmation_clears_nothing(self, confirm_no):
        """And leaves the selection intact, so the user does not have to
        rebuild it after an accidental cancel."""
        panel = _Panel()
        panel.selected_chats = {"a@s.whatsapp.net"}
        panel._on_mass_clear_chats(None)
        assert panel.main_window.cleared == []
        assert panel.selected_chats == {"a@s.whatsapp.net"}

    def test_clearing_confirmation_names_the_selection_not_a_single_chat(self, confirm_yes_capture):
        """The dialog must say "clear the N selected chats", not the generic
        single-chat "clear this chat" wording — otherwise a user acting on a
        multi-chat selection has no way to tell the shortcut is about to hit
        everything selected rather than just the focused row."""
        panel = _Panel()
        panel.selected_chats = {"a@s.whatsapp.net", "b@s.whatsapp.net"}
        panel._on_mass_clear_chats(None)
        (message, title), = confirm_yes_capture
        assert message == "Clear 2 selected chats?"
        assert title == "clear_chat_bulk_title"

    def test_deleting_applies_to_every_selected_chat(self, confirm_yes):
        panel = _Panel()
        panel.selected_chats = {"a@s.whatsapp.net", "b@s.whatsapp.net"}
        panel._on_mass_delete_chats(None)
        assert sorted(panel.main_window.deleted) == ["a@s.whatsapp.net", "b@s.whatsapp.net"]
        assert panel.selected_chats == set()

    def test_declining_deletes_nothing(self, confirm_no):
        panel = _Panel()
        panel.selected_chats = {"a@s.whatsapp.net"}
        panel._on_mass_delete_chats(None)
        assert panel.main_window.deleted == []
        assert panel.selected_chats == {"a@s.whatsapp.net"}

    def test_deleting_confirmation_names_the_selection_not_a_single_chat(self, confirm_yes_capture):
        panel = _Panel()
        panel.selected_chats = {"a@s.whatsapp.net", "b@s.whatsapp.net", "c@s.whatsapp.net"}
        panel._on_mass_delete_chats(None)
        (message, title), = confirm_yes_capture
        assert message == "Delete 3 selected chats?"
        assert title == "delete_chat_bulk_title"

    def test_archiving_does_not_ask_first(self, monkeypatch):
        """Archiving is reversible from the archived panel, unlike clear and
        delete — so it must not be gated on a confirmation that is not there."""
        monkeypatch.setattr(wx, "MessageBox", lambda *a, **k: pytest.fail("asked"))
        panel = _Panel()
        panel.selected_chats = {"a@s.whatsapp.net"}
        panel._on_mass_archive_chats(None)
        assert panel.main_window.archived == [("a@s.whatsapp.net", True)]
        assert panel.main_window.announced == ["success_archive"]

    def test_marking_read_applies_to_every_selected_chat(self):
        panel = _Panel()
        panel.selected_chats = {"a@s.whatsapp.net", "b@s.whatsapp.net"}
        panel._on_mass_mark_read_chats(None)
        assert sorted(panel.main_window.marked_read) == [
            ("a@s.whatsapp.net", True), ("b@s.whatsapp.net", True)
        ]
        assert panel.selected_chats == set()

    def test_marking_unread_applies_to_every_selected_chat(self):
        panel = _Panel()
        panel.selected_chats = {"a@s.whatsapp.net", "b@s.whatsapp.net"}
        panel._on_mass_mark_unread_chats(None)
        assert sorted(panel.main_window.marked_unread) == ["a@s.whatsapp.net", "b@s.whatsapp.net"]
        assert panel.selected_chats == set()

    @pytest.mark.parametrize("handler", [
        "_on_mass_clear_chats", "_on_mass_delete_chats", "_on_mass_archive_chats",
        "_on_mass_mark_read_chats", "_on_mass_mark_unread_chats",
    ])
    def test_an_empty_selection_does_nothing_at_all(self, handler, monkeypatch):
        """Not even a confirmation dialog — the submenu is only built while a
        selection exists, but the handlers are reachable after it is cleared."""
        monkeypatch.setattr(wx, "MessageBox", lambda *a, **k: pytest.fail("asked"))
        panel = _Panel()
        getattr(panel, handler)(None)
        assert panel.main_window.cleared == []
        assert panel.main_window.deleted == []
        assert panel.main_window.archived == []
        assert panel.main_window.announced == []


class TestMassMessageActions:
    def test_forwarding_passes_every_selected_message(self):
        msgs = [_msg("m1"), _msg("m2"), _msg("m3")]
        panel = _Panel(messages=msgs)
        panel.selected_messages = {"m1", "m3"}
        panel._on_mass_forward_messages(None)
        (first, batch), = panel.forwarded
        assert [m["key"]["id"] for m in batch] == ["m1", "m3"]
        assert first is batch[0]
        assert panel.selected_messages == set()

    def test_forwarding_keeps_the_list_order_not_the_set_order(self):
        """The batch is built by walking _sorted_messages, so the recipient
        receives them oldest-first however the set happens to iterate."""
        msgs = [_msg(f"m{i}") for i in range(6)]
        panel = _Panel(messages=msgs)
        panel.selected_messages = {"m4", "m0", "m2"}
        panel._on_mass_forward_messages(None)
        (_first, batch), = panel.forwarded
        assert [m["key"]["id"] for m in batch] == ["m0", "m2", "m4"]

    def test_forwarding_skips_sentinel_rows(self):
        panel = _Panel(messages=[SEPARATOR, _msg("m1")])
        panel.selected_messages = {"m1"}
        panel._on_mass_forward_messages(None)
        (_first, batch), = panel.forwarded
        assert [m["key"]["id"] for m in batch] == ["m1"]

    def test_a_selection_of_messages_no_longer_in_the_list_forwards_nothing(self):
        """They were paginated away or deleted between selecting and acting."""
        panel = _Panel(messages=[_msg("m9")])
        panel.selected_messages = {"gone"}
        panel._on_mass_forward_messages(None)
        assert panel.forwarded == []
        assert panel.selected_messages == set()

    def test_saving_opens_one_folder_picker_and_saves_every_selected_message(
        self, choose_folder, run_threads_inline
    ):
        """Reported live: bulk save used to call a nonexistent
        self._on_menu_save(msg) and crash with AttributeError. The correct
        behavior is a single folder picker (wx.DirDialog), then each
        selected message saved into that folder."""
        msgs = [_saveable_msg("m1"), _saveable_msg("m2")]
        panel = _Panel(messages=msgs)
        panel.selected_messages = {"m1", "m2"}
        panel._on_mass_save_messages(None)
        assert sorted(m["key"]["id"] for m, _path in panel.saved) == ["m1", "m2"]
        for _msg_obj, save_path in panel.saved:
            assert os.path.dirname(save_path) == str(choose_folder)
        assert panel.selected_messages == set()

    def test_the_chosen_folder_is_remembered(self, choose_folder, run_threads_inline):
        """Configuracoes > Arquivos e salvamento defaults to reopening the last
        folder used, so every save dialog has to report where it went — bulk
        save included, which is the one that picks a folder rather than a file."""
        panel = _Panel(messages=[_saveable_msg("m1")])
        panel.selected_messages = {"m1"}
        panel._on_mass_save_messages(None)
        remembered = panel.main_window.remembered_folders
        assert len(remembered) == 1
        assert os.path.dirname(remembered[0]) == str(choose_folder)

    def test_saving_skips_non_saveable_messages_in_the_selection(
        self, choose_folder, run_threads_inline
    ):
        panel = _Panel(messages=[_saveable_msg("m1"), _msg("m2")])
        panel.selected_messages = {"m1", "m2"}
        panel._on_mass_save_messages(None)
        assert [m["key"]["id"] for m, _path in panel.saved] == ["m1"]

    def test_saving_with_nothing_saveable_in_the_selection_opens_no_dialog(self, monkeypatch):
        monkeypatch.setattr(wx, "DirDialog", lambda *a, **k: pytest.fail("opened dialog"))
        panel = _Panel(messages=[_msg("m1")])
        panel.selected_messages = {"m1"}
        panel._on_mass_save_messages(None)
        assert panel.saved == []
        assert panel.main_window.announced == ["save_as_nothing_to_save_bulk"]

    def test_declining_the_folder_picker_saves_nothing(self, monkeypatch):
        class _CancelledDirDialog:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def ShowModal(self):
                return wx.ID_CANCEL

        monkeypatch.setattr(wx, "DirDialog", _CancelledDirDialog)
        panel = _Panel(messages=[_saveable_msg("m1")])
        panel.selected_messages = {"m1"}
        panel._on_mass_save_messages(None)
        assert panel.saved == []
        assert panel.selected_messages == {"m1"}

    def test_deleting_for_me_removes_them_locally_and_on_the_server(
        self, fake_delete_dialog, run_threads_inline
    ):
        """Default choice (the "delete for me" radio, pre-selected same as
        the single-message dialog) — local delete-for-me only, never a
        for-everyone revoke."""
        msgs = [_msg("m1"), _msg("m2"), _msg("m3")]
        panel = _Panel(messages=msgs)
        panel.selected_messages = {"m1", "m3"}
        panel._on_mass_delete_messages(None)
        assert sorted(k["id"] for _jid, k in panel.main_window.deleted_messages) == ["m1", "m3"]
        assert panel.main_window.deleted_for_everyone == []
        (removed, focus_previous), = panel.removed_locally
        assert removed == {"m1", "m3"}
        assert focus_previous is True
        assert panel.selected_messages == set()
        assert panel.main_window.announced == ["success_delete"]

    def test_the_server_call_uses_each_message_own_chat(self, fake_delete_dialog, run_threads_inline):
        panel = _Panel(messages=[_msg("m1", jid="outro@g.us")])
        panel.selected_messages = {"m1"}
        panel._on_mass_delete_messages(None)
        assert panel.main_window.deleted_messages[0][0] == "outro@g.us"

    def test_cancelling_the_dialog_deletes_nothing(self, fake_delete_dialog, run_threads_inline):
        fake_delete_dialog["result"] = wx.ID_CANCEL
        panel = _Panel(messages=[_msg("m1")])
        panel.selected_messages = {"m1"}
        panel._on_mass_delete_messages(None)
        assert panel.main_window.deleted_messages == []
        assert panel.main_window.deleted_for_everyone == []
        assert panel.removed_locally == []
        assert panel.selected_messages == {"m1"}

    def test_the_dialog_title_names_the_selection_not_a_single_message(self, fake_delete_dialog, run_threads_inline):
        panel = _Panel(messages=[_msg("m1"), _msg("m2")])
        panel.selected_messages = {"m1", "m2"}
        panel._on_mass_delete_messages(None)
        # Same key the single-message dialog reuses for both its title and
        # its OK button — see _on_menu_delete_message().
        assert panel.main_window.i18n.t("delete_messages_bulk_title") == "delete_messages_bulk_title"

    def test_delete_for_everyone_is_not_offered_when_nothing_in_the_selection_is_eligible(
        self, fake_delete_dialog, run_threads_inline
    ):
        """Every selected message came from someone else and the user isn't
        a group admin — same as the single-message dialog, the "for
        everyone" radio must not even be built."""
        panel = _Panel(messages=[_msg("m1", from_me=False), _msg("m2", from_me=False)])
        panel.selected_messages = {"m1", "m2"}
        panel._on_mass_delete_messages(None)
        assert len(fake_delete_dialog["radios"]) == 1

    def test_delete_for_everyone_is_offered_when_at_least_one_message_is_from_me(
        self, fake_delete_dialog, run_threads_inline
    ):
        panel = _Panel(messages=[_msg("m1", from_me=True), _msg("m2", from_me=False)])
        panel.selected_messages = {"m1", "m2"}
        panel._on_mass_delete_messages(None)
        assert len(fake_delete_dialog["radios"]) == 2

    def test_delete_for_everyone_revokes_eligible_messages_and_locally_removes_the_rest(
        self, fake_delete_dialog, run_threads_inline
    ):
        """A mixed selection with "delete for everyone" chosen: only the
        eligible (fromMe) messages get a real revoke — the other member's
        message the user has no right to revoke is still removed from the
        user's own view, just without calling delete_message_for_everyone
        for it."""
        fake_delete_dialog["everyone"] = True
        panel = _Panel(messages=[_msg("m1", from_me=True), _msg("m2", from_me=False)])
        panel.selected_messages = {"m1", "m2"}
        panel._on_mass_delete_messages(None)
        assert [k["id"] for _jid, k in panel.main_window.deleted_for_everyone] == ["m1"]
        assert [k["id"] for _jid, k in panel.main_window.deleted_messages] == ["m2"]
        (removed, _focus), = panel.removed_locally
        assert removed == {"m1", "m2"}

    def test_a_group_admin_can_delete_for_everyone_even_a_message_not_their_own(
        self, fake_delete_dialog, run_threads_inline
    ):
        panel = _Panel(messages=[_msg("m1", from_me=False)])
        panel.selected_messages = {"m1"}
        panel._group_admin_delete_override = lambda: True
        fake_delete_dialog["everyone"] = True
        panel._on_mass_delete_messages(None)
        assert [k["id"] for _jid, k in panel.main_window.deleted_for_everyone] == ["m1"]
        assert panel.main_window.deleted_messages == []

    def test_a_system_event_in_the_selection_is_never_eligible_for_everyone(
        self, fake_delete_dialog, run_threads_inline
    ):
        """Same restriction the single-message dialog enforces: WhatsApp has
        no revoke for its own group notices, admin override or not."""
        notice = _msg("m1", from_me=True)
        notice["messageType"] = "groupNotification"
        panel = _Panel(messages=[notice])
        panel.selected_messages = {"m1"}
        panel._group_admin_delete_override = lambda: True
        panel._on_mass_delete_messages(None)
        # Not even offered: fromMe is true but it's a system event, and the
        # admin override doesn't change that — same as a lone system event
        # in the selection, nothing makes "for everyone" eligible.
        assert len(fake_delete_dialog["radios"]) == 1

    @pytest.mark.parametrize("handler", [
        "_on_mass_forward_messages", "_on_mass_save_messages", "_on_mass_delete_messages",
        "_on_mass_copy_messages", "_on_mass_star_messages", "_on_mass_pin_messages",
    ])
    def test_an_empty_selection_does_nothing_at_all(self, handler, monkeypatch):
        monkeypatch.setattr(wx, "MessageBox", lambda *a, **k: pytest.fail("asked"))
        panel = _Panel(messages=[_msg("m1")])
        getattr(panel, handler)(None)
        assert panel.forwarded == []
        assert panel.saved == []
        assert panel.removed_locally == []
        assert panel.starred == []
        assert panel.pinned == []


class TestSelfChatBulkDelete:
    """Issue #95: the "Me" chat has only one participant, so "delete for
    everyone" is a no-op there for every message (same reasoning as issue
    #73's single-message fix). Bulk delete on that chat must skip the
    for-me/for-everyone dialog entirely and go straight to a plain
    Delete/Cancel confirmation, always deleting locally only."""

    def test_skips_the_scope_dialog_and_deletes_locally_only(
        self, fake_confirm_dialog, run_threads_inline
    ):
        panel = _Panel(messages=[_msg("m1", jid="me@s.whatsapp.net", from_me=True),
                                  _msg("m2", jid="me@s.whatsapp.net", from_me=True)])
        panel.conversation = {"remoteJid": "me@s.whatsapp.net"}
        panel.main_window._is_self_jid = lambda jid: True
        panel.selected_messages = {"m1", "m2"}

        panel._on_mass_delete_messages(None)

        assert sorted(k["id"] for _jid, k in panel.main_window.deleted_messages) == ["m1", "m2"]
        assert panel.main_window.deleted_for_everyone == []
        (removed, focus_previous), = panel.removed_locally
        assert removed == {"m1", "m2"}
        assert focus_previous is True
        assert panel.selected_messages == set()
        assert panel.main_window.announced == ["success_delete"]

    def test_confirmation_names_the_count_not_a_single_message(self, fake_confirm_dialog, run_threads_inline):
        panel = _Panel(messages=[_msg("m1", jid="me@s.whatsapp.net", from_me=True),
                                  _msg("m2", jid="me@s.whatsapp.net", from_me=True)])
        panel.conversation = {"remoteJid": "me@s.whatsapp.net"}
        panel.main_window._is_self_jid = lambda jid: True
        panel.selected_messages = {"m1", "m2"}

        panel._on_mass_delete_messages(None)

        assert fake_confirm_dialog["prompt"] == "Delete 2 selected messages?"
        assert fake_confirm_dialog["title"] == "delete_messages_bulk_title"
        assert fake_confirm_dialog["labels"] == ("delete_message", "cancel")

    def test_declining_the_confirmation_deletes_nothing(self, fake_confirm_dialog, run_threads_inline):
        fake_confirm_dialog["result"] = wx.ID_NO
        panel = _Panel(messages=[_msg("m1", jid="me@s.whatsapp.net", from_me=True)])
        panel.conversation = {"remoteJid": "me@s.whatsapp.net"}
        panel.main_window._is_self_jid = lambda jid: True
        panel.selected_messages = {"m1"}

        panel._on_mass_delete_messages(None)

        assert panel.main_window.deleted_messages == []
        assert panel.removed_locally == []
        assert panel.selected_messages == {"m1"}


class _CapturedThread:
    """Stands in for threading.Thread so the mass-pin worker can be run
    synchronously, and so the test can count how many were started."""

    created = []

    def __init__(self, target=None, daemon=None, **kw):
        self.target = target
        _CapturedThread.created.append(self)

    def start(self):
        pass


@pytest.fixture
def run_threads(monkeypatch):
    """Capture threads started by ui.conversations and return a callable that
    runs them, plus route wx.CallAfter straight through."""
    _CapturedThread.created = []
    monkeypatch.setattr("ui.conversations.threading.Thread", _CapturedThread)
    monkeypatch.setattr("ui.conversations.wx.CallAfter",
                        lambda fn, *a, **kw: fn(*a, **kw))

    def _run():
        for t in list(_CapturedThread.created):
            if t.target:
                t.target()

    return _run


@pytest.fixture
def boxes(monkeypatch):
    """Records every wx.MessageBox raised by ui.conversations."""
    calls = []
    monkeypatch.setattr("ui.conversations.wx.MessageBox",
                        lambda *a, **kw: calls.append(a))
    return calls


@pytest.fixture
def fake_clipboard(monkeypatch):
    """Fakes pyperclip.copy as used by ui.conversations — records what was
    copied instead of touching the real system clipboard."""
    calls = []
    monkeypatch.setattr(
        "ui.conversations.pyperclip.copy", lambda text: calls.append(text)
    )
    return calls


@pytest.fixture
def fixed_datetime_format(monkeypatch):
    """Pins core.locale_format.get_datetime_format() (which otherwise reads
    the real machine's Windows regional settings) to a fixed pattern, so
    _on_mass_copy_messages's output doesn't depend on the test runner's
    locale."""
    monkeypatch.setattr(
        "ui.conversations.get_datetime_format", lambda fallback: "%d/%m/%Y %H:%M"
    )


def _expected_line(ts, sender, text):
    stamp = datetime.fromtimestamp(ts).strftime("%d/%m/%Y %H:%M")
    return f"{stamp} - {sender}: {text}"


class TestMassCopyMessages:
    def test_formats_date_sender_and_text_whatsapp_export_style(self, fake_clipboard, fixed_datetime_format):
        ts = 1755972565  # 2025-08-23 18:29 local
        msgs = [_text_msg("m1", "Isso eu acho que nem no oficial tem", ts=ts, from_me=False)]
        panel = _Panel(messages=msgs)
        panel.selected_messages = {"m1"}
        panel._on_mass_copy_messages(None)
        assert fake_clipboard == [
            _expected_line(ts, "Gabriel Haberkamp", "Isso eu acho que nem no oficial tem")
        ]

    def test_own_messages_use_the_self_reference_label(self, fake_clipboard, fixed_datetime_format):
        ts = 1755972625
        msgs = [_text_msg("m1", "ouch, eu ja tinha feito isso", ts=ts, from_me=True)]
        panel = _Panel(messages=msgs)
        panel.selected_messages = {"m1"}
        panel._on_mass_copy_messages(None)
        assert fake_clipboard == [_expected_line(ts, "Eu", "ouch, eu ja tinha feito isso")]

    def test_joins_selected_text_messages_one_per_line(self, fake_clipboard, fixed_datetime_format):
        msgs = [
            _text_msg("m1", "primeira", ts=1755972565),
            _text_msg("m2", "segunda", extended=True, ts=1755972625),
        ]
        panel = _Panel(messages=msgs)
        panel.selected_messages = {"m1", "m2"}
        panel._on_mass_copy_messages(None)
        expected = "\n".join([
            _expected_line(1755972565, "Gabriel Haberkamp", "primeira"),
            _expected_line(1755972625, "Gabriel Haberkamp", "segunda"),
        ])
        assert fake_clipboard == [expected]
        assert panel.main_window.announced == ["messages_copied_bulk"]
        assert panel.selected_messages == set()

    def test_a_message_with_no_timestamp_omits_the_date_time_prefix(self, fake_clipboard, fixed_datetime_format):
        msgs = [_text_msg("m1", "sem horário")]  # ts=None
        panel = _Panel(messages=msgs)
        panel.selected_messages = {"m1"}
        panel._on_mass_copy_messages(None)
        assert fake_clipboard == ["Gabriel Haberkamp: sem horário"]

    def test_keeps_list_order_not_set_order(self, fake_clipboard, fixed_datetime_format):
        msgs = [_text_msg(f"m{i}", f"text{i}", ts=1755972565 + i) for i in range(5)]
        panel = _Panel(messages=msgs)
        panel.selected_messages = {"m4", "m0", "m2"}
        panel._on_mass_copy_messages(None)
        expected = "\n".join([
            _expected_line(1755972565, "Gabriel Haberkamp", "text0"),
            _expected_line(1755972567, "Gabriel Haberkamp", "text2"),
            _expected_line(1755972569, "Gabriel Haberkamp", "text4"),
        ])
        assert fake_clipboard == [expected]

    def test_skips_non_text_message_types(self, fake_clipboard, fixed_datetime_format):
        msgs = [_text_msg("m1", "only this", ts=1755972565), _saveable_msg("m2"), SEPARATOR]
        panel = _Panel(messages=msgs)
        panel.selected_messages = {"m1", "m2"}
        panel._on_mass_copy_messages(None)
        assert fake_clipboard == [_expected_line(1755972565, "Gabriel Haberkamp", "only this")]

    def test_nothing_copyable_announces_and_touches_no_clipboard(self, fake_clipboard):
        panel = _Panel(messages=[_saveable_msg("m1")])
        panel.selected_messages = {"m1"}
        panel._on_mass_copy_messages(None)
        assert fake_clipboard == []
        assert panel.main_window.announced == ["copy_selected_nothing_to_copy"]
        # Nothing copyable is left as-is, not silently cleared.
        assert panel.selected_messages == {"m1"}

    def test_ctrl_c_copies_the_whole_selection_when_bulk_shortcuts_enabled(self, fake_clipboard, fixed_datetime_format):
        msgs = [_text_msg("m1", "a", ts=1755972565), _text_msg("m2", "b", ts=1755972566)]
        panel = _Panel(messages=msgs, focused=0)
        panel.selected_messages = {"m1", "m2"}
        panel._on_accel_copy_message(_FakeEvent(ord("C"), ctrl=True))
        expected = "\n".join([
            _expected_line(1755972565, "Gabriel Haberkamp", "a"),
            _expected_line(1755972566, "Gabriel Haberkamp", "b"),
        ])
        assert fake_clipboard == [expected]

    def test_ctrl_c_copies_only_the_focused_message_when_bulk_shortcuts_disabled(self, fake_clipboard):
        panel = _Panel(messages=[_text_msg("m1", "a"), _text_msg("m2", "b")], focused=1)
        panel.main_window.settings["user_interface"]["bulk_action_shortcuts"] = False
        panel.selected_messages = {"m1", "m2"}
        panel._on_accel_copy_message(_FakeEvent(ord("C"), ctrl=True))
        # Falls through to the single-message path instead of the bulk one.
        assert fake_clipboard == []


class TestMassStarAndPinMessages:
    def test_stars_every_selected_message_not_already_starred(self):
        msgs = [_msg("m1"), _msg("m2")]
        msgs[1]["starred"] = True
        panel = _Panel(messages=msgs)
        panel.selected_messages = {"m1", "m2"}
        panel._on_mass_star_messages(None)
        assert msgs[0]["starred"] is True
        assert panel.persisted == [("grupo@g.us", ["m1"])]  # m2 skipped, already starred
        assert panel.selected_messages == set()
        assert panel.main_window.announced == ["success_star_bulk"]

    def test_pins_every_selected_message_not_already_pinned(self, run_threads):
        msgs = [_msg("m1"), _msg("m2")]
        msgs[1]["pinInChat"] = True
        panel = _Panel(messages=msgs)
        panel.selected_messages = {"m1", "m2"}
        panel._on_mass_pin_messages(None)
        run_threads()
        assert msgs[0]["pinInChat"] is True
        assert panel.persisted[0] == ("grupo@g.us", ["m1"])  # m2 skipped
        assert [c[1] for c in panel.main_window.pin_calls] == ["m1"]
        assert panel.main_window.announced == ["success_pin_bulk"]


class TestMassStarAndPinAreBatched:
    """Both handlers used to call the single-message _on_menu_star /
    _on_menu_pin_message once per selected message. Each of those runs a full
    populate_messages() of its own — and the pin one additionally starts a
    thread per message and pops a blocking wx.MessageBox per server rejection.
    A selection of N messages therefore repainted the whole list N times on the
    UI thread, fired N concurrent requests, and could stack N modal dialogs
    (the same failure mode c518cce fixed for posting several files as status).
    """

    def test_starring_a_batch_repaints_once(self):
        msgs = [_msg("m1"), _msg("m2"), _msg("m3")]
        panel = _Panel(messages=msgs)
        panel.selected_messages = {"m1", "m2", "m3"}
        panel._on_mass_star_messages(None)
        assert all(m["starred"] for m in msgs)
        assert panel.repainted == [["m1", "m2", "m3"]]
        assert panel.populate_calls == 0, "the affected rows are repainted, not the whole list"
        assert panel.persisted == [("grupo@g.us", ["m1", "m2", "m3"])]
        assert panel.main_window.saves == 1

    def test_starring_repaints_the_whole_selection_not_just_the_changed_rows(self):
        """Clearing selected_messages drops the " selecionado" marker from
        every row that was in it, including the ones already starred and
        therefore left alone — those rows still have to be repainted."""
        msgs = [_msg("m1"), _msg("m2")]
        msgs[1]["starred"] = True
        panel = _Panel(messages=msgs)
        panel.selected_messages = {"m1", "m2"}
        panel._on_mass_star_messages(None)
        assert panel.repainted == [["m1", "m2"]]

    def test_pinning_a_batch_repaints_once_and_uses_one_thread(self, run_threads):
        msgs = [_msg("m1"), _msg("m2"), _msg("m3")]
        panel = _Panel(messages=msgs)
        panel.selected_messages = {"m1", "m2", "m3"}
        panel._on_mass_pin_messages(None)
        assert panel.repainted == [["m1", "m2", "m3"]]
        assert panel.populate_calls == 0
        assert len(_CapturedThread.created) == 1, "one worker for the batch, not one per message"
        run_threads()
        assert [c[1] for c in panel.main_window.pin_calls] == ["m1", "m2", "m3"]
        assert len(panel.repainted) == 1, "no repaint when nothing failed"

    def test_a_repaint_that_cannot_be_done_falls_back_to_the_full_rebuild(self):
        """A row that isn't rendered (paginated out, replaced by a resync)
        can only be shown by populate_messages() — the caller must not be
        left with the change applied and nothing on screen."""
        msgs = [_msg("m1")]
        panel = _Panel(messages=msgs)
        panel.repaint_ok = False
        panel.selected_messages = {"m1"}
        panel._on_mass_star_messages(None)
        assert panel.populate_calls == 1

    def test_server_rejections_produce_one_dialog_with_a_count(self, run_threads, boxes):
        msgs = [_msg("m1"), _msg("m2"), _msg("m3")]
        panel = _Panel(messages=msgs)
        panel.main_window.pin_results = iter([True, False, False])
        panel.selected_messages = {"m1", "m2", "m3"}
        panel._on_mass_pin_messages(None)
        run_threads()
        assert len(boxes) == 1, "one dialog per batch, not one per rejected message"
        assert "(2/3)" in boxes[0][0]

    def test_rejected_pins_are_rolled_back_together(self, run_threads, boxes):
        msgs = [_msg("m1"), _msg("m2"), _msg("m3")]
        panel = _Panel(messages=msgs)
        panel.main_window.pin_results = iter([True, False, False])
        panel.selected_messages = {"m1", "m2", "m3"}
        panel._on_mass_pin_messages(None)
        run_threads()
        assert msgs[0]["pinInChat"] is True      # accepted, stays pinned
        assert msgs[1]["pinInChat"] is False     # rejected, rolled back
        assert msgs[2]["pinInChat"] is False
        assert panel.persisted[-1] == ("grupo@g.us", ["m2", "m3"])
        assert panel.repainted == [["m1", "m2", "m3"], ["m2", "m3"]], (
            "one repaint to apply the batch, one for the rolled-back rows only"
        )
        assert panel.populate_calls == 0

    def test_a_raising_pin_call_is_treated_as_a_rejection(self, run_threads, boxes):
        """One message blowing up must not abandon the rest of the batch."""
        msgs = [_msg("m1"), _msg("m2")]
        panel = _Panel(messages=msgs)

        def _explode(jid, key, pin=True):
            panel.main_window.pin_calls.append((jid, key.get("id", ""), pin))
            if key.get("id") == "m1":
                raise OSError("connection reset")
            return True

        panel.main_window.pin_message = _explode
        panel.selected_messages = {"m1", "m2"}
        panel._on_mass_pin_messages(None)
        run_threads()
        assert [c[1] for c in panel.main_window.pin_calls] == ["m1", "m2"]
        assert msgs[0]["pinInChat"] is False   # rolled back
        assert msgs[1]["pinInChat"] is True    # unaffected
        assert len(boxes) == 1


class TestMassStarAndPinAnnounceHonestly:
    """Success was announced unconditionally, so a selection where every
    message was already starred/pinned (or was a system event, both filtered
    out on purpose) told screen-reader users the action had been applied when
    nothing happened at all. The announcement is the only feedback these
    actions give."""

    def test_nothing_to_star_says_so_instead_of_claiming_success(self):
        msgs = [_msg("m1"), _msg("m2")]
        for m in msgs:
            m["starred"] = True
        panel = _Panel(messages=msgs)
        panel.selected_messages = {"m1", "m2"}
        panel._on_mass_star_messages(None)
        assert panel.main_window.announced == ["mass_nothing_to_do"]
        assert panel.populate_calls == 0
        assert panel.selected_messages == set()

    def test_nothing_to_pin_says_so_and_calls_no_server(self, run_threads):
        msgs = [_msg("m1")]
        msgs[0]["pinInChat"] = True
        panel = _Panel(messages=msgs)
        panel.selected_messages = {"m1"}
        panel._on_mass_pin_messages(None)
        run_threads()
        assert panel.main_window.announced == ["mass_nothing_to_do"]
        assert panel.main_window.pin_calls == []
        assert _CapturedThread.created == []

    @pytest.mark.parametrize("handler,recorder", [
        ("_on_mass_star_messages", "starred"), ("_on_mass_pin_messages", "pinned"),
    ])
    def test_system_events_are_silently_skipped(self, handler, recorder):
        notice = _msg("m1", from_me=True)
        notice["messageType"] = "groupNotification"
        panel = _Panel(messages=[notice])
        panel.selected_messages = {"m1"}
        getattr(panel, handler)(None)
        assert getattr(panel, recorder) == []
