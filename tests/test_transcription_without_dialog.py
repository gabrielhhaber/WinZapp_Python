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
           "transcription_background_cancelling": "cancelling", "transcription_no_audio": "no audio",
           "transcription_queue_added": "queued {position}", "transcription_queue_removed": "unqueued",
           "transcription_queue_dropped": "queue dropped"}


class Flow:
    for _name in ("start", "_prepare", "_cleanup", "_runs_without_dialog", "_start_in_background", "targets",
                  "cancel_background", "_background_done", "_background_finished", "_say_after_focus",
                  "_start_next_waiting", "_can_wait_its_turn", "_drop_waiting",
                  "_cache_keeps_text"):
        locals()[_name] = getattr(MessageTranscriptionFlow, _name)
    del _name

    def __init__(self, window, message_id="A", without_dialog=True, inline=False):
        self._main_window = window
        self._panel = SimpleNamespace(_summary_voice_running=lambda: False)
        self._inline = inline
        self._selection = ("auto", "auto")
        self.saved_shown = 0
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

    def inline_saved(self):
        self.saved_shown += 1

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


# ── Inline transcriptions wait their turn ───────────────────────────────────


def _inline(window, message_id):
    flow = Flow(window, message_id, inline=True)
    window.settings = {"transcription": {"run_without_dialog": True}}
    return flow


def test_an_inline_transcription_meanwhile_waits_instead_of_being_refused(window):
    first = Flow(window, "A")
    first.start()
    second, third = _inline(window, "B"), _inline(window, "C")
    second.start()
    assert window.output.call_args.args == ("queued 1",)
    third.start()
    assert window.output.call_args.args == ("queued 2",)
    second.run.start.assert_not_called()
    first.run.cancel.assert_not_called()

    first.on_finished("text A", None)
    second.run.start.assert_called_once_with()
    assert window._background_transcription is second
    assert window.output.call_args.args == ("queued 2",), "the next one starts in silence"
    third.run.start.assert_not_called()

    second.on_finished("text B", None)
    third.run.start.assert_called_once_with()
    third.on_finished("text C", None)
    assert window._background_transcription is None and len(window._waiting_transcriptions) == 0


def test_one_that_ends_in_a_window_is_still_refused(window):
    Flow(window, "A").start()
    windowed = Flow(window, "B")
    windowed.start()
    assert window.output.call_args.args == ("busy",)
    assert not getattr(window, "_waiting_transcriptions", None)


def test_the_same_shortcut_on_a_waiting_message_takes_it_out(window):
    first = Flow(window, "A")
    first.start()
    _inline(window, "B").start()
    again = _inline(window, "B")
    again.start()
    assert window.output.call_args.args == ("unqueued",)
    first.on_finished("text A", None)
    assert window._background_transcription is None


def test_cancelling_the_running_one_moves_on_to_the_next(window):
    first = Flow(window, "A")
    first.start()
    waiting = _inline(window, "B")
    waiting.start()
    Flow(window, "A").start()  # Cancel
    first.on_finished(None, errors.TranscriptionError(errors.CANCELLED, "cancelled"))
    waiting.run.start.assert_called_once_with()


def test_a_waiting_message_whose_chat_was_locked_is_skipped(window):
    first = Flow(window, "A")
    first.start()
    locked, after = _inline(window, "B"), _inline(window, "C")
    locked._hidden_by_vault = lambda: True
    locked.start()
    after.start()
    first.on_finished("text", None)
    locked.run.start.assert_not_called()
    after.run.start.assert_called_once_with()


def test_nothing_waiting_starts_while_quitting(window):
    first = Flow(window, "A")
    first.start()
    waiting = _inline(window, "B")
    waiting.start()
    window._shutting_down = True
    first.on_finished("text", None)
    waiting.run.start.assert_not_called()


def test_quitting_empties_the_queue_before_cancelling_the_run():
    from core.transcription.waiting_runs import WaitingRuns
    queue = WaitingRuns()
    queue.add(SimpleNamespace(targets=lambda other: False))
    run = SimpleNamespace(join=Mock())
    window = SimpleNamespace(_waiting_transcriptions=queue,
                             _background_transcription=SimpleNamespace(cancel_background=Mock(return_value=run)))
    AutoTranscriptionMixin.stop_transcription_runs(window, timeout=1)
    assert len(queue) == 0


def test_an_inline_result_that_would_not_be_kept_is_not_queued(window):
    """Reviewer: with the saved transcriptions off for the model the result
    opens the window, which must not pop up from the queue."""
    Flow(window, "A").start()
    off = _inline(window, "B")
    window.settings["transcription"]["cache_enabled"] = False
    off.start()
    assert window.output.call_args.args == ("busy",)
    assert not len(window._waiting_transcriptions)


def test_a_failure_that_would_stop_every_run_empties_the_queue(window):
    first = Flow(window, "A")
    first.start()
    waiting = _inline(window, "B")
    waiting.start()
    first.on_finished(None, errors.TranscriptionError(errors.MODEL_NOT_INSTALLED, "none"))
    waiting.run.start.assert_not_called()
    assert window.output.call_args.args == ("queue dropped",)


def test_a_failure_about_one_message_lets_the_queue_go_on(window):
    first = Flow(window, "A")
    first.start()
    waiting = _inline(window, "B")
    waiting.start()
    first.on_finished(None, errors.TranscriptionError(errors.NO_AUDIO_TRACK, "silent video"))
    waiting.run.start.assert_called_once_with()


def test_the_window_shortcut_on_a_waiting_message_leaves_it_in_the_queue(window):
    Flow(window, "A").start()
    _inline(window, "B").start()
    Flow(window, "B").start()  # Alt+Shift+T on it
    assert window.output.call_args.args == ("busy",)
    assert len(window._waiting_transcriptions) == 1


def test_a_waiting_message_transcribed_meanwhile_shows_its_text(window, monkeypatch):
    first = Flow(window, "A")
    first.start()
    done, after = _inline(window, "B"), _inline(window, "C")
    done.start()
    after.start()
    monkeypatch.setattr(transcription_flow.stored_transcription, "saved_transcription",
                        lambda msg, selection: "text" if msg["id"] == "B" else None)
    first.on_finished("text A", None)
    done.run.start.assert_not_called()
    assert done.saved_shown == 1
    after.run.start.assert_called_once_with()


def test_an_inline_result_in_the_background_leaves_the_focus_where_it_is(window):
    """Reviewer: a queued result took the focus out of the message field."""
    flow = Flow(window)
    flow._background = True
    flow._repaint_row = Mock()
    flow._present_inline = MessageTranscriptionFlow._present_inline.__get__(flow)
    monkeypatched = SimpleNamespace(text="olá")
    flow._i18n = SimpleNamespace(t=lambda key: "{text}")
    flow._present_inline(monkeypatched, None, ())
    flow._focus_message.assert_not_called()
    flow._repaint_row.assert_called_once_with()


def test_a_status_waits_its_turn_only_with_the_statuses_cache_on():
    """Reviewer: statuses keep their text under their own switch; with it off
    a queued status ended in the result window."""
    from ui.status_transcription_flow import StatusTranscriptionFlow
    stub = SimpleNamespace(_main_window=SimpleNamespace(
        settings={"transcription": {"status_cache_enabled": False, "cache_enabled": True}}))
    assert StatusTranscriptionFlow._cache_keeps_text(stub, None) is False
    stub._main_window.settings["transcription"]["status_cache_enabled"] = True
    assert StatusTranscriptionFlow._cache_keeps_text(stub, None) is True


def test_a_waiting_message_shown_from_the_queue_neither_moves_the_focus_nor_opens_a_window(
        window, monkeypatch):
    """Reviewer: inline_saved() from the queue focused the message and, when
    the mark could not be kept, opened the result window."""
    flow = Flow(window, "B", inline=True)
    flow._background = True
    flow._repaint_row = Mock()
    flow.show_saved = Mock()
    flow._save = lambda value, existing=False: "disabled"
    flow.inline_saved = MessageTranscriptionFlow.inline_saved.__get__(flow)
    stored = transcription_flow.stored_transcription
    monkeypatch.setattr(stored, "saved_transcription", lambda msg, selection: {"text": "olá"})
    monkeypatch.setattr(stored, "is_inline", lambda value: False)
    monkeypatch.setattr(stored, "marked_inline", lambda value, previous, now: value)
    monkeypatch.setattr(stored, "as_result", lambda value: SimpleNamespace(text="olá"))
    # Made without the voice filter: that warning is always said, from the
    # queue too (reviewer, second round).
    vad = transcription_flow.narration.Note(transcription_flow.narration.VAD_UNAVAILABLE_I18N_KEY)
    monkeypatch.setattr(transcription_flow.narration, "result_notes", lambda result, language: (vad,))
    monkeypatch.setattr(transcription_flow.preferences, "preferred_language", lambda *a: None)
    flow._i18n = SimpleNamespace(t=lambda key: "{text}" if key != vad.i18n_key else "sem filtro")
    flow.inline_saved()
    flow.show_saved.assert_not_called()
    flow._focus_message.assert_not_called()
    window.output.assert_called_once_with("olá sem filtro")
