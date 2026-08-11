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

from status_panel import StatusPanel, _status_content_label


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
        "notif_unsupported": "Mensagem não suportada",
    }

    def t(self, key):
        return self._STRINGS.get(key, f"[{key}]")


class _FakeWidget:
    def __init__(self):
        self.shown = False
        self.label = ""

    def Show(self, show=True):
        self.shown = bool(show)

    def Hide(self):
        self.shown = False

    def IsShown(self):
        return self.shown

    def SetLabel(self, text):
        self.label = text


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
    def __init__(self, contact_names=None, send_text_result=True, settings=None):
        self.i18n = _FakeI18n()
        self.outputs = []
        self._contact_names = contact_names or {}
        self.chats = {}
        self._status_updates = {}
        self.app_name = "WinZapp"
        self._send_text_result = send_text_result
        self.send_text_calls = []
        self.settings = settings if settings is not None else {}
        self.save_settings_calls = 0

    def _resolve_contact_name(self, chat):
        return self._contact_names.get(chat.get("remoteJid", ""))

    def _is_self_jid(self, jid):
        return False

    def send_text_message(self, remote_jid, text, quoted=None):
        self.send_text_calls.append((remote_jid, text, quoted))
        return self._send_text_result

    def save_settings(self):
        self.save_settings_calls += 1
        
    def mark_conversation_as_read(self, jid):
        pass

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
        self.select_calls = []

    def GetFocusedItem(self):
        return self._focused

    def Select(self, idx):
        self.select_calls.append(idx)

    def SetFocus(self):
        pass


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
    _is_current_status_playable  = StatusPanel._is_current_status_playable
    _on_reply_field_text_changed = StatusPanel._on_reply_field_text_changed
    _resolve_name                = StatusPanel._resolve_name
    _status_preview              = StatusPanel._status_preview
    _on_play_pause_video         = StatusPanel._on_play_pause_video
    _update_play_pause_label     = StatusPanel._update_play_pause_label
    _is_status_liked             = StatusPanel._is_status_liked
    _on_like_status              = StatusPanel._on_like_status
    _on_like_sent                = StatusPanel._on_like_sent
    _on_unlike_status_attempted  = StatusPanel._on_unlike_status_attempted
    _parse_statuses              = StatusPanel._parse_statuses
    _latest_ts                   = StatusPanel._latest_ts
    _on_next_status               = StatusPanel._on_next_status
    _on_escape                    = StatusPanel._on_escape
    _MAX_REMEMBERED_LIKES         = StatusPanel._MAX_REMEMBERED_LIKES
    _on_send_status_reply         = StatusPanel._on_send_status_reply
    _send_status_reply_bg         = StatusPanel._send_status_reply_bg
    _on_status_reply_sent         = StatusPanel._on_status_reply_sent

    def __init__(self, contact_names=None, send_text_result=True, settings=None):
        self.main_window = _FakeMainWindow(
            contact_names=contact_names, send_text_result=send_text_result, settings=settings,
        )
        self._status_contacts      = []
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
        self._list_indices = {}
        self.my_status_dialog_calls = 0

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
        stub._list_indices = {1: 0}
        stub._selected_contact_idx = 0
        stub._current_status_idx   = 2  # "status 3 de 5"

        class _Evt:
            def GetIndex(self):
                return 1  # row 1 = the same already-selected contact (row 0 is My Status)

        stub._on_status_contact_selected(_Evt())

        assert stub._current_status_idx == 2
        assert "3 de 5" in stub._status_content_label.label

    def test_selecting_a_different_contact_resets_to_the_first_status(self):
        stub = _Stub()
        statuses_a = [_text_status("a"), _text_status("b")]
        statuses_b = [_text_status("x")]
        stub._status_contacts = [
            _entry("a@s.whatsapp.net", statuses_a),
            _entry("b@s.whatsapp.net", statuses_b),
        ]
        stub._list_indices = {1: 0, 2: 1}
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


class TestEnterAndSpaceTogglePlaybackOnStatusList:
    """Feature request: Enter/Space activating a status-list item should
    play/pause its media, not just re-select an already-shown status
    (which would stop() and restart the player instead of pausing it)."""

    def test_enter_on_a_video_status_already_shown_toggles_pause(self):
        stub = _Stub()
        stub._status_contacts = [_entry("a", [_video_status()])]
        stub._list_indices = {1: 0}
        stub._selected_contact_idx = 0
        stub._current_status = _video_status()
        stub._video_player.is_playing = True

        class _Evt:
            def GetIndex(self):
                return 1  # row 1 = contact at _status_contacts[0]

        stub._on_status_contact_activated(_Evt())

        assert stub._video_player.toggle_pause_calls == 1

    def test_enter_on_a_text_status_does_not_try_to_toggle(self):
        stub = _Stub()
        stub._status_contacts = [_entry("a@s.whatsapp.net", [_text_status("oi")])]
        stub._list_indices = {1: 0}
        stub._selected_contact_idx = 0
        stub._current_status = _text_status("oi")

        class _Evt:
            def GetIndex(self):
                return 1

        stub._on_status_contact_activated(_Evt())

        assert stub._video_player.toggle_pause_calls == 0

    def test_enter_on_a_not_yet_selected_contact_selects_instead_of_toggling(self):
        stub = _Stub()
        stub._status_contacts = [_entry("a", [_video_status()])]
        stub._list_indices = {1: 0}
        stub._selected_contact_idx = -1
        stub._current_status = None

        class _Evt:
            def GetIndex(self): return 1
            def GetKeyCode(self): return wx.WXK_RETURN
        stub._on_status_contact_activated(_Evt())

        assert stub._video_player.toggle_pause_calls == 0
        assert stub._selected_contact_idx == 0

    def test_enter_on_row_zero_opens_my_status_dialog(self):
        stub = _Stub()
        stub._list_indices = {0: -1}

        class _Evt:
            def GetIndex(self): return 0
            def GetKeyCode(self): return wx.WXK_RETURN
        stub._on_status_contact_activated(_Evt())

        assert stub.my_status_dialog_calls == 1

    def test_space_on_a_video_status_already_shown_toggles_pause_without_reselecting(self):
        stub = _Stub()
        stub._status_contacts = [_entry("a", [_video_status()])]
        stub._list_indices = {1: 0}
        stub._selected_contact_idx = 0
        stub._current_status = _video_status()
        stub._video_player.is_playing = True
        stub._status_list = _FakeStatusList(focused=1)

        stub._on_status_list_key_down(_FakeKeyEvent(wx.WXK_SPACE))

        assert stub._video_player.toggle_pause_calls == 1
        assert stub._status_list.select_calls == []

    def test_space_on_a_non_playing_row_still_selects_normally(self):
        stub = _Stub()
        stub._status_contacts = [_entry("a@s.whatsapp.net", [_video_status()])]
        stub._list_indices = {1: 0}
        stub._selected_contact_idx = -1
        stub._current_status = None
        stub._status_list = _FakeStatusList(focused=1)

        stub._on_status_list_key_down(_FakeKeyEvent(wx.WXK_SPACE))

        assert stub._video_player.toggle_pause_calls == 0
        assert stub._status_list.select_calls == [1]
        assert stub._selected_contact_idx == 0

    def test_other_keys_are_skipped(self):
        stub = _Stub()
        evt = _FakeKeyEvent(ord("A"))

        stub._on_status_list_key_down(evt)

        assert evt.skip_calls == 1


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

    def test_unknown_type_falls_back_to_translated_generic_label(self):
        # Never the raw type string itself.
        label = _status_content_label("someBrandNewWhatsAppType", {}, self.i18n)
        assert label == "Mensagem não suportada"


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


class TestLikeStatusSendsAPlainEmojiMessage:
    """_on_like_status() no longer uses send_reaction() (WhatsApp Web's
    Store never indexes another person's status — see the method's own
    docstring) — it sends the like emoji as a normal message to the
    poster instead, deliberately WITHOUT quoting the status: WPPConnect's
    send-reply endpoint can't resolve a status as a quote target any more
    than the reaction endpoint could resolve it as a reaction target, so
    quoting it always failed server-side and silently fell back to a
    plain send anyway (reported live as "Não foi possível citar a
    mensagem original")."""

    def test_sends_heart_emoji_without_quoting(self, monkeypatch):
        _run_threads_synchronously(monkeypatch)
        status = _text_status("oi")
        entry = _entry("poster@s.whatsapp.net", [status])
        stub = _Stub()
        stub._status_contacts = [entry]
        stub._selected_contact_idx = 0
        stub._current_status_idx = 0
        stub._current_status = status

        stub._on_like_status(None)

        assert stub.main_window.send_text_calls == [
            ("poster@s.whatsapp.net", "❤️", None)
        ]

    def test_marks_liked_persists_to_settings_and_updates_button_label_on_success(self, monkeypatch):
        _run_threads_synchronously(monkeypatch)
        status = _text_status("oi")
        stub = _Stub(send_text_result=True)
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
        stub = _Stub(send_text_result=False)
        stub._status_contacts = [_entry("poster@s.whatsapp.net", [status])]
        stub._selected_contact_idx = 0
        stub._current_status_idx = 0
        stub._current_status = status

        stub._on_like_status(None)

        assert stub._liked_statuses.get(status["key"]["id"]) is None
        assert len(calls) == 1
        assert stub.main_window.save_settings_calls == 0

    def test_clicking_like_again_shows_unlike_unsupported_message_instead_of_resending(self, monkeypatch):
        _run_threads_synchronously(monkeypatch)
        calls = []
        monkeypatch.setattr(wx, "MessageBox", lambda *a, **kw: calls.append(a))
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
        assert len(calls) == 1


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


class TestNextStatusNoLongerWrapsAround:
    """Ctrl+Right past the last status of a contact used to loop back to
    their first one — now closes the viewer and refreshes instead, like
    moving on to the next contact rather than re-showing the one just
    finished."""

    def test_advances_to_the_next_status_normally(self):
        stub = _Stub()
        stub._status_contacts = [_entry("a@s.whatsapp.net", [_text_status("a"), _text_status("b")])]
        stub._selected_contact_idx = 0
        stub._current_status_idx = 0
        stub._show_current_status = lambda: None

        stub._on_next_status(None)

        assert stub._current_status_idx == 1

    def test_closes_and_refreshes_instead_of_wrapping_at_the_last_status(self):
        stub = _Stub()
        stub._status_contacts = [_entry("a@s.whatsapp.net", [_text_status("a"), _text_status("b")])]
        stub._selected_contact_idx = 0
        stub._current_status_idx = 1  # already on the last one
        stub._viewer_panel.Show()

        stub._on_next_status(None)

        assert stub._current_status_idx == 1  # unchanged — did not wrap to 0
        assert stub._viewer_panel.shown is False
        assert stub.refresh_calls == 1


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


class TestStatusReplySendsWithoutQuoting:
    """Same reasoning as TestLikeStatusSendsAPlainEmojiMessage: WPPConnect
    can't resolve a status as a quote target from the poster's own chat,
    so send-reply always failed server-side and silently fell back to a
    plain send anyway — reported live as "Não foi possível citar a
    mensagem original"."""

    def test_reply_is_sent_without_the_quoted_kwarg(self, monkeypatch):
        _run_threads_synchronously(monkeypatch)
        status = _text_status("oi")
        stub = _Stub()
        stub._current_status = status
        stub._current_status_entry = _entry("poster@s.whatsapp.net", [status])
        stub._reply_field.SetValue("valeu!")

        stub._on_send_status_reply(None)

        assert stub.main_window.send_text_calls == [
            ("poster@s.whatsapp.net", "valeu!", None)
        ]


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
