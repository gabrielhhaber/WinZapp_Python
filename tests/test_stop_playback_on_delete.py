"""Regression test: deleting a message that is currently playing (audio or
in-app video) must stop that playback, not just remove the row.

Before this fix, only the live "delete for everyone" detection path
(on_message_revoked, see tests/test_remote_revoke.py) stopped playback.
Every other way a message's row disappears — "apagar para mim", "apagar
para todos" (removed locally the instant the user confirms, before the
server even round-trips), mass-delete, and the periodic poll that mirrors
phone-side deletions (MainWindow._mirror_remote_deletions ->
remove_messages_by_id) — left an actively-playing audio/video message with
no row left in the UI to stop it from, so it kept playing indefinitely.

The fix centralizes the check in _stop_playback_for_removed_messages(),
called from both on_message_revoked() and remove_messages_by_id() (the
method shared by every removal path above).

ConversationsPanel is a wx.Panel and cannot be instantiated without a
running wx.App, so the methods under test are exercised as plain functions
against a small stub — same approach as tests/test_message_bookmarks.py.
"""

import pytest

from ui.conversations import ConversationsPanel
import app_paths

@pytest.fixture(autouse=True)
def _account_scope(tmp_path, monkeypatch):
    """Deleting messages now also cleans up kept noise-reduction originals,
    which goes through app_paths.data_path() - and that refuses to answer
    without an active account (the guard that keeps one account's files out
    of another's folder). The real flows under test always run with an
    account open, so the tests set one - but scoped: the data root is a
    per-test tmp dir so cleanup writes/deletes (e.g. voice_messages/
    <id>.resend.wav) can never reach the repo's real data/ tree, and the
    previously active account is restored afterwards so this fixture is
    transparent to any surrounding account state."""
    previous_account = app_paths.active_account_id()
    monkeypatch.setattr(app_paths, "_data_root", lambda: str(tmp_path))
    app_paths.set_active_account("acct-under-test")
    yield tmp_path
    app_paths.set_active_account(previous_account)



class _FakeMessagesList:
    def __init__(self, focused_item=-1, item_count=0):
        self._focused_item = focused_item
        self._item_count = item_count
        self.deleted = []

    def GetFocusedItem(self):
        return self._focused_item

    def GetItemCount(self):
        return self._item_count

    def DeleteItem(self, idx):
        self.deleted.append(idx)
        self._item_count = max(0, self._item_count - 1)

    def Focus(self, idx):
        self._focused_item = idx

    def Select(self, idx, on=True):
        pass

    def EnsureVisible(self, idx):
        pass


class _FakeDB:
    def delete_message(self, jid, msg_id):
        pass


class _FakeMainWindow:
    def __init__(self):
        self.db = _FakeDB()
        self.recompute_calls = []
        self.set_chats_calls = 0

    def _recompute_chat_last_message(self, jid):
        self.recompute_calls.append(jid)

    def _schedule_set_chats(self):
        self.set_chats_calls += 1


class _Stub:
    """Minimal stand-in for ConversationsPanel.

    _stop_audio/_hide_audio_controls/_hide_all_media_controls are
    overridden with simple recorders — the real implementations drive a
    web of wx widgets (buttons, BASS audio streams, the ffmpeg-backed video
    player) that are irrelevant to what's actually under test here: whether
    _stop_playback_for_removed_messages() decides to call them at all.
    """

    _stop_playback_for_removed_messages = ConversationsPanel._stop_playback_for_removed_messages
    on_message_revoked = ConversationsPanel.on_message_revoked
    remove_messages_by_id = ConversationsPanel.remove_messages_by_id
    _focused_msg_id = ConversationsPanel._focused_msg_id
    _is_separator = ConversationsPanel._is_separator

    def __init__(self, sorted_messages=None, current_audio_id=None, audio_stream=None,
                 current_video_msg_id=None, conversation=None, focused_item=-1):
        self._sorted_messages = sorted_messages if sorted_messages is not None else []
        self._all_sorted_messages = list(self._sorted_messages)
        self._unread_sep_idx = -1
        self._messages_offset = 0
        # A removed id also leaves the selection — see
        # tests/test_selection_mode.py for why that matters.
        self.selected_messages = set()
        self._current_audio_id = current_audio_id
        self._audio_stream = audio_stream
        self._current_video_msg_id = current_video_msg_id
        self.conversation = conversation
        self.main_window = _FakeMainWindow()
        self.messages_list = _FakeMessagesList(
            focused_item=focused_item, item_count=len(self._sorted_messages)
        )
        self.stop_audio_calls = 0
        self.hide_audio_calls = 0
        self.hide_all_media_calls = 0
        self.refresh_calls = 0
        self.repainted = []
        self.repaint_ok = True

    def _repaint_message_rows(self, msg_ids):
        # Stands in for the real per-row repaint (which needs the whole
        # rendering stack); repaint_ok drives the "couldn't repaint" branch.
        self.repainted.append(sorted(i for i in msg_ids if i))
        return self.repaint_ok

    def _stop_audio(self):
        self.stop_audio_calls += 1
        self._audio_stream = None
        self._current_audio_id = None

    def _hide_audio_controls(self):
        self.hide_audio_calls += 1

    def _hide_all_media_controls(self):
        self.hide_all_media_calls += 1
        self._current_video_msg_id = None

    def refresh_active_conversation_messages(self):
        self.refresh_calls += 1


def _msg(mid):
    return {"key": {"id": mid, "fromMe": False}, "messageType": "conversation"}


class TestStopPlaybackForRemovedMessages:
    def test_stops_playing_audio_matching_the_id(self):
        s = _Stub(current_audio_id="A", audio_stream=object())

        s._stop_playback_for_removed_messages({"A"})

        assert s.stop_audio_calls == 1
        assert s.hide_audio_calls == 1
        assert s.hide_all_media_calls == 0

    def test_leaves_unrelated_audio_alone(self):
        s = _Stub(current_audio_id="A", audio_stream=object())

        s._stop_playback_for_removed_messages({"B"})

        assert s.stop_audio_calls == 0
        assert s._current_audio_id == "A"

    def test_no_op_when_audio_id_matches_but_nothing_is_actually_streaming(self):
        # _current_audio_id can linger after a natural stop; only an id AND
        # a live stream together mean audio is actually playing.
        s = _Stub(current_audio_id="A", audio_stream=None)

        s._stop_playback_for_removed_messages({"A"})

        assert s.stop_audio_calls == 0

    def test_stops_playing_video_matching_the_id(self):
        s = _Stub(current_video_msg_id="V")

        s._stop_playback_for_removed_messages({"V"})

        assert s.hide_all_media_calls == 1

    def test_leaves_unrelated_video_alone(self):
        s = _Stub(current_video_msg_id="V")

        s._stop_playback_for_removed_messages({"other"})

        assert s.hide_all_media_calls == 0
        assert s._current_video_msg_id == "V"

    def test_stops_both_when_both_match(self):
        s = _Stub(current_audio_id="A", audio_stream=object(), current_video_msg_id="A")

        s._stop_playback_for_removed_messages({"A"})

        assert s.stop_audio_calls == 1
        assert s.hide_all_media_calls == 1


class TestOnMessageRevokedStopsPlayback:
    def test_stops_playing_audio_even_if_not_focused(self):
        """This is the exact gap _stop_playback_for_removed_messages closes:
        on_message_revoked used to match audio by id (fine) but only hid
        media controls — and only stopped video at all — when the revoked
        message also happened to be the focused row."""
        s = _Stub(
            sorted_messages=[_msg("A"), _msg("B")],
            current_audio_id="B",
            audio_stream=object(),
            focused_item=0,  # focused row is A, not the playing message B
        )

        s.on_message_revoked("B")

        assert s.stop_audio_calls == 1

    def test_stops_playing_video_even_if_not_focused(self):
        s = _Stub(
            sorted_messages=[_msg("A"), _msg("B")],
            current_video_msg_id="B",
            focused_item=0,
        )

        s.on_message_revoked("B")

        assert s.hide_all_media_calls == 1

    def test_repaints_only_the_revoked_row(self):
        """A revoke swaps one row's text for "Mensagem apagada" and moves
        nothing, so re-rendering every row of the conversation for it was
        disproportionate."""
        s = _Stub(sorted_messages=[_msg("A")])
        s.on_message_revoked("A")
        assert s.repainted == [["A"]]
        assert s.refresh_calls == 0

    def test_falls_back_to_the_full_refresh_when_the_row_cannot_be_repainted(self):
        s = _Stub(sorted_messages=[_msg("A")])
        s.repaint_ok = False
        s.on_message_revoked("A")
        assert s.refresh_calls == 1


class TestRemoveMessagesByIdStopsPlayback:
    def test_deleting_the_playing_audio_message_stops_it(self):
        """Covers _on_menu_delete_message (apagar para mim/para todos) and
        _on_mass_delete_messages, both of which call remove_messages_by_id
        directly without ever checking playback state themselves."""
        s = _Stub(
            sorted_messages=[_msg("A"), _msg("B")],
            current_audio_id="B",
            audio_stream=object(),
        )

        s.remove_messages_by_id({"B"})

        assert s.stop_audio_calls == 1
        assert [m["key"]["id"] for m in s._sorted_messages] == ["A"]

    def test_deleting_the_playing_video_message_stops_it(self):
        s = _Stub(
            sorted_messages=[_msg("A"), _msg("B")],
            current_video_msg_id="B",
        )

        s.remove_messages_by_id({"B"})

        assert s.hide_all_media_calls == 1

    def test_a_background_playing_message_scrolled_out_of_view_still_stops(self):
        """Audio is allowed to keep playing after the user scrolls/selects
        elsewhere, so its row may already be outside _sorted_messages —
        that must not block the stop-playback check, only the row removal."""
        s = _Stub(
            sorted_messages=[_msg("A")],  # "B" already paginated out
            current_audio_id="B",
            audio_stream=object(),
        )

        s.remove_messages_by_id({"B"})

        assert s.stop_audio_calls == 1

    def test_deleting_an_unrelated_message_does_not_touch_playback(self):
        s = _Stub(
            sorted_messages=[_msg("A"), _msg("B")],
            current_audio_id="B",
            audio_stream=object(),
        )

        s.remove_messages_by_id({"A"})

        assert s.stop_audio_calls == 0
        assert s._current_audio_id == "B"
