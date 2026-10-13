"""Local transcription of audio and video statuses.

The conversation's transcription, on the Status tab: the same two keys
(Alt+Shift+T opens the text, Alt+Shift+N puts it in the status's line), the
same alternation with "Enter and Space transcribe", automatic transcription on
arrival and a cache switch of its own. The text is kept in the status's own
record, so the periodic prune of expired statuses deletes it with the status.

Four layers, each on stubs (none of these classes can be built without a
wx.App): the rules (core/transcription/statuses.py), the window's storage
(main_window/status_transcription.py), the flow
(ui/status_transcription_flow.py) and the panel's keys and row
(status_tab/status_transcription.py).
"""

import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import wx
from cryptography.fernet import Fernet

from core import shortcut_catalog
from core.transcription import statuses, stored, temp_sweep
from core.transcription.backend import TranscriptionResult
from core.transcription.preferences import DEFAULTS
from core.utils import DEFAULT_SETTINGS, decrypt_bytes
from main_window import status_transcription as window_module
from main_window.status_transcription import StatusTranscriptionMixin
from status_panel import StatusPanel
from status_tab import status_transcription as panel_module
from ui import status_transcription_flow as flow_module
from ui.status_transcription_flow import StatusTranscriptionFlow
from ui.transcription_flow import MessageTranscriptionFlow

ROOT = Path(__file__).resolve().parents[1]
POSTER = "5511988887777@s.whatsapp.net"


def _status(msg_id="s1", message_type="audioMessage", participant=POSTER, from_me=False,
            timestamp=None):
    return {
        "key": {"id": msg_id, "fromMe": from_me, "remoteJid": statuses.STATUS_JID,
                "participant": participant},
        "messageType": message_type,
        "message": {message_type: {"seconds": 12, "ptt": True}},
        "messageTimestamp": time.time() if timestamp is None else timestamp,
    }


def _value(text="hello there", inline=False, model_id="small", at=None):
    result = TranscriptionResult(text=text, language="en", language_probability=0.9,
                                 duration_seconds=12.0, backend="faster_whisper",
                                 model_id=model_id)
    return stored.with_result(None, stored.record_from_result(
        result, time.time() if at is None else at, inline=inline))


# ── the rules ────────────────────────────────────────────────────────────────


def test_the_three_switches_default_to_what_the_conversation_defaults_to():
    assert statuses.auto_enabled({}) is False
    assert statuses.enter_transcribes({}) is False
    assert statuses.cache_enabled_for({}, "small") is True
    assert statuses.auto_enabled(None) is False
    assert statuses.cache_enabled_for(None, "") is True


def test_only_a_real_true_turns_a_switch_on_and_only_a_real_false_the_cache_off():
    assert statuses.auto_enabled({"transcription": {"auto_transcribe_statuses": True}})
    assert not statuses.auto_enabled({"transcription": {"auto_transcribe_statuses": "yes"}})
    assert statuses.enter_transcribes({"user_interface": {"enter_transcribes_statuses": True}})
    assert not statuses.enter_transcribes({"user_interface": {"enter_transcribes_statuses": 1}})
    assert not statuses.cache_enabled_for(
        {"transcription": {"status_cache_enabled": False}}, "small")
    assert statuses.cache_enabled_for({"transcription": {"status_cache_enabled": None}}, "small")


def test_the_statuses_cache_switch_is_not_the_conversations():
    """Each has its own general switch; turning one off leaves the other."""
    settings = {"transcription": {"cache_enabled": False}}
    assert stored.cache_enabled_for(settings, "small") is False
    assert statuses.cache_enabled_for(settings, "small") is True
    settings = {"transcription": {"status_cache_enabled": False}}
    assert stored.cache_enabled_for(settings, "small") is True
    assert statuses.cache_enabled_for(settings, "small") is False


def test_a_models_own_cache_choice_is_shared_and_outranks_the_general_switch():
    settings = {"transcription": {"status_cache_enabled": False,
                                  "cache_by_model": {"small": True, "tiny": False}}}
    assert statuses.cache_enabled_for(settings, "small") is True
    assert statuses.cache_enabled_for(settings, "tiny") is False
    assert statuses.may_cache(settings, _value(model_id="tiny")) is False
    assert statuses.may_cache(settings, stored.tombstone(1.0)) is True


@pytest.mark.parametrize("message_type,expected", [
    ("audioMessage", True), ("videoMessage", True), ("imageMessage", False),
    ("conversation", False)])
def test_audio_and_video_statuses_are_the_transcribable_ones(message_type, expected):
    assert statuses.is_transcribable(_status(message_type=message_type)) is expected
    assert statuses.is_transcribable(None) is False


def test_only_a_fresh_status_of_someone_else_with_nothing_decided_is_automatic():
    assert statuses.auto_eligible(_status()) is True
    assert statuses.auto_eligible(_status(from_me=True)) is False
    assert statuses.auto_eligible(_status(message_type="imageMessage")) is False
    assert statuses.auto_eligible(_status(timestamp=time.time() - 3600)) is False
    decided = _status()
    decided[statuses.KEY] = stored.tombstone(1.0)
    assert statuses.auto_eligible(decided) is False


def test_the_text_survives_the_list_being_rebuilt_from_the_api():
    """The API's copy of a status comes first in the merge and knows nothing
    of the transcription saved with the cached one."""
    cached = _status("s1")
    cached[statuses.KEY] = _value("kept")
    from_api, other = _status("s1"), _status("s2")

    assert statuses.carry_over([from_api, other], [cached]) == 1

    assert from_api[statuses.KEY] is cached[statuses.KEY]
    assert statuses.KEY not in other


def test_the_later_decision_wins_when_both_copies_hold_one():
    cached, shown = _status("s1"), _status("s1")
    cached[statuses.KEY] = _value("old", at=100.0)
    shown[statuses.KEY] = stored.tombstone(200.0)

    assert statuses.carry_over([shown], [cached]) == 0
    assert shown[statuses.KEY].get("deleted") is True


def test_only_an_inline_record_shows_in_the_line():
    plain, inline = _status(), _status()
    plain[statuses.KEY] = _value("window only")
    inline[statuses.KEY] = _value("in the line", inline=True)

    assert statuses.inline_text(plain, {}) == ""
    assert statuses.inline_text(inline, {}) == "in the line"
    assert statuses.holds_text(plain) and statuses.holds_text(inline)
    assert statuses.holds_text(_status()) is False


def test_the_shipped_defaults_agree_in_the_three_places_that_hold_them():
    import json

    shipped = json.loads((ROOT / "client" / "data" / "settings_default.json").read_text(
        encoding="utf-8"))
    for source in (DEFAULTS, DEFAULT_SETTINGS["transcription"], shipped["transcription"]):
        assert source["auto_transcribe_statuses"] is False
        assert source["status_cache_enabled"] is True
    for source in (DEFAULT_SETTINGS["user_interface"], shipped["user_interface"]):
        assert source["enter_transcribes_statuses"] is False


def test_the_runs_download_folder_is_swept_at_startup():
    """A killed run leaves its (encrypted) download in %TEMP%; a prefix that is
    not listed is a folder nobody ever collects."""
    assert statuses.TEMP_PREFIX in temp_sweep.PREFIXES


# ── the window: keeping, deleting, clearing ──────────────────────────────────


class _Window:
    for _name in ("_status_panel_get", "_cached_status_records", "_shown_status_copies",
                  "_persist_status_records", "store_status_transcription",
                  "delete_status_transcription", "clear_status_transcriptions",
                  "_fetch_status_media", "_maybe_auto_transcribe_status",
                  "_store_status_auto_transcription"):
        locals()[_name] = getattr(StatusTranscriptionMixin, _name)
    del _name

    def __init__(self, settings=None, cached=(), shown=()):
        self.settings = settings if settings is not None else {}
        self._status_updates = {}
        for record in cached:
            self._status_updates.setdefault(statuses.participant_of(record), []).append(record)
        self.db = SimpleNamespace(upsert_status_update=Mock())
        self.key = Fernet.generate_key()
        self.panel = SimpleNamespace(
            _status_contacts=[{"name": "Ana", "statuses": list(shown)}],
            _my_statuses=[], _current_status=None, _repaint_status_row=Mock())
        self.navigation_panel = SimpleNamespace(status_panel=self.panel)
        self.queue = SimpleNamespace(submit=Mock(return_value="accepted"))

    def _auto_transcription_queue_get(self):
        return self.queue


@pytest.fixture(autouse=True)
def _inline_threads(monkeypatch):
    """The write happens on a thread and callbacks through wx.CallAfter: both
    run in place, so a test reads the outcome on the next line."""

    class _Now:
        def __init__(self, target=None, daemon=None, name=None):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(window_module.threading, "Thread", _Now)
    monkeypatch.setattr(window_module.wx, "CallAfter", lambda fn, *args: fn(*args))


def test_storing_writes_every_copy_and_the_database_row():
    cached, shown = _status("s1"), _status("s1")
    window = _Window(cached=[cached], shown=[shown])
    asked = _status("s1")
    value = _value()

    assert window.store_status_transcription(asked, value) == stored.SAVE_STORED

    assert cached[statuses.KEY] is value and shown[statuses.KEY] is value
    assert asked[statuses.KEY] is value
    window.db.upsert_status_update.assert_called_once_with(POSTER, cached)


def test_a_status_shown_from_the_api_and_never_cached_becomes_a_cached_one():
    """It has no row yet; kept under its poster, it is pruned when it expires
    like every other status, and the text goes with it."""
    window = _Window()
    status = _status("s9")

    assert window.store_status_transcription(status, _value()) == stored.SAVE_STORED

    assert window._status_updates[POSTER] == [status]
    window.db.upsert_status_update.assert_called_once_with(POSTER, status)


def test_a_status_nobody_can_be_filed_under_is_not_kept():
    window = _Window()
    orphan = _status("s9", participant="")

    assert window.store_status_transcription(orphan, _value()) == stored.SAVE_MISSING
    assert window.store_status_transcription({}, _value()) == stored.SAVE_MISSING
    window.db.upsert_status_update.assert_not_called()


def test_the_statuses_cache_switch_stops_new_saves_only():
    off = {"transcription": {"status_cache_enabled": False}}
    status = _status("s1")
    window = _Window(settings=off, cached=[status])

    assert window.store_status_transcription(status, _value()) == stored.SAVE_DISABLED
    assert statuses.KEY not in status
    # A text already kept that is only being marked for the line goes through.
    assert window.store_status_transcription(
        status, _value(inline=True), existing=True) == stored.SAVE_STORED


def test_deleting_leaves_a_dated_tombstone_on_every_copy():
    cached, shown = _status("s1"), _status("s1")
    cached[statuses.KEY] = shown[statuses.KEY] = _value(at=100.0)
    window = _Window(cached=[cached], shown=[shown])
    done = Mock()

    window.delete_status_transcription(shown, done)

    done.assert_called_once_with(True)
    for copy in (cached, shown):
        assert copy[statuses.KEY].get("deleted") is True
        assert copy[statuses.KEY]["at"] > 100.0
    window.db.upsert_status_update.assert_called_once_with(POSTER, cached)


def test_deleting_what_is_not_there_answers_false_and_writes_nothing():
    window = _Window(cached=[_status("s1")])
    done = Mock()

    window.delete_status_transcription(_status("s1"), done)

    done.assert_called_once_with(False)
    window.db.upsert_status_update.assert_not_called()


def test_clearing_removes_every_saved_text_and_counts_the_statuses():
    first, second, bare = _status("s1"), _status("s2"), _status("s3")
    first[statuses.KEY] = _value("one")
    second[statuses.KEY] = _value("two", inline=True)
    shown = _status("s2")
    shown[statuses.KEY] = second[statuses.KEY]
    window = _Window(cached=[first, second, bare], shown=[shown])
    done = Mock()

    window.clear_status_transcriptions(done)

    done.assert_called_once_with(2)
    assert not statuses.holds_text(first) and not statuses.holds_text(second)
    assert not statuses.holds_text(shown)
    assert statuses.KEY not in bare
    assert window.db.upsert_status_update.call_count == 2
    assert {call.args[0] for call in window.panel._repaint_status_row.call_args_list} \
        == {"s1", "s2"}


def test_clearing_with_nothing_saved_says_so():
    window = _Window(cached=[_status("s1")])
    done = Mock()
    window.clear_status_transcriptions(done)
    done.assert_called_once_with(0)


def test_the_media_of_a_run_is_downloaded_encrypted(monkeypatch, tmp_path):
    """The run decrypts every local media file with the window's key, so the
    download is written the same way; the status is never on disk otherwise."""
    window = _Window()
    monkeypatch.setattr(window_module, "_download_status_media", lambda mw, status: b"OggS-audio")
    target = tmp_path / "run" / "s1.msv"

    assert window._fetch_status_media(_status(), str(target)) is True
    assert target.read_bytes() != b"OggS-audio"
    assert decrypt_bytes(target.read_bytes(), window.key) == b"OggS-audio"


def test_a_download_that_fails_answers_false_and_raises_nothing(monkeypatch, tmp_path):
    window = _Window()

    def boom(mw, status):
        raise ValueError("expired")

    monkeypatch.setattr(window_module, "_download_status_media", boom)
    assert window._fetch_status_media(_status(), str(tmp_path / "s1.msv")) is False


# ── the window: statuses transcribed as they arrive ──────────────────────────

AUTO = {"transcription": {"auto_transcribe_statuses": True}}


def test_nothing_is_queued_while_the_setting_is_off():
    window = _Window()
    window._maybe_auto_transcribe_status(_status())
    window.queue.submit.assert_not_called()


def test_a_fresh_audio_status_is_queued_once_under_its_own_key():
    window = _Window(settings=AUTO)
    status = _status("s1")

    window._maybe_auto_transcribe_status(status)

    window.queue.submit.assert_called_once_with(
        (statuses.STATUS_JID, "s1"), (statuses.STATUS_JID, "s1", status))


@pytest.mark.parametrize("status", [
    _status(from_me=True), _status(message_type="imageMessage"),
    _status(timestamp=time.time() - 3600)])
def test_own_old_and_silent_statuses_are_never_queued(status):
    window = _Window(settings=AUTO)
    window._maybe_auto_transcribe_status(status)
    window.queue.submit.assert_not_called()


def test_a_model_chosen_explicitly_whose_cache_is_off_is_not_run_for_nothing():
    settings = {"transcription": {"auto_transcribe_statuses": True, "model": "tiny",
                                  "cache_by_model": {"tiny": False}}}
    window = _Window(settings=settings)
    window._maybe_auto_transcribe_status(_status())
    window.queue.submit.assert_not_called()


def test_the_automatic_text_goes_into_the_line_and_the_row_is_redrawn():
    status = _status("s1")
    window = _Window(settings=AUTO, cached=[status])
    result = TranscriptionResult(text="good morning", language="en",
                                 language_probability=0.9, duration_seconds=3.0,
                                 backend="faster_whisper", model_id="small")

    window._store_status_auto_transcription(status, result)

    assert statuses.inline_text(status, AUTO) == "good morning"
    window.panel._repaint_status_row.assert_called_once_with("s1")


def test_a_decision_the_user_made_meanwhile_stands():
    status = _status("s1")
    status[statuses.KEY] = stored.tombstone(time.time())
    window = _Window(settings=AUTO, cached=[status])
    result = TranscriptionResult(text="late", language="en", language_probability=0.9,
                                 duration_seconds=3.0)

    window._store_status_auto_transcription(status, result)

    assert status[statuses.KEY].get("deleted") is True
    window.panel._repaint_status_row.assert_not_called()


def test_the_queue_is_shared_and_a_status_goes_to_its_own_runner():
    """Structural: one transcription at a time, whatever it is of; and a
    status arriving is what queues it."""
    auto = (ROOT / "client" / "main_window" / "auto_transcription.py").read_text(encoding="utf-8")
    assert "if remote_jid == statuses.STATUS_JID:" in auto
    assert "self._run_status_auto_transcription(msg)" in auto
    store = (ROOT / "client" / "main_window" / "chats_store.py").read_text(encoding="utf-8")
    assert "self._maybe_auto_transcribe_status(msg)" in store
    loading = (ROOT / "client" / "status_tab" / "status_loading.py").read_text(encoding="utf-8")
    assert "transcription_statuses.carry_over(shown, records)" in loading


# ── the flow ─────────────────────────────────────────────────────────────────


def _flow_panel(settings=None):
    window = SimpleNamespace(
        settings=settings if settings is not None else {},
        i18n=SimpleNamespace(t=lambda key: key, language="en"),
        store_status_transcription=Mock(return_value=stored.SAVE_STORED),
        delete_status_transcription=Mock(),
        store_message_transcription=Mock(return_value=stored.SAVE_STORED),
        delete_message_transcription=Mock(),
        is_chat_hidden_by_vault=Mock(return_value=True),
        output=Mock(),
    )
    return SimpleNamespace(main_window=window, conversation={"remoteJid": "chat@c.us"},
                           _status_poster_name=Mock(return_value="Ana"),
                           _repaint_status_row=Mock(), _focus_status_list=Mock())


def test_a_status_is_kept_with_the_status_not_with_a_chat():
    panel, status, value = _flow_panel(), _status("s1"), _value()
    flow = StatusTranscriptionFlow(panel, status)

    assert flow._save(value) == stored.SAVE_STORED
    flow._save(value, existing=True)
    flow._delete("callback")

    window = panel.main_window
    assert window.store_status_transcription.call_args_list[0].args == (status, value)
    assert window.store_status_transcription.call_args_list[0].kwargs == {"existing": False}
    assert window.store_status_transcription.call_args_list[1].kwargs == {"existing": True}
    window.delete_status_transcription.assert_called_once_with(status, "callback")
    window.store_message_transcription.assert_not_called()
    window.delete_message_transcription.assert_not_called()


def test_no_vault_no_withdrawal_and_no_insert_button_for_a_status():
    """The vault hides chats, and this window answers "hidden" for any: a
    status belongs to none, so it must not be asked."""
    panel = _flow_panel()
    flow = StatusTranscriptionFlow(panel, _status("s1"))

    assert flow._hidden_by_vault() is False
    assert flow._withdrawn_now() is False
    assert flow._offers_insert is False
    panel.main_window.is_chat_hidden_by_vault.assert_not_called()


def test_the_window_title_row_and_focus_are_the_status_tabs():
    panel = _flow_panel()
    flow = StatusTranscriptionFlow(panel, _status("s1"))

    assert flow._title_name() == "Ana"
    flow._repaint_row()
    flow._focus_message()
    flow._insert_into_message_field("text")

    panel._repaint_status_row.assert_called_once_with("s1")
    assert panel._focus_status_list.call_count == 2


def test_the_conversations_flow_stores_exactly_as_it_did():
    """The hooks were added for the status flow; a message's calls are
    unchanged, down to the arguments."""
    panel, value = _flow_panel(), _value()
    flow = MessageTranscriptionFlow(panel, {"key": {"id": "m1"}, "messageType": "audioMessage"})
    window = panel.main_window

    flow._save(value)
    assert window.store_message_transcription.call_args == (("chat@c.us", "m1", value),)
    flow._save(value, existing=True)
    assert window.store_message_transcription.call_args.kwargs == {"existing": True}
    flow._delete("callback")
    window.delete_message_transcription.assert_called_once_with("chat@c.us", "m1", "callback")
    assert flow._offers_insert is True


@pytest.mark.parametrize("entry,saved,expected", [
    (flow_module.open_or_transcribe_status, False, "start"),
    (flow_module.open_or_transcribe_status, True, "show_saved"),
    (flow_module.transcribe_status_inline, False, "start"),
    (flow_module.transcribe_status_inline, True, "inline_saved"),
])
def test_a_saved_transcription_is_used_instead_of_a_second_run(monkeypatch, entry, saved,
                                                               expected):
    called = []
    for name in ("start", "show_saved", "inline_saved"):
        monkeypatch.setattr(StatusTranscriptionFlow, name,
                            lambda self, _name=name: called.append(_name))
    status = _status("s1")
    if saved:
        status[statuses.KEY] = _value()

    entry(_flow_panel(), status)

    assert called == [expected]


# ── the panel: keys, row and focus ───────────────────────────────────────────


class _List:
    def __init__(self, texts, focused=1):
        self.texts, self.focused, self.writes = list(texts), focused, []
        self.got_focus = 0

    def GetFocusedItem(self):
        return self.focused

    def GetItemText(self, row):
        return self.texts[row]

    def SetItemText(self, row, text):
        self.texts[row] = text
        self.writes.append((row, text))

    def IsShown(self):
        return True

    def SetFocus(self):
        self.got_focus += 1


class _Panel:
    for _name in ("_status_shown_by_row", "_status_for_transcription", "_status_or_say_none",
                  "_on_transcribe_status", "_on_transcribe_status_inline_key",
                  "_enter_transcribes_status_row", "_status_inline_suffix",
                  "_status_contact_index_of", "_status_poster_name", "_repaint_status_row",
                  "_focus_status_list", "_status_row_text", "_status_preview"):
        locals()[_name] = getattr(StatusPanel, _name)
    del _name

    def __init__(self, shown, settings=None, focused=1, viewer_open=False):
        self.main_window = SimpleNamespace(
            settings=settings if settings is not None else {},
            i18n=SimpleNamespace(t=lambda key: key), output=Mock())
        self._status_contacts = [{"name": "Ana", "statuses": list(shown)}]
        self._status_row_contact = {1: 0}
        self._status_contact_row = {0: 1}
        self._selected_contact_idx = -1
        self._current_status_idx = 0
        self._current_status = None
        self._viewer_panel = SimpleNamespace(IsShown=lambda: viewer_open)
        self._status_list = _List(["my status", "Ana: old text"], focused)
        self.opened_rows = []

    def _open_status_row(self, row):
        self.opened_rows.append(row)

    def _update_focused_status_row_text(self):
        self._status_list.writes.append("focused-row")


@pytest.fixture
def flows(monkeypatch):
    calls = []
    monkeypatch.setattr(panel_module.status_transcription_flow, "open_or_transcribe_status",
                        lambda panel, status: calls.append(("window", status)))
    monkeypatch.setattr(panel_module.status_transcription_flow, "transcribe_status_inline",
                        lambda panel, status: calls.append(("inline", status)))
    return calls


ENTER = {"user_interface": {"enter_transcribes_statuses": True}}


def test_the_keys_act_on_the_status_the_focused_row_is_showing(flows):
    newest, older = _status("s1"), _status("s2")
    panel = _Panel([newest, older])

    panel._on_transcribe_status()
    assert flows == [("window", newest)]

    # Opened in the viewer and navigated to its second status (Ctrl+Right).
    panel._selected_contact_idx, panel._current_status_idx = 0, 1
    panel._on_transcribe_status()
    assert flows[-1] == ("window", older)


def test_the_status_open_in_the_inline_viewer_comes_first(flows):
    in_viewer = _status("s5")
    panel = _Panel([_status("s1")], viewer_open=True)
    panel._current_status = in_viewer

    panel._on_transcribe_status()

    assert flows == [("window", in_viewer)]


@pytest.mark.parametrize("focused,shown", [(1, [_status(message_type="imageMessage")]),
                                           (0, [_status()]), (-1, [_status()]), (1, [])])
def test_a_row_with_no_audio_or_video_gets_one_short_sentence(flows, focused, shown):
    """A photo, the "my status" row, nothing focused, a contact with nothing."""
    panel = _Panel(shown, focused=focused)

    panel._on_transcribe_status()
    panel._on_transcribe_status_inline_key()

    assert flows == []
    assert [call.args[0] for call in panel.main_window.output.call_args_list] \
        == ["transcription_no_audio"] * 2


def test_alt_shift_n_transcribes_into_the_line_while_enter_opens(flows):
    status = _status("s1")
    panel = _Panel([status])

    panel._on_transcribe_status_inline_key()
    assert flows == [("inline", status)]
    assert panel._enter_transcribes_status_row(1) is False, "Enter still opens the status"
    assert panel.opened_rows == []


def test_with_the_setting_on_the_two_swap(flows):
    """Enter and Space transcribe into the line; Alt+Shift+N opens or plays.
    Each of the two actions always has a key."""
    status = _status("s1")
    panel = _Panel([status], settings=ENTER)

    assert panel._enter_transcribes_status_row(1) is True
    assert flows == [("inline", status)]

    panel._on_transcribe_status_inline_key()
    assert panel.opened_rows == [1]
    assert flows == [("inline", status)], "the key did not transcribe a second time"


def test_enter_still_opens_what_cannot_be_transcribed(flows):
    """A photo or the "my status" row: the setting is about audio and video."""
    panel = _Panel([_status(message_type="imageMessage")], settings=ENTER)
    assert panel._enter_transcribes_status_row(1) is False
    assert panel._enter_transcribes_status_row(0) is False
    assert flows == []


def test_the_rows_text_carries_an_inline_transcription_and_only_that():
    plain, inline = _status("s1"), _status("s2")
    plain[statuses.KEY] = _value("only in the window")
    inline[statuses.KEY] = _value("good morning", inline=True)
    panel = _Panel([inline, plain])
    i18n = panel.main_window.i18n

    assert panel._status_inline_suffix(plain, i18n) == ""
    assert panel._status_inline_suffix(inline, i18n) == ", transcription_row_label: good morning"
    assert panel._status_preview(inline, i18n).endswith(", transcription_row_label: good morning")
    assert "transcription_row_label" not in panel._status_preview(plain, i18n)


def test_repainting_rewrites_only_a_row_whose_text_changed():
    status = _status("s1")
    panel = _Panel([status])

    panel._repaint_status_row("unknown-id")
    assert panel._status_list.writes == []

    status[statuses.KEY] = _value("good morning", inline=True)
    panel._repaint_status_row("s1")
    assert len(panel._status_list.writes) == 1 and panel._status_list.writes[0][0] == 1
    assert "good morning" in panel._status_list.texts[1]

    panel._repaint_status_row("s1")
    assert len(panel._status_list.writes) == 1, "identical text is not written again"

    # The contact open in the viewer keeps its "status X of Y" suffix.
    panel._selected_contact_idx = 0
    panel._repaint_status_row("s1")
    assert panel._status_list.writes[-1] == "focused-row"


def test_the_result_window_is_named_after_the_poster_and_focus_returns_to_the_list():
    panel = _Panel([_status("s1")])
    assert panel._status_poster_name(_status("s1")) == "Ana"
    assert panel._status_poster_name(_status("nope")) == ""
    panel._focus_status_list()
    assert panel._status_list.got_focus == 1


def test_enter_and_space_reach_the_setting_before_opening():
    """Structural: both handlers ask before they open; Space's old body is
    _open_status_row(), which Alt+Shift+N reuses."""
    import inspect

    activated = inspect.getsource(StatusPanel._on_status_contact_activated)
    assert activated.index("self._enter_transcribes_status_row(idx)") \
        < activated.index("self._open_my_status_dialog()")
    key_down = inspect.getsource(StatusPanel._on_status_list_key_down)
    assert key_down.index("self._enter_transcribes_status_row(idx)") \
        < key_down.index("self._open_status_row(idx)")


def test_the_two_keys_are_catalogued_and_bound_on_the_status_tab():
    """Customisable like every shortcut: an accelerator the catalogue does not
    know raises when the table is built."""
    rows = {s.id: s for s in shortcut_catalog.SHORTCUTS if s.scope == "status"}
    messages = {s.id: s for s in shortcut_catalog.SHORTCUTS if s.scope == "messages"}
    for letter in ("T", "N"):
        status = rows[f"status.ID_ALT_SHIFT_{letter}"]
        message = messages[f"messages.ID_ALT_SHIFT_{letter}"]
        assert (status.mod, status.key, status.slot) == (message.mod, message.key, message.slot),             "the same chord as the conversation"
    panel = (ROOT / "client" / "status_panel.py").read_text(encoding="utf-8")
    assert 'ord("T"),      self.ID_ALT_SHIFT_T' in panel
    assert 'ord("N"),      self.ID_ALT_SHIFT_N' in panel
    assert "self._on_transcribe_status,    id=self.ID_ALT_SHIFT_T" in panel
    assert "self._on_transcribe_status_inline_key, id=self.ID_ALT_SHIFT_N" in panel
