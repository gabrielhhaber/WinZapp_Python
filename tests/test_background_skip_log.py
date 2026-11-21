"""A background notification dropped on purpose leaves a line in log.log (#795)."""

import logging

from core.notification_burst import log_background_skip


def test_skip_reason_is_logged_at_info(caplog):
    with caplog.at_level(logging.INFO):
        log_background_skip("muted chat")
    assert "[notify] background notification skipped: muted chat." in caplog.text
