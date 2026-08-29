"""Tests for in-app video-message playback wiring in
ConversationsPanel (client/ui/conversations.py) — audio via BASS, frames
via ffmpeg, see client/core/video_player.py.

Enter on a video message row is the only trigger now (the separate
Play/Pause button that used to sit next to Open/Save As was removed — it
made no sense as a standing UI element and duplicated what Enter should
just do directly). Only the parts that don't require a real ffmpeg
process / running wx.App are exercised here: _play_toggle_video_message()'s
"already playing the SAME video -> just toggle pause" vs. "different video
-> switch to it" branches, and _hide_all_media_controls() always stopping
the player (selection/conversation changes must never leave an orphaned
ffmpeg subprocess or audio channel running in the background).
"""

from ui.conversations import ConversationsPanel


class _FakeVideoPlayer:
    def __init__(self, is_playing=False):
        self.is_playing = is_playing
        self.toggle_pause_calls = 0
        self.stop_calls = 0
        self.load_and_play_calls = []

    def toggle_pause(self):
        self.toggle_pause_calls += 1

    def stop(self):
        self.stop_calls += 1
        self.is_playing = False

    def load_and_play(self, path, speed=1.0):
        self.load_and_play_calls.append((path, speed))
        self.is_playing = True


class _FakeWidget:
    def __init__(self):
        self.shown = False
        # Last size the sizer was asked to reserve for this control. Video
        # playback installs ConversationsPanel._VIDEO_BITMAP_SIZE here and
        # releases it (-1, -1) again afterwards — see _start_video_playback /
        # _hide_all_media_controls, and core/video_player.fit_frame_size for
        # why the box has to exist at all.
        self.min_size = (-1, -1)

    def Show(self, show=True):
        self.shown = bool(show)

    def Hide(self):
        self.shown = False

    def SetMinSize(self, size):
        self.min_size = tuple(size)


class _FakeMainWindow:
    def output(self, text, interrupt=False):
        pass


class _FakeMessagesList:
    def __init__(self, focused=0):
        self._focused = focused

    def GetFirstSelected(self):
        return self._focused

    def GetFocusedItem(self):
        return self._focused


def _video_msg(mid="v1"):
    return {"key": {"id": mid}, "messageType": "videoMessage", "message": {"videoMessage": {}}}


class _Stub:
    _VIDEO_BITMAP_SIZE           = ConversationsPanel._VIDEO_BITMAP_SIZE
    _play_toggle_video_message   = ConversationsPanel._play_toggle_video_message
    _hide_all_media_controls     = ConversationsPanel._hide_all_media_controls
    _update_links_panel          = lambda self, links: None
    _update_mentions_panel       = lambda self, mentions: None
    _hide_media_transfer_gauge   = lambda self: None

    def __init__(self, sorted_messages, is_playing=False, current_video_msg_id=None):
        self.main_window   = _FakeMainWindow()
        self._sorted_messages = sorted_messages
        self.messages_list = _FakeMessagesList()
        self._video_player = _FakeVideoPlayer(is_playing=is_playing)
        self._current_video_msg_id = current_video_msg_id
        self._media_bitmap         = _FakeWidget()
        self._action_open_btn      = _FakeWidget()
        self._action_save_as_btn   = _FakeWidget()
        self._action_download_btn  = _FakeWidget()
        self._buttons_container    = _FakeWidget()
        self._contact_converse_btn = _FakeWidget()
        self._contact_save_btn     = _FakeWidget()
        self._contact_msg_jid      = None
        self.conversation_panel    = _FakeWidget()
        self.conversation_panel.IsShown = lambda: False
        self.conversation_panel.Layout = lambda: None
        self.hide_audio_controls_calls = 0

    def _hide_audio_controls(self):
        self.hide_audio_controls_calls += 1


class TestPlayToggleTogglesPauseForTheSameVideo:
    def test_toggles_pause_instead_of_restarting(self):
        stub = _Stub([_video_msg("v1")], is_playing=True, current_video_msg_id="v1")

        stub._play_toggle_video_message(_video_msg("v1"))

        assert stub._video_player.toggle_pause_calls == 1
        assert stub._video_player.load_and_play_calls == []

    def test_non_video_message_is_ignored(self):
        stub = _Stub([], is_playing=True, current_video_msg_id="v1")

        stub._play_toggle_video_message({"key": {"id": "x"}, "messageType": "conversation"})

        assert stub._video_player.toggle_pause_calls == 0


class TestHideAllMediaControlsAlwaysStopsTheVideoPlayer:
    def test_stops_the_player(self):
        stub = _Stub([], is_playing=True, current_video_msg_id="v1")

        stub._hide_all_media_controls()

        assert stub._video_player.stop_calls == 1
        assert stub._current_video_msg_id is None

    def test_releases_the_video_sized_box_so_thumbnails_size_themselves_again(self):
        stub = _Stub([], is_playing=True, current_video_msg_id="v1")
        stub._media_bitmap.SetMinSize(ConversationsPanel._VIDEO_BITMAP_SIZE)

        stub._hide_all_media_controls()

        assert stub._media_bitmap.min_size == (-1, -1)

    def test_stops_the_player_even_when_nothing_was_playing(self):
        stub = _Stub([], is_playing=False)

        stub._hide_all_media_controls()

        assert stub._video_player.stop_calls == 1

    def test_hides_the_shared_speed_slider_controls_when_video_was_playing(self):
        """A video holds the same shared speed button/progress slider audio
        uses (see TestStartVideoPlaybackShowsSharedControls below) — nothing
        else hides them once video stops here, so this must do it itself."""
        stub = _Stub([], is_playing=True, current_video_msg_id="v1")

        stub._hide_all_media_controls()

        assert stub.hide_audio_controls_calls == 1

    def test_does_not_touch_shared_controls_when_video_was_not_playing(self):
        """No video was active — the shared controls may currently belong to
        audio playing in the background, which must be left alone here."""
        stub = _Stub([], is_playing=False, current_video_msg_id=None)

        stub._hide_all_media_controls()

        assert stub.hide_audio_controls_calls == 0


# ── Shared speed/seek controls (same widgets ConversationsPanel's own audio
# playback uses — see on_audio_speed_btn/on_audio_slider) now also apply to
# video playback via _start_video_playback/on_audio_timer. ──────────────────

class _FakeI18n:
    def t(self, key):
        return {"decimal_separator": "."}.get(key, key)


class _FakeSettingsMainWindow(_FakeMainWindow):
    def __init__(self):
        self.i18n = _FakeI18n()
        self.settings = {}
        self.save_settings_calls = 0

    def save_settings(self):
        self.save_settings_calls += 1


class _ControlsStub:
    _VIDEO_BITMAP_SIZE    = ConversationsPanel._VIDEO_BITMAP_SIZE
    _start_video_playback = ConversationsPanel._start_video_playback
    _focused_msg_id       = ConversationsPanel._focused_msg_id
    _is_separator         = ConversationsPanel._is_separator
    _format_speed         = ConversationsPanel._format_speed
    on_audio_timer        = ConversationsPanel.on_audio_timer
    on_audio_slider       = ConversationsPanel.on_audio_slider
    _apply_audio_speed    = ConversationsPanel._apply_audio_speed

    def __init__(self, sorted_messages, focused=0, current_video_msg_id=None,
                 video_playing=False, audio_stream=None, current_audio_id=None):
        self.main_window = _FakeSettingsMainWindow()
        self._sorted_messages = sorted_messages
        self.messages_list = _FakeMessagesList(focused=focused)
        self._video_player = _FakeVideoPlayer(is_playing=video_playing)
        self._current_video_msg_id = current_video_msg_id
        self._audio_stream = audio_stream
        self._audio_tempo_ctrl = None
        self._current_audio_id = current_audio_id
        self._audio_speed_steps = [1.0, 1.5, 2.0]
        self._audio_speed_index = 0
        self._audio_tempo_map = {1.0: 0, 1.5: 50, 2.0: 100}
        self.audio_speed_btn = _FakeWidget()
        self.audio_speed_btn.SetLabel = lambda label: setattr(self.audio_speed_btn, "label", label)
        self.audio_slider = _FakeSlider()
        self._audio_timer = _FakeTimer()
        self._media_bitmap = _FakeWidget()
        self.conversation_panel = _FakeWidget()
        self.conversation_panel.Layout = lambda: None
        self.hide_audio_controls_calls = 0
        self.show_audio_controls_calls = 0

    def _hide_audio_controls(self):
        self.hide_audio_controls_calls += 1

    def _show_audio_controls(self):
        self.show_audio_controls_calls += 1


class _FakeTimer:
    def __init__(self):
        self.running = False

    def IsRunning(self):
        return self.running

    def Start(self, interval):
        self.running = True


class _FakeSlider(_FakeWidget):
    def __init__(self, value=0):
        super().__init__()
        self._value = value

    def GetValue(self):
        return self._value

    def SetValue(self, value):
        self._value = value

    def Refresh(self):
        pass


class TestStartVideoPlaybackShowsSharedControls:
    def test_shows_controls_when_focused_row_is_the_one_playing(self):
        stub = _ControlsStub([_video_msg("v1")], focused=0)

        stub._start_video_playback("/tmp/v1.mp4", 1.5, "v1")

        assert stub._video_player.load_and_play_calls == [("/tmp/v1.mp4", 1.5)]
        assert stub.show_audio_controls_calls == 1
        assert stub.audio_speed_btn.label == "1.5×"

    def test_shows_the_bitmap_control_even_without_a_thumbnail_ever_shown(self):
        """_media_bitmap is otherwise only shown by _try_show_thumbnail()
        (gated on the message having an embedded jpegThumbnail) — a video
        with none left the player rendering frames into a hidden control.
        Regression test for that: must always show it, thumbnail or not."""
        stub = _ControlsStub([_video_msg("v1")], focused=0)
        assert stub._media_bitmap.shown is False

        stub._start_video_playback("/tmp/v1.mp4", 1.0, "v1")

        assert stub._media_bitmap.shown is True

    def test_gives_the_bitmap_a_video_sized_box_to_render_into(self):
        """wx.StaticBitmap clips rather than scales, so a 480 px-wide frame
        drawn into a control still sized for a <=200 px thumbnail (or for
        nothing at all) showed only a corner of the picture — reported as
        "os videos so abrem pela metade". The player scales each frame down
        into whatever box it finds (core/video_player.fit_frame_size), so a
        real box has to be installed before playback starts."""
        stub = _ControlsStub([_video_msg("v1")], focused=0)

        stub._start_video_playback("/tmp/v1.mp4", 1.0, "v1")

        assert stub._media_bitmap.min_size == ConversationsPanel._VIDEO_BITMAP_SIZE

    def test_does_not_show_controls_when_a_different_row_is_focused(self):
        stub = _ControlsStub([_video_msg("v1"), _video_msg("v2")], focused=1)

        stub._start_video_playback("/tmp/v1.mp4", 1.0, "v1")

        assert stub.show_audio_controls_calls == 0


class TestOnAudioTimerVideoBranch:
    def test_updates_slider_while_video_is_playing(self):
        stub = _ControlsStub([], current_video_msg_id="v1", video_playing=True)
        stub._video_player.get_position = lambda: 250
        stub._video_player.get_length = lambda: 1000

        stub.on_audio_timer(None)

        assert stub.audio_slider.GetValue() == 250
        assert stub.hide_audio_controls_calls == 0

    def test_hides_controls_once_video_stops_playing(self):
        stub = _ControlsStub([], current_video_msg_id="v1", video_playing=False)

        stub.on_audio_timer(None)

        assert stub._current_video_msg_id is None
        assert stub.hide_audio_controls_calls == 1

    def test_leaves_audio_playback_untouched_when_no_video_is_active(self):
        """No _current_audio_id/_audio_stream in this stub — must return
        early rather than raising on the audio branch it doesn't set up."""
        stub = _ControlsStub([], current_video_msg_id=None)

        stub.on_audio_timer(None)  # must not raise


class TestOnAudioSliderVideoBranch:
    def test_seeks_the_video_player_while_it_is_playing(self):
        stub = _ControlsStub([], current_video_msg_id="v1", video_playing=True)
        stub._video_player.get_length = lambda: 2000
        stub._video_player.set_position = lambda pos: setattr(stub, "seek_pos", pos)
        stub.audio_slider.SetValue(500)

        stub.on_audio_slider(None)

        assert stub.seek_pos == 1000

    def test_does_not_touch_video_player_when_no_video_is_active(self):
        stub = _ControlsStub([], current_video_msg_id=None, audio_stream=None)

        stub.on_audio_slider(None)  # must not raise, falls through (no audio_stream either)


class TestApplyAudioSpeedVideoBranch:
    def test_sets_speed_on_the_video_player_while_it_is_playing(self):
        stub = _ControlsStub([], current_video_msg_id="v1", video_playing=True)
        stub._video_player.set_speed = lambda speed: setattr(stub, "applied_speed", speed)
        stub._audio_speed_index = 2  # 2.0x

        stub._apply_audio_speed()

        assert stub.applied_speed == 2.0
        assert stub.audio_speed_btn.label == "2.0×"
        assert stub.main_window.save_settings_calls == 1


# ── Settings > Interface do usuário > "Mostrar vídeos nas conversas em
# player separado" — unchecked keeps the classic in-app player (BASS/ffmpeg,
# no dialog) instead of MediaViewerDialog, mirroring the equivalent status
# setting (_use_status_media_viewer_dialog in status_panel.py). Images are
# never affected by this setting. ───────────────────────────────────────────

class _FakeSettingsHolder:
    def __init__(self, settings):
        self.settings = settings


class _ActivationStub:
    _is_separator                              = ConversationsPanel._is_separator
    _use_conversation_video_media_viewer_dialog = (
        ConversationsPanel._use_conversation_video_media_viewer_dialog
    )
    _do_activate_message                        = ConversationsPanel._do_activate_message

    def __init__(self, sorted_messages, settings=None):
        self.main_window = _FakeSettingsHolder(settings or {})
        self._sorted_messages = sorted_messages
        self._render_message_line = lambda msg: ""
        self._extract_links = lambda rendered: []
        self._extract_mentions = lambda msg: []
        self._update_links_panel = lambda links: None
        self._update_mentions_panel = lambda mentions: None
        self._sync_media_action_slot_visibility = lambda: None
        self.media_viewer_calls = []
        self.play_toggle_calls = []

    def _open_conversation_media_viewer(self, index):
        self.media_viewer_calls.append(index)

    def _play_toggle_video_message(self, msg):
        self.play_toggle_calls.append(msg)


class TestUseConversationVideoMediaViewerDialogSetting:
    def test_default_setting_uses_the_dialog(self):
        stub = _ActivationStub([])  # no settings override — must default to True
        assert stub._use_conversation_video_media_viewer_dialog() is True

    def test_disabled_setting_uses_the_classic_player(self):
        stub = _ActivationStub(
            [], settings={"user_interface": {"conversation_video_media_viewer_dialog": False}}
        )
        assert stub._use_conversation_video_media_viewer_dialog() is False


class TestVideoActivationRespectsTheSetting:
    def test_dialog_mode_opens_the_media_viewer(self):
        stub = _ActivationStub([_video_msg("v1")])

        stub._do_activate_message(0)

        assert stub.media_viewer_calls == [0]
        assert stub.play_toggle_calls == []

    def test_classic_mode_plays_in_app_instead(self):
        stub = _ActivationStub(
            [_video_msg("v1")],
            settings={"user_interface": {"conversation_video_media_viewer_dialog": False}},
        )

        stub._do_activate_message(0)

        assert stub.play_toggle_calls == [_video_msg("v1")]
        assert stub.media_viewer_calls == []

    def test_classic_mode_ignores_gif_playback(self):
        """GIFs (videoMessage with gifPlayback) have no audio track — Enter
        must no-op in classic mode instead of trying to play one, matching
        the pre-dialog behaviour this mode restores."""
        gif_msg = {
            "key": {"id": "g1"}, "messageType": "videoMessage",
            "message": {"videoMessage": {"gifPlayback": True}},
        }
        stub = _ActivationStub(
            [gif_msg],
            settings={"user_interface": {"conversation_video_media_viewer_dialog": False}},
        )

        stub._do_activate_message(0)

        assert stub.play_toggle_calls == []
        assert stub.media_viewer_calls == []

    def test_image_messages_always_use_the_dialog_regardless_of_the_setting(self):
        image_msg = {
            "key": {"id": "i1"}, "messageType": "imageMessage",
            "message": {"imageMessage": {}},
        }
        stub = _ActivationStub(
            [image_msg],
            settings={"user_interface": {"conversation_video_media_viewer_dialog": False}},
        )

        stub._do_activate_message(0)

        assert stub.media_viewer_calls == [0]
        assert stub.play_toggle_calls == []


# ── The "Abrir" (Open) action is a different action from Enter/click: it
# always means "hand this file to my OS-default video player", regardless
# of the in-app dialog/classic setting above — that setting only controls
# what Enter/click does. Reported live: "Abrir" on a video opened the exact
# same in-app viewer Enter already opens, instead of the external player. ──

class _FakeThread:
    """Records the target/args instead of actually spawning a thread —
    _on_action_open's fallthrough path (decrypt + tempfile + external open)
    needs a live wx.App/network to run for real; only the *dispatch*, not
    its result, is under test here."""
    instances = []

    def __init__(self, target=None, args=(), daemon=None):
        self.target = target
        self.args = args
        _FakeThread.instances.append(self)

    def start(self):
        pass


class _OpenActionStub:
    _on_action_open    = ConversationsPanel._on_action_open
    open_media_message = ConversationsPanel.open_media_message
    _use_conversation_video_media_viewer_dialog = (
        ConversationsPanel._use_conversation_video_media_viewer_dialog
    )

    def __init__(self, sorted_messages, settings=None):
        self.main_window = _FakeSettingsHolder(settings or {})
        self._sorted_messages = sorted_messages
        self.messages_list = _FakeMessagesList(focused=0)
        self._location_maps_url = lambda msg: None
        self.media_viewer_calls = []

    def _open_conversation_media_viewer(self, index):
        self.media_viewer_calls.append(index)


class TestOpenActionAlwaysExternalForVideo:
    def _run(self, settings):
        _FakeThread.instances = []
        video_msg = {
            "key": {"id": "v1"}, "messageType": "videoMessage",
            "message": {"videoMessage": {}},
        }
        stub = _OpenActionStub([video_msg], settings=settings)
        import ui.conversations as conv_mod
        orig_thread = conv_mod.threading.Thread
        orig_data_path = conv_mod.data_path
        conv_mod.threading.Thread = _FakeThread
        conv_mod.data_path = lambda *parts: "/".join(("fake_data",) + parts)
        try:
            stub._on_action_open(None, index=0)
        finally:
            conv_mod.threading.Thread = orig_thread
            conv_mod.data_path = orig_data_path
        return stub

    def test_dialog_mode_still_opens_externally_not_the_dialog(self):
        stub = self._run(settings=None)  # default: dialog mode for Enter/click

        assert stub.media_viewer_calls == []
        assert len(_FakeThread.instances) == 1

    def test_classic_mode_also_opens_externally(self):
        stub = self._run(
            settings={"user_interface": {"conversation_video_media_viewer_dialog": False}}
        )

        assert stub.media_viewer_calls == []
        assert len(_FakeThread.instances) == 1

    def test_image_open_action_opens_externally(self):
        _FakeThread.instances = []
        image_msg = {
            "key": {"id": "i1"}, "messageType": "imageMessage",
            "message": {"imageMessage": {}},
        }
        stub = _OpenActionStub([image_msg])
        import ui.conversations as conv_mod
        orig_thread = conv_mod.threading.Thread
        orig_data_path = conv_mod.data_path
        conv_mod.threading.Thread = _FakeThread
        conv_mod.data_path = lambda *parts: "/".join(("fake_data",) + parts)
        try:
            stub._on_action_open(None, index=0)
        finally:
            conv_mod.threading.Thread = orig_thread
            conv_mod.data_path = orig_data_path

        assert stub.media_viewer_calls == []
        assert len(_FakeThread.instances) == 1
