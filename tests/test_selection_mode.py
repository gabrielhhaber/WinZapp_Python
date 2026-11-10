"""Tests for the persistent "selection mode" (issue #99).

Once a first selection exists on a surface — the messages list, the
conversations list or the forward dialog's contact list — plain Space keeps
selecting/deselecting instead of doing its normal job, and stops doing so the
moment the selection goes empty again. The mode is *derived* from the
selection set being non-empty (plus the setting), never stored in a flag, so
it cannot drift away from the set the mass actions clear behind it.

Also covered here: Esc clearing an active message selection before it closes
the conversation, and the unconditional bug fix that stops a selection leaking
into the next conversation (which ignores both settings on purpose).

And the four ways the derived mode can go wrong, each found in review: an id
left in the set by a message that was deleted underneath it, a filter hiding
every selected chat (or a search hiding every selected contact in the forward
dialog) while the mode stays on, plain Space being consumed by a toggle that
could not happen, and — the one the third pass found — the announcement and
the gate deriving the mode from two different predicates, which put the silent
dead key back by way of the fix for it.

ConversationsPanel is a wx.Panel and cannot be instantiated without a running
wx.App, so the methods under test are bound onto plain stubs carrying only the
attributes each one touches — same pattern as tests/test_bulk_selection.py and
tests/test_message_bookmarks.py.
"""

import ast
from pathlib import Path
from unittest.mock import Mock

import wx

from ui.conversations import (
    ConversationsPanel,
    toggle_jid_selection,
    visible_jid_selected,
)
from tests.god_modules import conversations_method_source
from ui.conversation_panel.reading_position import ReadingPositionMixin

ROOT = Path(__file__).resolve().parents[1]


class _FakeI18n:
    """The real pt-BR values for the keys these paths announce."""

    _STRINGS = {
        "selected": "Selecionado",
        "unselected": "Desmarcado",
        "all_selected": "Tudo selecionado",
        "all_unselected": "Tudo desmarcado",
        "selection_mode_on": "Modo de seleção ativado",
        "selection_mode_off": "Modo de seleção desativado",
    }

    def t(self, key):
        return self._STRINGS[key]


class _FakeMainWindow:
    def __init__(self, ui_settings=None):
        self.settings = {"user_interface": dict(ui_settings or {})}
        self.i18n = _FakeI18n()
        self.outputs = []
        self.add_chats_to_ui = Mock()

    def output(self, text, interrupt=False):
        self.outputs.append(text)


class _FakeList:
    """Just enough wx.ListCtrl for the two key handlers."""

    def __init__(self, focused=0, count=0):
        self._focused = focused
        self._count = count

    def GetFocusedItem(self):
        return self._focused

    def GetItemCount(self):
        return self._count

    def Focus(self, idx):
        self._focused = idx

    def Select(self, idx, on=True):
        pass

    def EnsureVisible(self, idx):
        pass


def _key_event(key, ctrl=False, shift=False):
    event = Mock()
    event.GetKeyCode.return_value = key
    event.ControlDown.return_value = ctrl
    event.ShiftDown.return_value = shift
    return event


def _msg(msg_id, msg_type="conversation", **message):
    return {
        "key": {"id": msg_id},
        "messageType": msg_type,
        "message": {msg_type: dict(message)} if message else {},
    }


def _separator():
    return {"_type": "unread_separator", "count": 1}


class _Stub(ReadingPositionMixin):
    """Minimal stand-in for ConversationsPanel."""

    _is_separator = ConversationsPanel._is_separator
    _selection_mode_enabled = ConversationsPanel._selection_mode_enabled
    _escape_clears_selection_enabled = ConversationsPanel._escape_clears_selection_enabled
    _selection_mode_announcement = ConversationsPanel._selection_mode_announcement
    _toggle_message_selection = ConversationsPanel._toggle_message_selection
    _space_toggles_playback = ConversationsPanel._space_toggles_playback
    _toggle_chat_selection = ConversationsPanel._toggle_chat_selection
    _chat_selection_visible = ConversationsPanel._chat_selection_visible
    _select_message_at = ConversationsPanel._select_message_at
    _select_chat_at = ConversationsPanel._select_chat_at
    _all_chat_jids = ConversationsPanel._all_chat_jids
    _all_selectable_message_ids = ConversationsPanel._all_selectable_message_ids
    _on_messages_list_key_down = ConversationsPanel._on_messages_list_key_down
    _on_conv_list_key_down = ConversationsPanel._on_conv_list_key_down
    _handle_chat_selection_key = ConversationsPanel._handle_chat_selection_key
    _repaint_chat_selection = ConversationsPanel._repaint_chat_selection
    _announce_chat_selected = ConversationsPanel._announce_chat_selected
    _reset_after_chat_cleared = ConversationsPanel._reset_after_chat_cleared
    _on_escape_conversation = ConversationsPanel._on_escape_conversation

    def __init__(self, sorted_messages=None, chats_list=None, ui_settings=None,
                 focused=0, video_viewer_dialog=False):
        self._sorted_messages = sorted_messages or []
        self.chats_list = chats_list or []
        self.selected_messages = set()
        self.selected_chats = set()
        self.main_window = _FakeMainWindow(ui_settings)
        self.messages_list = _FakeList(focused, len(self._sorted_messages))
        self.conversations_list = _FakeList(focused, len(self.chats_list))
        self.selection_sound = Mock()
        self._is_loading_more = False
        self._messages_offset = 0
        self._refresh_message_rows_by_ids = Mock()
        self._toggle_audio_message_playback = Mock()
        self._play_toggle_video_message = Mock()
        self._paste_from_messages_list = Mock(return_value=False)
        self._use_conversation_video_media_viewer_dialog = Mock(
            return_value=video_viewer_dialog)
        self.close_conversation = Mock()
        self._page_jump_size = Mock(return_value=15)


class TestSettingReaders:
    def test_space_selects_defaults_to_enabled(self):
        assert _Stub()._selection_mode_enabled() is True

    def test_space_selects_can_be_turned_off(self):
        stub = _Stub(ui_settings={"space_selects_in_selection_mode": False})
        assert stub._selection_mode_enabled() is False

    def test_escape_clears_defaults_to_enabled(self):
        assert _Stub()._escape_clears_selection_enabled() is True

    def test_escape_clears_can_be_turned_off(self):
        stub = _Stub(ui_settings={"escape_clears_selection": False})
        assert stub._escape_clears_selection_enabled() is False


class TestSelectionModeAnnouncement:
    def test_empty_to_non_empty_announces_the_mode_turning_on(self):
        stub = _Stub()
        assert stub._selection_mode_announcement("Selecionado", False, True) == (
            "Selecionado. Modo de seleção ativado"
        )

    def test_non_empty_to_empty_announces_the_mode_turning_off(self):
        stub = _Stub()
        assert stub._selection_mode_announcement("Tudo desmarcado", True, False) == (
            "Tudo desmarcado. Modo de seleção desativado"
        )

    def test_no_transition_leaves_the_base_text_alone(self):
        stub = _Stub()
        assert stub._selection_mode_announcement("Selecionado", True, True) == "Selecionado"
        assert stub._selection_mode_announcement("Desmarcado", False, False) == "Desmarcado"

    def test_setting_off_leaves_the_base_text_alone(self):
        stub = _Stub(ui_settings={"space_selects_in_selection_mode": False})
        assert stub._selection_mode_announcement("Selecionado", False, True) == "Selecionado"


class TestMessagesListPlainSpace:
    def test_with_no_selection_it_plays_the_focused_audio(self):
        stub = _Stub(sorted_messages=[_msg("A1", "audioMessage", seconds=7)])

        stub._on_messages_list_key_down(_key_event(wx.WXK_SPACE))

        stub._toggle_audio_message_playback.assert_called_once_with(
            stub._sorted_messages[0])
        assert stub.selected_messages == set()

    def test_with_a_selection_it_toggles_instead_of_playing(self):
        stub = _Stub(sorted_messages=[_msg("A1", "audioMessage", seconds=7),
                                      _msg("A2", "audioMessage", seconds=3)],
                     focused=1)
        stub.selected_messages.add("A1")

        stub._on_messages_list_key_down(_key_event(wx.WXK_SPACE))

        assert stub.selected_messages == {"A1", "A2"}
        stub._toggle_audio_message_playback.assert_not_called()

    def test_deselecting_the_last_one_ends_the_mode_out_loud(self):
        stub = _Stub(sorted_messages=[_msg("A1", "audioMessage", seconds=7)])
        stub.selected_messages.add("A1")

        stub._on_messages_list_key_down(_key_event(wx.WXK_SPACE))

        assert stub.selected_messages == set()
        assert stub.main_window.outputs == ["Desmarcado. Modo de seleção desativado"]

    def test_setting_off_keeps_space_on_playback_even_with_a_selection(self):
        stub = _Stub(sorted_messages=[_msg("A1", "audioMessage", seconds=7)],
                     ui_settings={"space_selects_in_selection_mode": False})
        stub.selected_messages.add("A1")

        stub._on_messages_list_key_down(_key_event(wx.WXK_SPACE))

        stub._toggle_audio_message_playback.assert_called_once()
        assert stub.selected_messages == {"A1"}

    def test_non_playable_message_with_no_selection_skips_the_key(self):
        stub = _Stub(sorted_messages=[_msg("A1", "conversation")])
        event = _key_event(wx.WXK_SPACE)

        stub._on_messages_list_key_down(event)

        event.Skip.assert_called_once()
        assert stub.selected_messages == set()

    def test_ctrl_space_still_toggles_with_nothing_selected(self):
        stub = _Stub(sorted_messages=[_msg("A1", "audioMessage", seconds=7)])

        stub._on_messages_list_key_down(_key_event(wx.WXK_SPACE, ctrl=True))

        assert stub.selected_messages == {"A1"}
        assert stub.main_window.outputs == ["Selecionado. Modo de seleção ativado"]


class TestSpaceTogglesPlayback:
    """Space stays strictly narrower than Enter — it never opens a window."""

    def test_separator_row_is_not_playable(self):
        stub = _Stub()
        assert stub._space_toggles_playback(_separator()) is False

    def test_audio_is_played(self):
        stub = _Stub()
        msg = _msg("A1", "audioMessage", seconds=4)
        assert stub._space_toggles_playback(msg) is True
        stub._toggle_audio_message_playback.assert_called_once_with(msg)

    def test_classic_mode_video_is_played(self):
        stub = _Stub()
        msg = _msg("V1", "videoMessage", seconds=10)
        assert stub._space_toggles_playback(msg) is True
        stub._play_toggle_video_message.assert_called_once_with(msg)

    def test_gif_has_no_audio_track_so_space_does_nothing(self):
        stub = _Stub()
        msg = _msg("V1", "videoMessage", gifPlayback=True)
        assert stub._space_toggles_playback(msg) is False
        stub._play_toggle_video_message.assert_not_called()

    def test_video_in_media_viewer_mode_is_left_to_enter(self):
        stub = _Stub(video_viewer_dialog=True)
        msg = _msg("V1", "videoMessage", seconds=10)
        assert stub._space_toggles_playback(msg) is False
        stub._play_toggle_video_message.assert_not_called()

    def test_documents_images_contacts_and_text_never_open_from_space(self):
        stub = _Stub()
        for msg_type in ("documentMessage", "imageMessage", "contactMessage",
                         "locationMessage", "conversation", "extendedTextMessage"):
            assert stub._space_toggles_playback(_msg("X", msg_type)) is False


class TestConversationsListPlainSpace:
    def test_with_a_selection_it_selects_the_focused_chat(self):
        stub = _Stub(chats_list=[{"remoteJid": "a@s.whatsapp.net"},
                                 {"remoteJid": "b@s.whatsapp.net"}],
                     focused=1)
        stub.selected_chats.add("a@s.whatsapp.net")
        event = _key_event(wx.WXK_SPACE)

        stub._on_conv_list_key_down(event)

        assert stub.selected_chats == {"a@s.whatsapp.net", "b@s.whatsapp.net"}
        event.Skip.assert_not_called()

    def test_with_no_selection_it_keeps_falling_through(self):
        stub = _Stub(chats_list=[{"remoteJid": "a@s.whatsapp.net"}])
        event = _key_event(wx.WXK_SPACE)

        stub._on_conv_list_key_down(event)

        assert stub.selected_chats == set()
        event.Skip.assert_called_once()

    def test_setting_off_keeps_falling_through_even_with_a_selection(self):
        stub = _Stub(chats_list=[{"remoteJid": "a@s.whatsapp.net"},
                                 {"remoteJid": "b@s.whatsapp.net"}],
                     focused=1,
                     ui_settings={"space_selects_in_selection_mode": False})
        stub.selected_chats.add("a@s.whatsapp.net")
        event = _key_event(wx.WXK_SPACE)

        stub._on_conv_list_key_down(event)

        assert stub.selected_chats == {"a@s.whatsapp.net"}
        event.Skip.assert_called_once()

    def test_ctrl_space_goes_through_the_same_toggle(self):
        stub = _Stub(chats_list=[{"remoteJid": "a@s.whatsapp.net"}])

        stub._on_conv_list_key_down(_key_event(wx.WXK_SPACE, ctrl=True))

        assert stub.selected_chats == {"a@s.whatsapp.net"}
        stub.main_window.add_chats_to_ui.assert_called_once()
        assert stub.main_window.outputs == ["Selecionado. Modo de seleção ativado"]


class TestADeletedMessageLeavesTheSelection:
    """The mode is derived from selected_messages being non-empty, so an id
    left behind by a message that no longer exists keeps plain Space silently
    in selecting mode with nothing selected — and nothing ever announced the
    mode turning on. Reachable with no user action at all, through
    _mirror_remote_deletions()' 60s poll."""

    def test_a_deletion_drops_the_id_from_the_selection(self):
        from tests.test_phone_side_sync import _RemovalStub, _msg as _removal_msg

        panel = _RemovalStub([_removal_msg("A1"), _removal_msg("A2")])
        panel.selected_messages = {"A1", "A2"}

        panel.remove_messages_by_id({"A1"})

        assert panel.selected_messages == {"A2"}

    def test_removing_the_only_selected_one_empties_the_set(self):
        from tests.test_phone_side_sync import _RemovalStub, _msg as _removal_msg

        panel = _RemovalStub([_removal_msg("A1"), _removal_msg("A2")])
        panel.selected_messages = {"A1"}

        panel.remove_messages_by_id({"A1"})

        assert panel.selected_messages == set()

    def test_an_id_with_no_row_on_screen_leaves_the_selection_too(self):
        # The ordinary shape of a mirrored phone-side deletion: the ids are
        # routinely not rendered rows (paginated out, or non-displayable).
        from tests.test_phone_side_sync import _RemovalStub, _msg as _removal_msg

        panel = _RemovalStub([_removal_msg("A1")])
        panel.selected_messages = {"A1", "gone"}

        panel.remove_messages_by_id({"gone"})

        assert panel.selected_messages == {"A1"}


class TestAFilteredChatListTakesSpaceBack:
    """chats_list is reassigned to the filtered view whenever the conversation
    filter or the search box changes, and nothing clears selected_chats. With
    every selected chat hidden the user sees no selection and was told nothing,
    so plain Space must be the native key again rather than quietly adding a
    third chat to a selection they believe does not exist."""

    def test_a_visible_selected_chat_keeps_the_mode_on(self):
        stub = _Stub(chats_list=[{"remoteJid": "a@s.whatsapp.net"},
                                 {"remoteJid": "b@s.whatsapp.net"}])
        stub.selected_chats.add("a@s.whatsapp.net")

        assert stub._chat_selection_visible() is True

    def test_a_selection_hidden_by_the_filter_does_not(self):
        stub = _Stub(chats_list=[{"remoteJid": "c@s.whatsapp.net"}])
        stub.selected_chats.add("a@s.whatsapp.net")

        assert stub._chat_selection_visible() is False

    def test_an_empty_selection_is_not_visible_either(self):
        stub = _Stub(chats_list=[{"remoteJid": "a@s.whatsapp.net"}])

        assert stub._chat_selection_visible() is False

    def test_space_falls_through_while_every_selected_chat_is_hidden(self):
        stub = _Stub(chats_list=[{"remoteJid": "c@s.whatsapp.net"}])
        stub.selected_chats.add("a@s.whatsapp.net")
        event = _key_event(wx.WXK_SPACE)

        stub._on_conv_list_key_down(event)

        event.Skip.assert_called_once()
        # Untouched: the mass actions still act on everything the user picked,
        # filtered out of the list or not.
        assert stub.selected_chats == {"a@s.whatsapp.net"}

    def test_ctrl_space_is_unaffected_by_the_filter(self):
        stub = _Stub(chats_list=[{"remoteJid": "c@s.whatsapp.net"}])
        stub.selected_chats.add("a@s.whatsapp.net")

        stub._on_conv_list_key_down(_key_event(wx.WXK_SPACE, ctrl=True))

        assert stub.selected_chats == {"a@s.whatsapp.net", "c@s.whatsapp.net"}


class TestTheSpokenModeAgreesWithTheGate:
    """Gating plain Space on _chat_selection_visible() while the announcements
    still read the raw set put the two out of step, and the gap was the silent
    dead key all over again: with one selected chat hidden by the filter,
    nothing ever said the mode turned on, nothing said it turned off, and the
    Space that followed did nothing at all. Both booleans now come from the
    same predicate as the gate."""

    def _filtered_stub(self):
        # "a" is selected and filtered out of the list; "b" and "c" are the
        # only rows the user can see.
        stub = _Stub(chats_list=[{"remoteJid": "b@s.whatsapp.net"},
                                 {"remoteJid": "c@s.whatsapp.net"}])
        stub.selected_chats.add("a@s.whatsapp.net")
        return stub

    def test_ctrl_space_on_a_visible_chat_announces_the_mode_turning_on(self):
        stub = self._filtered_stub()

        stub._on_conv_list_key_down(_key_event(wx.WXK_SPACE, ctrl=True))

        assert stub.main_window.outputs == ["Selecionado. Modo de seleção ativado"]

    def test_the_whole_sequence_never_leaves_space_dead(self):
        stub = self._filtered_stub()

        stub._on_conv_list_key_down(_key_event(wx.WXK_SPACE, ctrl=True))  # select b
        second = _key_event(wx.WXK_SPACE)
        stub._on_conv_list_key_down(second)                               # deselect b

        assert stub.main_window.outputs == [
            "Selecionado. Modo de seleção ativado",
            "Desmarcado. Modo de seleção desativado",
        ]
        second.Skip.assert_not_called()

        # Space on c now hands the key back to the control — the native
        # behaviour the mode-off line just announced, rather than silence.
        stub.conversations_list.Focus(1)
        third = _key_event(wx.WXK_SPACE)
        stub._on_conv_list_key_down(third)

        third.Skip.assert_called_once()
        assert stub.selected_chats == {"a@s.whatsapp.net"}

    def test_shift_down_announces_it_too(self):
        stub = self._filtered_stub()
        stub.conversations_list.Focus(-1)

        stub._on_conv_list_key_down(_key_event(wx.WXK_DOWN, shift=True))

        assert stub.main_window.outputs == ["Selecionado. Modo de seleção ativado"]

    def test_select_all_announces_it_too(self):
        stub = self._filtered_stub()

        stub._on_conv_list_key_down(_key_event(wx.WXK_SPACE, ctrl=True, shift=True))

        assert stub.main_window.outputs == ["Tudo selecionado. Modo de seleção ativado"]

    def test_deselecting_the_last_visible_one_ends_the_mode(self):
        # The hidden "a" is still selected afterwards — the mass actions act
        # on it exactly as before — but the mode the user perceives is over,
        # and that is what has to be said out loud, or the next Space goes
        # nowhere with no explanation.
        stub = self._filtered_stub()
        stub.selected_chats.add("b@s.whatsapp.net")

        stub._on_conv_list_key_down(_key_event(wx.WXK_SPACE))

        assert stub.main_window.outputs == ["Desmarcado. Modo de seleção desativado"]
        assert stub.selected_chats == {"a@s.whatsapp.net"}

    def test_an_unfiltered_list_still_reads_the_same(self):
        stub = _Stub(chats_list=[{"remoteJid": "a@s.whatsapp.net"},
                                 {"remoteJid": "b@s.whatsapp.net"}],
                     focused=1)
        stub.selected_chats.add("a@s.whatsapp.net")

        stub._on_conv_list_key_down(_key_event(wx.WXK_SPACE))

        # Nothing is hidden, so the mode was already on and stays on: no
        # transition line, exactly as before the gate existed.
        assert stub.main_window.outputs == ["Selecionado"]


class TestPlainSpaceIsNeverSwallowedForNothing:
    """Space is the one key people press by accident, so consuming it with no
    sound, no speech and no effect is worse than doing nothing with it."""

    def test_the_unread_separator_hands_the_key_back(self):
        stub = _Stub(sorted_messages=[_msg("A1", "audioMessage", seconds=3),
                                      _separator()],
                     focused=1)
        stub.selected_messages.add("A1")
        event = _key_event(wx.WXK_SPACE)

        stub._on_messages_list_key_down(event)

        event.Skip.assert_called_once()
        assert stub.selected_messages == {"A1"}

    def test_a_message_with_no_id_hands_the_key_back(self):
        stub = _Stub(sorted_messages=[_msg("A1"), _msg("")], focused=1)
        stub.selected_messages.add("A1")
        event = _key_event(wx.WXK_SPACE)

        stub._on_messages_list_key_down(event)

        event.Skip.assert_called_once()
        assert stub.selected_messages == {"A1"}

    def test_a_refused_toggle_does_not_fall_through_to_playback(self):
        # The fork itself: in selection mode a row the toggle refuses hands
        # the key back rather than playing it. The separator above cannot pin
        # this — it is unplayable either way — so it takes an audio message
        # with no id, the one shape that reaches both branches.
        stub = _Stub(sorted_messages=[_msg("A1", "audioMessage", seconds=3),
                                      _msg("", "audioMessage", seconds=9)],
                     focused=1)
        stub.selected_messages.add("A1")
        event = _key_event(wx.WXK_SPACE)

        stub._on_messages_list_key_down(event)

        event.Skip.assert_called_once()
        stub._toggle_audio_message_playback.assert_not_called()
        stub._play_toggle_video_message.assert_not_called()
        assert stub.selected_messages == {"A1"}

    def test_the_same_row_plays_with_nothing_selected(self):
        # ...and the fork's other side is unchanged: outside selection mode
        # that very row still plays, which is what makes the branch above a
        # real fork rather than a no-op.
        stub = _Stub(sorted_messages=[_msg("", "audioMessage", seconds=9)])

        stub._on_messages_list_key_down(_key_event(wx.WXK_SPACE))

        stub._toggle_audio_message_playback.assert_called_once_with(
            stub._sorted_messages[0])

    def test_a_real_toggle_still_consumes_it(self):
        stub = _Stub(sorted_messages=[_msg("A1"), _msg("A2")], focused=1)
        stub.selected_messages.add("A1")
        event = _key_event(wx.WXK_SPACE)

        stub._on_messages_list_key_down(event)

        event.Skip.assert_not_called()
        assert stub.selected_messages == {"A1", "A2"}

    def test_no_focused_chat_hands_the_key_back(self):
        stub = _Stub(chats_list=[{"remoteJid": "a@s.whatsapp.net"}], focused=-1)
        stub.selected_chats.add("a@s.whatsapp.net")
        event = _key_event(wx.WXK_SPACE)

        stub._on_conv_list_key_down(event)

        event.Skip.assert_called_once()

    def test_a_chat_row_with_no_jid_hands_the_key_back(self):
        stub = _Stub(chats_list=[{"remoteJid": "a@s.whatsapp.net"}, {}], focused=1)
        stub.selected_chats.add("a@s.whatsapp.net")
        event = _key_event(wx.WXK_SPACE)

        stub._on_conv_list_key_down(event)

        event.Skip.assert_called_once()

    def test_ctrl_space_keeps_swallowing_it(self):
        # Out of scope for the fix — Ctrl+Space is deliberate, so a user who
        # presses it on the separator is not surprised by silence.
        stub = _Stub(sorted_messages=[_msg("A1"), _separator()], focused=1)
        event = _key_event(wx.WXK_SPACE, ctrl=True)

        stub._on_messages_list_key_down(event)

        event.Skip.assert_not_called()
        assert stub.selected_messages == set()


class TestForwardDialogSelectionMode:
    """The third surface. Its toggle lives in a closure inside
    _on_menu_forward() (the selection set is local to the dialog), so
    only the pure bookkeeping is reachable — the closure keeps the row repaint,
    the sound and the announcement, none of which the set alone decides."""

    def test_the_first_selection_turns_the_mode_on(self):
        selected = set()

        now_selected, was_active = toggle_jid_selection(selected, "a@s.whatsapp.net")

        assert (now_selected, was_active) == (True, False)
        assert selected == {"a@s.whatsapp.net"}

    def test_deselecting_the_last_one_turns_it_off(self):
        selected = {"a@s.whatsapp.net"}

        now_selected, was_active = toggle_jid_selection(selected, "a@s.whatsapp.net")

        assert (now_selected, was_active) == (False, True)
        assert selected == set()

    def test_a_middle_toggle_reports_no_transition(self):
        selected = {"a@s.whatsapp.net", "b@s.whatsapp.net"}

        now_selected, was_active = toggle_jid_selection(selected, "c@s.whatsapp.net")

        assert (now_selected, was_active) == (True, True)
        assert selected == {"a@s.whatsapp.net", "b@s.whatsapp.net", "c@s.whatsapp.net"}

    def test_the_announcement_names_the_mode_the_same_way(self):
        # What the closure does with the two booleans, against the shared
        # helper the other two surfaces use.
        stub = _Stub()
        selected = set()

        now_selected, was_active = toggle_jid_selection(selected, "a@s.whatsapp.net")
        assert stub._selection_mode_announcement(
            "Selecionado", was_active, bool(selected)
        ) == "Selecionado. Modo de seleção ativado"

        now_selected, was_active = toggle_jid_selection(selected, "a@s.whatsapp.net")
        assert stub._selection_mode_announcement(
            "Desmarcado", was_active, bool(selected)
        ) == "Desmarcado. Modo de seleção desativado"

    def test_the_dialog_actually_goes_through_it(self):
        # The extraction is only worth anything while the closure still calls
        # it — the dialog itself builds real widgets and cannot be run here.
        # Matched on the call alone: the local set can be renamed and the call
        # wrapped across two lines without any of that changing behaviour, and
        # the only thing this really buys is catching the helper becoming dead
        # code.
        assert "toggle_jid_selection(" in _method_source(
            "ConversationsPanel", "_on_menu_forward")


class TestTheForwardDialogSearchHidesTheSelectionToo:
    """The dialog has its own search box, which rebuilds the rows while
    selected_jids survives untouched — the same trap _chat_selection_visible()
    answers on the chat list, on the one surface that actually sends. Gating
    plain Space on the raw set let a user select a contact, type a query that
    hides it, press Space on another one, and forward the message to both."""

    def test_a_listed_selected_jid_keeps_the_mode_on(self):
        assert visible_jid_selected(
            {"a@s.whatsapp.net"},
            ["a@s.whatsapp.net", "b@s.whatsapp.net"]) is True

    def test_a_selection_the_search_hid_does_not(self):
        assert visible_jid_selected(
            {"a@s.whatsapp.net"},
            ["b@s.whatsapp.net", "c@s.whatsapp.net"]) is False

    def test_an_empty_selection_is_not_visible_either(self):
        assert visible_jid_selected(set(), ["a@s.whatsapp.net"]) is False

    def test_an_empty_result_list_is_not_visible_either(self):
        assert visible_jid_selected({"a@s.whatsapp.net"}, []) is False

    def test_it_takes_a_generator_and_stops_at_the_first_hit(self):
        # The dialog passes a generator over the rows currently in the
        # ListBox, so it must not require a materialised sequence — and must
        # not read past the row that answers the question.
        read = []

        def rows():
            for jid in ("a@s.whatsapp.net", "b@s.whatsapp.net"):
                read.append(jid)
                yield jid

        assert visible_jid_selected({"a@s.whatsapp.net"}, rows()) is True
        assert read == ["a@s.whatsapp.net"]

    def test_the_dialog_gates_plain_space_on_it(self):
        # Same reasoning as test_the_dialog_actually_goes_through_it above:
        # the closure builds real widgets, so only its source is reachable.
        source = _method_source("ConversationsPanel", "_on_menu_forward")
        assert "visible_jid_selected(" in source
        # The gate and the announcements must read the one predicate, never
        # the raw set — that disagreement is the defect this replaced.
        assert "bool(selected_jids)" not in source

    def test_a_refused_toggle_hands_the_key_back(self):
        # _toggle_at() returns False on a row with no jid (an empty search
        # result focuses nothing), and the plain-Space branch must fall
        # through to Skip() rather than swallow the key in silence.
        source = _method_source("ConversationsPanel", "_on_menu_forward")
        assert "if _toggle_at(focus):" in source


class TestEscape:
    def test_first_escape_clears_the_selection_instead_of_closing(self):
        stub = _Stub(sorted_messages=[_msg("A1")])
        stub.selected_messages.add("A1")

        stub._on_escape_conversation(None)

        assert stub.selected_messages == set()
        stub.close_conversation.assert_not_called()
        stub._refresh_message_rows_by_ids.assert_called_once_with(["A1"])
        assert stub.main_window.outputs == ["Tudo desmarcado. Modo de seleção desativado"]

    def test_second_escape_closes_the_conversation(self):
        stub = _Stub(sorted_messages=[_msg("A1")])
        stub.selected_messages.add("A1")

        stub._on_escape_conversation(None)
        stub._on_escape_conversation(None)

        stub.close_conversation.assert_called_once()

    def test_setting_off_closes_immediately_even_with_a_selection(self):
        stub = _Stub(sorted_messages=[_msg("A1")],
                     ui_settings={"escape_clears_selection": False})
        stub.selected_messages.add("A1")

        stub._on_escape_conversation(None)

        stub.close_conversation.assert_called_once()
        assert stub.selected_messages == {"A1"}

    def test_the_mention_popup_still_wins_the_first_escape(self):
        # It is an overlay right in front of the user, and the close path
        # already answers Esc by just dismissing it.
        stub = _Stub(sorted_messages=[_msg("A1")])
        stub.selected_messages.add("A1")
        stub._mention_panel = Mock()
        stub._mention_panel.IsShown.return_value = True

        stub._on_escape_conversation(None)

        stub.close_conversation.assert_called_once()
        assert stub.selected_messages == {"A1"}

    def test_a_chat_selection_does_not_change_what_escape_does(self):
        # Scoped to the message selection: Esc inside a conversation must not
        # start answering to what is selected in the chat list behind it.
        stub = _Stub()
        stub.selected_chats.add("a@s.whatsapp.net")

        stub._on_escape_conversation(None)

        stub.close_conversation.assert_called_once()
        assert stub.selected_chats == {"a@s.whatsapp.net"}


def _method_source(class_name: str, method_name: str) -> str:
    if class_name == "ConversationsPanel":
        return conversations_method_source(method_name)
    path = ROOT / "client" / "ui" / "conversations.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    lines = source.splitlines()
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == method_name:
                    return "\n".join(lines[item.lineno - 1:item.end_lineno])
    raise AssertionError(f"{class_name}.{method_name} not found")


class TestSelectionDoesNotSurviveTheConversation:
    """Unconditional — a user who unticked the Esc option would otherwise
    still reopen a conversation with its old rows still selected."""

    def test_closing_clears_the_message_selection(self):
        from tests.test_close_conversation_for_panel_switch import _Stub as _CloseStub

        stub = _CloseStub({"remoteJid": "5511999999999@s.whatsapp.net"})
        stub.selected_messages = {"A1", "A2"}

        stub.close_conversation_for_panel_switch()

        assert stub.selected_messages == set()

    def test_switching_straight_to_another_chat_clears_it_too(self):
        # navigate_to_conversation() starts threads and drives real widgets, so
        # it cannot be bound onto a stub — the clear is pinned at the source
        # seam instead, the same way test_media_transfer_progress_wiring.py
        # pins _hide_media_transfer_gauge() in this very method.
        assert "self.selected_messages.clear()" in _method_source(
            "ConversationsPanel", "navigate_to_conversation")

    def test_the_close_path_carries_the_same_clear(self):
        assert "self.selected_messages.clear()" in _method_source(
            "ConversationsPanel", "_close_conversation_core")
