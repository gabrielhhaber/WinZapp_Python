"""Coalesce presence *painting*, never the ordered data/speech/HTTP events.

Installed once per real window. Partial startup callers without the renderer
retain synchronous painting. No new thread, timer, focus or speech operation.
"""
import logging
import time
from itertools import islice

from core.contact_presence import cached
from ui.dialogs.personal_presence import notify_personal_presence
from ui.conversation_panel.typing_row import note_typing_activity


def _context(owner):
    return getattr(owner, "_contact_presence_epoch", 0), id(getattr(owner, "_presence_cache", None))


def _live(owner, context):
    return not getattr(owner, "_shutting_down", False) and context == _context(owner)


def _failed(part, error):
    # Widget/extension exceptions can contain contact text; report class only.
    logging.warning("[presence-ui] %s refresh failed: %s", part, type(error).__name__)


def _refresh_row(owner, jid):
    refresh = getattr(owner, "_refresh_presence_row_in_list", None)
    if refresh is None:
        refresh = owner._refresh_chat_row_in_list
    refresh(jid)


def _refresh_views(owner, chat="", fresh=()):
    context = _context(owner)
    if not _live(owner, context):
        return
    try:
        notify_personal_presence(owner)
    except Exception as error:
        _failed("personal", error)
    if not _live(owner, context):
        return
    panel = getattr(owner, "conversations_panel", None)
    if panel is None:
        return
    if hasattr(panel, "refresh_typing_row"):
        try:
            panel.refresh_typing_row(chat, fresh)
        except Exception as error:
            _failed("typing", error)
    if not _live(owner, context) or panel is not getattr(owner, "conversations_panel", None):
        return
    conversation = getattr(panel, "conversation", None)
    if conversation:
        jid = conversation.get("remoteJid", "")
        if cached(owner, jid, fresh=False):
            try:
                panel._refresh_presence_note(jid)
            except Exception as error:
                _failed("note", error)


def request_presence_ui(owner, chat, *, row=False, fresh=()):
    if getattr(owner, "_shutting_down", False):
        return
    renderer = getattr(owner, "_presence_ui", None)
    if renderer is None:
        # Also usable during partial startup, before the UI pump is installed.
        if row:
            _refresh_row(owner, chat)
        _refresh_views(owner, chat, fresh)
        return
    panel = getattr(owner, "conversations_panel", None)
    if panel is not None:
        # This is logical state, not paint: a later message may dismiss typing
        # before our paint runs. Never resurrect it by replaying `fresh` then.
        note_typing_activity(panel, chat, fresh)
    renderer.request(chat if row else None)


class PresenceUiUpdates:
    """One pending UI wakeup; at most 16 row refreshes, with a soft 4 ms budget.

    Called only on the UI thread, after each original event applied its cache,
    timer and speech transition. A queued paint reads the *current* state;
    disconnect/replacement/shutdown invalidates its old dirty rows. There is
    no trailing debounce that a continuous stream can keep postponing.
    The budget yields between rows; it cannot preempt one slow native call.
    """
    ROWS_PER_TURN = 16
    SECONDS_PER_TURN = 0.004

    def __init__(self, owner, post, *, clock=None):
        self.owner, self.post = owner, post
        self.clock = time.perf_counter if clock is None else clock
        self._rows = {}
        self._views = self._pending = False
        self._context = _context(owner)

    def request(self, row=None):
        if getattr(self.owner, "_shutting_down", False):
            return
        context = _context(self.owner)
        if context != self._context:
            self._rows.clear()
            self._context = context
        if row:
            self._rows[row] = None
        self._views = True
        if not self._pending:
            self._schedule()

    def _schedule(self):
        self._pending = True
        try:
            self.post(self._flush)
        except Exception:
            self._pending = False
            raise

    def _flush(self):
        owner = self.owner
        context = self._context
        # A reentrant observer may replace the context or add more work. Only
        # this turn's snapshot belongs to this callback; fresh work stays queued.
        rows = tuple(islice(self._rows, self.ROWS_PER_TURN))
        started, painted = self.clock(), 0
        try:
            if not _live(owner, context):
                return
            views, self._views = self._views, False
            if views:
                _refresh_views(owner)
            for jid in rows:
                if not _live(owner, context):
                    break
                # Soft work budget: yield between rows, while guaranteeing at
                # least one row's progress even when views were expensive.
                if painted and self.clock() - started >= self.SECONDS_PER_TURN:
                    break
                if jid not in self._rows:
                    continue
                del self._rows[jid]
                try:
                    _refresh_row(owner, jid)
                except Exception as error:
                    _failed("row", error)
                painted += 1
        finally:
            if not _live(owner, self._context):
                self._rows.clear()
                self._views = False
            self._pending = False
            if self._rows or self._views:
                self._schedule()
