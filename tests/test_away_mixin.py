"""AwayMixin on a plain stub: what counts as away, busy and present (no window)."""
from types import SimpleNamespace

from core.away_mode import AwayTracker
from main_window.away import AwayMixin


def owner(*, away=False, enabled=True, **extra):
    tracker = AwayTracker()
    tracker._away = away
    stub = SimpleNamespace(
        _away_tracker=tracker,
        settings={"user_interface": {"away_enabled": enabled}},
        **extra,
    )
    stub._away_settings = lambda: AwayMixin._away_settings(stub)
    return stub


def test_away_only_while_the_option_is_on():
    assert AwayMixin.is_user_away(owner(away=True)) is True
    assert AwayMixin.is_user_away(owner(away=True, enabled=False)) is False
    assert AwayMixin.is_user_away(owner(away=False)) is False


def test_no_tracker_yet_is_not_away():
    assert AwayMixin.is_user_away(SimpleNamespace()) is False


def test_a_call_a_recording_or_a_playing_audio_is_busy():
    assert AwayMixin._away_busy(owner(_active_voice_call={"call_id": "1"})) is True
    panel = SimpleNamespace(_is_recording=True)
    assert AwayMixin._away_busy(owner(conversations_panel=panel)) is True
    assert AwayMixin._away_busy(owner(_voice_playback_active=lambda: True)) is True
    assert AwayMixin._away_busy(owner()) is False
