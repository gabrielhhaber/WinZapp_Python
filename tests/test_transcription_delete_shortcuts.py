"""Deleting saved local transcriptions from the keyboard, and the Status tab's
transcription keys in the "my status" window.

* A message's saved transcription could only be deleted from its context
  menu. Alt+Shift+Delete does it for the focused message, follows "Substituir
  atalhos por ações em massa" when messages are selected, and
  Ctrl+Alt+Shift+Delete (also in the "Ações em massa" submenu) does it for the
  selection.
* The Status tab has the same key for the status the focused row shows.
* The "my status" row of the list opens a window of its own, which the list's
  keys never reached; that window now has the three transcription keys.
* Every one of these keys is a row of the shortcut catalogue, so it is listed
  on the Keyboard shortcuts tab and can be changed there.

The classes cannot be built without a wx.App, so their methods are bound onto
stubs.
"""

import inspect
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import wx

from core import shortcut_catalog
from core.transcription import statuses, stored
from core.transcription.backend import TranscriptionResult
from main_window.status_transcription import StatusTranscriptionMixin
from status_panel import StatusPanel
from status_tab import my_status_transcription as own_module
from status_tab import status_transcription as panel_module
from status_tab.my_status_transcription import MyStatusTranscriptionKeys
from status_tab.status_dialogs import MyStatusDialog
from tests.locales import registered_locale_codes
from tests.mnemonics import load_strings
from ui.conversation_panel import transcription_delete as delete_module
from ui.conversations import ConversationsPanel
from ui.dialogs.shortcuts_tab import SCOPE_LABELS, visible_shortcuts
from ui.status_transcription_flow import StatusTranscriptionFlow
from ui.transcription_flow import MessageTranscriptionFlow

ROOT = Path(__file__).resolve().parents[1]
JID = "5511999999999@s.whatsapp.net"

#: Every shortcut this work added, with its chord: (modifiers, key).
ALT_SHIFT, CTRL_ALT_SHIFT, DELETE = 5, 7, 127
NEW_SHORTCUTS = {
    "messages.ID_ALT_SHIFT_DELETE": (ALT_SHIFT, DELETE),
    "messages.ID_BULK_DELETE_TRANSCRIPTIONS": (CTRL_ALT_SHIFT, DELETE),
    "status.ID_ALT_SHIFT_T": (ALT_SHIFT, ord("T")),
    "status.ID_ALT_SHIFT_N": (ALT_SHIFT, ord("N")),
    "status.ID_ALT_SHIFT_DELETE": (ALT_SHIFT, DELETE),
}


def _value(text="hello", inline=False):
    result = TranscriptionResult(text=text, language="en", language_probability=0.9,
                                 duration_seconds=3.0, backend="faster_whisper",
                                 model_id="small")
    return stored.with_result(None, stored.record_from_result(result, time.time(), inline=inline))


def _msg(msg_id, transcribed=True):
    msg = {"key": {"id": msg_id, "fromMe": False, "remoteJid": JID},
           "messageType": "audioMessage", "message": {"audioMessage": {"seconds": 3}}}
    if transcribed:
        msg[stored.TRANSCRIPTION_KEY] = _value()
    return msg


# ── every new key is configurable ────────────────────────────────────────────


def test_every_new_shortcut_is_a_row_of_the_catalogue_with_its_default_chord():
    rows = {s.id: s for s in shortcut_catalog.SHORTCUTS}
    for shortcut_id, (mod, key) in NEW_SHORTCUTS.items():
        row = rows[shortcut_id]
        assert (row.mod, row.key) == (mod, key), shortcut_id
        assert row.scope == shortcut_id.split(".")[0]


def test_the_keyboard_shortcuts_tab_lists_every_one_of_them():
    """The tab is built from visible_shortcuts(); a row it hides, or a scope
    with no section title, cannot be changed by the user."""
    window = SimpleNamespace(_chat_lock_vault=None, account_id="a", registry=object())
    listed = {s.id: s for s in visible_shortcuts(window) if hasattr(s, "id")}
    for shortcut_id in NEW_SHORTCUTS:
        assert shortcut_id in listed, shortcut_id
        assert listed[shortcut_id].scope in SCOPE_LABELS


@pytest.mark.parametrize("locale", registered_locale_codes())
def test_each_has_a_label_in_every_language_that_names_its_default_key(locale):
    strings = load_strings(locale)
    rows = {s.id: s for s in shortcut_catalog.SHORTCUTS}
    for shortcut_id, (mod, _key) in NEW_SHORTCUTS.items():
        label = strings[rows[shortcut_id].label]
        assert label.startswith("Ctrl+Alt+Shift+" if mod == CTRL_ALT_SHIFT else "Alt+Shift+"), \
            (locale, shortcut_id, label)


def test_the_tables_that_own_them_are_the_configurable_kind():
    """A key reaches the user's preference only through make_shortcut_table();
    a plain wx.AcceleratorTable would be fixed for good."""
    conversation = (ROOT / "client/ui/conversation_panel/accelerators.py").read_text(
        encoding="utf-8")
    assert "(AS,               wx.WXK_DELETE,     self.ID_ALT_SHIFT_DELETE)" in conversation
    assert "(CAS,              wx.WXK_DELETE,      self.ID_BULK_DELETE_TRANSCRIPTIONS)" \
        in conversation
    assert "self._on_accel_delete_transcription, id=self.ID_ALT_SHIFT_DELETE" in conversation
    status = (ROOT / "client/status_panel.py").read_text(encoding="utf-8")
    assert "make_shortcut_table(self, 'status', [" in status
    assert "wx.WXK_DELETE, self.ID_ALT_SHIFT_DELETE" in status
    assert "self._on_delete_status_transcription, id=self.ID_ALT_SHIFT_DELETE" in status
    own = inspect.getsource(MyStatusTranscriptionKeys._create_transcription_accelerators)
    assert "make_shortcut_table(self, 'status', [" in own
    for name in ("ID_ALT_SHIFT_T", "ID_ALT_SHIFT_N", "ID_ALT_SHIFT_DELETE"):
        assert f"self.{name}" in own


# ── conversation: the focused message, and the selection ─────────────────────


class _Panel:
    for _name in ("_on_accel_delete_transcription",
                  "_on_accel_bulk_delete_transcriptions", "_on_mass_delete_transcriptions",
                  "_finish_bulk_transcription_delete", "_run_bulk_message_action"):
        locals()[_name] = getattr(ConversationsPanel, _name)
    del _name
    # A static method reads off the class as a plain function.
    _holds_transcription = staticmethod(ConversationsPanel._holds_transcription)

    def __init__(self, messages, selected=(), bulk_shortcuts=False, focused=0,
                 delete_ok=True):
        self._sorted_messages = list(messages)
        self.selected_messages = set(selected)
        self._bulk = bulk_shortcuts
        self.conversation = {"remoteJid": JID}
        self.messages_list = SimpleNamespace(GetFirstSelected=lambda: focused)
        self.menu_deleted = []
        self.repainted = []
        self.deleted = []
        outcome = delete_ok if callable(delete_ok) else (lambda msg_id: delete_ok)

        def delete(jid, msg_id, on_done):
            self.deleted.append((jid, msg_id))
            on_done(outcome(msg_id))

        self.main_window = SimpleNamespace(
            i18n=SimpleNamespace(t=lambda key: key), output=Mock(),
            delete_message_transcription=delete)

    def _is_separator(self, msg):
        return bool(msg.get("_separator"))

    def _bulk_shortcuts_enabled(self):
        return self._bulk

    def _on_menu_delete_transcription(self, msg):
        self.menu_deleted.append(msg["key"]["id"])

    def _repaint_or_repopulate(self, ids):
        self.repainted.append(list(ids))

    def said(self):
        return [call.args[0] for call in self.main_window.output.call_args_list]


@pytest.fixture
def answer(monkeypatch):
    """The yes/no question of the bulk delete, answered by the test."""
    state = {"result": wx.ID_YES, "asked": []}

    class _Dialog:
        def __init__(self, parent, text, title, style):
            state["asked"].append(text)

        def ShowModal(self):
            return state["result"]

        def Destroy(self):
            pass

    monkeypatch.setattr(delete_module.wx, "MessageDialog", _Dialog)
    return state


def test_the_key_deletes_the_focused_messages_transcription_like_the_menu():
    panel = _Panel([_msg("m1"), _msg("m2")], focused=1)
    panel._on_accel_delete_transcription(None)
    assert panel.menu_deleted == ["m2"]


def test_a_message_with_nothing_saved_gets_a_sentence_not_silence():
    panel = _Panel([_msg("m1", transcribed=False)])
    panel._on_accel_delete_transcription(None)
    assert panel.menu_deleted == []
    assert panel.said() == ["transcription_none_to_delete"]


def test_a_deleted_transcription_is_not_one_to_delete_again():
    """A tombstone is a decision, not a text."""
    msg = _msg("m1", transcribed=False)
    msg[stored.TRANSCRIPTION_KEY] = stored.tombstone(time.time())
    panel = _Panel([msg])
    panel._on_accel_delete_transcription(None)
    assert panel.said() == ["transcription_none_to_delete"]


def test_with_a_selection_the_key_follows_the_bulk_substitution_setting(answer):
    messages = [_msg("m1"), _msg("m2"), _msg("m3")]

    off = _Panel(messages, selected={"m1", "m2"}, bulk_shortcuts=False, focused=2)
    off._on_accel_delete_transcription(None)
    assert off.menu_deleted == ["m3"], "setting off: the focused message, as always"
    assert off.deleted == []

    on = _Panel(messages, selected={"m1", "m2"}, bulk_shortcuts=True, focused=2)
    on._on_accel_delete_transcription(None)
    assert on.menu_deleted == []
    assert [msg_id for _jid, msg_id in on.deleted] == ["m1", "m2"]


def test_the_bulk_key_without_a_selection_says_so():
    panel = _Panel([_msg("m1")])
    panel._on_accel_bulk_delete_transcriptions(None)
    assert panel.said() == ["bulk_no_message_selection"]
    assert panel.deleted == []


def test_the_bulk_delete_asks_once_and_touches_only_messages_that_have_a_text(answer):
    messages = [_msg("m1"), _msg("m2", transcribed=False), _msg("m3"), _msg("m4")]
    panel = _Panel(messages, selected={"m1", "m2", "m3"})

    panel._on_accel_bulk_delete_transcriptions(None)

    assert len(answer["asked"]) == 1
    assert panel.deleted == [(JID, "m1"), (JID, "m3")]
    assert panel.repainted == [["m1", "m3"]]
    assert panel.said() == ["transcription_bulk_deleted"]


def test_declining_the_question_deletes_nothing(answer):
    answer["result"] = wx.ID_NO
    panel = _Panel([_msg("m1")], selected={"m1"})
    panel._on_mass_delete_transcriptions()
    assert panel.deleted == [] and panel.repainted == [] and panel.said() == []


def test_a_selection_with_nothing_saved_is_not_asked_about(answer):
    panel = _Panel([_msg("m1", transcribed=False)], selected={"m1"})
    panel._on_mass_delete_transcriptions()
    assert answer["asked"] == []
    assert panel.said() == ["transcription_bulk_delete_none"]


def test_a_delete_the_database_refused_is_counted_apart(answer):
    panel = _Panel([_msg("m1"), _msg("m2")], selected={"m1", "m2"},
                   delete_ok=lambda msg_id: msg_id == "m1")
    panel._on_mass_delete_transcriptions()
    assert panel.said() == ["transcription_bulk_delete_partial"]


def test_an_answer_that_arrives_in_another_conversation_repaints_nothing(answer):
    panel = _Panel([_msg("m1")], selected={"m1"})
    panel._finish_bulk_transcription_delete("other@c.us", ["m1"], 1)
    assert panel.repainted == []
    assert panel.said() == ["transcription_bulk_deleted"]


def test_the_bulk_submenu_offers_it():
    menu = (ROOT / "client/ui/conversation_panel/message_menu.py").read_text(encoding="utf-8")
    assert "i18n.t('transcription_bulk_delete_menu')" in menu
    assert "self._on_mass_delete_transcriptions," in menu


# ── the Status tab ───────────────────────────────────────────────────────────


def _status(msg_id="s1", from_me=False, participant="5511988887777@s.whatsapp.net"):
    return {"key": {"id": msg_id, "fromMe": from_me, "remoteJid": statuses.STATUS_JID,
                    "participant": participant},
            "messageType": "audioMessage", "message": {"audioMessage": {"seconds": 3}},
            "messageTimestamp": time.time()}


def test_the_status_tabs_key_deletes_what_the_focused_row_shows(monkeypatch):
    deleted = []
    monkeypatch.setattr(panel_module.status_transcription_flow, "delete_status_transcription",
                        lambda panel, status: deleted.append(status))
    with_text, without = _status("s1"), _status("s2")
    with_text[statuses.KEY] = _value()
    panel = SimpleNamespace(
        main_window=SimpleNamespace(i18n=SimpleNamespace(t=lambda key: key), output=Mock()))

    panel._status_for_transcription = lambda: with_text
    StatusPanel._on_delete_status_transcription(panel)
    assert deleted == [with_text]

    panel._status_for_transcription = lambda: without
    StatusPanel._on_delete_status_transcription(panel)
    assert deleted == [with_text]
    assert panel.main_window.output.call_args.args[0] == "transcription_none_to_delete"


def test_the_question_about_a_status_does_not_talk_about_a_conversation():
    assert MessageTranscriptionFlow._delete_question_key == "transcription_delete_question"
    assert StatusTranscriptionFlow._delete_question_key == "transcription_status_delete_question"
    source = inspect.getsource(MessageTranscriptionFlow.delete_saved)
    assert "i18n.t(self._delete_question_key)" in source


# ── the "my status" window ───────────────────────────────────────────────────


class _OwnDialog:
    for _name in ("_own_status", "_own_status_or_say_none", "_on_transcribe_own_status",
                  "_on_transcribe_own_status_inline", "_on_delete_own_status_transcription",
                  "_own_status_inline_suffix", "_status_poster_name", "_repaint_status_row",
                  "_focus_status_list"):
        locals()[_name] = getattr(MyStatusTranscriptionKeys, _name)
    del _name
    main_window = MyStatusTranscriptionKeys.main_window
    _content_label_text = MyStatusDialog._content_label_text

    def __init__(self, own, current=0, settings=None):
        self._mw = SimpleNamespace(
            settings=settings if settings is not None else {},
            i18n=SimpleNamespace(t=lambda key: "{current}/{total}" if key == "status_of" else key),
            output=Mock())
        self._statuses = list(own)
        self._current = current
        self.label = None
        self._content_lbl = SimpleNamespace(SetLabel=lambda text: setattr(self, "label", text))
        self.layouts = 0

    def Layout(self):
        self.layouts += 1


@pytest.fixture
def own_flows(monkeypatch):
    calls = []
    for name, tag in (("open_or_transcribe_status", "window"),
                      ("transcribe_status_inline", "inline"),
                      ("delete_status_transcription", "delete")):
        monkeypatch.setattr(own_module.status_transcription_flow, name,
                            lambda panel, status, _tag=tag: calls.append((_tag, status)))
    return calls


def test_the_three_keys_act_on_the_own_status_being_shown(own_flows):
    first, second = _status("o1", from_me=True), _status("o2", from_me=True)
    second[statuses.KEY] = _value()
    dialog = _OwnDialog([first, second], current=1)

    dialog._on_transcribe_own_status()
    dialog._on_transcribe_own_status_inline()
    dialog._on_delete_own_status_transcription()

    assert own_flows == [("window", second), ("inline", second), ("delete", second)]


def test_a_photo_or_nothing_saved_gets_a_sentence(own_flows):
    photo = _status("o1", from_me=True)
    photo["messageType"] = "imageMessage"
    dialog = _OwnDialog([photo])

    dialog._on_transcribe_own_status()
    dialog._on_delete_own_status_transcription()

    assert own_flows == []
    assert [c.args[0] for c in dialog._mw.output.call_args_list] \
        == ["transcription_no_audio", "transcription_none_to_delete"]


def test_the_window_is_the_panel_the_flow_talks_to():
    dialog = _OwnDialog([_status("o1", from_me=True)])
    assert dialog.main_window is dialog._mw
    assert dialog._status_poster_name(dialog._statuses[0]) == "my_status"
    assert dialog._focus_status_list() is None


def test_the_line_shows_an_inline_transcription_and_is_redrawn_quietly():
    """Redrawn without announcing the status again or touching what plays:
    the flow says the text itself."""
    status = _status("o1", from_me=True)
    dialog = _OwnDialog([status])
    assert "transcription_row_label" not in dialog._content_label_text()

    status[statuses.KEY] = _value("good morning", inline=True)
    dialog._repaint_status_row("another-id")
    assert dialog.label is None

    dialog._repaint_status_row("o1")
    assert dialog.label.endswith(", transcription_row_label: good morning")
    assert dialog.label.startswith("1/1: ")
    dialog._mw.output.assert_not_called()


def test_the_window_builds_its_keys_only_when_it_shows_statuses():
    source = inspect.getsource(MyStatusDialog._init_ui)
    viewer = source[source.index("if self._statuses:"):source.index("# Close button")]
    assert "self._create_transcription_accelerators()" in viewer
    assert issubclass(MyStatusDialog, MyStatusTranscriptionKeys)


def test_an_own_status_that_names_no_poster_is_kept_under_the_account(monkeypatch):
    """The account's own statuses come from the API without a participant;
    filed under the account, as an arriving one is, they are kept and pruned
    like any other."""
    import main_window.status_transcription as window_module

    class _Now:
        def __init__(self, target=None, daemon=None, name=None):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(window_module.threading, "Thread", _Now)
    window = SimpleNamespace(
        settings={}, _status_updates={}, my_jid="5511900000000@s.whatsapp.net",
        db=SimpleNamespace(upsert_status_update=Mock()), navigation_panel=None)
    for name in ("_status_panel_get", "_cached_status_records", "_shown_status_copies",
                 "_persist_status_records", "store_status_transcription"):
        setattr(window, name, getattr(StatusTranscriptionMixin, name).__get__(window))
    own = _status("o1", from_me=True, participant="")

    assert window.store_status_transcription(own, _value()) == stored.SAVE_STORED
    assert window._status_updates == {window.my_jid: [own]}
    window.db.upsert_status_update.assert_called_once_with(window.my_jid, own)

    stranger = _status("s9", from_me=False, participant="")
    assert window.store_status_transcription(stranger, _value()) == stored.SAVE_MISSING
