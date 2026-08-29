"""Issue #52 (group data loading freeze), restated for the rebuilt Media tab.

The tab used to show a count produced by _count_group_media(), which ran
os.path.isfile() once per media message. That started life on the UI thread
and froze the dialog on a busy group — reported live as a freeze while
switching tabs. The fix at the time moved the count to the background fetch
thread, and tests/test_group_data_media_count_off_ui_thread.py pinned it there.

That count is gone: the tab now lists the media messages themselves, and
_count_group_media() had no other caller. Its test went with it, but the
invariant it protected did not — _refresh_media_list() runs on the UI thread
(it is called while the tab is built, and again on every checkbox toggle) and
walks every record in the chat. If anything in that path ever starts stat-ing
files per message, the same freeze comes back with a different name.

So this pins the property rather than the deleted method: the Media tab's
render path does no filesystem work.
"""

import inspect

from ui.conversations import ConversationsPanel
from ui.dialogs.conversation_data_dialog import ConversationDataDialog

_DISK_CALLS = ("os.path.isfile", "os.path.exists", "os.listdir", "os.stat",
               "data_path(", "open(")


def _source_of(fn):
    return inspect.getsource(fn)


class TestTheRenderPathDoesNoDiskIo:
    def test_refresh_media_list_does_not_touch_the_filesystem(self):
        src = _source_of(ConversationDataDialog._refresh_media_list)
        hits = [c for c in _DISK_CALLS if c in src]
        assert not hits, f"disk I/O on the UI thread in the Media tab: {hits}"

    def test_the_row_renderer_it_calls_does_not_either(self):
        """_render_message_line is called once per media message, so a single
        stat in there is a stat per row."""
        src = _source_of(ConversationsPanel._render_message_line)
        hits = [c for c in _DISK_CALLS if c in src]
        assert not hits, f"the message renderer now does disk I/O per row: {hits}"


class TestTheRetiredCountIsGone:
    def test_count_group_media_is_not_resurrected(self):
        """If it comes back it needs its background-thread placement back too —
        see this file's docstring."""
        assert not hasattr(ConversationDataDialog, "_count_group_media")
