"""The `[mm:ss]` list of a long transcription, and the jump to a second.

Pure rules (core/transcription/segment_list.py) and `audio_seek.seek_message_audio`
on a stub panel. The window itself (ui/dialogs/transcription_segments.py) builds
real controls, so it is not opened here.
"""

from core.transcription import segment_list
from core.transcription.backend import TranscriptionSegment as Seg
from ui.conversation_panel.audio_seek import seek_message_audio


def _segments():
    return (Seg(0.0, 5.0, "hello"), Seg(61.4, 70.0, " second one "), Seg(3725.0, 3730.0, "late"))


class TestStamps:
    def test_minutes_and_seconds(self):
        assert segment_list.format_stamp(0) == "00:00"
        assert segment_list.format_stamp(61.9) == "01:01"
        assert segment_list.format_stamp(599) == "09:59"

    def test_an_hour_or_more_gets_hours(self):
        assert segment_list.format_stamp(3725) == "1:02:05"

    def test_garbage_is_zero(self):
        assert segment_list.format_stamp(None) == "00:00"
        assert segment_list.format_stamp(-4) == "00:00"
        assert segment_list.format_stamp("x") == "00:00"


class TestTheList:
    def test_only_a_long_recording_with_several_segments_gets_one(self):
        assert segment_list.offers_list(300, _segments())
        assert not segment_list.offers_list(120, _segments())
        assert not segment_list.offers_list(300, _segments()[:1])
        assert not segment_list.offers_list(300, ())
        assert not segment_list.offers_list(None, _segments())

    def test_empty_segments_are_not_rows(self):
        shown = _segments() + (Seg(80.0, 81.0, "   "),)
        assert len(segment_list.rows(shown)) == 3

    def test_rows_carry_the_stamp_and_the_trimmed_text(self):
        assert segment_list.rows(_segments()) == [
            "[00:00] hello", "[01:01] second one", "[1:02:05] late"]

    def test_the_copy_with_times_is_one_row_per_line(self):
        assert segment_list.stamped_text(_segments()).splitlines()[1] == "[01:01] second one"

    def test_the_second_a_row_starts_at(self):
        assert segment_list.start_of(_segments(), 1) == 61.4
        assert segment_list.start_of(_segments(), 3) is None
        assert segment_list.start_of(_segments(), -1) is None
        assert segment_list.start_of(_segments(), None) is None


class _Ctrl:
    def __init__(self, total=1000, fail=False):
        self.total, self.fail, self.position, self.played = total, fail, None, False

    def get_length(self):
        return self.total

    def seconds_to_bytes(self, seconds):
        return int(seconds * 10)

    def set_position(self, value):
        if self.fail:
            raise RuntimeError("bass")
        self.position = value

    def play(self):
        self.played = True


class _Timer:
    started = None

    def Start(self, ms):
        self.started = ms


class _Panel:
    def __init__(self, ctrl, current="m1", playing=True, tempo=None):
        self._audio_stream = ctrl
        self._audio_tempo_ctrl = tempo
        self._current_audio_id = current
        self._is_audio_playing = playing
        self._audio_timer = _Timer()


class TestSeeking:
    def test_it_moves_the_playing_message(self):
        ctrl = _Ctrl()
        assert seek_message_audio(_Panel(ctrl), "m1", 12.5)
        assert ctrl.position == 125 and not ctrl.played

    def test_it_uses_the_tempo_control_when_there_is_one(self):
        raw, tempo = _Ctrl(), _Ctrl()
        assert seek_message_audio(_Panel(raw, tempo=tempo), "m1", 3)
        assert tempo.position == 30 and raw.position is None

    def test_a_paused_message_is_played_from_there(self):
        ctrl, panel = _Ctrl(), None
        panel = _Panel(ctrl, playing=False)
        assert seek_message_audio(panel, "m1", 3)
        assert ctrl.played and panel._is_audio_playing and panel._audio_timer.started == 30

    def test_never_past_the_end(self):
        ctrl = _Ctrl(total=100)
        assert seek_message_audio(_Panel(ctrl), "m1", 9999)
        assert ctrl.position == 99

    def test_another_message_or_nothing_playing_touches_nothing(self):
        ctrl = _Ctrl()
        assert not seek_message_audio(_Panel(ctrl, current="other"), "m1", 3)
        assert not seek_message_audio(_Panel(None), "m1", 3)
        assert not seek_message_audio(_Panel(ctrl), "", 3)
        assert ctrl.position is None

    def test_a_rejected_position_is_a_false_not_a_crash(self):
        assert not seek_message_audio(_Panel(_Ctrl(fail=True)), "m1", 3)
        assert not seek_message_audio(_Panel(_Ctrl(total=0)), "m1", 3)
