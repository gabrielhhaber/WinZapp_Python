"""TranslationActionsMixin on a plain stub: queue, pauses, locked chats, rows.

No wx window is built: the mixin's wx calls go to a fake module and the AI
worker is replaced by a list of submitted requests.
"""
import time
from types import SimpleNamespace

import pytest

from coord_locks import LockTimeout
from core.ai_media.errors import DescriptionError
from core.message_translation import TranslationSettings, credential_id, message_identity
from ui.conversation_panel import translation_actions as actions
from ui.conversation_panel import translation_backlog as backlog
from ui.conversation_panel import translation_to_end as to_end

JID = "5511999990000@s.whatsapp.net"
LID = "777@lid"
AUTOMATIC = {"provider": "gemini", "language": "Arabic", "automatic": True, "scope": "all",
             "all_accounts_confirmed": True, "daily_limit": 0}
TEXTS = {
    "translation_result": "Translation: {text}",
    "translation_auto_limit_reached": "limit {limit}",
    "translation_sending": "sending {provider}",
    "translation_locked_confirm": "locked {provider}",
}


class FakeWx:
    YES, NO, YES_NO, NO_DEFAULT, ID_CANCEL = 1, 2, 4, 8, 16

    def __init__(self):
        self.answer = self.YES
        self.asked = []
        self.later = []

    def CallAfter(self, function, *args):
        function(*args)

    def CallLater(self, delay, function):
        self.later.append((delay, function))
        return object()

    def MessageBox(self, prompt, *args):
        self.asked.append(prompt)
        return self.answer


class App:
    def __init__(self, raw):
        self.raw = dict(raw)
        self.busy = False

    def get_snapshot(self, key):
        assert key == "message_translation"
        if self.busy:
            raise LockTimeout()
        return self.raw

    def get(self, key):
        return self.raw

    def update(self, key, updater):
        self.raw = updater(self.raw)
        return self.raw


class List:
    def __init__(self):
        self.frozen = 0

    def Freeze(self):
        self.frozen += 1

    def Thaw(self):
        self.frozen -= 1

    def GetFocusedItem(self):
        return 0


class Panel(actions.TranslationActionsMixin, backlog.TranslationBacklogMixin, to_end.TranslationToEndMixin):
    def __init__(self, raw=AUTOMATIC):
        self.spoken = []
        self.locked = set()
        self.main_window = SimpleNamespace(
            _app_settings=App(raw), i18n=SimpleNamespace(t=lambda key: TEXTS.get(key, key)),
            output=lambda text, **kwargs: self.spoken.append(text), is_chat_locked=lambda jid: jid in self.locked,
            _chat_lock_unlocked=False, _normalize_jid=lambda jid: jid,
            _lid_to_phone={LID: JID}, _phone_to_lid={JID: LID}, settings={},
            db=SimpleNamespace(get_metadata_json=lambda key, default: {},
                               set_metadata_json=lambda key, value: None))
        self.conversation = {"remoteJid": JID}
        self.messages_list = List()
        self._sorted_messages = []
        self.refreshed = []

    def _refresh_message_rows_by_ids(self, ids):
        self.refreshed.append(set(ids))

    def _update_read_more_button(self, index):
        pass

    @staticmethod
    def _is_separator(msg):
        return bool(msg.get("separator"))

    def _resolve_mentions_in_text(self, text, mentioned):
        return text.replace("@5511888", "@Ana") if mentioned else text

    @staticmethod
    def _message_mentioned_jids(msg):
        ext = (msg.get("message") or {}).get("extendedTextMessage") or {}
        return (ext.get("contextInfo") or {}).get("mentionedJid") or []


def message(number, text=None, jid=JID):
    return {"key": {"remoteJid": jid, "id": f"id{number}"}, "messageType": "conversation",
            "message": {"conversation": text or f"hello {number}"},
            "messageTimestamp": int(time.time())}


@pytest.fixture
def wx(monkeypatch):
    fake = FakeWx()
    monkeypatch.setattr(actions, "wx", fake)
    monkeypatch.setattr(backlog, "wx", fake)
    return fake


@pytest.fixture
def submitted(monkeypatch):
    sent = []
    monkeypatch.setattr(actions, "submit", lambda work, complete: sent.append(complete))
    return sent


def test_a_busy_settings_lock_keeps_the_last_settings_instead_of_none():
    panel = Panel()
    first = panel._translation_settings()
    assert first.provider == "gemini"
    panel.main_window._app_settings.busy = True
    assert panel._translation_settings() == first
    fresh = Panel()
    fresh.main_window._app_settings.busy = True
    assert fresh._translation_settings().provider == "gemini"  # waited once instead


def test_the_queue_runs_one_request_and_skips_stale_entries_in_one_go(wx, submitted):
    panel = Panel()
    first, stale, third = message(1), message(2), message(3)
    for row in (first, stale, third):
        panel._maybe_auto_translate(JID, row)
    assert len(submitted) == 1
    stale["message"]["conversation"] = "edited before its turn"
    submitted[0]("translated 1", None)
    assert len(submitted) == 2
    assert panel._translation_running[1] == message_identity(third)
    panel._maybe_auto_translate(JID, message(4))
    assert len(submitted) == 2  # never a second request beside the queue's own


def test_the_same_message_is_queued_once(wx, submitted):
    panel = Panel()
    row = message(1)
    panel._maybe_auto_translate(JID, message(9))
    panel._maybe_auto_translate(JID, row)
    panel._maybe_auto_translate(JID, row)
    assert len(panel._translation_queue) == 1


@pytest.mark.parametrize("error", ["ai_error_authentication", "ai_error_quota",
                                   "ai_error_credits", "ai_error_credentials"])
def test_a_repeating_refusal_pauses_automatic_translation_and_says_so(wx, submitted, error):
    panel = Panel()
    panel._maybe_auto_translate(JID, message(1))
    panel._maybe_auto_translate(JID, message(2))
    submitted[0](None, error)
    assert panel.spoken == ["translation_auto_paused"]
    assert not panel._translation_queue
    panel._maybe_auto_translate(JID, message(3))
    assert len(submitted) == 1
    panel.resume_automatic_translation()
    panel._maybe_auto_translate(JID, message(4))
    assert len(submitted) == 2


def test_a_one_off_failure_neither_pauses_nor_speaks(wx, submitted):
    panel = Panel()
    panel._maybe_auto_translate(JID, message(1))
    submitted[0](None, "ai_error_network")
    panel._maybe_auto_translate(JID, message(2))
    assert len(submitted) == 2 and panel.spoken == []


def test_the_daily_limit_is_said_once_a_day(wx, submitted):
    panel = Panel(dict(AUTOMATIC, daily_limit=3))
    panel._maybe_auto_translate(JID, message(1))
    submitted[0](None, "ai_error_limit")
    panel._maybe_auto_translate(JID, message(2))
    submitted[1](None, "ai_error_limit")
    assert panel.spoken == ["limit 3"]


def test_a_busy_worker_puts_the_message_back_first_and_retries_later(wx, monkeypatch):
    def busy(work, complete):
        raise DescriptionError("busy")

    monkeypatch.setattr(actions, "submit", busy)
    panel = Panel()
    row = message(1)
    panel._maybe_auto_translate(JID, row)
    assert panel._translation_queue[0][1] == message_identity(row)
    assert panel._translation_running is None
    assert wx.later and wx.later[0][1] == panel._drain_translation_queue


def test_a_translation_is_shown_in_the_row_with_mentions_as_names(wx, monkeypatch):
    monkeypatch.setattr(actions, "link_preview_text", lambda ext, text, mw: f"{ext['title']}. {text}")
    panel = Panel(dict(AUTOMATIC, display="replace"))
    row = {"key": {"remoteJid": JID, "id": "id1"}, "messageType": "extendedTextMessage",
           "message": {"extendedTextMessage": {
               "text": "hi @5511888 https://example.test", "title": "Example",
               "contextInfo": {"mentionedJid": ["5511888@s.whatsapp.net"]}}}}
    config = panel._translation_settings()
    panel._present_translation(JID, message_identity(row), config, "hi @5511888 https://example.test",
                               "مرحبا @5511888 https://example.test", message=row, manual=True)
    assert panel.refreshed == [{"id1"}] and panel.messages_list.frozen == 0
    assert panel.spoken == ["Translation: مرحبا @Ana https://example.test"]
    assert panel._translated_row_body(row, "Example. hi @Ana https://example.test") == (
        "Example. مرحبا @Ana https://example.test")


def test_the_same_text_back_is_said_and_not_shown(wx):
    panel = Panel()
    row = message(1, "hello")
    panel._present_translation(JID, message_identity(row), panel._translation_settings(), "hello",
                               "hello", message=row, manual=True)
    assert panel.refreshed == [] and panel.spoken == ["translation_same_language"]


def test_a_locked_chat_needs_the_open_vault_and_fresh_consent_each_time(wx, submitted):
    panel = Panel(dict(AUTOMATIC, automatic=False))
    row = message(1)
    panel._sorted_messages = [row]
    panel.locked.add(JID)
    panel._on_translate_message(row)
    assert submitted == [] and wx.asked == []  # vault closed: nothing at all
    panel.main_window._chat_lock_unlocked = True
    wx.answer = wx.NO
    panel._on_translate_message(row)
    assert submitted == [] and wx.asked == ["locked Google Gemini"]
    wx.answer = wx.YES
    panel._on_translate_message(row)
    assert len(submitted) == 1 and panel.spoken == ["sending Google Gemini"]
    submitted[0]("مرحبا", None)
    assert panel.refreshed == [{"id1"}]


@pytest.mark.parametrize("meanwhile", ["vault closed", "chat closed"])
def test_a_yes_given_after_the_locked_chat_closed_sends_nothing(wx, submitted, meanwhile):
    panel = Panel(dict(AUTOMATIC, automatic=False))
    row = message(1)
    panel._sorted_messages = [row]
    panel.locked.add(JID)
    panel.main_window._chat_lock_unlocked = True

    def answer(prompt, *args):
        # The vault's timer fires inside the box's own modal loop.
        if meanwhile == "vault closed":
            panel.main_window._chat_lock_unlocked = False
        else:
            panel.conversation = None
        return wx.YES

    wx.MessageBox = answer
    panel._on_translate_message(row)
    assert submitted == [] and panel.spoken == []
    assert getattr(panel, "_manual_translation", None) is None


def test_a_manual_answer_for_a_chat_left_meanwhile_is_dropped(wx, submitted):
    panel = Panel(dict(AUTOMATIC, automatic=False))
    row = message(1)
    panel._sorted_messages = [row]
    panel._on_translate_message(row)
    panel.conversation = {"remoteJid": "other@s.whatsapp.net"}
    submitted[0]("مرحبا", None)
    assert panel.refreshed == [] and panel._manual_translation is None


def test_closing_the_vault_drops_locked_translations_and_queued_messages(wx, submitted):
    panel = Panel()
    other = "5511777770000@s.whatsapp.net"
    locked_row, open_row = message(1), message(2, jid=other)
    config = panel._translation_settings()
    for row, jid in ((locked_row, JID), (open_row, other)):
        panel._present_translation(jid, message_identity(row), config, "x", "y", message=row)
    panel._translation_queue = actions.deque([(JID, ("k", "q"), "t", config, locked_row)])
    panel.locked.add(JID)
    panel.drop_locked_translations()
    assert list(panel._translation_results) == [message_identity(open_row)]
    assert not panel._translation_queue


def test_turning_a_chat_on_saves_it_under_the_phone_address(wx):
    saved = {}
    panel = Panel(dict(AUTOMATIC, scope="selected", all_accounts_confirmed=False))
    panel.main_window.db.set_metadata_json = lambda key, value: saved.update(value)
    panel._translation_opt_in = {LID: {"provider": "gemini", "enabled": False}}
    config = panel._translation_settings()
    panel._toggle_translation_chat(LID, config)
    assert saved == {JID: {"provider": credential_id(config), "enabled": True}}
    assert panel.spoken == ["translation_auto_chat_enabled"]


def test_the_shortcut_leaves_rows_without_text_to_their_old_action(monkeypatch):
    from ui.conversation_panel import message_list

    monkeypatch.setattr(message_list, "matches", lambda owner, event, shortcut: True)
    calls = []
    panel = SimpleNamespace(messages_list=SimpleNamespace(GetFocusedItem=lambda: 0),
                            _on_translate_message=calls.append)
    panel._sorted_messages = [{"messageType": "audioMessage", "message": {"audioMessage": {}}}]
    assert message_list.handle_translation_shortcut(panel, object()) is False
    panel._sorted_messages = [message(1)]
    assert message_list.handle_translation_shortcut(panel, object()) is True
    assert calls == panel._sorted_messages


def test_the_menu_item_shows_the_chord_the_user_bound():
    from core.keyboard_shortcuts import SECTION
    from ui.shortcut_bindings import popup_item_label

    window = SimpleNamespace(settings={}, i18n=SimpleNamespace(t=lambda key: key))
    owner = SimpleNamespace(main_window=window)
    assert popup_item_label(owner, "Translate", "message_list.translate") == "Translate\tShift+Enter"
    window.settings[SECTION] = {"message_list.translate": [6, 84]}
    assert popup_item_label(owner, "Translate", "message_list.translate") == "Translate\tCtrl+Shift+T"
    window.settings[SECTION] = {"message_list.translate": None}
    assert popup_item_label(owner, "Translate", "message_list.translate") == "Translate"


def test_manual_translation_without_settings_says_what_is_missing(wx, submitted):
    panel = Panel({})
    panel._on_translate_message(message(1))
    assert submitted == [] and panel.spoken == ["translation_not_configured"]
    assert TranslationSettings().provider == ""


def old_message(number, text=None, age=3600):
    row = message(number, text)
    row["messageTimestamp"] = int(time.time()) - age
    return row


def test_turning_a_chat_on_translates_its_latest_old_messages_too(wx, submitted):
    panel = Panel(dict(AUTOMATIC, scope="selected", all_accounts_confirmed=False))
    panel._translation_opt_in = {}
    rows = [old_message(number) for number in range(1, 4)]
    panel._sorted_messages = [{"separator": True}] + rows
    config = panel._translation_settings()
    assert not panel._translation_candidate(rows[0], config)
    panel._toggle_translation_chat(JID, config)
    assert panel._translation_running[2] == "hello 1"
    assert [entry[2] for entry in panel._translation_queue] == ["hello 2", "hello 3"]
    assert panel._translation_candidate(rows[0], config)


def test_the_backlog_leaves_other_chats_and_own_messages_alone(wx, submitted):
    panel = Panel(dict(AUTOMATIC, scope="selected", all_accounts_confirmed=False))
    panel._translation_opt_in = {JID: {"provider": "gemini", "enabled": True}}
    mine = old_message(1)
    mine["key"]["fromMe"] = True
    panel._sorted_messages = [mine]
    panel._translate_chat_backlog(JID)
    panel._translate_chat_backlog("5511000@s.whatsapp.net")
    assert not submitted and not hasattr(panel, "_translation_running")


def test_selected_texts_keep_the_order_and_skip_what_has_no_text():
    rows = [message(1), {"separator": True, "key": {"id": "id2"}}, message(3), message(4)]
    audio = {"key": {"remoteJid": JID, "id": "id5"}, "messageType": "audioMessage",
             "message": {"audioMessage": {}}}
    found = backlog.selected_texts(rows + [audio], {"id1", "id2", "id3", "id5"},
                                   lambda row: bool(row.get("separator")))
    assert [row["key"]["id"] for row in found] == ["id1", "id3"]
    assert len(backlog.selected_texts([message(n) for n in range(80)], {f"id{n}" for n in range(80)})) == 50


def test_the_bulk_translation_asks_then_translates_each_selected_message(wx, submitted):
    panel = Panel(dict(AUTOMATIC, scope="selected"))
    panel._sorted_messages = [old_message(1), old_message(2)]
    panel.selected_messages = {"id1", "id2"}
    panel._on_mass_translate_messages()
    assert wx.asked and panel.spoken[-1] == "sending Google Gemini"
    assert len(submitted) == 1
    submitted[0]("one", None)
    assert len(submitted) == 2
    submitted[1]("two", None)
    assert panel.spoken[-1] == "translation_bulk_done"
    assert panel._bulk_translation is None
    assert [value[2] for value in panel._translation_results.values()] == ["one", "two"]


def test_the_bulk_translation_stops_on_the_first_error_and_says_why(wx, submitted):
    panel = Panel(dict(AUTOMATIC, scope="selected"))
    panel._sorted_messages = [old_message(1), old_message(2)]
    panel.selected_messages = {"id1", "id2"}
    panel._on_mass_translate_messages()
    assert len(submitted) == 1
    submitted[0](None, "ai_error_credentials")
    assert panel.spoken[-1] == "ai_error_credentials"
    assert panel._bulk_translation is None and len(submitted) == 1


def test_the_bulk_translation_refuses_an_empty_selection_and_a_second_run(wx, submitted):
    panel = Panel(dict(AUTOMATIC, scope="selected"))
    panel._sorted_messages = [message(1)]
    panel.selected_messages = {"nothing"}
    panel._on_mass_translate_messages()
    assert panel.spoken[-1] == "translation_bulk_none" and not wx.asked
    panel._bulk_translation = object()
    panel._on_mass_translate_messages()
    assert panel.spoken[-1] == "ai_error_busy"


def test_the_bulk_translation_shows_one_window_in_the_separate_display(wx, submitted):
    panel = Panel(dict(AUTOMATIC, scope="selected", display="separate"))
    panel._sorted_messages = [old_message(1), old_message(2)]
    panel.selected_messages = {"id1", "id2"}
    shown = []
    panel._show_translation = lambda answer, **kwargs: shown.append(answer)
    panel._on_mass_translate_messages()
    submitted[0]("one", None)
    submitted[1]("two", None)
    assert shown == ["one\n\ntwo"]


def test_from_here_takes_received_text_messages_from_the_focused_one_on():
    own = message(3)
    own["key"]["fromMe"] = True
    audio = {"key": {"remoteJid": JID, "id": "id4"}, "message": {}}
    rows = [message(1), message(2), own, audio, message(5), {"separator": True}, message(6)]
    found = to_end.messages_from_here(rows, rows[1], lambda row: bool(row.get("separator")))
    assert [row["key"]["id"] for row in found] == ["id2", "id5", "id6"]
    assert to_end.messages_from_here(rows, {"key": {}}) == []
    assert len(to_end.messages_from_here([message(n) for n in range(80)], message(0))) == 50


def test_translate_from_here_asks_then_translates_to_the_end(wx, submitted):
    panel = Panel(dict(AUTOMATIC, scope="selected"))
    panel._sorted_messages = [old_message(1), old_message(2), old_message(3)]
    panel._on_translate_from_here(panel._sorted_messages[1])
    assert wx.asked and len(submitted) == 1
    submitted[0]("two", None)
    submitted[1]("three", None)
    assert panel.spoken[-1] == "translation_bulk_done" and panel._bulk_translation is None
    assert [value[2] for value in panel._translation_results.values()] == ["two", "three"]


def test_cancelling_a_run_stops_it_and_says_so(wx, submitted):
    panel = Panel(dict(AUTOMATIC, scope="selected"))
    panel._sorted_messages = [old_message(1), old_message(2)]
    panel._on_translate_from_here(panel._sorted_messages[0])
    panel._cancel_bulk_translation()
    assert panel._bulk_translation is None and panel.spoken[-1] == "translation_bulk_cancelled"
    submitted[0]("late", None)
    assert len(submitted) == 1 and not getattr(panel, "_translation_results", {})
    panel._cancel_bulk_translation()
    assert panel.spoken.count("translation_bulk_cancelled") == 1


class Menu:
    def __init__(self):
        self.labels = []

    def Append(self, _id, label):
        self.labels.append(label)
        return label


def test_the_menu_offers_from_here_or_cancel(wx):
    panel = Panel()
    panel.Bind = lambda *args: None
    menu = Menu()
    panel._append_translation_to_end_items(menu, message(1))
    panel._bulk_translation = object()
    panel._append_translation_to_end_items(menu, message(1))
    assert [label.split("\t")[0] for label in menu.labels] == ["translate_from_here", "translation_bulk_cancel_menu"]
    assert all(label.endswith("\tAlt+Shift+Y") for label in menu.labels)


def test_the_shortcuts_start_a_run_or_cancel_the_one_going(wx, submitted):
    panel = Panel(dict(AUTOMATIC, scope="selected"))
    panel._sorted_messages = [old_message(1), old_message(2)]
    panel.messages_list.GetFirstSelected = lambda: 1
    assert panel._message_at_selection()["key"]["id"] == "id2"
    panel._on_translate_from_here_shortcut()
    assert wx.asked and len(submitted) == 1
    panel._on_translate_from_here_shortcut()
    assert panel._bulk_translation is None and panel.spoken[-1] == "translation_bulk_cancelled"
    panel.selected_messages = {"id1"}
    panel._on_translate_selected_shortcut()
    assert panel._bulk_translation is not None
    panel._on_translate_selected_shortcut()
    assert panel._bulk_translation is None
    panel.messages_list.GetFirstSelected = lambda: -1
    assert panel._message_at_selection() is None
