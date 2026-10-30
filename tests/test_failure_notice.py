"""One chat-list error box per outage (client/core/failure_notice.py)."""

from unittest.mock import MagicMock, patch

from core.failure_notice import FailureNotice
from main_window.sync import SyncMixin


def test_only_the_first_failure_of_an_outage_reports():
    notice = FailureNotice()
    assert notice.claim() is True
    assert [notice.claim() for _ in range(26)] == [False] * 26


def test_a_recovery_opens_the_next_outage():
    notice = FailureNotice()
    notice.claim()
    notice.clear()
    assert notice.claim() is True


def test_a_box_queued_before_the_recovery_stays_away():
    notice = FailureNotice()
    notice.claim()
    assert notice.still_failing() is True
    notice.clear()
    assert notice.still_failing() is False


def test_box_shows_while_the_failure_lasts():
    notice = FailureNotice()
    notice.claim()
    window = MagicMock()
    with patch("main_window.sync.wx") as wx, patch("main_window.sync._chat_list_notice", notice):
        SyncMixin._show_chat_list_failure(window, "HTTP 500")
    wx.MessageBox.assert_called_once()
    window.error_sound.play.assert_called_once()


def test_box_stays_silent_after_recovery():
    notice = FailureNotice()
    window = MagicMock()
    with patch("main_window.sync.wx") as wx, patch("main_window.sync._chat_list_notice", notice):
        SyncMixin._show_chat_list_failure(window, "HTTP 500")
    wx.MessageBox.assert_not_called()
    window.error_sound.play.assert_not_called()
