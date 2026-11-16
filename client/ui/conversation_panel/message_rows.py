"""MessageRowsMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

Writing the open conversation's rows into ``messages_list`` without ever
clearing it.

A native ListView row is one MSAA object. ``DeleteAllItems()`` followed by
re-``Append()``ing every row hands the screen reader a brand-new list, and
putting focus back on the row the user was sitting on re-announces it even
though nothing about it changed — the "NVDA reads the focused message again,
for no reason" bug. It cannot be made quiet (see
docs/traps/screen-reader-speech.md), so the rebuild must not happen: every
write goes through ``_sync_message_rows()``, which deletes and inserts only the
rows that really differ and rewrites a row's text only when the text differs.
A row that did not change — the focused one above all — is not touched.
"""

import logging
import time

from core.list_row_diff import plan_row_diff
from ui.conversation_panel.typing_row import message_row_count, typing_row_shown

# SysListView32 hands back at most this many UTF-16 units from GetItemText(),
# whatever was stored (measured on a real ListCtrl: 511; an emoji outside the
# BMP counts as two). See ConversationsPanel._LIST_CTRL_TEXT_LIMIT.
_LIST_CTRL_TEXT_UNITS = 511


def row_text_unchanged(shown: str, wanted: str, limit: int = _LIST_CTRL_TEXT_UNITS) -> bool:
    """Whether a row already shows *wanted*, given that *shown* is what
    GetItemText() returned for it.

    Plain equality is wrong for a long row: the control truncates what it
    reports, so a message of a few hundred characters never equals its own
    rendered line, and every refresh would rewrite it — a name change on the
    focused row, which NVDA reads, the very thing this module exists to avoid.
    A row longer than the limit is therefore compared on the part the control
    can report (minus its last unit, which may be half of a surrogate pair).
    """
    if shown == wanted:
        return True
    wanted_units = wanted.encode("utf-16-le", "surrogatepass")
    if len(wanted_units) // 2 <= limit:
        return False                    # fully reportable, and it differs
    shown_units = shown.encode("utf-16-le", "surrogatepass")
    keep = min(len(shown_units), len(wanted_units)) - 2
    return keep > 0 and wanted_units[:keep] == shown_units[:keep]


class MessageRowsMixin:
    """Per-row writes to the messages list."""

    def _message_row_key(self, msg):
        """The identity a row keeps across refreshes: the message id, or a
        fixed key for the two sentinel rows. A message without an id (a send
        still pending) falls back to the record object itself, which is stable
        for as long as the record is the same dict."""
        if isinstance(msg, dict):
            kind = msg.get("_type")
            if kind == "unread_separator":
                return ("separator",)
            if kind == "empty_placeholder":
                return ("placeholder",)
            mid = (msg.get("key") or {}).get("id", "")
            if mid:
                return ("id", mid)
        return ("object", id(msg))

    def _sync_message_rows(self, old_rows: list, new_rows: list) -> None:
        """Make ``messages_list`` show *new_rows*, given that it now shows
        *old_rows*, touching only the rows that differ.

        ``self._sorted_messages`` must already be *new_rows*: rendering a row
        (the unread separator's position, the ", N de M" suffix) reads it.

        The one fallback is a control that does not match *old_rows* — a count
        that disagrees means a targeted delete or insert would land on the
        wrong row, so the control is resynced from scratch. That is a repair of
        an inconsistency, not a way to refresh, and it is logged.
        """
        lst = self.messages_list
        started = time.monotonic()
        # Index and total are handed over: left to itself, _render_message_line()
        # finds a row's position with _sorted_messages.index(msg), a linear scan
        # per row — quadratic over the window. In an 11,000-row chat that was
        # tens of seconds on the main thread (issue #812).
        total = len(new_rows)
        texts = [self._render_message_line(m, index=i, total=total)
                 for i, m in enumerate(new_rows)]
        rendered = time.monotonic()
        # The typing row, when showing, is one row past old_rows and stays
        # there: every delete/insert index below is within the message rows.
        control_rows = message_row_count(self)
        if control_rows != len(old_rows):
            logging.warning(
                "[_sync_message_rows] list out of step (control=%d, backing=%d) "
                "— resyncing from scratch", control_rows, len(old_rows))
            typing_text = self._typing_row_text if typing_row_shown(self) else ""
            lst.DeleteAllItems()
            for text in texts:
                lst.Append((text,))
            if typing_text:
                lst.Append((typing_text,))
            return

        deletes, inserts = plan_row_diff(
            [self._message_row_key(m) for m in old_rows],
            [self._message_row_key(m) for m in new_rows],
        )
        for index in deletes:
            lst.DeleteItem(index)
        for index in inserts:
            lst.InsertItem(index, texts[index])
        inserted = set(inserts)
        rewritten = 0
        for index, text in enumerate(texts):
            if index in inserted:
                continue
            if not row_text_unchanged(lst.GetItemText(index), text):
                lst.SetItemText(index, text)
                rewritten += 1
        # A pass that changed nothing is logged too when rendering alone was slow:
        # that is the worst case of issue #812 and would otherwise leave no trace.
        if deletes or inserts or rewritten or rendered - started > 0.5:
            done = time.monotonic()
            logging.info(
                "[_sync_message_rows] %d row(s) deleted, %d inserted, %d rewritten, "
                "%d row(s) total (was %d) — no rebuild. render=%dms apply=%dms",
                len(deletes), len(inserts), rewritten, len(new_rows), len(old_rows),
                (rendered - started) * 1000, (done - rendered) * 1000)
