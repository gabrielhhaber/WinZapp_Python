"""Tests for StatusPanel (client/status_panel.py) — the Alt+5 tab.

Covers two of the reported bugs:

1. Pressing Space on the status list while viewing "status 3 de 5" reset
   the position back to "1 de 5". _on_status_contact_selected() (the
   handler that actually fires from Select()) used to reset
   _current_status_idx to 0 unconditionally on every selection event, even
   when re-selecting the SAME contact the user was already viewing deeper
   into.

2. The video "play/pause" button, and _show_current_status()'s handling of
   switching away from a playing video (must stop it) — verified here via
   button-visibility decisions (is_video -> _play_pause_btn shown) and the
   fake VideoPlayer's stop() call count.

Also covers the new copy-status-text computation (feature request #5): the
text handed to the clipboard must be the actual content — the full text for
a text status, or just the caption (not the "Foto:"/"Vídeo:" label prefix
used in the announced label) for a media status.

StatusPanel is a wx.Panel and cannot be instantiated without a running
wx.App — _show_current_status()/_on_status_contact_selected() are exercised
against a small stub with fake widgets recording Show/Hide/SetLabel calls,
same approach as tests/test_message_bookmarks.py.
"""

import pytest
import wx

import main as main_module
from main import MainWindow
from status_panel import StatusPanel, _status_content_label, _status_media_save_info


class _FakeI18n:
    _STRINGS = {
        "status_of": "Status {current} de {total}",
        "photo": "Foto",
        "video": "Vídeo",
        "status_like": "Curtir",
        "status_unlike": "Descurtir",
        "message_type_audio": "Áudio",
        "document": "Documento",
        "sticker": "Figurinha",
        "contact_message": "Contato: {name}",
        "unknown_contact": "Contato sem nome",
        "notif_unsupported": "Mensagem não suportada",
        "status_recent_updates": "Recentes",
        "status_viewed_updates": "Vistos",
        "my_status": "Meu status",
        "my_status_update": "toque para ver",
        "my_status_none": "toque para adicionar",
    }

    def t(self, key):
        return self._STRINGS.get(key, f"[{key}]")


class _FakeWidget:
    def __init__(self):
        self.shown = False
        self.enabled = True
        self.label = ""

    def Show(self, show=True):
        self.shown = bool(show)

    def Hide(self):
        self.shown = False

    def IsShown(self):
        return self.shown

    def Enable(self, enable=True):
        self.enabled = bool(enable)

    def Disable(self):
        self.enabled = False

    def SetLabel(self, text):
        self.label = text

    def SetMinSize(self, size):
        pass


class _FakeTextCtrl(_FakeWidget):
    """Stands in for _reply_field: a wx.TextCtrl, which _show_current_status()
    and _on_reply_field_text_changed() read via GetValue()."""

    def __init__(self, value=""):
        super().__init__()
        self._value = value
        self.set_focus_calls = 0

    def GetValue(self):
        return self._value

    def SetValue(self, value):
        self._value = value

    def SetFocus(self):
        self.set_focus_calls += 1


class _FakeMainWindow:
    def __init__(self, contact_names=None, send_text_result=True,
                 send_reaction_result=True, settings=None):
        self.i18n = _FakeI18n()
        self.outputs = []
        self._contact_names = contact_names or {}
        self.chats = {}
        self._status_updates = {}
        self.app_name = "WinZapp"
        self._send_text_result = send_text_result
        self.send_text_calls = []
        self._send_reaction_result = send_reaction_result
        self.send_reaction_calls = []
        self.settings = settings if settings is not None else {}
        self.save_settings_calls = 0

    def _resolve_contact_name(self, chat):
        return self._contact_names.get(chat.get("remoteJid", ""))

    def _is_self_jid(self, jid):
        return False

    def send_text_message(self, remote_jid, text, quoted=None):
        self.send_text_calls.append((remote_jid, text, quoted))
        return self._send_text_result

    def send_reaction(self, remote_jid, msg_key, emoji):
        self.send_reaction_calls.append((remote_jid, msg_key, emoji))
        return self._send_reaction_result

    def save_settings(self):
        self.save_settings_calls += 1

    def output(self, text, interrupt=False):
        self.outputs.append(text)


class _FakeVideoPlayer:
    def __init__(self):
        self.stop_calls = 0
        self.toggle_pause_calls = 0
        self.is_playing = False
        self.is_paused  = False

    def stop(self):
        self.stop_calls += 1
        self.is_playing = False
        self.is_paused  = False

    def toggle_pause(self):
        self.toggle_pause_calls += 1
        self.is_paused = not self.is_paused


class _FakeStatusList:
    def __init__(self, focused=-1):
        self._focused = focused
        self.shown = True
        self.select_calls = []
        self.items = []
        self.focus_calls = []

    def GetFocusedItem(self):
        return self._focused

    def Select(self, idx):
        self.select_calls.append(idx)

    def SetFocus(self):
        pass

    def Show(self, show=True):
        self.shown = bool(show)

    def Hide(self):
        self.shown = False

    def IsShown(self):
        return self.shown

    def Append(self, row):
        self.items.append(row[0])

    def DeleteAllItems(self):
        self.items = []

    def GetItemCount(self):
        return len(self.items)

    def Focus(self, idx):
        self.focus_calls.append(idx)
        self._focused = idx

    def SetItemText(self, idx, text):
        while len(self.items) <= idx:
            self.items.append("")
        self.items[idx] = text


class _FakeKeyEvent:
    def __init__(self, keycode):
        self._keycode = keycode
        self.skip_calls = 0

    def GetKeyCode(self):
        return self._keycode

    def Skip(self):
        self.skip_calls += 1


class _Stub:
    _show_current_status         = StatusPanel._show_current_status
    _on_status_contact_selected  = StatusPanel._on_status_contact_selected
    _on_status_contact_activated = StatusPanel._on_status_contact_activated
    _on_status_list_key_down     = StatusPanel._on_status_list_key_down
    _use_status_media_viewer_dialog = StatusPanel._use_status_media_viewer_dialog
    _status_to_media_viewer_item = StatusPanel._status_to_media_viewer_item
    _is_current_status_playable  = StatusPanel._is_current_status_playable
    _on_reply_field_text_changed = StatusPanel._on_reply_field_text_changed
    _resolve_name                = StatusPanel._resolve_name
    _status_preview              = StatusPanel._status_preview
    _on_play_pause_video         = StatusPanel._on_play_pause_video
    _update_play_pause_label     = StatusPanel._update_play_pause_label
    _is_status_liked             = StatusPanel._is_status_liked
    _on_like_status              = StatusPanel._on_like_status
    _on_like_sent                = StatusPanel._on_like_sent
    _parse_statuses              = StatusPanel._parse_statuses
    _latest_ts                   = staticmethod(StatusPanel._latest_ts)
    _on_next_status               = StatusPanel._on_next_status
    _on_prev_status                = StatusPanel._on_prev_status
    _on_escape                    = StatusPanel._on_escape
    _hide_post_panels             = StatusPanel._hide_post_panels
    _is_status_composer_open      = StatusPanel._is_status_composer_open
    _enter_status_composer        = StatusPanel._enter_status_composer
    _leave_status_composer        = StatusPanel._leave_status_composer
    _on_close_post_panel          = StatusPanel._on_close_post_panel
    _on_close_media_panel         = StatusPanel._on_close_media_panel
    _MAX_REMEMBERED_LIKES         = StatusPanel._MAX_REMEMBERED_LIKES
    _on_send_status_reply         = StatusPanel._on_send_status_reply
    _send_status_reply_bg         = StatusPanel._send_status_reply_bg
    _on_status_reply_sent         = StatusPanel._on_status_reply_sent
    _mark_status_viewed           = StatusPanel._mark_status_viewed
    _MAX_REMEMBERED_VIEWED        = StatusPanel._MAX_REMEMBERED_VIEWED
    _populate_list                = StatusPanel._populate_list
    _my_status_label              = StatusPanel._my_status_label
    _set_list_loading             = StatusPanel._set_list_loading
    _status_row_text              = StatusPanel._status_row_text
    _update_focused_status_row_text = StatusPanel._update_focused_status_row_text
    _on_viewer_status_opened      = StatusPanel._on_viewer_status_opened
    _viewer_like_status           = StatusPanel._viewer_like_status
    _viewer_reply_status          = StatusPanel._viewer_reply_status

    def __init__(self, contact_names=None, send_text_result=True,
                 send_reaction_result=True, settings=None):
        self.main_window = _FakeMainWindow(
            contact_names=contact_names, send_text_result=send_text_result,
            send_reaction_result=send_reaction_result, settings=settings,
        )
        self._status_contacts      = []
        self._status_row_contact   = {}
        self._status_contact_row   = {}
        self._selected_contact_idx = -1
        self._current_status_idx   = 0
        self._current_status       = None
        self._current_status_entry = None
        self._current_status_text  = ""
        self._liked_statuses       = {}
        self._video_local_path          = None
        self._video_download_status_id  = None
        self._video_player = _FakeVideoPlayer()
        self._status_list = _FakeStatusList()
        self._add_status_btn = _FakeWidget()
        self._refresh_status_btn = _FakeWidget()
        self._list_label = _FakeWidget()
        self._add_status_btn.Show()
        self._refresh_status_btn.Show()
        self._list_label.Show()
        self.my_status_dialog_calls = 0
        self.open_viewer_calls = []

        self._status_content_label = _FakeWidget()
        self._video_bitmap         = _FakeWidget()
        self._play_pause_btn       = _FakeWidget()
        self._save_media_btn       = _FakeWidget()
        self._copy_text_btn        = _FakeWidget()
        self._like_btn              = _FakeWidget()
        self._reply_label          = _FakeWidget()
        self._reply_field          = _FakeTextCtrl()
        self._reply_send_btn       = _FakeWidget()
        self._viewer_panel         = _FakeWidget()
        self._post_panel           = _FakeWidget()
        self._media_post_panel     = _FakeWidget()
        self._voice_post_panel     = _FakeWidget()
        self._selected_media_paths = []

    def _open_status_media_viewer(self, contact_idx: int):
        self.open_viewer_calls.append(contact_idx)
        if 0 <= contact_idx < len(self._status_contacts):
            entry = self._status_contacts[contact_idx]
            statuses = entry.get("statuses", [])
            if statuses:
                item = {
                    "status": statuses[0],
                    "entry": entry,
                    "status_id": statuses[0].get("key", {}).get("id", ""),
                    "from_me": statuses[0].get("key", {}).get("fromMe", False),
                }
                self._on_viewer_status_opened(item, 0)
        self.main_window.output("Status aberto", interrupt=False)

    def _open_my_status_dialog(self):
        self.my_status_dialog_calls += 1

    def _on_refresh(self, event):
        self.refresh_calls = getattr(self, "refresh_calls", 0) + 1

    def Layout(self):
        pass


def _text_status(text, from_me=False):
    return {
        "key": {"fromMe": from_me, "id": "s1"},
        "messageType": "conversation",
        "message": {"conversation": text},
        "messageTimestamp": 1700000000,
    }


def _image_status(caption="", from_me=False):
    return {
        "key": {"fromMe": from_me, "id": "s2"},
        "messageType": "imageMessage",
        "message": {"imageMessage": {"caption": caption, "mimetype": "image/jpeg"}},
        "messageTimestamp": 1700000000,
    }


def _video_status(caption="", from_me=False):
    return {
        "key": {"fromMe": from_me, "id": "s3"},
        "messageType": "videoMessage",
        "message": {"videoMessage": {"caption": caption, "mimetype": "video/mp4"}},
        "messageTimestamp": 1700000000,
    }


def _audio_status(from_me=False):
    return {
        "key": {"fromMe": from_me, "id": "s4"},
        "messageType": "audioMessage",
        "message": {"audioMessage": {"mimetype": "audio/ogg; codecs=opus"}},
        "messageTimestamp": 1700000000,
    }


def _entry(jid, statuses):
    return {"name": "Ana", "jid": jid, "statuses": statuses}


class TestPositionPreservedOnReselect:
    """Issue: Space on "status 3 de 5" reset the counter to "1 de 5"."""

    def test_reselecting_the_same_contact_keeps_the_current_status(self):
        stub = _Stub()
        statuses = [_text_status("a"), _text_status("b"), _text_status("c"),
                    _text_status("d"), _text_status("e")]
        stub._status_contacts = [_entry("j@s.whatsapp.net", statuses)]
        stub._status_row_contact = {1: 0}
        stub._selected_contact_idx = 0
        stub._current_status_idx   = 2  # "status 3 de 5"

        class _Evt:
            def GetIndex(self):
                return 1  # row 1 = the same already-selected contact (row 0 is My Status)

        stub._on_status_contact_selected(_Evt())

        assert stub._current_status_idx == 2

    def test_selecting_a_different_contact_resets_to_the_first_status(self):
        stub = _Stub()
        statuses_a = [_text_status("a"), _text_status("b")]
        statuses_b = [_text_status("x")]
        stub._status_contacts = [
            _entry("a@s.whatsapp.net", statuses_a),
            _entry("b@s.whatsapp.net", statuses_b),
        ]
        stub._status_row_contact = {1: 0, 2: 1}
        stub._selected_contact_idx = 0
        stub._current_status_idx   = 1  # viewing "2 de 2" of contact A

        class _Evt:
            def GetIndex(self):
                return 2  # row 2 = contact B (a genuinely different contact)

        stub._on_status_contact_selected(_Evt())

        assert stub._selected_contact_idx == 1
        assert stub._current_status_idx == 0

    def test_selecting_my_status_row_hides_the_viewer(self):
        stub = _Stub()
        stub._status_contacts = [_entry("a@s.whatsapp.net", [_text_status("a")])]
        stub._selected_contact_idx = 0
        stub._current_status_idx   = 0

        class _Evt:
            def GetIndex(self):
                return 0

        stub._on_status_contact_selected(_Evt())

        assert stub._selected_contact_idx == -1
        assert stub._viewer_panel.shown is False


class TestAnnouncementOnlyOnExplicitNavigation:
    """Issue: arrow-key navigation through the contact list re-announced
    "Nome — status X de Y: conteúdo" on every single row change — NVDA/JAWS
    already read the newly-focused list item on their own, so this was pure
    redundant chatter. Explicit navigation (Space, Enter/double-click
    activation, Ctrl+Left/Right between a contact's own statuses) must keep
    announcing, since none of those has an equivalent native readout."""

    def test_plain_list_selection_does_not_announce(self):
        stub = _Stub()
        stub._status_contacts = [_entry("a@s.whatsapp.net", [_text_status("oi")])]
        stub._status_row_contact = {1: 0}

        class _Evt:
            def GetIndex(self):
                return 1

        stub._on_status_contact_selected(_Evt())

        assert stub.main_window.outputs == []
        assert stub._selected_contact_idx == 0

    def test_activation_enter_or_doubleclick_still_announces(self):
        stub = _Stub()
        stub._status_contacts = [_entry("a@s.whatsapp.net", [_text_status("oi")])]
        stub._status_row_contact = {1: 0}

        class _Evt:
            def GetIndex(self):
                return 1

        stub._on_status_contact_activated(_Evt())

        assert len(stub.main_window.outputs) == 1

    def test_space_activation_still_announces(self):
        stub = _Stub()
        stub._status_contacts = [_entry("a@s.whatsapp.net", [_text_status("oi")])]
        stub._status_row_contact = {1: 0}
        stub._status_list = _FakeStatusList(focused=1)

        stub._on_status_list_key_down(_FakeKeyEvent(wx.WXK_SPACE))

        assert len(stub.main_window.outputs) == 1

    def test_explicit_next_status_still_announces(self):
        stub = _Stub()
        stub._status_contacts = [
            _entry("a@s.whatsapp.net", [_text_status("um"), _text_status("dois")])
        ]
        stub._selected_contact_idx = 0
        stub._current_status_idx = 0

        stub._on_next_status(None)

        assert len(stub.main_window.outputs) == 1

    def test_show_current_status_defaults_to_announcing_when_called_directly(self):
        """Sanity check the default parameter itself, independent of any
        particular caller."""
        stub = _Stub()
        stub._status_contacts = [_entry("a@s.whatsapp.net", [_text_status("oi")])]
        stub._selected_contact_idx = 0

        stub._show_current_status()

        assert len(stub.main_window.outputs) == 1


class TestStatusListRowShowsCurrentPositionWhenFocused:
    """Feature request: once a contact's status is open in the viewer, its
    own row in the list gains ", status X de Y" — updated live when
    navigating between that contact's statuses with Ctrl+Left/Right."""

    def test_opening_a_contact_appends_its_position_to_the_row(self):
        stub = _Stub()
        statuses = [_text_status("a"), _text_status("b"), _text_status("c")]
        stub._status_contacts = [_entry("j@s.whatsapp.net", statuses)]
        stub._status_row_contact = {1: 0}
        stub._status_contact_row = {0: 1}
        stub._status_list = _FakeStatusList()
        stub._status_list.items = ["My Status", "Ana: a"]
        stub._selected_contact_idx = 0
        stub._current_status_idx = 0

        stub._show_current_status()

        assert stub._status_list.items[1] == "Ana: a, Status 1 de 3"

    def test_navigating_to_the_next_status_updates_the_row_text(self):
        stub = _Stub()
        statuses = [_text_status("a"), _text_status("b"), _text_status("c")]
        stub._status_contacts = [_entry("j@s.whatsapp.net", statuses)]
        stub._status_row_contact = {1: 0}
        stub._status_contact_row = {0: 1}
        stub._status_list = _FakeStatusList()
        stub._status_list.items = ["My Status", "Ana: a"]
        stub._selected_contact_idx = 0
        stub._current_status_idx = 0

        stub._on_next_status(None)

        # The row's preview text tracks whatever status is actually being
        # viewed now, not always the newest one — otherwise NVDA/JAWS keep
        # announcing the first status's content after navigating away.
        assert stub._status_list.items[1] == "Ana: b, Status 2 de 3"

    def test_navigating_to_the_previous_status_updates_the_row_text(self):
        stub = _Stub()
        statuses = [_text_status("a"), _text_status("b"), _text_status("c")]
        stub._status_contacts = [_entry("j@s.whatsapp.net", statuses)]
        stub._status_row_contact = {1: 0}
        stub._status_contact_row = {0: 1}
        stub._status_list = _FakeStatusList()
        stub._status_list.items = ["My Status", "Ana: a"]
        stub._selected_contact_idx = 0
        stub._current_status_idx = 1

        stub._on_prev_status(None)

        assert stub._status_list.items[1] == "Ana: a, Status 1 de 3"

    def test_wrap_around_to_the_last_status_updates_the_row_preview_too(self):
        stub = _Stub()
        statuses = [_text_status("a"), _text_status("b"), _text_status("c")]
        stub._status_contacts = [_entry("j@s.whatsapp.net", statuses)]
        stub._status_row_contact = {1: 0}
        stub._status_contact_row = {0: 1}
        stub._status_list = _FakeStatusList()
        stub._status_list.items = ["My Status", "Ana: a"]
        stub._selected_contact_idx = 0
        stub._current_status_idx = 2  # already on the last one

        stub._on_next_status(None)  # wraps to the first

        assert stub._status_list.items[1] == "Ana: a, Status 1 de 3"

    def test_row_not_yet_in_the_map_is_a_safe_no_op(self):
        stub = _Stub()
        stub._status_contacts = [_entry("j@s.whatsapp.net", [_text_status("a")])]
        stub._status_contact_row = {}
        stub._selected_contact_idx = 0

        stub._show_current_status()  # must not raise

    def test_populate_list_builds_the_reverse_row_map(self):
        stub = _Stub()
        stub._status_list = _FakeStatusList()
        contacts = [_entry("a@s.whatsapp.net", [_text_status("oi")])]

        stub._populate_list([], contacts)

        # The contact's row must round-trip through both maps consistently.
        row = stub._status_contact_row[0]
        assert stub._status_row_contact[row] == 0


class TestVideoPlayback:
    def test_video_status_shows_the_play_pause_button(self):
        stub = _Stub()
        stub._status_contacts = [_entry("a@s.whatsapp.net", [_video_status()])]
        stub._selected_contact_idx = 0

        stub._show_current_status()

        assert stub._play_pause_btn.shown is True

    def test_text_status_hides_the_play_pause_button(self):
        stub = _Stub()
        stub._status_contacts = [_entry("a@s.whatsapp.net", [_text_status("oi")])]
        stub._selected_contact_idx = 0

        stub._show_current_status()

        assert stub._play_pause_btn.shown is False

    def test_audio_status_shows_the_play_pause_button(self):
        # Regression: audio statuses had no way to trigger playback at
        # all — the button only ever checked for "videoMessage".
        stub = _Stub()
        stub._status_contacts = [_entry("a@s.whatsapp.net", [_audio_status()])]
        stub._selected_contact_idx = 0

        stub._show_current_status()

        assert stub._play_pause_btn.shown is True

    def test_switching_status_stops_any_playing_video(self):
        """Reported live: leaving the video's status without stopping it
        first would keep its audio playing / ffmpeg decoding in the
        background — _show_current_status() must always stop() the player
        first, whatever was showing before."""
        stub = _Stub()
        stub._status_contacts = [_entry("a@s.whatsapp.net", [_video_status(), _text_status("oi")])]
        stub._selected_contact_idx = 0
        stub._current_status_idx = 0
        stub._show_current_status()
        assert stub._video_player.stop_calls == 1  # stopped once on first show too

        stub._current_status_idx = 1
        stub._show_current_status()

        assert stub._video_player.stop_calls == 2
        assert stub._video_bitmap.shown is False


class TestPlayPauseAcceptsAudioStatuses:
    """_on_play_pause_video() used to bail out for anything but
    "videoMessage" — the download/threading path itself isn't exercised
    here (see TestVideoPlayback / _download_and_play_video), just the
    guard that used to block audio entirely."""

    def test_ignores_a_status_type_with_no_playable_media(self):
        stub = _Stub()
        stub._current_status = _text_status("oi")

        stub._on_play_pause_video(None)

        assert stub._video_player.toggle_pause_calls == 0

    def test_toggles_pause_for_an_already_playing_audio_status(self):
        stub = _Stub()
        stub._current_status = _audio_status()
        stub._video_player.is_playing = True

        stub._on_play_pause_video(None)

        assert stub._video_player.toggle_pause_calls == 1

    def test_audio_status_shows_the_save_media_button(self):
        stub = _Stub()
        stub._status_contacts = [_entry("a@s.whatsapp.net", [_audio_status()])]
        stub._selected_contact_idx = 0

        stub._show_current_status()

        assert stub._save_media_btn.shown is True


class TestEnterAndSpaceTogglePlaybackOnStatusList:
    """Feature request: Enter/Space activating a status-list item should
    play/pause its media, not just re-select an already-shown status
    (which would stop() and restart the player instead of pausing it)."""

    def test_enter_on_a_video_status_already_shown_toggles_pause(self):
        stub = _Stub()
        stub._status_contacts = [_entry("a@s.whatsapp.net", [_video_status()])]
        stub._status_row_contact = {1: 0}
        stub._selected_contact_idx = 0
        stub._current_status = _video_status()
        stub._video_player.is_playing = True

        class _Evt:
            def GetIndex(self):
                return 1  # row 1 = contact at _status_contacts[0]

        stub._on_status_contact_activated(_Evt())

        assert stub.open_viewer_calls == [0]

    def test_enter_on_a_text_status_does_not_try_to_toggle(self):
        stub = _Stub()
        stub._status_contacts = [_entry("a@s.whatsapp.net", [_text_status("oi")])]
        stub._status_row_contact = {1: 0}
        stub._selected_contact_idx = 0
        stub._current_status = _text_status("oi")

        class _Evt:
            def GetIndex(self):
                return 1

        stub._on_status_contact_activated(_Evt())

        assert stub.open_viewer_calls == [0]

    def test_enter_on_a_not_yet_selected_contact_selects_instead_of_toggling(self):
        stub = _Stub()
        stub._status_contacts = [_entry("a@s.whatsapp.net", [_video_status()])]
        stub._status_row_contact = {1: 0}
        stub._selected_contact_idx = -1
        stub._current_status = None

        class _Evt:
            def GetIndex(self):
                return 1

        stub._on_status_contact_activated(_Evt())

        assert stub.open_viewer_calls == [0]
        assert stub._selected_contact_idx == 0

    def test_enter_on_row_zero_opens_my_status_dialog(self):
        stub = _Stub()

        class _Evt:
            def GetIndex(self):
                return 0

        stub._on_status_contact_activated(_Evt())

        assert stub.my_status_dialog_calls == 1

    def test_space_on_a_video_status_already_shown_toggles_pause_without_reselecting(self):
        stub = _Stub()
        stub._status_contacts = [_entry("a@s.whatsapp.net", [_video_status()])]
        stub._status_row_contact = {1: 0}
        stub._selected_contact_idx = 0
        stub._current_status = _video_status()
        stub._video_player.is_playing = True
        stub._status_list = _FakeStatusList(focused=1)

        stub._on_status_list_key_down(_FakeKeyEvent(wx.WXK_SPACE))

        assert stub.open_viewer_calls == [0]
        assert stub._status_list.select_calls == []

    def test_space_on_a_non_playing_row_still_selects_normally(self):
        stub = _Stub()
        stub._status_contacts = [_entry("a@s.whatsapp.net", [_video_status()])]
        stub._status_row_contact = {1: 0}
        stub._selected_contact_idx = -1
        stub._current_status = None
        stub._status_list = _FakeStatusList(focused=1)

        stub._on_status_list_key_down(_FakeKeyEvent(wx.WXK_SPACE))

        assert stub.open_viewer_calls == [0]
        assert stub._selected_contact_idx == 0

    def test_other_keys_are_skipped(self):
        stub = _Stub()
        evt = _FakeKeyEvent(ord("A"))

        stub._on_status_list_key_down(evt)

        assert evt.skip_calls == 1


def _classic_stub(**kwargs):
    kwargs.setdefault(
        "settings", {"user_interface": {"status_media_viewer_dialog": False}}
    )
    return _Stub(**kwargs)


class TestClassicInlineModeWhenSeparatePlayerDisabled:
    """Settings > Interface do usuário > "Mostrar os status em player
    separado" — unchecked keeps the classic in-panel viewer (pre-PR #103
    behaviour) instead of opening MediaViewerDialog, for every entry point:
    Enter/double-click activation, Space, and plain arrow-key selection."""

    def test_default_setting_uses_the_dialog(self):
        stub = _Stub()  # no settings override — must default to True
        assert stub._use_status_media_viewer_dialog() is True

    def test_disabled_setting_uses_the_classic_viewer(self):
        stub = _classic_stub()
        assert stub._use_status_media_viewer_dialog() is False

    def test_activation_shows_current_status_instead_of_the_dialog(self):
        stub = _classic_stub()
        stub._status_contacts = [_entry("a@s.whatsapp.net", [_text_status("oi")])]
        stub._status_row_contact = {1: 0}

        class _Evt:
            def GetIndex(self):
                return 1

        stub._on_status_contact_activated(_Evt())

        assert stub.open_viewer_calls == []
        assert stub._selected_contact_idx == 0

    def test_activation_on_an_already_playing_video_toggles_pause(self):
        stub = _classic_stub()
        stub._status_contacts = [_entry("a@s.whatsapp.net", [_video_status()])]
        stub._status_row_contact = {1: 0}
        stub._selected_contact_idx = 0
        stub._current_status = _video_status()
        stub._video_player.is_playing = True

        class _Evt:
            def GetIndex(self):
                return 1

        stub._on_status_contact_activated(_Evt())

        assert stub.open_viewer_calls == []
        assert stub._video_player.toggle_pause_calls == 1  # toggled, not reopened as a dialog

    def test_space_selects_and_shows_inline_instead_of_the_dialog(self):
        stub = _classic_stub()
        stub._status_contacts = [_entry("a@s.whatsapp.net", [_video_status()])]
        stub._status_row_contact = {1: 0}
        stub._selected_contact_idx = -1
        stub._current_status = None
        stub._status_list = _FakeStatusList(focused=1)

        stub._on_status_list_key_down(_FakeKeyEvent(wx.WXK_SPACE))

        assert stub.open_viewer_calls == []
        assert stub._selected_contact_idx == 0
        assert stub._status_list.select_calls == [1]

    def test_space_on_row_zero_selects_before_opening_my_status_dialog(self):
        stub = _classic_stub()
        stub._status_list = _FakeStatusList(focused=0)

        stub._on_status_list_key_down(_FakeKeyEvent(wx.WXK_SPACE))

        assert stub.my_status_dialog_calls == 1
        assert stub._status_list.select_calls == [0]

    def test_plain_arrow_selection_shows_the_status_inline(self):
        """The defining difference from dialog mode: mere focus movement
        (EVT_LIST_ITEM_SELECTED, arrow keys) drives the inline viewer
        directly instead of only tracking which row has focus."""
        stub = _classic_stub()
        stub._status_contacts = [_entry("a@s.whatsapp.net", [_text_status("oi")])]
        stub._status_row_contact = {1: 0}

        class _Evt:
            def GetIndex(self):
                return 1

        stub._on_status_contact_selected(_Evt())

        assert stub._selected_contact_idx == 0
        assert stub._current_status is not None
        assert stub.open_viewer_calls == []

    def test_selecting_my_status_row_hides_the_inline_viewer(self):
        stub = _classic_stub()
        stub._status_contacts = [_entry("a@s.whatsapp.net", [_text_status("a")])]
        stub._selected_contact_idx = 0

        class _Evt:
            def GetIndex(self):
                return 0

        stub._on_status_contact_selected(_Evt())

        assert stub._selected_contact_idx == -1
        assert stub._viewer_panel.shown is False

    def test_arrow_selection_marks_the_status_viewed_same_as_before_the_dialog_existed(self):
        """Regression: the dialog-mode rewrite moved "mark viewed" into
        _on_viewer_status_opened() only, which _show_current_status() never
        reaches from dialog mode — but classic mode still routes through
        _show_current_status() directly, and that legacy path stopped
        marking anything viewed at all until this was restored."""
        stub = _classic_stub()
        stub._status_contacts = [_entry("a@s.whatsapp.net", [_text_status("oi", from_me=False)])]
        stub._status_row_contact = {1: 0}

        class _Evt:
            def GetIndex(self):
                return 1

        stub._on_status_contact_selected(_Evt())

        assert stub.main_window.settings["status_panel"]["viewed_status_ids"] == ["s1"]


class TestCopyStatusText:
    def test_text_status_copy_text_is_the_full_text(self):
        stub = _Stub()
        stub._status_contacts = [_entry("a@s.whatsapp.net", [_text_status("Bom dia!")])]
        stub._selected_contact_idx = 0

        stub._show_current_status()

        assert stub._current_status_text == "Bom dia!"
        assert stub._copy_text_btn.shown is True

    def test_image_status_copy_text_is_just_the_caption(self):
        """Not "Foto: <caption>" — that prefix is only for the announced
        label, the clipboard should get the caption text alone."""
        stub = _Stub()
        stub._status_contacts = [_entry("a@s.whatsapp.net", [_image_status(caption="praia")])]
        stub._selected_contact_idx = 0

        stub._show_current_status()

        assert stub._current_status_text == "praia"

    def test_image_status_with_no_caption_hides_the_copy_button(self):
        stub = _Stub()
        stub._status_contacts = [_entry("a@s.whatsapp.net", [_image_status(caption="")])]
        stub._selected_contact_idx = 0

        stub._show_current_status()

        assert stub._current_status_text == ""
        assert stub._copy_text_btn.shown is False


class TestReplyAndLikeOnlyForOthersStatuses:
    def test_others_status_shows_reply_field_but_not_the_empty_send_button(self):
        # The reply field itself always shows for someone else's status —
        # only the send button waits for actual text (see
        # TestReplySendButtonFollowsFieldContent below).
        stub = _Stub()
        stub._status_contacts = [_entry("a@s.whatsapp.net", [_text_status("oi", from_me=False)])]
        stub._selected_contact_idx = 0

        stub._show_current_status()

        assert stub._reply_field.shown is True
        assert stub._reply_send_btn.shown is False
        assert stub._like_btn.shown is True

    def test_others_status_with_pending_reply_text_shows_the_send_button(self):
        stub = _Stub()
        stub._status_contacts = [_entry("a@s.whatsapp.net", [_text_status("oi", from_me=False)])]
        stub._selected_contact_idx = 0
        stub._reply_field.SetValue("valeu!")

        stub._show_current_status()

        assert stub._reply_send_btn.shown is True

    def test_own_status_hides_reply_and_like(self):
        stub = _Stub()
        stub._status_contacts = [_entry("a@s.whatsapp.net", [_text_status("oi", from_me=True)])]
        stub._selected_contact_idx = 0

        stub._show_current_status()

        assert stub._reply_field.shown is False
        assert stub._reply_send_btn.shown is False
        assert stub._like_btn.shown is False


class TestReplySendButtonFollowsFieldContent:
    """The send button only makes sense once there's something to send —
    _on_reply_field_text_changed() (bound to EVT_TEXT) keeps it in sync as
    the user types/clears the reply field."""

    def test_typing_text_shows_the_button(self):
        stub = _Stub()
        stub._reply_field.SetValue("oi")

        stub._on_reply_field_text_changed(None)

        assert stub._reply_send_btn.shown is True

    def test_clearing_the_field_hides_the_button(self):
        stub = _Stub()
        stub._reply_field.SetValue("oi")
        stub._on_reply_field_text_changed(None)
        assert stub._reply_send_btn.shown is True

        stub._reply_field.SetValue("")
        stub._on_reply_field_text_changed(None)

        assert stub._reply_send_btn.shown is False

    def test_whitespace_only_text_does_not_show_the_button(self):
        stub = _Stub()
        stub._reply_field.SetValue("   ")

        stub._on_reply_field_text_changed(None)

        assert stub._reply_send_btn.shown is False


class TestStatusContentLabelHandlesEveryMessageType:
    """Regression: audio/document/sticker/contact statuses used to fall
    through to the raw messageType string itself (literally "audioMessage")
    instead of a translated label — reported live as "Fulano: audioMessage"
    in the Alt+5 status list."""

    i18n = _FakeI18n()

    def test_audio_status_is_translated(self):
        label = _status_content_label("audioMessage", {"audioMessage": {}}, self.i18n)
        assert label == "Áudio"

    def test_document_status_includes_filename(self):
        msg_obj = {"documentMessage": {"fileName": "relatorio.pdf"}}
        label = _status_content_label("documentMessage", msg_obj, self.i18n)
        assert label == "Documento: relatorio.pdf"

    def test_document_status_without_filename(self):
        label = _status_content_label("documentMessage", {"documentMessage": {}}, self.i18n)
        assert label == "Documento"

    def test_sticker_status_is_translated(self):
        label = _status_content_label("stickerMessage", {}, self.i18n)
        assert label == "Figurinha"

    def test_contact_status_is_translated(self):
        msg_obj = {"contactMessage": {"displayName": "Ana"}}
        label = _status_content_label("contactMessage", msg_obj, self.i18n)
        assert label == "Contato: Ana"

    def test_contact_status_falls_back_to_vcard_fn_when_display_name_missing(self):
        """Issue #22: a contact status with no displayName used to show
        "Contato: " (empty name) instead of parsing the vCard's FN: line."""
        vcard = "BEGIN:VCARD\nVERSION:3.0\nFN:Bob Builder\nEND:VCARD"
        msg_obj = {"contactMessage": {"displayName": "", "vcard": vcard}}
        label = _status_content_label("contactMessage", msg_obj, self.i18n)
        assert label == "Contato: Bob Builder"

    def test_contact_status_falls_back_when_display_name_is_itself_a_vcard_blob(self):
        vcard = "BEGIN:VCARD\nVERSION:3.0\nFN:Carol\nEND:VCARD"
        msg_obj = {"contactMessage": {"displayName": vcard, "vcard": ""}}
        label = _status_content_label("contactMessage", msg_obj, self.i18n)
        assert label == "Contato: Carol"

    def test_contact_status_unknown_when_no_name_available_anywhere(self):
        msg_obj = {"contactMessage": {"displayName": "", "vcard": ""}}
        label = _status_content_label("contactMessage", msg_obj, self.i18n)
        assert label == "Contato: Contato sem nome"

    def test_unknown_type_falls_back_to_translated_generic_label(self):
        # Never the raw type string itself.
        label = _status_content_label("someBrandNewWhatsAppType", {}, self.i18n)
        assert label == "Mensagem não suportada"


class TestStatusMediaSaveInfoCoversEveryMediaType:
    """Feature request: "salvar mídia" for status must also work for audio
    statuses, not only image/video — _on_save_status_media() used to hide
    the button and refuse to build a wildcard for audioMessage entirely."""

    i18n = _FakeI18n()

    def test_image_uses_its_own_mimetype_extension(self):
        msg_obj = {"imageMessage": {"mimetype": "image/png"}}
        ext, wildcard = _status_media_save_info("imageMessage", msg_obj, self.i18n)
        assert ext == ".png"
        assert "Foto" in wildcard

    def test_video_uses_its_own_mimetype_extension(self):
        msg_obj = {"videoMessage": {"mimetype": "video/mp4"}}
        ext, wildcard = _status_media_save_info("videoMessage", msg_obj, self.i18n)
        assert ext == ".mp4"
        assert "Vídeo" in wildcard

    def test_audio_uses_its_own_mimetype_extension(self):
        msg_obj = {"audioMessage": {"mimetype": "audio/ogg; codecs=opus"}}
        ext, wildcard = _status_media_save_info("audioMessage", msg_obj, self.i18n)
        assert ext == ".ogg"
        assert "Áudio" in wildcard

    def test_audio_falls_back_to_ogg_when_mimetype_missing(self):
        ext, wildcard = _status_media_save_info("audioMessage", {"audioMessage": {}}, self.i18n)
        assert ext == ".ogg"

    def test_unsupported_type_returns_none(self):
        assert _status_media_save_info("documentMessage", {}, self.i18n) is None
        assert _status_media_save_info("conversation", {}, self.i18n) is None

    def test_jpeg_is_canonicalized_to_jpg_not_a_bare_mimetype_split(self):
        """Regression: a naive mimetype.split("/")[-1] gives ".jpeg" for
        image/jpeg — the far more common case than "image/jpg" ever
        actually appearing on the wire. _status_to_media_viewer_item()'s
        own canonicalizing table already got this right; this function
        used to disagree with it for the exact same status."""
        msg_obj = {"imageMessage": {"mimetype": "image/jpeg"}}
        ext, _wildcard = _status_media_save_info("imageMessage", msg_obj, self.i18n)
        assert ext == ".jpg"


class TestStatusMediaSaveInfoAgreesWithTheMediaViewer:
    """Regression: the classic "Salvar mídia" button/shortcut
    (_status_media_save_info(), reachable again once Settings > Interface
    do usuário > "Mostrar os status em player separado" can be unchecked)
    and the unified MediaViewerDialog's own Save As
    (_status_to_media_viewer_item()) used to compute the file extension
    with two independent implementations — the same image/jpeg status photo
    saved as status.jpeg from one button and status.jpg from the other.
    Both now share _status_media_extension()."""

    i18n = _FakeI18n()

    def _stub(self):
        return _Stub(contact_names={})

    @pytest.mark.parametrize(
        "msg_type, mimetype",
        [
            ("imageMessage", "image/jpeg"),
            ("imageMessage", "image/png"),
            ("videoMessage", "video/webm"),
            ("audioMessage", "audio/mp4"),
            ("audioMessage", "audio/ogg; codecs=opus"),
        ],
    )
    def test_both_entry_points_pick_the_same_extension(self, msg_type, mimetype):
        msg_obj = {msg_type: {"mimetype": mimetype}}
        ext_classic, _wildcard = _status_media_save_info(msg_type, msg_obj, self.i18n)

        stub = self._stub()
        status = {
            "messageType": msg_type,
            "message": msg_obj,
            "key": {"id": "s1", "fromMe": False},
        }
        item = stub._status_to_media_viewer_item({"name": "Ana", "jid": "a@s.whatsapp.net"}, status)

        assert item["extension"] == ext_classic


class TestResolveNamePrefersSavedContactNameOverPushName:
    """Regression: the status list always showed the sender's WhatsApp
    profile name (pushName) even when a different name was saved for them
    in the address book — unlike every chat list/conversation in the app,
    which prefers the saved contact name."""

    def test_prefers_saved_contact_name(self):
        stub = _Stub(contact_names={"5511999999999@s.whatsapp.net": "Apelido Salvo"})

        name = stub._resolve_name("5511999999999@s.whatsapp.net")

        assert name == "Apelido Salvo"

    def test_returns_empty_string_when_unresolved(self):
        # _parse_statuses() does `self._resolve_name(jid) or format_number(jid)`
        # — an empty string (not None) is what lets that fallback kick in.
        stub = _Stub(contact_names={})

        name = stub._resolve_name("5511999999999@s.whatsapp.net")

        assert name == ""

    def test_status_preview_uses_resolved_name_for_a_captioned_photo(self):
        stub = _Stub()
        status = {
            "messageType": "imageMessage",
            "message": {"imageMessage": {"caption": "praia"}},
        }
        preview = stub._status_preview(status, stub.main_window.i18n)
        assert preview == "Foto: praia"


def _run_threads_synchronously(monkeypatch):
    """Make status_panel.threading.Thread(...).start() run its target
    immediately on the calling (test) thread instead of a real background
    thread, and wx.CallAfter() run its callback inline instead of queuing
    it on a running wx.App's event loop (there isn't one in these tests) —
    status-like sends are threaded for real, but tests need a synchronous,
    deterministic result. Same pattern as test_remote_revoke.py."""
    import status_panel as status_panel_module

    class _SyncThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None):
            self._target = target
            self._args = args
            self._kwargs = kwargs or {}

        def start(self):
            self._target(*self._args, **self._kwargs)

    monkeypatch.setattr(status_panel_module.threading, "Thread", _SyncThread)
    monkeypatch.setattr(status_panel_module.wx, "CallAfter", lambda fn, *a, **kw: fn(*a, **kw))


class TestLikeStatusUsesTheNativeStatusReaction:
    """The Status Like button is a reaction to status@broadcast, never a
    literal heart sent into the poster's private conversation."""

    def test_sends_heart_through_reaction_endpoint_not_private_message(self, monkeypatch):
        _run_threads_synchronously(monkeypatch)
        status = _text_status("oi")
        entry = _entry("poster@s.whatsapp.net", [status])
        stub = _Stub()
        stub._status_contacts = [entry]
        stub._selected_contact_idx = 0
        stub._current_status_idx = 0
        stub._current_status = status

        stub._on_like_status(None)

        assert stub.main_window.send_text_calls == []
        assert stub.main_window.send_reaction_calls == [(
            "status@broadcast",
            {
                "fromMe": False,
                "id": "s1",
                "remoteJid": "status@broadcast",
                "participant": "poster@s.whatsapp.net",
            },
            "❤️",
        )]

    def test_marks_liked_persists_to_settings_and_updates_button_label_on_success(self, monkeypatch):
        _run_threads_synchronously(monkeypatch)
        status = _text_status("oi")
        stub = _Stub(send_reaction_result=True)
        stub._status_contacts = [_entry("poster@s.whatsapp.net", [status])]
        stub._selected_contact_idx = 0
        stub._current_status_idx = 0
        stub._current_status = status

        stub._on_like_status(None)

        status_id = status["key"]["id"]
        assert stub._liked_statuses[status_id] is True
        assert stub._like_btn.label == "Descurtir"
        assert status_id in stub.main_window.settings["status_panel"]["liked_status_ids"]
        assert stub.main_window.save_settings_calls == 1

    def test_shows_error_on_send_failure(self, monkeypatch):
        _run_threads_synchronously(monkeypatch)
        calls = []
        monkeypatch.setattr(wx, "MessageBox", lambda *a, **kw: calls.append(a))
        status = _text_status("oi")
        stub = _Stub(send_reaction_result=False)
        stub._status_contacts = [_entry("poster@s.whatsapp.net", [status])]
        stub._selected_contact_idx = 0
        stub._current_status_idx = 0
        stub._current_status = status

        stub._on_like_status(None)

        assert stub._liked_statuses.get(status["key"]["id"]) is None
        assert len(calls) == 1
        assert stub.main_window.save_settings_calls == 0

    def test_clicking_again_removes_the_native_reaction(self, monkeypatch):
        _run_threads_synchronously(monkeypatch)
        status = _text_status("oi")
        status_id = status["key"]["id"]
        stub = _Stub()
        stub._status_contacts = [_entry("poster@s.whatsapp.net", [status])]
        stub._selected_contact_idx = 0
        stub._current_status_idx = 0
        stub._current_status = status
        stub._liked_statuses[status_id] = True

        stub._on_like_status(None)

        assert stub.main_window.send_text_calls == []
        assert stub.main_window.send_reaction_calls[0][2] == ""
        assert stub._liked_statuses[status_id] is False
        assert stub._like_btn.label == "Curtir"

    def test_unlike_removes_the_persisted_status_id(self, monkeypatch):
        _run_threads_synchronously(monkeypatch)
        status = _text_status("oi")
        status_id = status["key"]["id"]
        stub = _Stub(settings={"status_panel": {"liked_status_ids": [status_id]}})
        stub._status_contacts = [_entry("poster@s.whatsapp.net", [status])]
        stub._selected_contact_idx = 0
        stub._current_status_idx = 0
        stub._current_status = status

        stub._on_like_status(None)

        assert status_id not in stub.main_window.settings["status_panel"]["liked_status_ids"]
        assert stub.main_window.save_settings_calls == 1

    def test_separate_viewer_uses_reaction_endpoint_not_private_message(self, monkeypatch):
        _run_threads_synchronously(monkeypatch)
        status = _text_status("oi")
        entry = _entry("poster@s.whatsapp.net", [status])
        stub = _Stub()
        done = []

        stub._viewer_like_status({
            "status": status,
            "entry": entry,
            "status_id": "s1",
        }, done.append)

        assert done == [True]
        assert stub.main_window.send_text_calls == []
        assert stub.main_window.send_reaction_calls == [(
            "status@broadcast",
            {
                "fromMe": False,
                "id": "s1",
                "remoteJid": "status@broadcast",
                "participant": "poster@s.whatsapp.net",
            },
            "❤️",
        )]

    def test_separate_viewer_removes_existing_native_reaction(self, monkeypatch):
        _run_threads_synchronously(monkeypatch)
        status = _text_status("oi")
        entry = _entry("poster@s.whatsapp.net", [status])
        stub = _Stub(settings={"status_panel": {"liked_status_ids": ["s1"]}})

        stub._viewer_like_status({
            "status": status,
            "entry": entry,
            "status_id": "s1",
        }, lambda ok: None)

        assert stub.main_window.send_reaction_calls[0][2] == ""
        assert "s1" not in stub.main_window.settings["status_panel"]["liked_status_ids"]


class TestIsStatusLikedRemembersAcrossSessions:
    """_liked_statuses only tracks likes sent THIS session — _is_status_liked()
    also checks settings["status_panel"]["liked_status_ids"] (persisted to
    settings.json by _on_like_sent()), so a like sent in a previous
    session is still detected."""

    def test_false_when_nothing_known(self):
        stub = _Stub()
        assert stub._is_status_liked("s1") is False

    def test_session_cache_wins_without_touching_settings(self):
        stub = _Stub()
        stub._liked_statuses["s1"] = True
        assert stub._is_status_liked("s1") is True

    def test_finds_a_status_id_remembered_from_a_prior_session(self):
        stub = _Stub(settings={"status_panel": {"liked_status_ids": ["s1"]}})
        assert stub._is_status_liked("s1") is True

    def test_does_not_match_a_different_status_id(self):
        stub = _Stub(settings={"status_panel": {"liked_status_ids": ["some-other-status"]}})
        assert stub._is_status_liked("s1") is False


class TestOnLikeSentCapsHowManyIdsAreRemembered:
    def test_old_ids_are_dropped_once_the_cap_is_exceeded(self):
        stub = _Stub(settings={
            "status_panel": {"liked_status_ids": [f"s{i}" for i in range(StatusPanel._MAX_REMEMBERED_LIKES)]}
        })
        stub._current_status = None

        stub._on_like_sent("s-new")

        remembered = stub.main_window.settings["status_panel"]["liked_status_ids"]
        assert len(remembered) == StatusPanel._MAX_REMEMBERED_LIKES
        assert "s-new" in remembered
        assert "s0" not in remembered  # oldest one dropped

    def test_liking_the_same_status_twice_does_not_duplicate_or_resave(self):
        stub = _Stub(settings={"status_panel": {"liked_status_ids": ["s1"]}})
        stub._current_status = None

        stub._on_like_sent("s1")

        assert stub.main_window.settings["status_panel"]["liked_status_ids"] == ["s1"]
        assert stub.main_window.save_settings_calls == 0


class TestParseStatusesFiltersReactionsAndDetectsSelfParticipant:
    def test_reaction_to_a_status_is_not_treated_as_a_story(self):
        stub = _Stub()
        reaction = {
            "key": {"fromMe": False, "id": "r1", "participant": "a@s.whatsapp.net"},
            "messageType": "reactionMessage",
            "message": {"reactionMessage": {"text": "❤️", "key": {"id": "s1"}}},
            "messageTimestamp": 1700000000,
        }
        my_statuses, contacts = stub._parse_statuses([reaction], stub.main_window.i18n)
        assert my_statuses == []
        assert contacts == []

    def test_participant_resolving_to_self_counts_as_my_status(self):
        stub = _Stub()
        stub.main_window._is_self_jid = lambda jid: jid == "me@lid"
        status = {
            "key": {"fromMe": False, "id": "s1", "participant": "me@lid"},
            "messageType": "conversation",
            "message": {"conversation": "oi"},
            "messageTimestamp": 1700000000,
        }
        my_statuses, contacts = stub._parse_statuses([status], stub.main_window.i18n)
        assert my_statuses == [status]
        assert contacts == []

    def test_a_real_status_from_someone_else_still_becomes_a_contact_entry(self):
        stub = _Stub()
        status = {
            "key": {"fromMe": False, "id": "s1", "participant": "a@s.whatsapp.net"},
            "messageType": "conversation",
            "message": {"conversation": "oi"},
            "messageTimestamp": 1700000000,
        }
        my_statuses, contacts = stub._parse_statuses([status], stub.main_window.i18n)
        assert my_statuses == []
        assert len(contacts) == 1
        assert contacts[0]["jid"] == "a@s.whatsapp.net"

    def test_viewed_all_is_false_when_no_status_has_been_opened(self):
        stub = _Stub()
        status = {
            "key": {"fromMe": False, "id": "s1", "participant": "a@s.whatsapp.net"},
            "messageType": "conversation", "message": {"conversation": "oi"},
            "messageTimestamp": 1700000000,
        }
        _, contacts = stub._parse_statuses([status], stub.main_window.i18n)
        assert contacts[0]["viewed_all"] is False

    def test_viewed_all_is_true_only_once_every_status_was_opened(self):
        stub = _Stub(settings={"status_panel": {"viewed_status_ids": ["s1"]}})
        s1 = {"key": {"fromMe": False, "id": "s1", "participant": "a@s.whatsapp.net"},
              "messageType": "conversation", "message": {"conversation": "a"},
              "messageTimestamp": 1700000000}
        s2 = {"key": {"fromMe": False, "id": "s2", "participant": "a@s.whatsapp.net"},
              "messageType": "conversation", "message": {"conversation": "b"},
              "messageTimestamp": 1700000001}

        _, contacts = stub._parse_statuses([s1], stub.main_window.i18n)
        assert contacts[0]["viewed_all"] is True, "the only status was opened"

        _, contacts = stub._parse_statuses([s1, s2], stub.main_window.i18n)
        assert contacts[0]["viewed_all"] is False, "s2 was never opened"


class TestMarkStatusViewed:
    """_mark_status_viewed() — same persistence shape as _on_like_sent()
    (tests/test_status_panel.py::TestOnLikeSentCapsHowManyIdsAreRemembered),
    just for the Recentes/Vistos split instead of the like button."""

    def test_first_view_is_remembered_and_persisted(self):
        stub = _Stub()

        stub._mark_status_viewed("s1")

        assert stub.main_window.settings["status_panel"]["viewed_status_ids"] == ["s1"]
        assert stub.main_window.save_settings_calls == 1

    def test_viewing_the_same_status_again_does_not_re_save(self):
        stub = _Stub()
        stub._mark_status_viewed("s1")

        stub._mark_status_viewed("s1")

        assert stub.main_window.settings["status_panel"]["viewed_status_ids"] == ["s1"]
        assert stub.main_window.save_settings_calls == 1

    def test_list_is_capped_to_the_most_recent_ids(self):
        stub = _Stub()
        stub._MAX_REMEMBERED_VIEWED = 3

        for sid in ("s1", "s2", "s3", "s4"):
            stub._mark_status_viewed(sid)

        assert stub.main_window.settings["status_panel"]["viewed_status_ids"] == ["s2", "s3", "s4"]

    def test_opening_a_status_in_the_viewer_marks_it_viewed(self):
        """End-to-end: _open_status_media_viewer() for someone else's status
        calls through to _mark_status_viewed()."""
        stub = _Stub()
        stub._status_contacts = [_entry("a@s.whatsapp.net", [_text_status("oi", from_me=False)])]
        stub._status_row_contact = {1: 0}
        stub._open_status_media_viewer(0)

        assert stub.main_window.settings["status_panel"]["viewed_status_ids"] == ["s1"]

    def test_opening_my_own_status_does_not_mark_it_viewed(self):
        stub = _Stub()
        stub._status_contacts = [_entry("a@s.whatsapp.net", [_text_status("oi", from_me=True)])]
        stub._status_row_contact = {1: 0}
        stub._open_status_media_viewer(0)

        assert stub.main_window.settings.get("status_panel", {}).get("viewed_status_ids", []) == []


class TestPopulateListRecentViewedSections:
    """_populate_list() splits contacts into "Recentes" (something unseen)
    and "Vistos" (everything already opened) sections, each with its own
    "--- header ---" row inserted directly into the list — and every row
    index shifts because of it, tracked via _status_row_contact. Reported
    as a real regression risk in an earlier attempt at this feature: the
    Space-key handler (_on_status_list_key_down) computed its own row->
    contact mapping via a hardcoded `idx - 1` instead of reusing the same
    map the selection/activation handlers use, so Space silently opened
    the WRONG contact once header rows existed — covered here by going
    through the real row map _populate_list() builds instead of a
    hand-written one, unlike the tests above."""

    def test_recent_and_viewed_contacts_land_in_separate_sections(self):
        stub = _Stub(settings={"status_panel": {"viewed_status_ids": ["seen1"]}})
        seen = _entry("seen@s.whatsapp.net", [
            {"key": {"id": "seen1", "fromMe": False}, "messageType": "conversation",
             "message": {"conversation": "x"}, "messageTimestamp": 1},
        ])
        seen["viewed_all"] = True
        unseen = _entry("unseen@s.whatsapp.net", [
            {"key": {"id": "unseen1", "fromMe": False}, "messageType": "conversation",
             "message": {"conversation": "y"}, "messageTimestamp": 2},
        ])
        unseen["viewed_all"] = False

        stub._populate_list([], [seen, unseen])

        # Row 0 = My Status, row 1 = "Recentes" header, row 2 = unseen
        # contact, row 3 = "Vistos" header, row 4 = seen contact.
        assert stub._status_list.items[1] == "— Recentes —"
        assert "unseen@s.whatsapp.net" in stub._status_contacts[stub._status_row_contact[2]]["jid"]
        assert stub._status_list.items[3] == "— Vistos —"
        assert "seen@s.whatsapp.net" in stub._status_contacts[stub._status_row_contact[4]]["jid"]

    def test_header_rows_are_not_selectable_contacts(self):
        stub = _Stub(settings={"status_panel": {"viewed_status_ids": ["seen1"]}})
        seen = _entry("seen@s.whatsapp.net", [
            {"key": {"id": "seen1", "fromMe": False}, "messageType": "conversation",
             "message": {"conversation": "x"}, "messageTimestamp": 1},
        ])
        seen["viewed_all"] = True
        unseen = _entry("unseen@s.whatsapp.net", [
            {"key": {"id": "unseen1", "fromMe": False}, "messageType": "conversation",
             "message": {"conversation": "y"}, "messageTimestamp": 2},
        ])
        unseen["viewed_all"] = False
        stub._populate_list([], [seen, unseen])

        assert stub._status_row_contact[0] == -1   # My Status
        assert stub._status_row_contact[1] == -1   # "--- Recentes ---"
        assert stub._status_row_contact[3] == -1   # "--- Vistos ---"

    def test_only_a_recent_section_is_shown_when_nothing_has_been_viewed(self):
        stub = _Stub()
        entry = _entry("a@s.whatsapp.net", [_text_status("oi")])
        entry["viewed_all"] = False

        stub._populate_list([], [entry])

        assert "— Vistos —" not in stub._status_list.items

    def test_space_key_uses_the_same_row_map_after_populate_list(self):
        """Regression coverage for the exact bug described in this class's
        own docstring: Space must resolve the row it's actually focused on
        via _status_row_contact, not a hardcoded idx - 1, once header rows
        exist ahead of a contact's row."""
        stub = _Stub(settings={"status_panel": {"viewed_status_ids": ["seen1"]}})
        seen = _entry("seen@s.whatsapp.net", [
            {"key": {"id": "seen1", "fromMe": False}, "messageType": "conversation",
             "message": {"conversation": "x"}, "messageTimestamp": 1},
        ])
        seen["viewed_all"] = True
        unseen = _entry("unseen@s.whatsapp.net", [
            {"key": {"id": "unseen1", "fromMe": False}, "messageType": "conversation",
             "message": {"conversation": "y"}, "messageTimestamp": 2},
        ])
        unseen["viewed_all"] = False
        stub._populate_list([], [seen, unseen])
        # Row 4 is the "Vistos" section's contact (seen@s.whatsapp.net) —
        # a hardcoded `idx - 1` would wrongly resolve to _status_contacts[3],
        # which doesn't exist (only indices 0/1 are real contacts here).
        stub._status_list = _FakeStatusList(focused=4)
        stub._status_list.items = ["My Status", "— Recentes —", "unseen", "— Vistos —", "seen"]

        stub._on_status_list_key_down(_FakeKeyEvent(wx.WXK_SPACE))

        assert stub._selected_contact_idx == stub._status_row_contact[4]
        assert stub._status_contacts[stub._selected_contact_idx]["jid"] == "seen@s.whatsapp.net"


class TestStatusNavigationWrapsAround:
    """Ctrl+Right past the last status of a contact wraps back to their
    first one, and Ctrl+Left before the first wraps to their last — an
    earlier version closed the viewer instead of wrapping on Ctrl+Right;
    the user explicitly asked for wrap-around on both directions."""

    def test_advances_to_the_next_status_normally(self):
        stub = _Stub()
        stub._status_contacts = [_entry("a@s.whatsapp.net", [_text_status("a"), _text_status("b")])]
        stub._selected_contact_idx = 0
        stub._current_status_idx = 0
        stub._show_current_status = lambda: None

        stub._on_next_status(None)

        assert stub._current_status_idx == 1

    def test_next_wraps_from_the_last_status_back_to_the_first(self):
        stub = _Stub()
        stub._status_contacts = [_entry("a@s.whatsapp.net", [_text_status("a"), _text_status("b")])]
        stub._selected_contact_idx = 0
        stub._current_status_idx = 1  # already on the last one
        stub._show_current_status = lambda: None

        stub._on_next_status(None)

        assert stub._current_status_idx == 0

    def test_prev_wraps_from_the_first_status_back_to_the_last(self):
        stub = _Stub()
        stub._status_contacts = [_entry("a@s.whatsapp.net", [_text_status("a"), _text_status("b")])]
        stub._selected_contact_idx = 0
        stub._current_status_idx = 0  # already on the first one
        stub._show_current_status = lambda: None

        stub._on_prev_status(None)

        assert stub._current_status_idx == 1


class TestEscapeClosesTheViewer:
    def test_closes_when_shown(self):
        stub = _Stub()
        stub._viewer_panel.Show()
        stub._selected_contact_idx = 0

        stub._on_escape(None)

        assert stub._viewer_panel.shown is False
        assert stub._selected_contact_idx == -1
        assert stub._video_player.stop_calls == 1

    def test_no_op_when_already_hidden(self):
        stub = _Stub()
        stub._viewer_panel.Hide()

        stub._on_escape(None)  # must not raise even with event=None

        assert stub._video_player.stop_calls == 0


class TestStatusComposerKeepsTheMainScreenClean:
    @pytest.mark.parametrize(
        "panel_name",
        ["_post_panel", "_media_post_panel", "_voice_post_panel"],
    )
    def test_only_the_selected_add_status_panel_is_visible(self, panel_name):
        stub = _Stub()
        stub._viewer_panel.Show()
        selected_panel = getattr(stub, panel_name)

        stub._enter_status_composer(selected_panel)

        assert selected_panel.shown is True
        assert sum(
            panel.shown
            for panel in (
                stub._post_panel,
                stub._media_post_panel,
                stub._voice_post_panel,
            )
        ) == 1
        assert stub._viewer_panel.shown is False
        assert stub._add_status_btn.shown is False
        assert stub._refresh_status_btn.shown is False
        assert stub._list_label.shown is False
        assert stub._status_list.shown is False
        assert stub._video_player.stop_calls == 1
        assert selected_panel.enabled is True
        assert all(
            panel.enabled is (panel is selected_panel)
            for panel in (
                stub._post_panel,
                stub._media_post_panel,
                stub._voice_post_panel,
            )
        )

    def test_closing_a_composer_restores_the_status_browser(self):
        stub = _Stub()
        stub._enter_status_composer(stub._post_panel)

        stub._leave_status_composer()

        assert stub._is_status_composer_open() is False
        assert stub._add_status_btn.shown is True
        assert stub._refresh_status_btn.shown is True
        assert stub._list_label.shown is True
        assert stub._status_list.shown is True

    def test_escape_closes_the_active_composer_before_the_viewer(self):
        stub = _Stub()
        stub._enter_status_composer(stub._post_panel)

        stub._on_escape(None)

        assert stub._is_status_composer_open() is False
        assert stub._status_list.shown is True


class TestStatusReplyKeepsTheStatusQuote:
    """A status reply must arrive as a reply to that status, not a plain DM.

    The Node route resolves the status in the poster's StatusV3Model, which
    means the UI must pass the complete selected status as the quote target.
    """

    def test_reply_passes_the_selected_status_as_quote(self, monkeypatch):
        _run_threads_synchronously(monkeypatch)
        status = _text_status("oi")
        stub = _Stub()
        stub._current_status = status
        stub._current_status_entry = _entry("poster@s.whatsapp.net", [status])
        stub._reply_field.SetValue("valeu!")

        stub._on_send_status_reply(None)

        assert stub.main_window.send_text_calls == [
            ("poster@s.whatsapp.net", "valeu!", status)
        ]

    def test_media_viewer_reply_passes_the_open_status_as_quote(self, monkeypatch):
        _run_threads_synchronously(monkeypatch)
        status = _text_status("oi")
        item = {
            "status": status,
            "entry": _entry("poster@s.whatsapp.net", [status]),
        }
        stub = _Stub()
        completed = []

        stub._viewer_reply_status(item, "valeu!", completed.append)

        assert stub.main_window.send_text_calls == [
            ("poster@s.whatsapp.net", "valeu!", status)
        ]
        assert completed == [True]


class TestFailedStatusReplyNeverDegradesToPlainMessage:
    """If the quote cannot be created, reporting failure is safer than
    silently delivering a normal DM and calling it a successful status reply.
    """

    class _Response:
        status_code = 500
        text = "status quote not found"

    class _MainStub:
        send_text_message = MainWindow.send_text_message
        _build_link_preview_options = staticmethod(MainWindow._build_link_preview_options)

        def __init__(self):
            self.wpp_server = "http://127.0.0.1"
            self.wpp_port = 21465
            self.token = "session:token"
            self.i18n = _FakeI18n()
            self.outputs = []

        def _resolve_jid_for_send(self, jid):
            return jid.replace("@s.whatsapp.net", "@c.us")

        def _serialize_quoted_id(self, quoted, fallback_jid=None):
            return "false_status@broadcast_s1_poster@c.us"

        def _legacy_phone_for_send(self, jid):
            return ""

        def _check_wa_connection_closed(self, response):
            return False

        def _set_wa_connected(self, connected, reason):
            pass

        def _classify_send_exception(self, exc, where):
            raise AssertionError(f"unexpected exception in {where}: {exc}")

        def output(self, text, interrupt=False):
            self.outputs.append(text)

    def test_http_failure_is_not_retried_through_send_message(self, monkeypatch):
        calls = []

        def _post(url, **kwargs):
            calls.append((url, kwargs["json"]))
            return self._Response()

        monkeypatch.setattr(main_module, "api_post", _post)
        stub = self._MainStub()

        result = stub.send_text_message(
            "poster@s.whatsapp.net",
            "valeu!",
            quoted=_text_status("oi"),
        )

        assert result["ok"] is False
        assert len(calls) == 1
        assert calls[0][0].endswith("/send-reply")
        assert calls[0][1]["messageId"].startswith(
            "false_status@broadcast_"
        )
        assert stub.outputs == []


class TestStatusReplySentRefocusesTheField:
    """Reported live: after a successful reply, _reply_send_btn hides
    again (the field is cleared, and _on_reply_field_text_changed() hides
    the button once it's empty) but keyboard focus was left on that now-
    hidden button with nothing to land on."""

    def test_clears_and_refocuses_the_reply_field(self):
        stub = _Stub()
        stub._reply_field.SetValue("valeu!")

        stub._on_status_reply_sent()

        assert stub._reply_field.GetValue() == ""
        assert stub._reply_field.set_focus_calls == 1
