"""UI-thread presence text views; reader lifecycle delegated, no focus/speech."""
import logging
from weakref import WeakSet

from core.contact_presence import activity, cached, visible_online
from ui.conversation_panel.text_helpers import _fmt_last_seen
from ui.dialogs.personal_presence_reader import track_personal_reader


def presence_line(owner, jid, i18n):
    entry = cached(owner, jid)
    state = activity(entry)
    if state:
        return i18n.t('typing_indicator' if state == 'composing' else 'recording_indicator')
    if visible_online(entry):
        return i18n.t('online_status')
    return _fmt_last_seen(entry['lastSeen'], i18n) if entry.get('lastSeen') else ''


def moved_position(position, start, old_end, new_end):
    """Keep anchors outside a replaced native-text span; clamp inside it."""
    if position < start:
        return position
    if position >= old_end:
        return position + new_end - old_end
    return start + min(position - start, new_end - start)


class PersonalPresenceView:
    def __init__(self, dialog, before, after, line):
        self.owner, self.jid, self.i18n = dialog._mw, dialog._jid, dialog._i18n
        self.control = dialog._info_ctrl
        self.closed = getattr(dialog, '_closed', None)
        self.before, self.after = '\n'.join(before), '\n'.join(after)
        self.line = line
        self.active = True
        self.reader = None
        if not hasattr(self.owner, '_personal_presence_views'):
            self.owner._personal_presence_views = WeakSet()
        self.owner._personal_presence_views.add(self)

    def stop(self):
        if self.reader is not None:
            self.reader.stop()
            self.reader = None
        self.active = False
        self.owner._personal_presence_views.discard(self)

    def _span(self, line):
        # TextCtrl positions are not Python string indexes on every port:
        # use its native line/position API for CRLF and supplementary Unicode.
        control = self.control
        if self.after:
            row = self.before.count('\n') + 1 if self.before else 0
            return (control.XYToPosition(0, row),
                    control.XYToPosition(0, row + line.count('\n') + 1) if line else
                    control.XYToPosition(0, row))
        row = self.before.count('\n')
        start = control.XYToPosition(control.GetLineLength(row), row) if self.before else 0
        return start, control.GetLastPosition()

    def refresh(self):
        if (not self.active or (self.closed is not None and self.closed.is_set())
                or getattr(self.owner, '_shutting_down', False)):
            self.stop()
            return
        line = presence_line(self.owner, self.jid, self.i18n)
        if line == self.line:
            return
        control = self.control
        start, old_end = self._span(self.line)
        if start < 0 or old_end < start:
            self.stop()
            return
        selection = control.GetSelection()
        caret = control.GetInsertionPoint()
        value = '\n'.join(part for part in (self.before, line, self.after) if part)
        # ChangeValue suppresses EVT_TEXT. Never call the initial renderer,
        # whose SetFocus would interrupt reading or move focus from another tab.
        control.ChangeValue(value)
        _, new_end = self._span(line)
        left, right = (moved_position(pos, start, old_end, new_end) for pos in selection)
        desired = moved_position(caret, start, old_end, new_end)
        control.SetSelection(left, right)
        # Ports differ in which selection end is the insertion point. Keep
        # the exposed caret without collapsing a nonempty selection.
        if left != right and control.GetInsertionPoint() != desired:
            control.SetSelection(right, left)
        self.line = line


def stop_personal_presence(dialog):
    view = getattr(dialog, '_personal_presence_view', None)
    if view is not None:
        view.stop()
        dialog._personal_presence_view = None


def track_personal_presence(dialog, before, after, line):
    stop_personal_presence(dialog)
    view = PersonalPresenceView(dialog, before, after, line)
    dialog._personal_presence_view = view
    # Own exactly the text already drawn. Expiry may have been delivered
    # during initial filling, before this observer could receive it.
    try:
        view.refresh()
        track_personal_reader(view, dialog)
    except Exception:
        view.stop()
        raise


def notify_personal_presence(owner):
    for view in tuple(getattr(owner, '_personal_presence_views', ())):
        try:
            view.refresh()
        except RuntimeError:
            view.stop()  # wx C++ control was destroyed before notification.
        except Exception:
            view.stop()
            logging.exception('[presence-view] personal text refresh failed')
