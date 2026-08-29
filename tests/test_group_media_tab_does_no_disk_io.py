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
    """Source with docstrings and comments stripped.

    These checks look for calls, and the prose around them legitimately names
    the very calls being forbidden — a plain substring match on the raw source
    flags a docstring that explains why the I/O is NOT here.
    """
    import ast
    import textwrap

    raw = textwrap.dedent(inspect.getsource(fn))
    tree = ast.parse(raw)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef, ast.Module)):
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body.pop(0)
    return ast.unparse(tree)


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


class TestTheDiskWorkRunsOnTheFetchThread:
    """The "baixada / nao baixada" filter needs one os.path.isfile() per
    message. That is the very loop issue #52 was about, so it happens once, on
    the background fetch thread, into a set — and the filter itself then only
    tests membership.
    """

    def test_the_history_load_is_called_from_fetch_data(self):
        src = _source_of(ConversationDataDialog._fetch_data)
        assert "self._load_media_history()" in src, (
            "the media history + downloaded-set scan must run on the fetch "
            "thread, not while the UI is being built"
        )

    def test_it_hands_the_result_back_through_callafter(self):
        src = _source_of(ConversationDataDialog._load_media_history)
        assert "wx.CallAfter" in src

    def test_the_download_filter_itself_does_no_io(self):
        """It runs on every radio change, on the UI thread."""
        from core.utils import filter_group_media_by_download
        src = _source_of(filter_group_media_by_download)
        hits = [c for c in _DISK_CALLS if c in src]
        assert not hits, f"the download filter stats files per message: {hits}"
