"""Issue #469: transcribing the selected messages in one go — the shortcuts,
the bulk-shortcut setting, the queue and what is said.

ConversationsPanel needs a wx.App, so the handlers run on a stub, with the
message runs faked at message_run.MessageTranscription.
"""

import pathlib
import re
import types

import pytest

import core.keyboard_shortcuts as keyboard_shortcuts
from core.shortcut_catalog import SHORTCUTS
from core.transcription import bulk, errors, message_run
from core.transcription import stored as stored_transcription
from core.transcription.backend import TranscriptionResult
from tests.locales import load_strings, registered_locale_codes
from ui.conversation_panel import bulk_transcription
from ui.conversation_panel.bulk_transcription import _BulkRun
from ui.conversations import ConversationsPanel

CLIENT = pathlib.Path(__file__).resolve().parents[1] / "client"
LOCALES = list(registered_locale_codes())
KEY = stored_transcription.TRANSCRIPTION_KEY
JID = "5511999999999@s.whatsapp.net"
BULK = {
    "messages.ID_BULK_TRANSCRIBE": ((7, ord("T")), "shortcut_bulk_transcribe_label"),
    "messages.ID_BULK_TRANSCRIBE_INLINE": ((7, ord("N")), "shortcut_bulk_transcribe_inline_label"),
}


def _voice(mid, **extra):
    msg = {"key": {"id": mid, "remoteJid": JID, "fromMe": False}, "messageType": "audioMessage",
           "message": {"audioMessage": {"seconds": 5}}, "messageTimestamp": 1_700_000_000}
    msg.update(extra)
    return msg


def _result(text):
    return TranscriptionResult(
        text=text, language="pt", language_probability=0.9, duration_seconds=5.0,
        segments=(), vad_used=True, backend="faster_whisper", model_id="small",
        device="cpu", compute_type="int8")


class TestTheShortcuts:
    @pytest.mark.parametrize("shortcut_id", sorted(BULK))
    def test_each_is_a_catalogue_row_with_its_default(self, shortcut_id):
        default, label = BULK[shortcut_id]
        [row] = [s for s in SHORTCUTS if s.id == shortcut_id]
        table = load_strings("en-US").__getitem__
        assert keyboard_shortcuts.default_binding(row, table) == default
        assert row.label == label

    @pytest.mark.parametrize("locale", LOCALES)
    @pytest.mark.parametrize("shortcut_id", sorted(BULK))
    def test_no_default_collides_in_any_language(self, locale, shortcut_id):
        default, label = BULK[shortcut_id]
        [row] = [s for s in SHORTCUTS if s.id == shortcut_id]
        table = load_strings(locale)
        assert keyboard_shortcuts.conflicts(row, default, {}, SHORTCUTS, table.__getitem__) == []
        assert table[label] and table[label] != label

    def test_the_table_and_the_handlers_are_wired(self):
        # Spacing-tolerant: the table is column-aligned.
        source = (CLIENT / "ui/conversation_panel/accelerators.py").read_text(encoding="utf-8")
        assert re.search(r'ord\("T"\),\s*self\.ID_BULK_TRANSCRIBE\)', source)
        assert re.search(r'ord\("N"\),\s*self\.ID_BULK_TRANSCRIBE_INLINE\)', source)
        assert re.search(r"self\._on_accel_bulk_transcribe,\s*id=self\.ID_BULK_TRANSCRIBE\)", source)
        assert re.search(
            r"self\._on_accel_bulk_transcribe_inline,\s*id=self\.ID_BULK_TRANSCRIBE_INLINE\)", source)

    @pytest.mark.parametrize("locale", LOCALES)
    def test_every_sentence_exists_and_formats(self, locale):
        table = load_strings(locale)
        table["bulk_transcription_confirm"].format(count=1, already=2, skipped=3)
        table["bulk_transcription_progress"].format(index=1, total=2)
        for key in ("bulk_transcription_done", "bulk_transcription_cancelled"):
            table[key].format(done=1, skipped=2, failed=3)
        table["bulk_transcription_cancel_confirm"].format(index=1, total=2)
        for key in ("transcribe_selected", "transcribe_selected_inline", "bulk_transcription_none",
                    "bulk_transcription_title", "bulk_transcription_cancel_menu",
                    "bulk_transcription_show_menu", "bulk_transcription_window_title",
                    "bulk_transcription_stopped_locked"):
            assert table[key]


class _I18n:
    language = "pt-BR"

    def __init__(self):
        self._table = load_strings("pt-BR")

    def t(self, key):
        return self._table.get(key, key)


class _MainWindow:
    def __init__(self, bulk=True, enter_transcribes=False):
        self.i18n = _I18n()
        self.settings = {"user_interface": {"bulk_action_shortcuts": bulk,
                                            "enter_transcribes_voice_messages": enter_transcribes},
                         "transcription": {"model": "small"}}
        self.key = b"k"
        self._app_settings = None
        self._wa_connected = True
        self.said = []
        self.stored = []
        self.hidden = False
        self.active = True
        #: msg_id -> what store_message_transcription() answers (STORED otherwise).
        self.answers = {}
        #: The messages memory holds, set by _Panel.
        self.messages = []

    def output(self, text, interrupt=False):
        self.said.append(text)

    def IsActive(self):
        return self.active

    def is_chat_hidden_by_vault(self, jid):
        return self.hidden

    def _transcription_copies(self, jid, msg_id):
        return [m for m in self.messages if (m.get("key") or {}).get("id") == msg_id]

    def store_message_transcription(self, jid, msg_id, value, existing=False):
        self.stored.append((msg_id, value))
        answer = self.answers.get(msg_id, stored_transcription.SAVE_STORED)
        if answer == stored_transcription.SAVE_STORED:
            for msg in self._transcription_copies(jid, msg_id):
                msg[KEY] = value
        return answer

    def _find_api_ffmpeg(self):
        return ""


class _Panel:
    _bulk_shortcuts_enabled = ConversationsPanel._bulk_shortcuts_enabled
    _enter_transcribes_voice = ConversationsPanel._enter_transcribes_voice
    _on_accel_transcribe = ConversationsPanel._on_accel_transcribe
    _on_accel_transcribe_inline = ConversationsPanel._on_accel_transcribe_inline
    _is_separator = ConversationsPanel._is_separator
    _bulk_transcription_running = ConversationsPanel._bulk_transcription_running
    _summary_voice_running = ConversationsPanel._summary_voice_running
    _bulk_transcription_menu_key = ConversationsPanel._bulk_transcription_menu_key
    _bulk_transcription_worker = ConversationsPanel._bulk_transcription_worker
    _bulk_transcribe_one = ConversationsPanel._bulk_transcribe_one
    _bulk_install_wide_models = ConversationsPanel._bulk_install_wide_models
    _store_bulk_transcription = ConversationsPanel._store_bulk_transcription
    _finish_bulk_transcription = ConversationsPanel._finish_bulk_transcription
    _start_bulk_transcription = ConversationsPanel._start_bulk_transcription
    _on_mass_transcribe_messages = ConversationsPanel._on_mass_transcribe_messages
    _on_mass_transcribe_inline_messages = ConversationsPanel._on_mass_transcribe_inline_messages
    _mark_bulk_saved_inline = ConversationsPanel._mark_bulk_saved_inline
    _bulk_window_may_open = ConversationsPanel._bulk_window_may_open
    _bulk_shown_entries = ConversationsPanel._bulk_shown_entries
    _cancel_bulk_transcription_now = ConversationsPanel._cancel_bulk_transcription_now

    def __init__(self, messages=(), selected=(), **kwargs):
        self.main_window = _MainWindow(**kwargs)
        self._sorted_messages = list(messages)
        self.main_window.messages = self._sorted_messages
        self.selected_messages = set(selected)
        self.conversation = {"remoteJid": JID}
        self.messages_list = types.SimpleNamespace(GetFirstSelected=lambda: 0)
        self.calls = []
        self.repainted = []
        self.shown = 0

    # What the single-message paths would do.
    def _on_menu_transcribe(self, msg):
        self.calls.append(("single", msg["key"]["id"]))

    def _on_menu_toggle_voice_action(self, msg):
        self.calls.append(("toggle", msg["key"]["id"]))

    def _refresh_message_rows_by_ids(self, ids):
        pass

    def _repaint_or_repopulate(self, ids):
        self.repainted.extend(ids)

    def _download_media_to_disk(self, msg, path):
        return True

    def _on_show_bulk_transcriptions(self, event=None, spoken=""):
        self.shown += 1
        self.shown_spoken = spoken


@pytest.fixture(autouse=True)
def _synchronous(monkeypatch):
    monkeypatch.setattr(bulk_transcription.wx, "CallAfter", lambda f, *a, **k: f(*a, **k))
    monkeypatch.setattr(bulk_transcription.wx, "MessageBox", lambda *a, **k: bulk_transcription.wx.YES)
    monkeypatch.setattr(bulk_transcription, "data_path", lambda *parts: "/".join(parts))

    class _Thread:
        def __init__(self, target, args=(), **kwargs):
            self._target, self._args = target, args

        def start(self):
            self._target(*self._args)

    monkeypatch.setattr(bulk_transcription.threading, "Thread", _Thread)


class _FakeRun:
    outcomes = []
    made = []

    def __init__(self, msg, *args, **kwargs):
        self.msg = msg
        self.kwargs = kwargs
        self.cancelled = False
        _FakeRun.made.append(self)

    def start(self):
        result, error = _FakeRun.outcomes.pop(0)
        self.kwargs["on_finished"](result, error)

    def join(self):
        pass

    def cancel(self):
        self.cancelled = True


@pytest.fixture
def runs(monkeypatch):
    _FakeRun.outcomes = []
    _FakeRun.made = []
    monkeypatch.setattr(message_run, "MessageTranscription", _FakeRun)
    return _FakeRun


class TestTheSingleKeysWithASelection:
    def test_alt_shift_t_transcribes_the_selection_when_the_setting_is_on(self, runs):
        runs.outcomes = [(_result("um"), None), (_result("dois"), None)]
        panel = _Panel([_voice("a"), _voice("b")], selected={"a", "b"})
        panel._on_accel_transcribe(None)
        assert [m.msg["key"]["id"] for m in runs.made] == ["a", "b"]
        assert panel.calls == []

    def test_alt_shift_t_stays_single_when_the_setting_is_off(self, runs):
        panel = _Panel([_voice("a")], selected={"a"}, bulk=False)
        panel._on_accel_transcribe(None)
        assert panel.calls == [("single", "a")] and runs.made == []

    def test_alt_shift_n_transcribes_the_selection_into_the_lines(self, runs):
        runs.outcomes = [(_result("um"), None)]
        panel = _Panel([_voice("a")], selected={"a"})
        panel._on_accel_transcribe_inline(None)
        [(msg_id, value)] = panel.main_window.stored
        assert msg_id == "a" and stored_transcription.is_inline(value)

    def test_alt_shift_n_that_plays_stays_single(self, runs):
        panel = _Panel([_voice("a")], selected={"a"}, enter_transcribes=True)
        panel._on_accel_transcribe_inline(None)
        assert panel.calls == [("toggle", "a")] and runs.made == []

    def test_without_a_selection_both_stay_single(self, runs):
        panel = _Panel([_voice("a")])
        panel._on_accel_transcribe(None)
        panel._on_accel_transcribe_inline(None)
        assert panel.calls == [("single", "a"), ("toggle", "a")]


class TestTheQueue:
    def test_progress_is_said_and_the_tally_ends_it(self, runs):
        runs.outcomes = [(_result("um"), None),
                         (None, errors.TranscriptionError(errors.FFMPEG_FAILED, "x")),
                         (_result(""), None)]
        messages = [_voice("a"), _voice("b"), _voice("c"),
                    {"key": {"id": "t"}, "messageType": "conversation"}]
        panel = _Panel(messages, selected={"a", "b", "c", "t"})
        panel._on_mass_transcribe_inline_messages()
        said = panel.main_window.said
        assert said[0] == panel.main_window.i18n.t("bulk_transcription_progress").format(index=1, total=3)
        assert said[-1] == panel.main_window.i18n.t("bulk_transcription_done").format(
            done=1, skipped=2, failed=1)
        assert [m for m, _v in panel.main_window.stored] == ["a"]
        assert panel.repainted == ["a"]
        assert panel.selected_messages == set()

    def test_the_window_form_keeps_them_and_opens_the_window(self, runs):
        runs.outcomes = [(_result("um"), None)]
        panel = _Panel([_voice("a")], selected={"a"})
        panel._on_mass_transcribe_messages()
        [(_m, value)] = panel.main_window.stored
        assert not stored_transcription.is_inline(value)
        assert panel.shown == 1
        assert panel._bulk_transcription_menu_key() == "bulk_transcription_show_menu"

    def test_the_inline_form_opens_nothing(self, runs):
        runs.outcomes = [(_result("um"), None)]
        panel = _Panel([_voice("a")], selected={"a"})
        panel._on_mass_transcribe_inline_messages()
        assert panel.shown == 0

    def test_an_already_transcribed_one_is_reused_not_run(self, runs):
        saved = {"text": "salvo", "model_id": "small", "backend": "faster_whisper", "at": 1.0}
        runs.outcomes = [(_result("novo"), None)]
        panel = _Panel([_voice("a", **{KEY: saved}), _voice("b")], selected={"a", "b"})
        panel._on_mass_transcribe_inline_messages()
        assert [m.msg["key"]["id"] for m in runs.made] == ["b"]
        assert [e["text"] for e in panel._bulk_transcription.entries] == ["salvo", "novo"]
        # Put in its line without a run, as Alt+Shift+N does on one message.
        assert [m for m, _v in panel.main_window.stored] == ["a", "b"]

    def test_a_setup_failure_stops_the_queue_and_says_why(self, runs):
        runs.outcomes = [(None, errors.TranscriptionError(errors.MODEL_NOT_INSTALLED, "x"))]
        panel = _Panel([_voice("a"), _voice("b")], selected={"a", "b"})
        panel._on_mass_transcribe_inline_messages()
        assert len(runs.made) == 1
        reason = panel.main_window.i18n.t(errors.error_i18n_key(errors.MODEL_NOT_INSTALLED))
        assert panel.main_window.said[-1].startswith(reason)

    def test_a_locked_chat_stops_it_and_says_nothing_about_the_messages(self, runs):
        panel = _Panel([_voice("a")], selected={"a"})
        panel.main_window.hidden = True
        panel._on_mass_transcribe_inline_messages()
        assert runs.made == []
        assert panel.main_window.said == [
            panel.main_window.i18n.t("bulk_transcription_stopped_locked")]
        assert panel._bulk_transcription.entries == []

    def test_nothing_transcribable_is_one_sentence(self, runs):
        panel = _Panel([{"key": {"id": "t"}, "messageType": "conversation"}], selected={"t"})
        panel._on_mass_transcribe_messages()
        assert panel.main_window.said == [panel.main_window.i18n.t("bulk_transcription_none")]
        assert runs.made == []

    def test_declining_the_confirmation_runs_nothing(self, runs, monkeypatch):
        monkeypatch.setattr(bulk_transcription.wx, "MessageBox", lambda *a, **k: object())  # not YES
        panel = _Panel([_voice("a")], selected={"a"})
        panel._on_mass_transcribe_messages()
        assert runs.made == [] and panel.selected_messages == {"a"}


class TestTheWindowOpensOnlyWhereTheUserIs:
    def _run(self, runs, **change):
        runs.outcomes = [(_result("um"), None)]
        panel = _Panel([_voice("a")], selected={"a"})
        for name, value in change.items():
            setattr(panel.main_window, name, value)
        return panel

    def test_it_opens_and_says_the_tally_itself(self, runs):
        panel = self._run(runs)
        panel._on_mass_transcribe_messages()
        assert panel.shown == 1
        assert panel.shown_spoken == panel.main_window.i18n.t("bulk_transcription_done").format(
            done=1, skipped=0, failed=0)
        # Said by the window, after its own announcement — not here.
        assert panel.shown_spoken not in panel.main_window.said

    def test_with_the_app_in_the_background_it_is_offered_instead(self, runs):
        panel = self._run(runs, active=False)
        panel._on_mass_transcribe_messages()
        assert panel.shown == 0
        i18n = panel.main_window.i18n
        assert panel.main_window.said[-1].endswith(i18n.t("bulk_transcription_open_hint"))

    def test_in_another_conversation_it_is_offered_instead(self, runs):
        runs.outcomes = [(_result("um"), None)]
        panel = _Panel([_voice("a")], selected={"a"})
        real_worker = panel._bulk_transcription_worker

        def worker(run):
            # The user opened another conversation while it ran.
            panel.conversation = {"remoteJid": "other@s.whatsapp.net"}
            real_worker(run)

        panel._bulk_transcription_worker = worker
        panel._on_mass_transcribe_messages()
        assert panel.shown == 0
        assert panel.main_window.said[-1].endswith(
            panel.main_window.i18n.t("bulk_transcription_open_hint"))


    def test_nothing_left_to_show_offers_nothing(self, runs):
        runs.outcomes = [(_result("um"), None)]
        panel = self._run(runs)
        panel.main_window.answers["a"] = stored_transcription.SAVE_WITHDRAWN
        panel._on_mass_transcribe_messages()
        assert panel.shown == 0
        i18n = panel.main_window.i18n
        assert panel.main_window.said[-1] == i18n.t("bulk_transcription_done").format(
            done=1, skipped=0, failed=0)


class TestWhatTheWindowMayShow:
    """The SAVE_WITHDRAWN rule: a withdrawn message's text reaches no window."""

    def test_a_message_withdrawn_mid_run_is_left_out(self, runs):
        runs.outcomes = [(_result("um"), None), (_result("dois"), None)]
        panel = _Panel([_voice("a"), _voice("b")], selected={"a", "b"})
        panel.main_window.answers["a"] = stored_transcription.SAVE_WITHDRAWN
        panel._on_mass_transcribe_messages()
        entries = panel._bulk_transcription.entries
        assert [e["kept"] for e in entries] == [bulk.REFUSED, bulk.KEPT]
        shown = bulk.shown_entries(
            entries, lambda mid: stored_transcription.find_record(
                panel.main_window._transcription_copies(JID, mid), mid))
        assert [text for _rec, text in shown] == ["dois"]

    def test_a_text_deleted_after_the_run_is_left_out(self, runs):
        runs.outcomes = [(_result("um"), None)]
        panel = _Panel([_voice("a")], selected={"a"})
        panel._on_mass_transcribe_messages()
        panel._sorted_messages[0][KEY] = stored_transcription.tombstone(9e12)
        shown = bulk.shown_entries(
            panel._bulk_transcription.entries,
            lambda mid: stored_transcription.find_record(
                panel.main_window._transcription_copies(JID, mid), mid))
        assert shown == []

    def test_with_the_cache_off_the_runs_text_is_shown_and_the_tally_says_so(self, runs):
        runs.outcomes = [(_result("um"), None)]
        panel = _Panel([_voice("a")], selected={"a"})
        panel.main_window.answers["a"] = stored_transcription.SAVE_DISABLED
        panel._on_mass_transcribe_inline_messages()
        [entry] = panel._bulk_transcription.entries
        assert entry["kept"] == bulk.NOT_KEPT
        i18n = panel.main_window.i18n
        assert panel.main_window.said[-1].endswith(i18n.t("bulk_transcription_not_kept"))
        shown = bulk.shown_entries(
            [entry], lambda mid: stored_transcription.find_record(
                panel.main_window._transcription_copies(JID, mid), mid))
        assert [text for _rec, text in shown] == ["um"]

    def test_nothing_left_to_show_is_said_with_the_tally(self, monkeypatch):
        panel = _Panel([_voice("a")])
        run = _BulkRun(JID, [], 0, 0, False)
        run.running = False
        run.entries = [bulk.entry(panel._sorted_messages[0], "um", bulk.REFUSED)]
        panel._bulk_transcription = run
        ConversationsPanel._on_show_bulk_transcriptions(panel, None, "tally.")
        assert panel.main_window.said == [
            "tally. " + panel.main_window.i18n.t("bulk_transcription_nothing_to_show")]


class TestQuitting:
    def test_a_running_bulk_run_is_cancelled_with_no_question(self):
        panel = _Panel()
        run = _BulkRun(JID, [], 2, 0, True)
        current = types.SimpleNamespace(cancelled=False)
        current.cancel = lambda: setattr(current, "cancelled", True)
        run.current = current
        panel._bulk_transcription = run
        assert panel._cancel_bulk_transcription_now() is current
        assert run.cancelled.is_set() and current.cancelled

    def test_nothing_running_returns_none(self):
        panel = _Panel()
        assert panel._cancel_bulk_transcription_now() is None
        panel._bulk_transcription = _BulkRun(JID, [], 0, 0, True)
        panel._bulk_transcription.running = False
        assert panel._cancel_bulk_transcription_now() is None


class TestCancelling:
    def test_cancel_stops_the_one_running_and_the_rest(self):
        run = _BulkRun(JID, [], 2, 0, True)
        current = types.SimpleNamespace(cancelled=False)
        current.cancel = lambda: setattr(current, "cancelled", True)
        run.current = current
        run.cancel()
        assert run.cancelled.is_set() and current.cancelled

    def test_while_running_the_menu_offers_to_cancel(self):
        panel = _Panel()
        panel._bulk_transcription = _BulkRun(JID, [], 2, 0, True)
        assert panel._bulk_transcription_menu_key() == "bulk_transcription_cancel_menu"
        panel._bulk_transcription.running = False
        assert panel._bulk_transcription_menu_key() is None
