"""FormattingMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

Moved verbatim out of ui/conversations.py. Methods run with ``self`` bound to
the ConversationsPanel instance, so every attribute set in
ConversationsPanel.__init__/init_UI is available here.
"""

from datetime import (
    datetime,
    timedelta,
)
from core.locale_format import (
    get_datetime_format,
    get_time_format,
)
from ui.conversation_panel.media_paths import probe_media_duration


class FormattingMixin:
    """Formatting timestamps, dates, durations and file sizes for display.
    """

    # ── Message content helpers ─────────────────────────────────────────────

    def _extract_timestamp(self, msg):
        if not isinstance(msg, dict):
            return None
        ts = msg.get("messageTimestamp")
        if ts is None:
            return None
        try:
            ts_val = int(ts)
            if ts_val > 1_000_000_000_000:
                ts_val //= 1000
            return ts_val
        except Exception:
            return None

    def _format_date(self, ts):
        if not ts:
            return ""
        try:
            ts_val = int(ts)
            if ts_val > 1_000_000_000_000:
                ts_val //= 1000
            dt    = datetime.fromtimestamp(ts_val)
            today = datetime.now()
            i18n  = self.main_window.i18n
            if dt.date() == today.date():
                return dt.strftime(get_time_format(i18n.t("time_fmt")))
            # Settings > Interface do usuário > "Mostrar mensagens do dia
            # anterior com data omitida (ontem)" (default on). A message from
            # yesterday (any time up to 23:59) announces as "ontem às HH:MM"
            # instead of the full date — still through get_time_format() so
            # it respects the user's own time format either way.
            if dt.date() == today.date() - timedelta(days=1):
                show_yesterday = self.main_window.settings.get("user_interface", {}).get(
                    "show_yesterday_label", True
                )
                if show_yesterday:
                    time_str = dt.strftime(get_time_format(i18n.t("time_fmt")))
                    return i18n.t("yesterday_at").format(time=time_str)
            return dt.strftime(get_datetime_format(i18n.t("datetime_fmt")))
        except Exception:
            return ""

    def _format_full_datetime(self, ts):
        """Like _format_date(), but always the full date and time: never just
        "14:32" for today or "ontem às 14:32". The message-data window lists
        several stages (sent, delivered, read) and each has to be unambiguous on
        its own, without relying on "today" still meaning the day it is read."""
        if not ts:
            return ""
        try:
            ts_val = int(ts)
            if ts_val > 1_000_000_000_000:
                ts_val //= 1000
            dt = datetime.fromtimestamp(ts_val)
            return dt.strftime(get_datetime_format(self.main_window.i18n.t("datetime_fmt")))
        except Exception:
            return ""

    def _probe_audio_duration(self, path: str):
        """Method form of probe_media_duration() — see that function."""
        return probe_media_duration(path)

    def _format_duration(self, seconds):
        """Human-readable length, or "" when it isn't known.

        None means "never told us" — a forwarded media message arrives over
        the live socket with no duration on it (issue #43) — and callers
        treat "" as "omit the duration clause", which beats stating a length
        that is certainly wrong.

        Zero is NOT that case: a voice note shorter than a second really does
        report 0, and WhatsApp itself shows "0:00" for those. It is formatted
        like any other length. Only a negative value, which no medium has, is
        folded back into "unknown".
        """
        if seconds is None:
            return ""
        try:
            seconds = int(seconds)
        except (ValueError, TypeError):
            return ""
        if seconds < 0:
            return ""
        i18n = self.main_window.i18n
        if seconds < 60:
            unit = i18n.t("second") if seconds == 1 else i18n.t("seconds")
            return f"{seconds} {unit}"
        elif seconds < 3600:
            m, s = seconds // 60, seconds % 60
            return (
                f"{m} {i18n.t('minute') if m == 1 else i18n.t('minutes')}"
                f" {i18n.t('and')} {s} {i18n.t('second') if s == 1 else i18n.t('seconds')}"
            )
        else:
            h, m, s = seconds // 3600, (seconds % 3600) // 60, seconds % 60
            return (
                f"{h} {i18n.t('hour') if h == 1 else i18n.t('hours')},"
                f" {m} {i18n.t('minute') if m == 1 else i18n.t('minutes')}"
                f" {i18n.t('and')} {s} {i18n.t('second') if s == 1 else i18n.t('seconds')}"
            )

    def _download_progress_text(self, progress: float, total_bytes) -> str:
        """Row text for a download in progress: percentage plus bytes so far.

        Falls back to the percentage alone when the message carries no usable
        size, since "13.8 mb of 0 b" would be wrong.
        """
        i18n = self.main_window.i18n
        pct = int(progress * 100)
        try:
            total = int(total_bytes)
        except (TypeError, ValueError):
            total = 0
        if total <= 0:
            return i18n.t("downloading_progress").format(pct=pct)
        done = min(total, int(progress * total))
        return i18n.t("downloading_progress_size").format(
            pct=pct,
            done=self._format_filesize(done),
            total=self._format_filesize(total),
        )

    def _format_filesize(self, size_bytes) -> str:
        if size_bytes is None:
            return ""
        try:
            size = int(size_bytes)
        except (ValueError, TypeError):
            return ""
        sep = self.main_window.i18n.t("decimal_separator")
        if size < 1024:
            return f"{size} b"
        elif size < 1024 ** 2:
            return f"{size / 1024:.1f}".replace(".", sep) + " kb"
        elif size < 1024 ** 3:
            return f"{size / 1024 ** 2:.1f}".replace(".", sep) + " mb"
        else:
            return f"{size / 1024 ** 3:.2f}".replace(".", sep) + " gb"
