"""The temporary "X is typing..." last row of the messages list.

The row lives only in the control, one past ``_sorted_messages``
(client/ui/conversation_panel/typing_row.py). What has to hold:

- it is the LAST row and never a message: _sorted_messages, the index ->
  message mapping and every count compared against it stay untouched;
- adding it never moves focus/selection and never speaks;
- it goes away when presence stops or expires, when the typer's message
  arrives (that message lands above it), and when the conversation changes;
- a new message row is inserted above it, in every control flavour.

ConversationsPanel is a wx.Panel, so the module functions run against a plain
stub panel carrying a fake list that models how the native controls move
focus on insert/delete: the classic ListCtrl keeps the focused item and loses
focus when that item is deleted, the list boxes (CompatListBoxMessagesCtrl and
the Mac MacListCtrl) move the selection to the row above.
"""

import pytest
import wx

from main import MainWindow
from ui.conversations import ConversationsPanel
from ui.conversation_panel.typing_row import (
    append_message_row,
    dismiss_typing_row_for_message,
    message_row_count,
    sync_typing_row,
    typing_row_enabled,
    typing_row_shown,
    typing_row_text,
)

CONTACT = "5511999999999@s.whatsapp.net"
GROUP = "120363000000000000@g.us"
MARIA = "5511911111111@s.whatsapp.net"
JOAO = "5511922222222@s.whatsapp.net"

STRINGS = {
    "typing_text": "{name} is typing...",
    "recording_text": "{name} is recording audio...",
}
NAMES = {CONTACT: "Ana", MARIA: "Maria", JOAO: "João"}


class _I18n:
    def t(self, key):
        return STRINGS.get(key, key)


class _FakeList:
    """Rows plus the focused row, moved the way the native controls move it."""

    def __init__(self, rows, focused=-1, listbox=False):
        self.rows = list(rows)
        self.focused = focused
        self.listbox = listbox
        self.log = []
        self.frozen = 0

    def Freeze(self):
        self.frozen += 1

    def Thaw(self):
        self.frozen -= 1

    def GetItemCount(self):
        return len(self.rows)

    def GetItemText(self, index, col=0):
        return self.rows[index]

    def SetItemText(self, index, text):
        self.log.append(("set", index, text))
        self.rows[index] = text

    def InsertItem(self, index, text):
        self.log.append(("insert", index, text))
        self.rows.insert(index, text)
        if self.focused >= 0 and index <= self.focused:
            self.focused += 1

    def Append(self, entry):
        self.log.append(("append", entry[0]))
        self.rows.append(entry[0])

    def DeleteItem(self, index):
        self.log.append(("delete", index))
        del self.rows[index]
        if self.focused == index:
            self.focused = max(0, index - 1) if (self.listbox and self.rows) else -1
        elif self.focused > index:
            self.focused -= 1

    def DeleteAllItems(self):
        self.log.append(("delete_all",))
        self.rows.clear()
        self.focused = -1

    def GetFocusedItem(self):
        return self.focused

    def Focus(self, index):
        self.log.append(("focus", index))
        self.focused = index

    def Select(self, index, on=True):
        self.log.append(("select", index))


class _MainWindow:
    _normalize_jid = staticmethod(MainWindow._normalize_jid)

    def __init__(self):
        self._composing_chats = {}
        self._lid_to_phone = {}
        self._phone_to_lid = {}
        self.i18n = _I18n()
        self.spoken = []
        # The row ships off; these tests are about the row, so it is on.
        self.settings = {"user_interface": {"show_typing_row": True}}

    def _resolve_jid_name(self, jid, chat_jid="", *, resolve_missing=True):
        assert resolve_missing is False  # never a network lookup from a repaint
        return NAMES.get(jid, "")

    def output(self, *a, **k):  # pragma: no cover - must never be reached
        self.spoken.append(a)


def _msg(mid, participant=None, from_me=False):
    key = {"id": mid, "fromMe": from_me}
    if participant:
        key["participant"] = participant
    return {"key": key, "_text": mid}


class _Panel:
    _sync_message_rows = ConversationsPanel._sync_message_rows
    _message_row_key = ConversationsPanel._message_row_key
    refresh_typing_row = ConversationsPanel.refresh_typing_row

    def __init__(self, jid=CONTACT, n=3, listbox=False):
        self.main_window = _MainWindow()
        self.conversation = {"remoteJid": jid}
        self._sorted_messages = [_msg(f"m{i}") for i in range(n)]
        self.messages_list = _FakeList(
            [m["_text"] for m in self._sorted_messages], focused=n - 1, listbox=listbox
        )

    def _render_message_line(self, msg, index=None, total=None):
        return msg["_text"]

    def typing(self, chat, participant, action="composing"):
        self.main_window._composing_chats.setdefault(chat, {})[participant] = action


@pytest.fixture(params=[False, True], ids=["listctrl", "listbox"])
def listbox(request):
    return request.param


# ── pure text ────────────────────────────────────────────────────────────────

def test_text_reuses_the_announcement_strings():
    t = _I18n().t
    assert typing_row_text([("Ana", "composing")], t) == "Ana is typing..."
    assert typing_row_text([("Ana", "recording")], t) == "Ana is recording audio..."


def test_text_names_every_participant_and_skips_the_nameless():
    t = _I18n().t
    entries = [("Maria", "composing"), ("", "composing"), ("João", "recording"), ("X", "paused")]
    assert typing_row_text(entries, t) == "Maria is typing... João is recording audio..."
    assert typing_row_text([], t) == ""


# ── appearing ────────────────────────────────────────────────────────────────

def test_row_appears_last_without_moving_focus_or_touching_messages(listbox):
    panel = _Panel(listbox=listbox)
    records = list(panel._sorted_messages)
    panel.typing(CONTACT, CONTACT)

    panel.refresh_typing_row()

    lst = panel.messages_list
    assert lst.rows[-1] == "Ana is typing..."
    assert lst.focused == 2                      # still on the last message
    assert not any(e[0] in ("focus", "select") for e in lst.log)
    assert panel._sorted_messages == records     # never a message record
    assert message_row_count(panel) == 3 == len(panel._sorted_messages)
    assert lst.frozen == 0
    assert panel.main_window.spoken == []


def test_repeated_composing_events_do_not_rewrite_the_row():
    panel = _Panel()
    panel.typing(CONTACT, CONTACT)
    panel.refresh_typing_row()
    panel.messages_list.log.clear()

    panel.refresh_typing_row(CONTACT, {CONTACT})

    assert panel.messages_list.log == []


def test_typing_in_another_chat_shows_nothing_here():
    panel = _Panel()
    panel.typing(GROUP, MARIA)
    panel.refresh_typing_row(GROUP, {MARIA})
    assert not typing_row_shown(panel)
    assert panel.messages_list.rows == ["m0", "m1", "m2"]


def test_lid_presence_key_still_matches_the_open_phone_chat():
    panel = _Panel()
    lid = "123456789@lid"
    panel.main_window._phone_to_lid[CONTACT] = lid
    panel.typing(lid, CONTACT)
    panel.refresh_typing_row()
    assert panel.messages_list.rows[-1] == "Ana is typing..."


def test_group_names_each_participant_and_switching_action_rewrites_in_place():
    panel = _Panel(jid=GROUP)
    panel.typing(GROUP, MARIA)
    panel.typing(GROUP, JOAO, "recording")
    panel.refresh_typing_row()
    assert panel.messages_list.rows[-1] == "Maria is typing... João is recording audio..."

    panel.typing(GROUP, MARIA, "recording")
    panel.messages_list.log.clear()
    panel.refresh_typing_row()
    assert panel.messages_list.log == [
        ("set", 3, "Maria is recording audio... João is recording audio...")
    ]


# ── going away ───────────────────────────────────────────────────────────────

def test_presence_stopping_or_expiring_removes_the_row(listbox):
    panel = _Panel(listbox=listbox)
    panel.typing(CONTACT, CONTACT)
    panel.refresh_typing_row()

    panel.main_window._composing_chats[CONTACT].pop(CONTACT)   # paused / 10 s timer
    panel.refresh_typing_row()

    assert panel.messages_list.rows == ["m0", "m1", "m2"]
    assert not typing_row_shown(panel)
    assert panel.messages_list.focused == 2


def test_removing_the_focused_row_puts_the_cursor_on_the_last_message(listbox):
    panel = _Panel(listbox=listbox)
    panel.typing(CONTACT, CONTACT)
    panel.refresh_typing_row()
    panel.messages_list.focused = 3              # the user arrowed onto it

    panel.main_window._composing_chats[CONTACT].clear()
    panel.refresh_typing_row()

    assert panel.messages_list.focused == 2


def _focus_recording_suppression(panel):
    """Record _suppress_selection_side_effects at each Focus() — the flag
    _on_message_focused() reads to skip mark-as-read."""
    seen = []
    focus = panel.messages_list.Focus

    def recording_focus(index):
        seen.append(getattr(panel, "_suppress_selection_side_effects", False))
        focus(index)
    panel.messages_list.Focus = recording_focus
    return seen


@pytest.mark.parametrize("active,shown,present", [
    (False, True, False),   # window in the tray or inactive
    (True, False, False),   # conversation hidden behind another panel
    (True, True, True),     # the user is looking at it
], ids=["away", "hidden", "looking"])
def test_cursor_moved_off_the_row_marks_read_only_for_a_present_user(active, shown, present):
    panel = _Panel()
    panel.main_window._allow_ui_focus_changes = lambda: active
    panel.conversation_panel = type("P", (), {"IsShown": lambda self: shown})()
    panel.typing(CONTACT, CONTACT)
    panel.refresh_typing_row()
    panel.messages_list.focused = 3              # the cursor rests on the row
    seen = _focus_recording_suppression(panel)

    msg = _msg("new")
    panel._sorted_messages.append(msg)
    append_message_row(panel, "new")
    dismiss_typing_row_for_message(panel, msg)

    assert panel.messages_list.focused == 3      # the cursor is on "new"
    # Away (tray, inactive window): no read receipts for an unseen message.
    assert seen == [not present]
    assert getattr(panel, "_suppress_selection_side_effects", False) is False


def test_duplicate_phrases_are_said_once():
    text = typing_row_text([("Unnamed", "composing"), ("Unnamed", "composing")],
                           _I18n().t)
    assert text == "Unnamed is typing..."


def test_incoming_message_lands_above_and_takes_the_sender_off(listbox):
    panel = _Panel(listbox=listbox)
    panel.typing(CONTACT, CONTACT)
    panel.refresh_typing_row()

    msg = _msg("new")
    panel._sorted_messages.append(msg)
    append_message_row(panel, "new")
    assert panel.messages_list.rows == ["m0", "m1", "m2", "new", "Ana is typing..."]
    dismiss_typing_row_for_message(panel, msg)

    assert panel.messages_list.rows == ["m0", "m1", "m2", "new"]
    assert panel.messages_list.focused == 2      # user was not on it: untouched
    # The shared presence state (chat-list label, Alt+T) is not touched.
    assert panel.main_window._composing_chats[CONTACT] == {CONTACT: "composing"}

    # A later presence event without a new "composing" keeps it off...
    panel.refresh_typing_row(CONTACT, set())
    assert not typing_row_shown(panel)
    # ...a fresh one brings it back.
    panel.refresh_typing_row(CONTACT, {CONTACT})
    assert panel.messages_list.rows[-1] == "Ana is typing..."


def test_group_message_dismisses_only_its_sender():
    panel = _Panel(jid=GROUP)
    panel.typing(GROUP, MARIA)
    panel.typing(GROUP, JOAO)
    panel.refresh_typing_row()

    msg = _msg("from-maria", participant=MARIA)
    panel._sorted_messages.append(msg)
    append_message_row(panel, "from-maria")
    dismiss_typing_row_for_message(panel, msg)

    assert panel.messages_list.rows[-2:] == ["from-maria", "João is typing..."]


def test_own_message_does_not_dismiss():
    panel = _Panel()
    panel.typing(CONTACT, CONTACT)
    panel.refresh_typing_row()
    dismiss_typing_row_for_message(panel, _msg("mine", from_me=True))
    assert panel.messages_list.rows[-1] == "Ana is typing..."


def test_changing_or_closing_the_conversation_removes_it():
    panel = _Panel()
    panel.typing(CONTACT, CONTACT)
    panel.refresh_typing_row()

    panel.conversation = {"remoteJid": GROUP}
    sync_typing_row(panel)
    assert not typing_row_shown(panel)

    panel.conversation = {"remoteJid": CONTACT}
    sync_typing_row(panel)
    assert typing_row_shown(panel)
    panel.conversation = None
    sync_typing_row(panel)
    assert not typing_row_shown(panel)


# ── the row-sync machinery with the row present ─────────────────────────────

def test_sync_message_rows_keeps_the_row_last_and_does_not_rebuild():
    panel = _Panel()
    panel.typing(CONTACT, CONTACT)
    panel.refresh_typing_row()
    panel.messages_list.log.clear()

    old = list(panel._sorted_messages)
    new = old + [_msg("m3")]
    panel._sorted_messages = new
    panel._sync_message_rows(old, new)

    assert panel.messages_list.rows == ["m0", "m1", "m2", "m3", "Ana is typing..."]
    assert ("delete_all",) not in panel.messages_list.log


def test_sync_message_rows_repair_keeps_the_row():
    panel = _Panel()
    panel.typing(CONTACT, CONTACT)
    panel.refresh_typing_row()

    old = list(panel._sorted_messages)
    panel.messages_list.rows.insert(0, "stray")   # control out of step
    panel._sync_message_rows(old, old)

    assert panel.messages_list.rows == ["m0", "m1", "m2", "Ana is typing..."]


def test_without_the_row_the_helpers_are_the_plain_calls():
    panel = _Panel()
    append_message_row(panel, "x")
    assert panel.messages_list.log == [("append", "x")]
    assert message_row_count(panel) == panel.messages_list.GetItemCount()


# ── focus on the row is not a message ────────────────────────────────────────

class _Event:
    def __init__(self, idx):
        self.idx = idx

    def GetIndex(self):
        return self.idx

    def Skip(self):
        pass


def test_focusing_the_row_does_not_mark_the_conversation_read(monkeypatch):
    started = []
    monkeypatch.setattr(
        "ui.conversation_panel.message_list.threading.Thread",
        lambda *a, **k: started.append(k) or type("T", (), {"start": lambda s: None})(),
    )

    class _Stub(_Panel):
        _on_message_focused = ConversationsPanel._on_message_focused
        _is_separator = ConversationsPanel._is_separator
        _should_dismiss_unread_separator = staticmethod(
            ConversationsPanel._should_dismiss_unread_separator)

        def _update_return_call_button(self, idx): pass
        def _update_read_more_button(self, idx): pass
        def _update_reactions_button(self, idx): pass

    panel = _Stub()
    panel.selected_messages = set()
    panel._unread_sep_idx = 1
    panel._unread_sep_marked_read = False
    panel._sep_anchors_read_position = False
    panel._current_audio_id = None
    panel._audio_stream = None
    panel._is_loading_more = False
    panel.typing(CONTACT, CONTACT)
    panel.refresh_typing_row()

    panel._on_message_focused(_Event(3))         # the typing row

    assert started == []
    assert panel._unread_sep_marked_read is False
    assert panel._sep_anchors_read_position is False


# ── MainWindow wiring: silent, after the existing announcement ──────────────

class _Speech:
    def __init__(self):
        self.outputs = []

    def output(self, text):
        self.outputs.append(text)


class _RecordingPanel:
    def __init__(self):
        self.conversation = {"remoteJid": CONTACT}
        self.calls = []

    def refresh_typing_row(self, chat="", fresh=()):
        self.calls.append((chat, set(fresh)))

    def _refresh_presence_note(self, _jid):
        pass


class _MW:
    on_presence_update = MainWindow.on_presence_update
    _normalize_jid = staticmethod(MainWindow._normalize_jid)

    def __init__(self):
        self._window_hidden = False
        self._lid_to_phone = {}
        self._phone_to_lid = {}
        self._presence_cache = {}
        self._composing_chats = {}
        self._presence_timers = {}
        self._presence_pushname_map = {}
        self.contacts = {}
        self.chats = {}
        self.db = None
        self.settings = {"speech_content": {"announce_typing": True}}
        self.conversations_panel = _RecordingPanel()
        self.speak_output = _Speech()
        self.i18n = _I18n()

    def IsShown(self):
        return True

    def IsIconized(self):
        return False

    def IsActive(self):
        return True

    def _resolve_jid_name(self, *_a, **_k):
        return "Ana"

    def _refresh_chat_row_in_list(self, jid):
        pass


def test_presence_update_refreshes_the_row_and_speaks_only_the_existing_announcement(monkeypatch):
    monkeypatch.setattr(wx, "CallLater", lambda *_a, **_k: type("T", (), {"Stop": lambda s: None})())
    mw = _MW()

    mw.on_presence_update(CONTACT, {CONTACT: {"lastKnownPresence": "composing"}})
    mw.on_presence_update(CONTACT, {CONTACT: {"lastKnownPresence": "paused"}})

    assert mw.speak_output.outputs == ["Ana is typing..."]
    assert mw.conversations_panel.calls == [(CONTACT, {CONTACT}), (CONTACT, set())]


# ── a message that arrives through sync, not the live path ──────────────────

def test_message_appended_by_the_sync_tail_path_also_dismisses():
    """_append_new_tail_rows() appends a message the live path never painted
    (it came in through sync). It lands above the typing row and takes its
    sender off it, exactly like on_incoming_message() — without a rebuild."""
    from tests.test_message_list_refresh import _Stub as _RefreshStub, _msg as _rmsg

    s = _RefreshStub([_rmsg("a")], jid=CONTACT)
    s.main_window = _MainWindow()
    s.messages_list = _FakeList([])
    s.refresh_messages_if_changed()              # initial rebuild: ["oi"]
    s.main_window._composing_chats[CONTACT] = {CONTACT: "composing"}
    sync_typing_row(s)
    assert s.messages_list.rows == ["oi", "Ana is typing..."]

    s._records.append(_rmsg("b", text="nova", ts=2000))
    s.refresh_messages_if_changed()

    assert s.populate_calls == [True]           # tail append, not a rebuild
    assert s.messages_list.rows == ["oi", "nova"]
    assert not typing_row_shown(s)


# ── Settings > User Interface: show_typing_row ───────────────────────────────


@pytest.mark.parametrize("settings, expected", [
    (None, False),                                           # no settings at all
    ({}, False),                                             # install without the key
    ({"user_interface": {}}, False),
    ({"user_interface": "junk"}, False),
    ({"user_interface": {"show_typing_row": "yes"}}, False),
    ({"user_interface": {"show_typing_row": True}}, True),
    ({"user_interface": {"show_typing_row": False}}, False),
])
def test_the_setting_is_off_unless_turned_on(settings, expected):
    assert typing_row_enabled(settings) is expected


def test_turned_off_no_row_appears():
    panel = _Panel()
    panel.main_window.settings = {"user_interface": {"show_typing_row": False}}
    panel.typing(CONTACT, CONTACT)
    panel.refresh_typing_row()

    assert panel.messages_list.rows == ["m0", "m1", "m2"]
    assert not typing_row_shown(panel)
    assert panel.messages_list.log == []


def test_turning_it_off_removes_a_row_already_showing_and_on_brings_it_back(listbox):
    panel = _Panel(listbox=listbox)
    panel.main_window.settings = {"user_interface": {"show_typing_row": True}}
    panel.typing(CONTACT, CONTACT)
    panel.refresh_typing_row()
    assert panel.messages_list.rows[-1] == "Ana is typing..."

    # What Settings > Apply does: store the choice, then refresh_typing_row().
    panel.main_window.settings["user_interface"]["show_typing_row"] = False
    panel.refresh_typing_row()
    assert panel.messages_list.rows == ["m0", "m1", "m2"]
    assert not typing_row_shown(panel)
    assert panel.messages_list.frozen == 0
    assert panel.main_window.spoken == []

    panel.main_window.settings["user_interface"]["show_typing_row"] = True
    panel.refresh_typing_row()
    assert panel.messages_list.rows[-1] == "Ana is typing..."
