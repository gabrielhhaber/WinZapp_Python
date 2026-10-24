"""The chat lists' single column follows the width of the control (issue #574)."""

from types import SimpleNamespace

from ui.list_column_fit import column_width_for, fit_single_column


class _Size:
    def __init__(self, width):
        self._w = width

    def GetWidth(self):
        return self._w


class _ListStub:
    def __init__(self, client_width, column_width):
        self._client = client_width
        self.column_width = column_width
        self.set_calls = []

    def GetClientSize(self):
        return _Size(self._client)

    def GetColumnWidth(self, col):
        return self.column_width

    def SetColumnWidth(self, col, width):
        self.set_calls.append((col, width))
        self.column_width = width


def test_width_is_client_width():
    assert column_width_for(640) == 640


def test_unusable_width_is_zero():
    assert column_width_for(0) == 0
    assert column_width_for(-1) == 0


def test_column_grows_with_control():
    lst = _ListStub(client_width=700, column_width=200)
    fit_single_column(lst)
    assert lst.set_calls == [(0, 700)]


def test_column_shrinks_with_control():
    lst = _ListStub(client_width=150, column_width=200)
    fit_single_column(lst)
    assert lst.set_calls == [(0, 150)]


def test_no_change_when_already_fitted():
    lst = _ListStub(client_width=200, column_width=200)
    fit_single_column(lst)
    assert lst.set_calls == []


def test_control_without_width_is_left_alone():
    lst = _ListStub(client_width=0, column_width=200)
    fit_single_column(lst)
    assert lst.set_calls == []
