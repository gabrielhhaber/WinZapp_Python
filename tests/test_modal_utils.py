"""end_modal_if_running must never call EndModal on a stopped modal loop."""

from ui.modal_utils import end_modal_if_running


class _Dialog:
    def __init__(self, modal=True, destroyed=False, end_raises=None):
        self.modal = modal
        self.destroyed = destroyed
        self.end_raises = end_raises
        self.results = []

    def __bool__(self):
        return not self.destroyed

    def IsModal(self):
        return self.modal

    def EndModal(self, result):
        if self.end_raises:
            raise self.end_raises
        self.results.append(result)


def test_ends_a_running_modal():
    dlg = _Dialog()
    assert end_modal_if_running(dlg, 5101) is True
    assert dlg.results == [5101]


def test_late_completion_after_loop_ended_is_ignored():
    dlg = _Dialog(modal=False)
    assert end_modal_if_running(dlg, 5101) is False
    assert dlg.results == []


def test_destroyed_dialog_is_ignored():
    dlg = _Dialog(destroyed=True)
    assert end_modal_if_running(dlg, 5101) is False
    assert dlg.results == []


def test_loop_stopping_between_check_and_end_is_swallowed():
    assert end_modal_if_running(_Dialog(end_raises=AssertionError("IsRunning")), 1) is False
    assert end_modal_if_running(_Dialog(end_raises=RuntimeError("deleted")), 1) is False
