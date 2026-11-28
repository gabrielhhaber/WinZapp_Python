"""The result window's streamed answer: layout of the text ahead of the answer,
the quiet update (caret kept, one announcement), and discarding a partial.
Methods are called unbound on a stub: no window is created."""
from types import SimpleNamespace

from core.ai_media.config import preferences
from core.ai_media.transcript import render_history
from ui.dialogs.ai_result_streaming import AIStreamingMixin, stream_base

LABELS = {"question_label": "Question", "answer_label": "Answer"}


class Field:
    def __init__(self, value="", selection=(0, 0)):
        self.value, self.selection, self.writes = value, selection, 0

    def GetValue(self):
        return self.value

    def GetSelection(self):
        return self.selection

    def SetSelection(self, start, end):
        self.selection = (start, end)

    def ChangeValue(self, value):
        self.value, self.selection = value, (0, 0)
        self.writes += 1


class Cue:
    stopped = 0

    def stop(self):
        self.stopped += 1


def stub(config=None, kind="image", history=(), original="", description_first=False):
    spoken = []
    dialog = SimpleNamespace(
        config=config if config is not None else {"ai_streaming": True}, kind=kind, _stream=None,
        session=SimpleNamespace(history=list(history), generation=1, active=object()),
        _description_first=description_first, result=Field(original), status=Field(),
        _processing=Cue(), i18n=SimpleNamespace(t=lambda key: key), _valid=lambda: True,
        _summary_heading=lambda: "", _speak=spoken.append)
    dialog.spoken = spoken
    dialog._write_result = lambda text: AIStreamingMixin._write_result(dialog, text)
    dialog._stream_show = lambda g, s: AIStreamingMixin._stream_show(dialog, g, s)
    return dialog


def test_the_streamed_layout_is_the_final_layout_up_to_the_answer():
    history = (("user", "Describe"), ("assistant", "A cat."))
    final = render_history(history + (("user", "Color?"), ("assistant", "Grey.")),
                           description_first=True, **LABELS)
    base = stream_base(history, "Color?", regenerate=False, description_first=True, heading="", **LABELS)
    assert final.startswith(base) and final[len(base):] == "Grey."
    assert stream_base(history, "Describe", regenerate=True, description_first=True, heading="", **LABELS) == ""
    assert stream_base(history, "Describe", regenerate=True, description_first=True, heading="Note",
                       **LABELS) == "Note\n\n"
    assert stream_base((), "Q", regenerate=False, description_first=False, heading="",
                       **LABELS) == "Question\nQ\n\nAnswer\n"


def test_streaming_is_off_by_default_and_never_for_voice_messages():
    def saved(raw):
        return SimpleNamespace(get=lambda key: raw if key == "ai_media" else None)
    assert preferences(saved({}))["ai_streaming"] is False
    assert preferences(saved({"ai_streaming": "yes"}))["ai_streaming"] is False
    assert preferences(saved({"ai_streaming": True}))["ai_streaming"] is True
    assert AIStreamingMixin._stream_begin(stub({}), 1, "q", True) is None
    assert AIStreamingMixin._stream_begin(stub({"ai_streaming": False}), 1, "q", True) is None
    assert AIStreamingMixin._stream_begin(stub(kind="audio"), 1, "q", True) is None
    assert AIStreamingMixin._stream_begin(stub(), 1, "q", True) is not None


def test_the_first_words_stop_the_waiting_sound_and_are_announced_once():
    dialog = stub()
    AIStreamingMixin._stream_begin(dialog, 1, "q", True)
    dialog._stream_show(1, "First paragraph.")
    dialog._stream_show(1, "First paragraph.\nSecond.")
    assert dialog.result.value == "First paragraph.\nSecond."
    assert dialog._processing.stopped == 1
    assert dialog.spoken == ["ai_streaming_arriving"]  # once, never per update
    assert dialog.status.value == "ai_streaming_arriving"


def test_the_caret_the_person_moved_survives_an_update():
    dialog = stub()
    AIStreamingMixin._stream_begin(dialog, 1, "q", True)
    dialog._stream_show(1, "One.")
    dialog.result.selection = (2, 4)
    dialog._stream_show(1, "One.\nTwo.")
    assert dialog.result.selection == (2, 4)
    dialog.result.selection = (0, 0)
    dialog._stream_show(1, "One.\nTwo.\nThree.")
    assert dialog.result.selection == (0, 0)


def test_a_follow_up_keeps_what_was_on_screen_and_its_caret():
    shown = "Earlier answer."
    dialog = stub(original=shown, history=(("user", "Describe"), ("assistant", "Earlier answer.")),
                  description_first=True)
    dialog.result.selection = (3, 3)
    AIStreamingMixin._stream_begin(dialog, 1, "More?", False)
    dialog._stream_show(1, "Yes.")
    assert dialog.result.value.startswith(shown) and dialog.result.value.endswith("ai_history_answer\nYes.")
    assert dialog.result.selection == (3, 3)


def test_late_or_stale_updates_are_ignored():
    dialog = stub()
    AIStreamingMixin._stream_begin(dialog, 1, "q", True)
    dialog._stream_show(2, "Old generation.")
    dialog.session.active = None
    dialog._stream_show(1, "After the end.")
    dialog._stream = None
    dialog._stream_show(1, "After the window closed.")
    assert dialog.result.writes == 0 and dialog.spoken == []


def test_a_failure_puts_back_what_the_field_showed():
    dialog = stub(original="Previous answer.")
    AIStreamingMixin._stream_begin(dialog, 1, "q", True)
    dialog._stream_show(1, "Partial.")
    AIStreamingMixin._stream_discard(dialog)
    assert dialog.result.value == "Previous answer." and dialog._stream is None
    AIStreamingMixin._stream_discard(dialog)  # idempotent
    assert dialog.result.value == "Previous answer."


def test_nothing_shown_means_nothing_to_restore():
    dialog = stub(original="Previous answer.")
    AIStreamingMixin._stream_begin(dialog, 1, "q", True)
    AIStreamingMixin._stream_discard(dialog)
    assert dialog.result.writes == 0
