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


class Panel(actions.TranslationActionsMixin):
    def __init__(self, raw=AUTOMATIC):
        self.spoken = []
        self.locked = set()
        self.main_window = SimpleNamespace(
            _app_settings=App(raw), i18n=SimpleNamespace(t=lambda key: TEXTS.get(key, key)),
            output=self.spoken.append, is_chat_locked=lambda jid: jid in self.locked,
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
    assert panel.spoken == ["translation_auto_on"]


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
