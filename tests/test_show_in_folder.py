"""Tests for issue #94's Save-As-only "Show in folder" action.

The action has three entry points: Ctrl+Enter on the focused message, a
context-menu item and a Tab-reachable button after Open / Save as.  None of the
tests opens Explorer or constructs a wx window; ConversationsPanel methods are
bound to the same small stubs used throughout this suite.
"""

import inspect

import pytest
import wx

import app_paths
import ui.conversations as conversations
from ui.accessible import AccessibleShowInFolder, CompatListBoxMessagesCtrl
from ui.conversations import (
    ConversationsPanel,
    reveal_file_in_folder,
    saved_media_path,
)


@pytest.fixture(autouse=True)
def _account_scope():
    app_paths.set_active_account("show-in-folder-test")
    yield
    app_paths.set_active_account(None)


def _message(msg_type="documentMessage", msg_id="ABC123", **extra):
    msg = {
        "key": {"id": msg_id, "fromMe": False},
        "messageType": msg_type,
        "message": {msg_type: {}},
    }
    msg.update(extra)
    return msg


class TestSavedMediaPath:
    def test_prefers_the_copy_chosen_by_save_as(self, tmp_path, monkeypatch):
        saved = tmp_path / "Desktop" / "video.mp4"
        saved.parent.mkdir()
        saved.write_bytes(b"video")
        monkeypatch.setattr(
            conversations,
            "cached_media_path",
            lambda *_: pytest.fail("cache consulted after Save As"),
        )

        assert saved_media_path(
            _message(_saved_media_path=str(saved))
        ) == str(saved.resolve())

    def test_a_deleted_save_as_copy_does_not_fall_back_to_the_cache(
        self, tmp_path, monkeypatch
    ):
        assert saved_media_path(
            _message(_saved_media_path=str(tmp_path / "gone.mp4"))
        ) == ""

    def test_an_original_sent_attachment_is_not_a_save_as_copy(self, tmp_path):
        original = tmp_path / "report.pdf"
        original.write_bytes(b"pdf")

        assert saved_media_path(
            _message(_attachment_path=str(original))
        ) == ""

    def test_a_downloaded_internal_cache_is_not_user_visible(self, tmp_path, monkeypatch):
        cached = tmp_path / "ABC123.wzmedia"
        cached.write_bytes(b"encrypted")
        monkeypatch.setattr(
            conversations,
            "cached_media_path",
            lambda *_: str(cached),
        )

        assert saved_media_path(_message()) == ""

    @pytest.mark.parametrize("msg_type", ["conversation", "contactMessage", "locationMessage"])
    def test_non_file_messages_are_never_available(self, msg_type, monkeypatch):
        monkeypatch.setattr(
            conversations,
            "cached_media_path",
            lambda *_: pytest.fail("non-file message consulted the cache"),
        )

        assert saved_media_path(_message(msg_type)) == ""

class TestExplorerLaunch:
    def test_selects_the_absolute_file_in_explorer(self, tmp_path, monkeypatch):
        media = tmp_path / "video.mp4"
        media.write_bytes(b"video")
        calls = []
        monkeypatch.setattr(
            conversations.subprocess,
            "Popen",
            lambda argv: calls.append(argv),
        )

        assert reveal_file_in_folder(str(media)) is True
        assert calls == [["explorer.exe", "/select,", str(media.resolve())]]

    def test_a_file_that_disappeared_does_not_open_explorer(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(
            conversations.subprocess,
            "Popen",
            lambda argv: calls.append(argv),
        )

        assert reveal_file_in_folder(str(tmp_path / "gone.wzmedia")) is False
        assert calls == []


class _I18n:
    def t(self, key):
        return {
            "show_in_folder_save_first": "Save the file with Save As first",
            "show_in_folder_missing": "The saved file is no longer available",
            "show_in_folder_failed": "Could not open the file's folder",
        }.get(key, key)


class _MainWindow:
    def __init__(self):
        self.i18n = _I18n()
        self.outputs = []

    def output(self, text, interrupt=False):
        self.outputs.append((text, interrupt))


class _SelectedList:
    def __init__(self, selected=0):
        self.selected = selected

    def GetFirstSelected(self):
        return self.selected


class _ActionPanel:
    _on_action_show_in_folder = ConversationsPanel._on_action_show_in_folder
    _saved_media_path = ConversationsPanel._saved_media_path
    show_message_in_folder = ConversationsPanel.show_message_in_folder

    def __init__(self, messages, selected=0):
        self._sorted_messages = messages
        self.messages_list = _SelectedList(selected)
        self.main_window = _MainWindow()
        self._saved_media_paths = {}


class TestAction:
    def test_button_or_menu_action_reveals_the_selected_message(self, monkeypatch):
        msg = _message()
        panel = _ActionPanel([msg])
        calls = []
        monkeypatch.setattr(conversations, "saved_media_path", lambda value: "C:\\Saved\\video.mp4")
        monkeypatch.setattr(conversations, "reveal_file_in_folder", lambda path: calls.append(path) or True)

        panel._on_action_show_in_folder(None)

        assert calls == ["C:\\Saved\\video.mp4"]
        assert panel.main_window.outputs == []

    def test_unsaved_file_tells_the_user_to_save_as_first(self, monkeypatch):
        panel = _ActionPanel([_message()])
        monkeypatch.setattr(conversations, "saved_media_path", lambda value: "")

        assert panel.show_message_in_folder(panel._sorted_messages[0]) is False
        assert panel.main_window.outputs == [
            ("Save the file with Save As first", True)
        ]

    def test_deleted_saved_copy_is_announced(self, monkeypatch):
        panel = _ActionPanel([_message(_saved_media_path="C:\\gone.mp4")])
        monkeypatch.setattr(conversations, "saved_media_path", lambda value: "")

        assert panel.show_message_in_folder(panel._sorted_messages[0]) is False
        assert panel.main_window.outputs == [
            ("The saved file is no longer available", True)
        ]

    def test_explorer_failure_is_announced(self, monkeypatch):
        panel = _ActionPanel([_message()])
        monkeypatch.setattr(conversations, "saved_media_path", lambda value: "C:\\Saved\\video.mp4")

        def fail(_path):
            raise OSError("Explorer unavailable")

        monkeypatch.setattr(conversations, "reveal_file_in_folder", fail)

        assert panel.show_message_in_folder(panel._sorted_messages[0]) is False
        assert panel.main_window.outputs == [
            ("Could not open the file's folder", True)
        ]


class _CtrlEnterEvent:
    def GetKeyCode(self):
        return wx.WXK_RETURN

    def ControlDown(self):
        return True

    def ShiftDown(self):
        return False


class _FocusedList:
    def GetFocusedItem(self):
        return 0

    def GetItemCount(self):
        return 1


def test_ctrl_enter_acts_on_the_focused_message():
    msg = _message()
    calls = []
    panel = type("_ShortcutPanel", (), {})()
    panel.messages_list = _FocusedList()
    panel._sorted_messages = [msg]
    panel._is_loading_more = False
    panel._messages_offset = 0
    panel._is_separator = lambda value: False
    panel.show_message_in_folder = lambda value: calls.append(value)

    ConversationsPanel._on_messages_list_key_down(panel, _CtrlEnterEvent())

    assert calls == [msg]


@pytest.mark.parametrize(
    "msg",
    [
        _message("conversation"),
        {"_type": "unread_separator", "count": 1},
    ],
)
def test_ctrl_enter_ignores_rows_that_cannot_be_saved(msg):
    calls = []
    panel = type("_ShortcutPanel", (), {})()
    panel.messages_list = _FocusedList()
    panel._sorted_messages = [msg]
    panel._is_loading_more = False
    panel._messages_offset = 0
    panel._is_separator = ConversationsPanel._is_separator.__get__(panel)
    panel.show_message_in_folder = lambda value: calls.append(value)

    ConversationsPanel._on_messages_list_key_down(panel, _CtrlEnterEvent())

    assert calls == []


class _CompatCtrlEnterEvent(_CtrlEnterEvent):
    def __init__(self):
        self.skipped = False

    def GetSkipped(self):
        return self.skipped

    def Skip(self):
        self.skipped = True


def test_listbox_ctrl_enter_uses_shortcut_instead_of_plain_activation():
    shortcut_calls = []
    activation_calls = []
    control = type("_CompatStub", (), {})()
    control.HasFocus = lambda: True
    control.GetSelection = lambda: 0
    control._key_down_handler = lambda event: shortcut_calls.append(event)
    control._activated_handler = lambda event: activation_calls.append(event)
    event = _CompatCtrlEnterEvent()

    CompatListBoxMessagesCtrl._on_char_hook(control, event)

    assert shortcut_calls == [event]
    assert activation_calls == []


def test_native_activation_with_control_held_does_not_play_video(monkeypatch):
    msg = _message("videoMessage")
    calls = []
    panel = type("_ActivationPanel", (), {})()
    panel.messages_list = _FocusedList()
    panel._sorted_messages = [msg]
    panel._is_separator = lambda value: False
    panel.show_message_in_folder = lambda value: calls.append(("reveal", value))
    panel._do_activate_message = lambda index: calls.append(("play", index))
    monkeypatch.setattr(wx, "GetKeyState", lambda key: key == wx.WXK_CONTROL)

    ConversationsPanel.on_message_activated(panel, object())

    assert calls == [("reveal", msg)]


def test_native_ctrl_enter_ignores_stale_focus_index(monkeypatch):
    panel = type("_ActivationPanel", (), {})()
    panel.messages_list = type("_StaleFocus", (), {"GetFocusedItem": lambda self: 4})()
    panel._sorted_messages = [_message("videoMessage")]
    panel.show_message_in_folder = lambda value: pytest.fail("stale row revealed")
    panel._do_activate_message = lambda index: pytest.fail("stale row activated")
    monkeypatch.setattr(wx, "GetKeyState", lambda key: True)

    ConversationsPanel.on_message_activated(panel, object())


def test_save_as_from_another_message_copy_updates_the_current_row(tmp_path):
    saved = tmp_path / "video.mp4"
    saved.write_bytes(b"video")
    current = _message("videoMessage")
    completed = _message("videoMessage", _saved_media_path=str(saved))
    shown = []
    panel = type("_SavedPanel", (), {})()
    panel._saved_media_path = ConversationsPanel._saved_media_path.__get__(panel)
    panel._saved_media_paths = {}
    panel._sorted_messages = [current]
    panel._all_sorted_messages = [current]
    panel.messages_list = _SelectedList()
    panel._action_show_in_folder_btn = type(
        "_Button", (), {"Show": lambda self: shown.append(True)}
    )()
    panel._sync_media_action_slot_visibility = lambda: None
    panel.conversation_panel = type("_Panel", (), {"Layout": lambda self: None})()

    ConversationsPanel._on_media_saved_as(panel, completed)

    assert current["_saved_media_path"] == str(saved.resolve())
    assert panel._saved_media_path(current) == str(saved.resolve())
    assert shown == [True]


def test_latest_remembered_save_as_wins_over_a_stale_message_copy(tmp_path):
    old_copy = tmp_path / "old" / "video.mp4"
    new_copy = tmp_path / "new" / "video.mp4"
    old_copy.parent.mkdir()
    new_copy.parent.mkdir()
    old_copy.write_bytes(b"old")
    new_copy.write_bytes(b"new")
    msg = _message("videoMessage", _saved_media_path=str(old_copy))
    panel = type("_SavedPanel", (), {})()
    panel._saved_media_paths = {"ABC123": str(new_copy.resolve())}

    result = ConversationsPanel._saved_media_path(panel, msg)

    assert result == str(new_copy.resolve())
    assert msg["_saved_media_path"] == str(new_copy.resolve())


def test_show_in_folder_accessible_reports_ctrl_enter():
    assert AccessibleShowInFolder().GetKeyboardShortcut(0) == (
        wx.ACC_OK,
        "Ctrl+Enter",
    )


def test_successful_save_as_becomes_the_reveal_target(tmp_path, monkeypatch):
    cached = tmp_path / "ABC123.wzmedia"
    cached.write_bytes(b"encrypted-video")
    saved = tmp_path / "Desktop" / "video.mp4"
    saved.parent.mkdir()
    msg = _message("videoMessage")
    panel = type("_SaveWorkerPanel", (), {})()
    panel.main_window = type("_MW", (), {"key": b"unused"})()
    panel._on_media_saved_as = lambda value: None
    monkeypatch.setattr(conversations, "cached_media_path", lambda *_: str(cached))
    monkeypatch.setattr(conversations, "decrypt_bytes", lambda content, key: content)
    monkeypatch.setattr(wx, "CallAfter", lambda callback, *args: callback(*args))

    ConversationsPanel._save_message_media(panel, msg, str(saved))

    assert saved.read_bytes() == b"encrypted-video"
    assert saved_media_path(msg) == str(saved.resolve())


class TestThreeEntryPointsStayInStep:
    def test_button_follows_open_and_save_as_in_tab_order(self):
        source = inspect.getsource(ConversationsPanel.init_UI)
        assert source.index("self._action_open_btn =") < source.index(
            "self._action_save_as_btn ="
        ) < source.index("self._action_show_in_folder_btn =")

    def test_context_menu_checks_the_same_path_helper(self):
        source = inspect.getsource(ConversationsPanel.on_messages_context_menu)
        assert "self._saved_media_path(msg)" in source
        assert "show_in_folder" in source
        assert "Ctrl+Enter" in source

    def test_download_completion_does_not_expose_the_button_before_save_as(self):
        source = inspect.getsource(ConversationsPanel._on_action_download)
        assert "self._action_show_in_folder_btn.Show()" not in source

    def test_save_as_completion_exposes_the_button(self):
        source = inspect.getsource(ConversationsPanel._on_media_saved_as)
        assert "self._action_show_in_folder_btn.Show()" in source

    def test_button_exposes_its_keyboard_shortcut_to_accessibility(self):
        source = inspect.getsource(ConversationsPanel.init_UI)
        assert "SetAccessible(AccessibleShowInFolder())" in source

    def test_every_entry_point_calls_one_message_based_action(self):
        assert "show_message_in_folder(" in inspect.getsource(
            ConversationsPanel._on_action_show_in_folder
        )
        assert "show_message_in_folder(" in inspect.getsource(
            ConversationsPanel._on_messages_list_key_down
        )
