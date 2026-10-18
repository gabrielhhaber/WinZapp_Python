"""A MacListCtrl write changes one native row instead of wx reloading the
whole table (native_rows.change / reload_plan). No window, no wx.App."""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path[:0] = [os.path.join(ROOT, "macos"), os.path.join(ROOT, "client")]

from winzapp_mac import native_rows as nr  # noqa: E402


class _Ctrl:
    _wz_table_ptr = 1234


def plan(kind, row, table_rows, data_rows, done=False, owned=True, columns=1):
    return nr.reload_plan([kind, row, done], owned, columns, table_rows, data_rows)


def test_a_text_change_sets_that_row_only():
    assert plan(nr.ROW, 7, 20, 20) == (nr.ROW, 7)


def test_an_insert_adds_one_row_when_the_data_has_exactly_one_more():
    assert plan(nr.INSERT, 0, 20, 21) == (nr.INSERT, 0)
    assert plan(nr.INSERT, 20, 20, 21) == (nr.INSERT, 20)      # appended at the end


def test_a_delete_removes_one_row_when_the_data_has_exactly_one_fewer():
    assert plan(nr.DELETE, 19, 20, 19) == (nr.DELETE, 19)


def test_the_reloads_after_the_change_in_the_same_write_do_nothing():
    # Insert's UpdateLineToEnd reloads again right after ListInsert.
    assert plan(nr.INSERT, 3, 21, 21, done=True) == ("none",)


def test_anything_that_does_not_line_up_is_a_full_reload():
    assert plan(nr.INSERT, 0, 20, 22) == ("full",)             # two rows appeared
    assert plan(nr.DELETE, 0, 20, 20) == ("full",)             # nothing was removed
    assert plan(nr.ROW, 7, 20, 21) == ("full",)                # count changed under a text write
    assert plan(nr.ROW, 25, 20, 20) == ("full",)               # row out of range
    assert plan(nr.INSERT, 3, 20, 22, done=True) == ("full",)  # count moved again after the change
    assert plan(nr.ROW, 7, 20, 20, owned=False) == ("full",)   # not a MacListCtrl table
    assert plan(nr.ROW, 7, 20, 20, columns=2) == ("full",)
    assert nr.reload_plan(None, True, 1, 20, 20) == ("full",)  # no write in progress: Clear(), Set()


def test_change_marks_the_table_only_inside_the_block_even_when_it_raises():
    with nr.change(_Ctrl(), nr.INSERT, 3) as c:
        assert nr._pending == {1234: c.entry} and c.entry == [nr.INSERT, 3, False]
    assert nr._pending == {}
    try:
        with nr.row_only(_Ctrl(), 3):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert nr._pending == {}


def test_a_control_that_was_never_adopted_marks_nothing():
    with nr.change(object(), nr.DELETE, 3):
        assert nr._pending == {}


# ---- a row view acts on the row it shows now, not the one it was made for --

import objc  # noqa: E402
from Foundation import NSObject  # noqa: E402


class _ShiftedTable(NSObject):
    """Stands in for the wxNSTableView: rowForView_ is the table's answer for
    where a view is now, after single-row inserts/deletes moved it."""

    def rowForView_(self, view):
        return self.now


class _ListCtrl:
    def __init__(self, focused):
        self.focused = focused
        self.focus_calls = []

    def GetFocusedItem(self):
        return self.focused

    def Focus(self, row):
        self.focus_calls.append(row)
        self.focused = row


def _cell_made_for_row(made_at, now_at):
    table = _ShiftedTable.alloc().init()
    table.now = now_at
    cell = nr.WZRowCell.alloc().initWithFrame_(nr.NSMakeRect(0, 0, 200, 17))
    cell._wz_row = made_at
    cell._wz_table = table
    return cell, table


def test_a_view_reports_the_row_the_table_now_has_it_at():
    # Made as row 3; a chat inserted at the top shifted it to row 4.
    cell, _table = _cell_made_for_row(3, 4)
    assert cell._row() == 4


def test_a_view_no_longer_in_the_table_has_no_row():
    cell, _table = _cell_made_for_row(3, -1)
    assert cell._row() == -1


def test_vo_space_opens_the_chat_the_row_shows_after_a_chat_moved_to_the_top(monkeypatch):
    cell, table = _cell_made_for_row(3, 4)
    ctrl = _ListCtrl(focused=0)
    activated = []
    monkeypatch.setitem(nr._owners, objc.pyobjc_id(table), lambda: ctrl)
    monkeypatch.setattr(nr.wx, "CallAfter", lambda fn, *a: activated.append((fn, a)))
    ctrl.activate_row = object()
    assert cell.accessibilityPerformPress()
    assert ctrl.focus_calls == [4]
    assert activated == [(ctrl.activate_row, (4,))]


def test_a_view_that_left_the_table_does_not_press_anything(monkeypatch):
    cell, table = _cell_made_for_row(3, -1)
    ctrl = _ListCtrl(focused=0)
    monkeypatch.setitem(nr._owners, objc.pyobjc_id(table), lambda: ctrl)
    monkeypatch.setattr(nr.wx, "CallAfter", lambda *a: (_ for _ in ()).throw(AssertionError))
    assert not cell.accessibilityPerformPress()
    assert ctrl.focus_calls == []
