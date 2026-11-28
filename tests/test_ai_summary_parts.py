"""Hierarchical (map-reduce) chat summary: core/ai_media/summary_parts.py, the
steps of Operation/MediaSession, and the window's part (ui/dialogs/ai_summary_parts.py).

Pure logic and fakes only: no window, no network. The request is a fake ``call``
(or a monkeypatched ``request_answer`` where the usage counters are the point).
"""
from types import SimpleNamespace as NS

import pytest

from core.ai_media import config as ai_config
from core.ai_media import service, usage
from core.ai_media.errors import DescriptionError
from core.ai_media.service import ChainFailed, Operation, run_chain
from core.ai_media.session import MediaSession
from core.ai_media.summary import VOICE_MARKER
from core.ai_media.summary_parts import (
    MAX_CALLS, MAX_PARTS, PART_CHARS, PartFailed, chunk_lines, joined_partials, may_need_parts,
    merge_input, plan_parts, run_parts)
from ui.dialogs.ai_result_dialog import failure_text
from ui.dialogs.ai_summary_parts import AISummaryPartsMixin


def lines(count, size):
    return [f"{n:04d}" + "x" * (size - 4) for n in range(count)]


class TestChunkLines:
    def test_nothing_is_nothing(self):
        assert chunk_lines([]) == []

    def test_what_fits_is_one_chunk(self):
        assert chunk_lines(["a", "b", "c"], 100) == ["a\nb\nc"]

    def test_never_cuts_a_line_and_keeps_the_order(self):
        rows = lines(25, 100)
        chunks = chunk_lines(rows, 1000)
        assert "\n".join(chunks).split("\n") == rows
        assert all(len(chunk) <= 1000 for chunk in chunks)
        assert all(line in rows for chunk in chunks for line in chunk.split("\n"))
        assert len(chunks) == 3  # nine 100-character lines and their separators fit in 1000

    def test_the_separator_counts_toward_the_limit(self):
        assert chunk_lines(["aaaa", "bbbb"], 9) == ["aaaa\nbbbb"]
        assert chunk_lines(["aaaa", "bbbb"], 8) == ["aaaa", "bbbb"]

    def test_a_line_longer_than_the_limit_is_a_chunk_of_its_own_and_whole(self):
        big = "y" * 50
        assert chunk_lines(["a", big, "b"], 10) == ["a", big, "b"]
        assert chunk_lines([big], 10) == [big]


def make_line(msg, text):
    return f"{msg}: {text}"


def text_of(msg):
    return msg if isinstance(msg, str) and msg else None


class TestPlanParts:
    def test_parts_are_chronological_and_complete_under_the_ceiling(self):
        rows = [f"m{n:03d}" + "z" * 90 for n in range(100)]
        plan = plan_parts(rows, lambda m, t: t, text_of, part_chars=1000, max_parts=6)
        assert len(plan.parts) <= 6 and plan.included + plan.left_out == 100
        joined = "\n".join(plan.parts).split("\n")
        assert joined == rows[plan.left_out:]  # the newest kept, in order

    def test_the_ceiling_drops_the_oldest_whole_parts_and_counts_them(self):
        rows = lines(50, 100)
        plan = plan_parts(rows, lambda m, t: t, text_of, part_chars=1000, max_parts=2)
        assert len(plan.parts) == 2 and plan.included == 14 and plan.left_out == 36
        assert plan.parts[-1].split("\n")[-1] == rows[-1]

    def test_default_ceiling_is_six_parts_of_twelve_thousand(self):
        assert (PART_CHARS, MAX_PARTS, MAX_CALLS) == (12_000, 6, 7)
        rows = lines(1000, 500)
        plan = plan_parts(rows, lambda m, t: t, text_of)
        assert len(plan.parts) == MAX_PARTS
        assert all(len(part) <= PART_CHARS for part in plan.parts)

    def test_unusable_messages_are_skipped_and_voice_markers_counted(self):
        rows = ["hello", None, "", "world"]
        plan = plan_parts(rows, make_line, text_of)
        assert plan.parts == ("hello: hello\nworld: world",) and plan.left_out == 0
        voice = {"messageType": "audioMessage"}
        plan = plan_parts([voice, "hi"], lambda m, t: t,
                          lambda m: VOICE_MARKER if isinstance(m, dict) else m)
        assert plan.voice_markers == 1

    def test_a_line_that_would_not_fit_a_part_is_truncated_marked(self):
        plan = plan_parts(["w" * 500], lambda m, t: t, text_of, part_chars=100)
        assert len(plan.parts) == 1 and plan.parts[0].endswith("[truncated]")
        assert len(plan.parts[0]) == 100

    def test_a_failing_line_format_falls_back_to_the_text(self):
        def boom(msg, text):
            raise ValueError
        assert plan_parts(["abc"], boom, text_of).parts == ("abc",)

    def test_nothing_usable_gives_no_parts(self):
        plan = plan_parts([None], make_line, text_of)
        assert plan.parts == () and plan.included == 0


class TestMayNeedParts:
    def test_many_messages_do(self):
        assert may_need_parts(["a"] * 201, text_of)

    def test_a_few_short_messages_do_not(self):
        assert not may_need_parts(["hello"] * 10, text_of)

    def test_long_text_does(self):
        assert may_need_parts(["q" * 2000] * 6, text_of)


class TestFinalStep:
    def test_the_join_keeps_the_order_and_numbers_each_part(self):
        text = merge_input(["first", "second"])
        assert text == "Part 1 of 2:\nfirst\n\nPart 2 of 2:\nsecond"

    def test_the_fallback_uses_the_windows_headings(self):
        text = joined_partials(["a", "b"], lambda n, total: f"<{n}/{total}>")
        assert text == "<1/2>\na\n\n<2/2>\nb"


class Calls:
    """A fake request: records what each call got, answers from a script."""

    def __init__(self, fail_at=None, error=None, on_call=None):
        self.seen = []
        self.fail_at = fail_at
        self.error = error or DescriptionError("server")
        self.on_call = on_call

    def __call__(self, operation, merging, question, text):
        self.seen.append((merging, question, text, operation))
        if self.on_call:
            self.on_call(len(self.seen), operation)
        if self.fail_at == len(self.seen):
            raise self.error
        return (f"summary {len(self.seen)}", f"provider{len(self.seen)}")


def parent_for(parts):
    return Operation("text", steps=len(parts) + 1)


class TestRunParts:
    def test_each_part_then_the_join_in_order(self):
        parts = ("one", "two", "three")
        calls, steps = Calls(), []
        result = run_parts(parent_for(parts), parts, calls, lambda step, total: steps.append((step, total)),
                           heading=lambda n, total: f"{n}/{total}")
        assert [(merging, text) for merging, _q, text, _op in calls.seen[:3]] == [
            (False, "one"), (False, "two"), (False, "three")]
        assert "part 2 of 3" in calls.seen[1][1]
        merging, question, text, _op = calls.seen[3]
        assert merging is True and text == merge_input(["summary 1", "summary 2", "summary 3"])
        assert len(calls.seen) == len(parts) + 1  # the ceiling a window announces
        assert steps == [(1, 3), (2, 3), (3, 3), (4, 3)]
        assert result.answer == "summary 4" and result.provider == "provider4" and result.merged
        assert result.joined == text

    def test_every_request_has_an_operation_of_its_own(self):
        parts = ("one", "two")
        calls = Calls()
        run_parts(parent_for(parts), parts, calls)
        operations = [seen[3] for seen in calls.seen]
        assert len({id(op) for op in operations}) == 3
        assert all(op.total_seconds == service.ATTEMPT_SECONDS["text"] * service.BUDGET_ATTEMPTS
                   for op in operations)

    def test_a_failed_part_names_itself_and_nothing_further_is_asked(self):
        parts = ("one", "two", "three")
        cause = ChainFailed([("gemini", "quota"), ("openai", "server")])
        calls = Calls(fail_at=2, error=cause)
        with pytest.raises(PartFailed) as raised:
            run_parts(parent_for(parts), parts, calls)
        error = raised.value
        assert (error.part, error.total) == (2, 3)
        assert error.attempts == (("gemini", "quota"), ("openai", "server"))
        assert str(error) == "ai_error_server"
        assert len(calls.seen) == 2  # no third part, no join: nothing shown as complete
        key = error.key
        assert (key.part, key.total, key.attempts) == (2, 3, error.attempts)

    def test_a_failed_join_offers_every_part_and_says_it_was_not_joined(self):
        parts = ("one", "two")
        calls = Calls(fail_at=3)
        result = run_parts(parent_for(parts), parts, calls, heading=lambda n, total: f"P{n}")
        assert result.merged is False
        assert result.answer == "P1\nsummary 1\n\nP2\nsummary 2"
        assert result.joined == merge_input(["summary 1", "summary 2"])

    def test_cancelling_during_a_part_ends_at_once(self):
        parts = ("one", "two", "three")
        parent = parent_for(parts)
        calls = Calls(on_call=lambda n, op: parent.cancel() if n == 2 else None,
                      fail_at=2, error=DescriptionError("cancelled"))
        with pytest.raises(DescriptionError) as raised:
            run_parts(parent, parts, calls)
        assert raised.value.category == "cancelled" and not isinstance(raised.value, PartFailed)
        assert len(calls.seen) == 2
        assert calls.seen[1][3].cancelled.is_set()  # the request in flight was told

    def test_cancelling_between_parts_asks_nothing_more(self):
        parts = ("one", "two")
        parent = parent_for(parts)
        calls = Calls(on_call=lambda n, op: parent.cancel() if n == 1 else None)
        with pytest.raises(DescriptionError) as raised:
            run_parts(parent, parts, calls)
        assert raised.value.category == "cancelled" and len(calls.seen) == 1

    def test_cancelling_during_the_join_is_not_a_partial_answer(self):
        parts = ("one",)
        parent = parent_for(parts)
        calls = Calls(on_call=lambda n, op: parent.cancel() if n == 2 else None,
                      fail_at=2, error=DescriptionError("cancelled"))
        with pytest.raises(DescriptionError) as raised:
            run_parts(parent, parts, calls)
        assert raised.value.category == "cancelled"

    def test_the_overall_deadline_is_the_actions_not_a_parts_failure(self):
        parts = ("one", "two")
        parent = parent_for(parts)
        calls = Calls(on_call=lambda n, op: parent.expire() if n == 1 else None,
                      fail_at=1, error=DescriptionError("cancelled"))
        with pytest.raises(DescriptionError) as raised:
            run_parts(parent, parts, calls)
        assert raised.value.category == "timeout" and not isinstance(raised.value, PartFailed)

    def test_a_cancel_before_a_step_reaches_it_when_adopted(self):
        parent = Operation("text", steps=2)
        parent.cancel()
        child = Operation("text")
        parent.adopt(child)
        assert child.cancelled.is_set()


class TestBudgets:
    def test_the_window_waits_for_every_step(self):
        one = Operation("text")
        assert Operation("text", steps=MAX_CALLS).total_seconds == one.total_seconds * MAX_CALLS
        assert Operation("text", steps=0).total_seconds == one.total_seconds

    def test_a_local_model_keeps_its_longer_attempts_in_every_step(self):
        local = Operation("text", local=True)
        assert local.total_seconds == 150 + (service.LOCAL_ATTEMPT_SECONDS["text"] - 60)
        assert Operation("text", local=True, steps=MAX_CALLS).total_seconds == local.total_seconds * MAX_CALLS
        # each step is itself a local-sized Operation, so the sum never passes the parent's
        assert MAX_CALLS * local.total_seconds == Operation("text", local=True, steps=MAX_CALLS).total_seconds

    def test_the_session_counts_one_action_whatever_the_steps(self):
        session = MediaSession("a", "c", "m", "text")
        generation, operation = session.begin("Summarize these messages.", steps=MAX_CALLS)
        assert session.requests == 1 and operation.total_seconds == Operation("text").total_seconds * MAX_CALLS


class TestUsageCounters:
    def test_every_request_is_counted(self, monkeypatch):
        monkeypatch.setattr(service, "request_answer", lambda *args, **kwargs: "ok")
        counted = []
        parts = ("one", "two", "three")

        def call(step, merging, question, text):
            return run_chain(step, ["gemini"], {"gemini": "m"}, lambda p: "key", None, (), question,
                             "instructions", "balanced", usage_sink=lambda p, c: counted.append((p, c)))
        run_parts(parent_for(parts), parts, call)
        assert counted == [("gemini", usage.OK)] * (len(parts) + 1)

    def test_a_failing_part_counts_its_failure_too(self, monkeypatch):
        def answer(provider, *args, **kwargs):
            raise DescriptionError("authentication")
        monkeypatch.setattr(service, "request_answer", answer)
        counted = []

        def call(step, merging, question, text):
            return run_chain(step, ["gemini"], {"gemini": "m"}, lambda p: "key", None, (), question,
                             "instructions", "balanced", usage_sink=lambda p, c: counted.append((p, c)))
        with pytest.raises(PartFailed) as raised:
            run_parts(parent_for(("one", "two")), ("one", "two"), call)
        assert raised.value.part == 1 and counted == [("gemini", "authentication")]


class TestPreference:
    def test_off_unless_saved_as_true(self):
        def prefs(raw):
            return ai_config.preferences(NS(get=lambda key: raw if key == "ai_media" else None))
        assert prefs({})["summary_hierarchy"] is False
        assert prefs({"summary_hierarchy": "yes"})["summary_hierarchy"] is False
        assert prefs({"summary_hierarchy": True})["summary_hierarchy"] is True


class TestFailureText:
    def test_a_part_failure_says_which_part_then_why(self):
        i18n = NS(t=lambda key: key + ("{part}/{total}" if key == "ai_summary_part_failed" else ""))
        text = failure_text(i18n, PartFailed(2, 5, DescriptionError("quota")))
        assert text == "ai_summary_part_failed2/5\nai_error_quota"

    def test_a_chain_inside_a_part_lists_its_providers(self):
        i18n = NS(t=lambda key: key)
        error = PartFailed(1, 3, ChainFailed([("gemini", "quota"), ("openai", "server")]))
        text = failure_text(i18n, error.key)
        assert text.startswith("ai_summary_part_failed") and "ai_chain_failed" in text
        assert text.count("ai_attempt_line") == 2


class Window(AISummaryPartsMixin):
    def __init__(self, parts=("a", "b", "c")):
        self._parts = parts
        self.spoken = []
        self.status = NS(ChangeValue=lambda text: self.status_texts.append(text))
        self.status_texts = []
        templates = {"ai_summary_parts_merging": "merging {total}",
                     "ai_summary_part_progress": "part {part}/{total}",
                     "ai_summary_parts_merge_failed": "merge failed"}
        self.i18n = NS(t=lambda key: templates[key])
        self.session = NS(generation=3, active=object())

    def _valid(self):
        return True

    def _speak(self, text):
        self.spoken.append(text)


class TestWindowPart:
    def test_only_the_opening_request_and_generate_again_are_hierarchical(self):
        window = Window()
        assert window._parts_run(True, False)
        assert not window._parts_run(False, False)  # a follow-up question
        assert not window._parts_run(True, True)  # a quick analysis mode
        assert not Window(())._parts_run(True, False)

    def test_the_first_part_is_announced_by_the_start_and_later_ones_once_each(self):
        window = Window()
        assert window._parts_loading() == "part 1/3"
        window._parts_progress(3, 1, 3)
        window._parts_progress(3, 2, 3)
        window._parts_progress(3, 4, 3)  # one past the last part: the join
        assert window.status_texts == ["part 1/3", "part 2/3", "merging 3"]
        assert window.spoken == ["part 2/3", "merging 3"]

    def test_a_late_report_of_a_cancelled_run_is_ignored(self):
        window = Window()
        window._parts_progress(2, 2, 3)  # an older generation
        window.session.active = None
        window._parts_progress(3, 2, 3)
        assert window.status_texts == [] and window.spoken == []

    def test_the_notice_is_only_there_after_a_failed_join(self):
        window = Window()
        assert window._parts_failed_notice() == ""
        window._parts_merge_failed = True
        assert window._parts_failed_notice() == "merge failed"
