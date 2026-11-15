"""Translating the messages already in a chat (issue #793).

core/translation_batch.py directly, and TranslationBatchMixin on a plain stub:
no wx window is built, the mixin's wx calls go to a fake module and the AI
worker is replaced by a list of submitted requests.
"""
import time
from types import SimpleNamespace

import pytest

from core.ai_media.errors import DescriptionError
from core.message_translation import MAX_TEXT_CHARS
from core.translation_batch import BATCH_LIMIT, batch_candidates, says_progress
from ui.conversation_panel import translation_actions as actions
from ui.conversation_panel import translation_batch as batch

JID = "5511999990000@s.whatsapp.net"
OTHER = "5511888880000@s.whatsapp.net"
SETTINGS = {"provider": "gemini", "language": "Portuguese"}
TEXTS = {
    "translation_batch_sending": "sending {provider} {count}",
    "translation_batch_left_out": "left {left_out}",
    "translation_batch_progress": "progress {done}/{total}",
    "translation_batch_done": "done {count}",
    "translation_batch_failed": "failed {error} {done}",
    "translation_batch_cancelled": "cancelled {done}",
    "translation_locked_confirm": "locked {provider}",
    "datetime_fmt": "%d/%m/%Y %H:%M",
}


def message(number, text=None, *, from_me=False, jid=JID):
    return {"key": {"remoteJid": jid, "id": f"id{number}", "fromMe": from_me},
            "messageType": "conversation", "message": {"conversation": text or f"hello {number}"},
            "messageTimestamp": int(time.time())}


# ── Which messages ───────────────────────────────────────────────────────

class TestCandidates:
    def test_only_received_text_messages_in_list_order(self):
        rows = [message(1), message(2, from_me=True), {"key": {"id": "x"}, "messageType": "imageMessage"},
                message(3), "separator"]
        assert [row["key"]["id"] for row in batch_candidates(rows)[0]] == ["id1", "id3"]

    def test_a_text_too_long_for_one_request_is_skipped(self):
        rows = [message(1, "a" * (MAX_TEXT_CHARS + 1)), message(2)]
        assert [row["key"]["id"] for row in batch_candidates(rows)[0]] == ["id2"]

    def test_the_first_ones_are_kept_and_the_rest_counted(self):
        rows = [message(n) for n in range(BATCH_LIMIT + 7)]
        kept, left_out = batch_candidates(rows)
        assert len(kept) == BATCH_LIMIT and kept[0]["key"]["id"] == "id0"
        assert left_out == 7

    def test_the_same_message_twice_counts_once(self):
        assert len(batch_candidates([message(1), message(1)])[0]) == 1

    def test_the_limit_stays_below_the_result_cache(self):
        # Every translation of a batch must still be on screen at its end.
        assert BATCH_LIMIT <= actions._RESULT_LIMIT

    @pytest.mark.parametrize("done, total, said", [
        (10, 30, True), (20, 30, True), (30, 30, False), (5, 30, False), (0, 30, False), (10, 10, False)])
    def test_progress_every_ten_but_not_on_the_last(self, done, total, said):
        assert says_progress(done, total) is said


# ── The panel ────────────────────────────────────────────────────────────

class FakeWx:
    YES, NO, YES_NO, NO_DEFAULT, ID_ANY, ID_CANCEL, EVT_MENU = 1, 2, 4, 8, -1, 16, "menu"

    def __init__(self):
        self.answer = self.YES
        self.asked = []
        self.later = []
        self.while_asking = None

    def CallAfter(self, function, *args):
        function(*args)

    def CallLater(self, delay, function):
        timer = SimpleNamespace(stopped=False)
        timer.Stop = lambda: setattr(timer, "stopped", True)
        self.later.append((delay, function, timer))
        return timer

    def MessageBox(self, prompt, *args):
        self.asked.append(prompt)
        if self.while_asking:
            self.while_asking()
        return self.answer


class App:
    def __init__(self, raw):
        self.raw = dict(raw)

    def get_snapshot(self, key):
        return self.raw

    def get(self, key):
        return self.raw

    def update(self, key, updater):
        self.raw = updater(self.raw)


class List:
    def Freeze(self):
        pass

    def Thaw(self):
        pass

    def GetFocusedItem(self):
        return 0


class Menu:
    def __init__(self):
        self.labels = []

    def Append(self, item_id, label):
        self.labels.append(label)
        return label


class Panel(actions.TranslationActionsMixin, batch.TranslationBatchMixin):
    def __init__(self, raw=SETTINGS):
        self.spoken = []
        self.locked = set()
        self.shown = []
        self.bound = {}
        self.main_window = SimpleNamespace(
            _app_settings=App(raw), i18n=SimpleNamespace(t=lambda key: TEXTS.get(key, key)),
            output=self.spoken.append, is_chat_locked=lambda jid: jid in self.locked,
            _chat_lock_unlocked=False, _normalize_jid=lambda jid: jid,
            _lid_to_phone={}, _phone_to_lid={}, settings={},
            db=SimpleNamespace(get_metadata_json=lambda key, default: {},
                               set_metadata_json=lambda key, value: None))
        self.conversation = {"remoteJid": JID}
        self.messages_list = List()
        self._sorted_messages = []
        self.selected_messages = set()
        self.refreshed = []

    def Bind(self, event, handler, item):
        self.bound[item] = handler

    @staticmethod
    def _is_separator(row):
        return row.get("separator") is True

    def _refresh_message_rows_by_ids(self, ids):
        self.refreshed.append(set(ids))

    def _update_read_more_button(self, index):
        pass

    def _resolve_mentions_in_text(self, text, mentioned):
        return text

    @staticmethod
    def _message_mentioned_jids(msg):
        return []

    @staticmethod
    def _extract_timestamp(msg):
        return msg.get("messageTimestamp")

    @staticmethod
    def _sender_label(msg):
        return "Ana"

    def _show_translation(self, answer, *, original, same, jid):
        self.shown.append(answer)


@pytest.fixture
def wx(monkeypatch):
    fake = FakeWx()
    monkeypatch.setattr(actions, "wx", fake)
    monkeypatch.setattr(batch, "wx", fake)
    return fake


@pytest.fixture
def submitted(monkeypatch):
    sent = []
    monkeypatch.setattr(actions, "submit", lambda work, complete: sent.append(complete))
    return sent


def answer_all(submitted, prefix="tr"):
    """Answer each request as it comes, until none is waiting."""
    count = 0
    while submitted:
        complete = submitted.pop(0)
        count += 1
        complete(f"{prefix} {count}", None)
    return count


class TestMenu:
    def test_from_here_and_selected_are_offered_when_set_up(self, wx):
        panel = Panel()
        panel._sorted_messages = [message(1), message(2)]
        panel.selected_messages = {"id2"}
        menu = Menu()
        panel._append_translation_batch_items(menu, message(1))
        assert menu.labels == ["translation_batch_from_here_menu", "translation_batch_selected_menu"]

    def test_nothing_without_a_provider(self, wx):
        panel = Panel(raw={})
        panel._sorted_messages = [message(1)]
        menu = Menu()
        panel._append_translation_batch_items(menu, message(1))
        assert menu.labels == []

    def test_only_own_messages_offer_nothing(self, wx):
        panel = Panel()
        panel._sorted_messages = [message(1, from_me=True)]
        menu = Menu()
        panel._append_translation_batch_items(menu, panel._sorted_messages[0])
        assert menu.labels == []

    def test_a_running_batch_offers_only_cancel(self, wx, submitted):
        panel = Panel()
        panel._sorted_messages = [message(1), message(2)]
        panel._on_translate_batch(panel._sorted_messages)
        menu = Menu()
        panel._append_translation_batch_items(menu, message(1))
        assert menu.labels == ["translation_batch_cancel_menu"]

    def test_from_here_starts_at_the_focused_message(self, wx):
        panel = Panel()
        panel._sorted_messages = [message(1), {"separator": True, "key": {}}, message(2), message(3)]
        assert [row["key"]["id"] for row in panel._translation_batch_from(message(2))] == ["id2", "id3"]


class TestRunning:
    def test_each_message_is_translated_in_turn_into_its_line(self, wx, submitted):
        panel = Panel()
        panel._sorted_messages = [message(1), message(2, from_me=True), message(3)]
        panel._on_translate_batch(panel._sorted_messages)
        assert panel.spoken[0].startswith("sending ") and panel.spoken[0].endswith(" 2")
        assert len(submitted) == 1  # one request at a time
        assert answer_all(submitted) == 2
        assert panel.refreshed == [{"id1"}, {"id3"}]
        assert panel.spoken[-1] == "done 2"
        assert panel._translation_batch is None

    def test_more_than_the_limit_says_how_many_were_left_out(self, wx, submitted):
        panel = Panel()
        panel._sorted_messages = [message(n) for n in range(BATCH_LIMIT + 3)]
        panel._on_translate_batch(panel._sorted_messages)
        assert panel.spoken[0].endswith(f"{BATCH_LIMIT} left 3")

    def test_progress_is_said_every_ten(self, wx, submitted):
        panel = Panel()
        panel._sorted_messages = [message(n) for n in range(25)]
        panel._on_translate_batch(panel._sorted_messages)
        answer_all(submitted)
        assert [text for text in panel.spoken if text.startswith("progress")] == [
            "progress 10/25", "progress 20/25"]
        assert panel.spoken[-1] == "done 25"

    def test_separate_display_shows_all_together_at_the_end(self, wx, submitted):
        panel = Panel(raw={**SETTINGS, "display": "separate"})
        panel._sorted_messages = [message(1), message(2)]
        panel._on_translate_batch(panel._sorted_messages)
        answer_all(submitted)
        assert len(panel.shown) == 1
        assert "Ana: tr 1" in panel.shown[0] and "Ana: tr 2" in panel.shown[0]
        assert panel.refreshed == []

    def test_a_message_already_translated_sends_no_second_request(self, wx, submitted):
        panel = Panel()
        panel._sorted_messages = [message(1), message(2)]
        panel._on_translate_batch([message(1)])
        answer_all(submitted)
        panel._on_translate_batch(panel._sorted_messages)
        assert answer_all(submitted) == 1
        assert panel.spoken[-1] == "done 2"

    def test_an_error_stops_and_says_how_many_were_done(self, wx, submitted):
        panel = Panel()
        panel._sorted_messages = [message(1), message(2), message(3)]
        panel._on_translate_batch(panel._sorted_messages)
        submitted.pop(0)("ok", None)
        submitted.pop(0)(None, "ai_error_quota")
        assert panel.spoken[-1] == "failed ai_error_quota 1"
        assert submitted == [] and panel._translation_batch is None

    def test_busy_workers_retry_the_same_message_later(self, wx, monkeypatch):
        sent = []
        busy = [True]

        def submit(work, complete):
            if busy[0]:
                raise DescriptionError("busy")
            sent.append(complete)

        monkeypatch.setattr(actions, "submit", submit)
        panel = Panel()
        panel._sorted_messages = [message(1)]
        panel._on_translate_batch(panel._sorted_messages)
        assert sent == [] and len(wx.later) == 1
        busy[0] = False
        wx.later[0][1]()
        sent.pop(0)("tr", None)
        assert panel.spoken[-1] == "done 1"

    def test_a_second_batch_while_one_runs_is_busy(self, wx, submitted):
        panel = Panel()
        panel._sorted_messages = [message(1)]
        panel._on_translate_batch(panel._sorted_messages)
        panel._on_translate_batch(panel._sorted_messages)
        assert panel.spoken[-1] == "ai_error_busy" and len(submitted) == 1

    def test_nothing_to_translate_is_said(self, wx, submitted):
        panel = Panel()
        panel._on_translate_batch([message(1, from_me=True)])
        assert panel.spoken == ["translation_batch_nothing"] and submitted == []

    def test_without_settings_it_says_what_is_missing(self, wx, submitted):
        panel = Panel(raw={})
        panel._on_translate_batch([message(1)])
        assert panel.spoken == ["translation_not_configured"] and submitted == []


class TestStopping:
    def test_cancel_stops_and_keeps_what_was_done(self, wx, submitted):
        panel = Panel()
        panel._sorted_messages = [message(1), message(2), message(3)]
        panel._on_translate_batch(panel._sorted_messages)
        submitted.pop(0)("tr 1", None)
        pending = submitted.pop(0)
        panel._cancel_translation_batch()
        assert panel.spoken[-1] == "cancelled 1"
        pending(None, "ai_error_cancelled")  # the cancelled request comes back
        assert submitted == [] and panel.spoken[-1] == "cancelled 1"
        assert panel.refreshed == [{"id1"}]

    def test_the_menu_cancel_item_cancels(self, wx, submitted):
        panel = Panel()
        panel._sorted_messages = [message(1), message(2)]
        panel._on_translate_batch(panel._sorted_messages)
        menu = Menu()
        panel._append_translation_batch_items(menu, message(1))
        panel.bound["translation_batch_cancel_menu"](None)
        assert panel._translation_batch is None

    def test_opening_another_chat_drops_the_rest(self, wx, submitted):
        panel = Panel()
        panel._sorted_messages = [message(1), message(2)]
        panel._on_translate_batch(panel._sorted_messages)
        panel.conversation = {"remoteJid": OTHER}
        submitted.pop(0)("tr 1", None)
        assert submitted == [] and panel._translation_batch is None and panel.refreshed == []

    def test_a_locked_chat_asks_every_time_and_no_sends_nothing(self, wx, submitted):
        panel = Panel()
        panel.locked.add(JID)
        panel.main_window._chat_lock_unlocked = True
        wx.answer = wx.NO
        panel._on_translate_batch([message(1)])
        assert len(wx.asked) == 1 and wx.asked[0].startswith("locked ")
        assert submitted == []
        wx.answer = wx.YES
        panel._on_translate_batch([message(1)])
        assert len(wx.asked) == 2 and len(submitted) == 1

    def test_a_locked_chat_with_the_vault_closed_sends_nothing(self, wx, submitted):
        panel = Panel()
        panel.locked.add(JID)
        panel._on_translate_batch([message(1)])
        assert wx.asked == [] and submitted == []

    def test_the_vault_closing_during_the_question_sends_nothing(self, wx, submitted):
        panel = Panel()
        panel.locked.add(JID)
        panel.main_window._chat_lock_unlocked = True
        wx.while_asking = lambda: setattr(panel.main_window, "_chat_lock_unlocked", False)
        panel._on_translate_batch([message(1)])
        assert submitted == [] and getattr(panel, "_translation_batch", None) is None

    def test_closing_the_vault_stops_a_locked_chat_batch_quietly(self, wx, submitted):
        panel = Panel()
        panel.locked.add(JID)
        panel.main_window._chat_lock_unlocked = True
        panel._sorted_messages = [message(1), message(2)]
        panel._on_translate_batch(panel._sorted_messages)
        spoken = list(panel.spoken)
        panel.main_window._chat_lock_unlocked = False
        panel.drop_locked_translations()
        assert panel._translation_batch is None and panel.spoken == spoken

    def test_closing_the_vault_leaves_an_unlocked_chat_batch_running(self, wx, submitted):
        panel = Panel()
        panel._sorted_messages = [message(1)]
        panel._on_translate_batch(panel._sorted_messages)
        panel.drop_locked_translations()
        assert panel._translation_batch is not None


class TestReviewFixes:
    def test_a_display_change_reuses_translations_already_paid_for(self, wx, submitted):
        panel = Panel(raw={**SETTINGS, "display": "append"})
        panel._sorted_messages = [message(1), message(2)]
        panel._on_translate_batch([message(1)])
        answer_all(submitted)
        panel.main_window._app_settings.raw["display"] = "replace"
        panel._on_translate_batch(panel._sorted_messages)
        assert answer_all(submitted) == 1

    def test_leaving_the_chat_ends_the_batch_before_its_request_returns(self, wx, submitted):
        panel = Panel()
        panel._sorted_messages = [message(1), message(2)]
        panel._on_translate_batch(panel._sorted_messages)
        panel.conversation = {"remoteJid": OTHER}
        panel._sorted_messages = [message(5, jid=OTHER)]
        menu = Menu()
        panel._append_translation_batch_items(menu, panel._sorted_messages[0])
        assert menu.labels == ["translation_batch_from_here_menu"]
        panel._on_translate_batch(panel._sorted_messages)
        assert panel.spoken[-1].startswith("sending ") and len(submitted) == 2
        submitted.pop(0)("late answer for the first chat", None)  # dropped
        assert panel._translation_batch is not None and panel.refreshed == []

    def test_a_settings_change_stops_the_batch(self, wx, submitted):
        panel = Panel()
        panel._sorted_messages = [message(1), message(2)]
        panel._on_translate_batch(panel._sorted_messages)
        panel.main_window._app_settings.raw["language"] = "Spanish"
        submitted.pop(0)("tr 1", None)
        assert submitted == [] and panel._translation_batch is None and panel.refreshed == []

    def test_shutting_down_stops_the_batch(self, wx, submitted):
        panel = Panel()
        panel._sorted_messages = [message(1), message(2)]
        panel._on_translate_batch(panel._sorted_messages)
        panel.main_window._shutting_down = True
        submitted.pop(0)("tr 1", None)
        assert submitted == [] and panel._translation_batch is None

    def test_cancel_stops_a_waiting_busy_retry(self, wx, monkeypatch):
        def busy(work, complete):
            raise DescriptionError("busy")

        monkeypatch.setattr(actions, "submit", busy)
        panel = Panel()
        panel._on_translate_batch([message(1)])
        panel._cancel_translation_batch()
        assert wx.later[0][2].stopped and panel._translation_batch is None

    def test_another_synchronous_refusal_ends_the_batch_and_says_it(self, wx, monkeypatch):
        def refuse(work, complete):
            raise DescriptionError("credentials")

        monkeypatch.setattr(actions, "submit", refuse)
        panel = Panel()
        panel._on_translate_batch([message(1)])
        assert panel._translation_batch is None and panel.spoken[-1] == "failed ai_error_credentials 0"
