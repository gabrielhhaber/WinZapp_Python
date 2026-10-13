"""Transcribing with no progress window (transcription.run_without_dialog).

Asked for: a setting so that Alt+Shift+T and Alt+Shift+N do not open the
window with Cancel, and the user stays on the messages or statuses until the
text is ready. The flow's methods run on a plain stub; no window is created.
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.transcription import errors
from core.transcription import preferences
from main_window.auto_transcription import AutoTranscriptionMixin
from ui import transcription_flow
from ui.status_transcription_flow import StatusTranscriptionFlow
from ui.transcription_flow import MessageTranscriptionFlow

STRINGS = {"transcription_background_started": "started", "transcription_background_busy": "busy",
           "transcription_background_cancelling": "cancelling", "transcription_no_audio": "no audio"}


class Flow:
    for _name in ("start", "_prepare", "_cleanup", "_runs_without_dialog", "_start_in_background", "targets",
                  "cancel_background", "_background_done", "_background_finished", "_say_after_focus"):
        locals()[_name] = getattr(MessageTranscriptionFlow, _name)
    del _name

    def __init__(self, window, message_id="A", without_dialog=True):
        self._main_window = window
        window.settings = {"transcription": {"run_without_dialog": without_dialog}}
        self._i18n = SimpleNamespace(t=lambda key: STRINGS[key])
        self._msg, self._msg_id = {"id": message_id}, message_id
        self._run, self._background, self._dialog = None, False, None
        self.run = SimpleNamespace(start=Mock(), cancel=Mock())
        self.finished = []
        self.events = []
        self._run_behind_dialog = Mock(return_value=(self.run, "result", None))
        self._focus_message = Mock()
        self._play_error_sound = Mock()

    def _hidden_by_vault(self):
        return False

    def _make_first_run(self, on_progress, on_finished):
        self._run = self.run
        self.on_finished = on_finished
        return self.run

    def _finish(self, run, result, error):
        self.finished.append((run, result, error))


@pytest.fixture
def window(monkeypatch):
    monkeypatch.setattr(transcription_flow.message_audio, "is_transcribable", lambda msg: True)
    monkeypatch.setattr(transcription_flow.wx, "CallAfter", lambda fn, *args: fn(*args))
    return SimpleNamespace(output=Mock(), _background_transcription=None)


def test_with_the_setting_off_the_run_goes_behind_the_window_as_before(window):
    flow = Flow(window, without_dialog=False)
    flow.start()
    flow._run_behind_dialog.assert_called_once()
    assert flow.finished == [(flow.run, "result", None)]
    assert window._background_transcription is None and not window.output.called


@pytest.mark.parametrize("value", [None, "true", 1, {}])
def test_only_exactly_true_turns_the_window_off(window, value):
    flow = Flow(window)
    window.settings = {"transcription": {"run_without_dialog": value}} if value is not None else {}
    assert flow._runs_without_dialog() is False


def test_no_window_opens_the_run_starts_and_the_user_is_told(window):
    flow = Flow(window)
    flow.start()
    flow._run_behind_dialog.assert_not_called()
    flow.run.start.assert_called_once_with()
    assert window._background_transcription is flow and flow._background is True
    window.output.assert_called_once_with("started")
    assert flow.finished == []  # nothing is reported until the run ends


def test_the_result_is_reported_when_the_run_ends_and_the_slot_is_freed(window):
    flow = Flow(window)
    flow.start()
    flow.on_finished("the text", None)
    assert flow.finished == [(flow.run, "the text", None)]
    assert window._background_transcription is None


def test_the_same_shortcut_on_the_same_message_cancels(window):
    first = Flow(window)
    first.start()
    again = Flow(window)
    again.start()
    first.run.cancel.assert_called_once_with()
    again.run.start.assert_not_called()
    assert window.output.call_args.args == ("cancelling",)
    assert window._background_transcription is first  # until its run reports the cancellation


def test_another_message_meanwhile_is_told_to_wait_and_nothing_is_cancelled(window):
    first = Flow(window, "A")
    first.start()
    other = Flow(window, "B")
    other.start()
    first.run.cancel.assert_not_called()
    other.run.start.assert_not_called()
    assert window.output.call_args.args == ("busy",)


def test_a_run_that_cannot_start_is_reported_as_a_failure_and_frees_the_slot(window):
    flow = Flow(window)
    flow.run.start.side_effect = RuntimeError("can't start new thread")
    flow.start()
    [(_run, result, error)] = flow.finished
    assert result is None and error.code == errors.BACKEND_ERROR
    assert window._background_transcription is None


def test_a_sentence_after_such_a_run_does_not_move_the_focus(window):
    flow = Flow(window)
    flow._background = True
    flow._say_after_focus("done", failed=True)
    flow._focus_message.assert_not_called()
    window.output.assert_called_once_with("done")
    flow._play_error_sound.assert_called_once()
    behind_the_window = Flow(window, without_dialog=False)
    behind_the_window._say_after_focus("done")
    behind_the_window._focus_message.assert_called_once()


def test_what_the_run_needs_lives_until_it_is_reported(window):
    order = []
    flow = Flow(window)
    flow._prepare = lambda: order.append("prepare")
    flow._cleanup = lambda: order.append("cleanup")
    flow._finish = lambda run, result, error: order.append("finish")
    flow.start()
    assert order == ["prepare"]
    flow.on_finished("text", None)
    assert order == ["prepare", "finish", "cleanup"]


def test_a_status_keeps_its_private_folder_for_the_whole_run(monkeypatch):
    entered, left = [], []

    class Folder:
        def __enter__(self):
            entered.append(1)
            return "C:/tmp/status"

        def __exit__(self, *args):
            left.append(1)

    monkeypatch.setattr("ui.status_transcription_flow._fileops.private_temp_dir", lambda prefix: Folder())
    flow = SimpleNamespace(_folder="")
    StatusTranscriptionFlow._prepare(flow)
    assert flow._folder == "C:/tmp/status" and entered and not left
    StatusTranscriptionFlow._cleanup(flow)
    assert flow._folder == "" and left == [1]
    StatusTranscriptionFlow._cleanup(flow)  # twice is harmless
    assert left == [1]


def test_quitting_cancels_a_run_without_the_window_and_waits_for_it():
    run = SimpleNamespace(join=Mock())
    window = SimpleNamespace(_background_transcription=SimpleNamespace(cancel_background=Mock(return_value=run)))
    AutoTranscriptionMixin.stop_transcription_runs(window, timeout=1)
    window._background_transcription.cancel_background.assert_called_once_with()
    run.join.assert_called_once()


def test_the_setting_is_off_by_default_and_a_damaged_value_is_reset():
    assert preferences.DEFAULTS[preferences.SETTING_RUN_WITHOUT_DIALOG] is False
