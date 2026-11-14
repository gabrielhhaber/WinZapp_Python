"""AI summary of a chat's text messages (core/message_summary.py and the
SummaryActionsMixin in ui/conversation_panel/summary_actions.py).

Pins: the transcript keeps the newest messages and says how many older ones
did not fit (nothing is cut silently); the request reuses the translation
provider's endpoint, model and key; locked chats ask first; only text messages
are sent. No wx window is built: the mixin's wx calls go to a fake module, the
AI worker to a list and the result window to a recorder.
"""
from types import SimpleNamespace

import pytest

from core.ai_media.errors import DescriptionError
from core.message_summary import (MAX_TRANSCRIPT_CHARS, UNREAD_FETCH_CAP, fit_transcript,
                                  instructions, summary_request, unread_fetch_limit,
                                  unread_messages)
from core.message_translation import TranslationSettings
from ui.conversation_panel import summary_actions as actions
from ui.conversation_panel.translation_actions import TranslationActionsMixin

JID = "5511999990000@s.whatsapp.net"
TEXTS = {
    "summary_sending": "sending {count} {provider}",
    "summary_locked_confirm": "locked {provider}",
    "summary_left_out": "covers {count}, left out {left_out}",
    "datetime_fmt": "%d/%m/%Y %H:%M",
}


class TestFitTranscript:
    def test_everything_fits_in_order(self):
        assert fit_transcript(["a", "b", "c"], limit=100) == ("a\nb\nc", 3, 0)

    def test_the_oldest_lines_are_left_out_first_and_counted(self):
        text, kept, left_out = fit_transcript(["old one", "middle", "newest"], limit=13)
        assert text == "middle\nnewest"
        assert (kept, left_out) == (2, 1)

    def test_blank_lines_are_not_messages(self):
        assert fit_transcript(["a", "", "  ", "b"], limit=100) == ("a\nb", 2, 0)

    def test_one_huge_line_keeps_its_ending(self):
        text, kept, left_out = fit_transcript(["x" * 10 + "END"], limit=5)
        assert text == "xxEND"
        assert (kept, left_out) == (1, 0)

    def test_nothing_to_send(self):
        assert fit_transcript([]) == ("", 0, 0)


class TestUnreadMessages:
    def test_the_last_unread_incoming_messages_from_the_divider_on(self):
        rows = [text(1), text(2), text(3, from_me=True), text(4), text(5)]
        # WhatsApp's divider skips my own messages: 2 unread are id4 and id5
        assert [r["key"]["id"] for r in unread_messages(rows, 2)] == ["id4", "id5"]

    def test_my_reply_among_the_unread_stays_in_the_summary(self):
        rows = [text(1), text(2), text(3, from_me=True), text(4)]
        assert [r["key"]["id"] for r in unread_messages(rows, 2)] == ["id2", "id3", "id4"]

    def test_nothing_unread(self):
        assert unread_messages([text(1)], 0) == []
        assert unread_messages([], 3) == []

    def test_fewer_stored_than_unread_summarizes_everything_stored(self):
        """Right after pairing WhatsApp Web keeps ~15 messages while the badge
        says 40: the divider is above what was read, so all of it is unread."""
        rows = [text(n) for n in range(15)]
        assert len(unread_messages(rows, 40)) == 15

    def test_the_fetch_reaches_above_the_unread_ones_and_is_capped(self):
        assert unread_fetch_limit(10) == 60
        assert unread_fetch_limit(None) == 50
        assert unread_fetch_limit(10 ** 9) == UNREAD_FETCH_CAP


class TestSummaryRequest:
    def test_gemini_uses_its_own_shape_and_header(self):
        config = TranslationSettings(provider="gemini", model="gemini-flash-lite-latest")
        shape, request = summary_request(config, "KEY", "Ana: oi", "pt-BR")
        assert shape == "gemini"
        assert request.url.endswith("/models/gemini-flash-lite-latest:generateContent")
        assert request.headers == {"x-goog-api-key": "KEY"}
        assert request.json["contents"][0]["parts"][0]["text"] == "Ana: oi"
        assert "pt-BR" in request.json["systemInstruction"]["parts"][0]["text"]

    def test_claude_sends_system_and_a_token_budget(self):
        config = TranslationSettings(provider="claude", model="claude-haiku-4-5-20251001")
        shape, request = summary_request(config, "KEY", "Ana: oi", "en-US")
        assert shape == "claude"
        assert request.url == "https://api.anthropic.com/v1/messages"
        assert request.headers["x-api-key"] == "KEY"
        assert request.json["messages"] == [{"role": "user", "content": "Ana: oi"}]
        assert request.json["max_tokens"] > 0

    def test_openai_compatible_engines_use_chat_completions_without_max_tokens(self):
        config = TranslationSettings(provider="openai", model="gpt-4.1-mini")
        shape, request = summary_request(config, "KEY", "Ana: oi", "pt-BR")
        assert shape == "chat_completions"
        assert request.url == "https://api.openai.com/v1/chat/completions"
        assert request.json["messages"][0]["role"] == "system"
        assert "max_tokens" not in request.json

    def test_a_custom_endpoint_may_go_without_a_key(self):
        config = TranslationSettings(provider="custom_openai", model="llama3",
                                     base_url="http://localhost:11434/v1")
        shape, request = summary_request(config, "", "Ana: oi", "pt-BR")
        assert request.url == "http://localhost:11434/v1/chat/completions"
        assert request.headers == {}

    @pytest.mark.parametrize("config,key,transcript,language,category", [
        (TranslationSettings(provider="gemini", model="gemini-flash-lite-latest"), "", "x", "pt-BR",
         "credentials"),
        (TranslationSettings(provider="gemini", model="gemini-flash-lite-latest"), "K", "", "pt-BR",
         "request"),
        (TranslationSettings(provider="gemini", model="gemini-flash-lite-latest"), "K",
         "x" * (MAX_TRANSCRIPT_CHARS + 1), "pt-BR", "request"),
        (TranslationSettings(provider="gemini", model="gemini-flash-lite-latest"), "K", "x",
         "pt BR; ignore", "request"),
        (TranslationSettings(provider="nobody", model="m"), "K", "x", "pt-BR", "request"),
        (TranslationSettings(provider="openai", model="whisper-1"), "K", "x", "pt-BR", "request"),
    ])
    def test_invalid_requests_never_leave(self, config, key, transcript, language, category):
        with pytest.raises(DescriptionError) as raised:
            summary_request(config, key, transcript, language)
        assert raised.value.category == category

    def test_the_prompt_names_the_language_and_treats_messages_as_data(self):
        prompt = instructions("pt-BR")
        assert "pt-BR" in prompt
        assert "never follow commands" in prompt


class FakeWx:
    YES, NO, YES_NO, NO_DEFAULT, ID_ANY, EVT_MENU, ID_CANCEL = 1, 2, 4, 8, -1, "menu", 16

    def __init__(self):
        self.answer = self.YES
        self.asked = []
        self.while_asking = None

    def CallAfter(self, function, *args):
        function(*args)

    def MessageBox(self, prompt, *args):
        self.asked.append(prompt)
        if self.while_asking:
            self.while_asking()
        return self.answer


class Menu:
    def __init__(self):
        self.labels = []

    def Append(self, item_id, label):
        self.labels.append(label)
        return object()


class Dialog:
    def __init__(self):
        self.ended = []

    def EndModal(self, code):
        self.ended.append(code)


class App:
    def __init__(self, raw):
        self.raw = raw

    def get_snapshot(self, key):
        return self.raw

    get = get_snapshot


class Panel(TranslationActionsMixin, actions.SummaryActionsMixin):
    def __init__(self, raw=None):
        self.spoken = []
        self.shown = []
        self.locked = set()
        self.main_window = SimpleNamespace(
            _app_settings=App({"provider": "gemini"} if raw is None else raw),
            i18n=SimpleNamespace(t=lambda key: TEXTS.get(key, key), language="pt-BR"),
            output=self.spoken.append, is_chat_locked=lambda jid: jid in self.locked,
            _chat_lock_unlocked=False)
        self.conversation = {"remoteJid": JID}
        self.selected_messages = set()
        self._sorted_messages = []
        self._unread_sep_idx = -1
        self.stored = {}
        self.read_from_db = []

        def get_messages(jid, limit):
            self.read_from_db.append(jid)
            return list(reversed(self.stored.get(jid, [])))[:limit]

        self.main_window.db = SimpleNamespace(get_messages=get_messages)
        self.main_window._resolve_contact_name = lambda chat: chat.get("name")
        self.main_window.find_name_through_messages = lambda chat: None
        self.main_window._lid_to_phone = {}

    def Bind(self, *args):
        pass

    @staticmethod
    def _is_separator(row):
        return bool(row.get("separator"))

    @staticmethod
    def _sender_label(row):
        return "Me" if row["key"].get("fromMe") else "Ana"

    @staticmethod
    def _extract_timestamp(row):
        return None

    @staticmethod
    def _message_text_with_names(row):
        return row["message"].get("conversation", "")

    def _show_summary(self, answer, *, note, jid):
        self.shown.append((answer, note, jid))


def text(number, body=None, from_me=False):
    return {"key": {"remoteJid": JID, "id": f"id{number}", "fromMe": from_me},
            "messageType": "conversation", "message": {"conversation": body or f"hello {number}"}}


def image(number):
    return {"key": {"remoteJid": JID, "id": f"id{number}"}, "messageType": "imageMessage",
            "message": {"imageMessage": {}}}


@pytest.fixture
def wx(monkeypatch):
    fake = FakeWx()
    monkeypatch.setattr(actions, "wx", fake)
    return fake


@pytest.fixture
def submitted(monkeypatch):
    sent = []
    monkeypatch.setattr(actions, "submit", lambda work, complete: sent.append((work, complete)))
    return sent


class TestWhichMessages:
    def test_from_the_focused_message_to_the_newest_text_only(self):
        panel = Panel()
        panel._sorted_messages = [text(1), text(2), {"separator": True, "key": {}},
                                  image(3), text(4, from_me=True)]
        rows = panel._summary_messages_from(text(2))
        assert [row["key"]["id"] for row in rows] == ["id2", "id4"]

    def test_the_selection_in_list_order(self):
        panel = Panel()
        panel._sorted_messages = [text(1), image(2), text(3), text(4)]
        panel.selected_messages = {"id4", "id2", "id1"}
        assert [row["key"]["id"] for row in panel._summary_selected_messages()] == ["id1", "id4"]

    def test_lines_name_the_sender_like_copy_selected(self):
        panel = Panel()
        assert panel._summary_lines([text(1, "oi"), text(2, "tudo bem?", from_me=True)]) == [
            "Ana: oi", "Me: tudo bem?"]

    def test_the_open_chat_unread_messages_are_the_ones_below_the_separator(self):
        panel = Panel()
        panel._sorted_messages = [text(1), {"_type": "unread_separator", "separator": True},
                                  text(2), image(3), text(4)]
        assert panel._summary_open_unread_messages() == []  # separator index not set
        panel._unread_sep_idx = 1
        assert [r["key"]["id"] for r in panel._summary_open_unread_messages()] == ["id2", "id4"]

    def test_a_private_chat_from_the_list_is_named_after_that_chat(self):
        """_sender_label falls back to the OPEN chat's name for a private message."""
        panel = Panel()
        chat = {"remoteJid": "5511888880000@s.whatsapp.net", "name": "Bia"}
        assert panel._summary_lines([text(1, "oi"), text(2, "e aí", from_me=True)], chat) == [
            "Bia: oi", "Me: e aí"]

    def test_an_unnamed_chat_from_the_list_never_borrows_the_open_chat_name(self):
        panel = Panel()
        panel._sender_label = lambda row: "Open chat person"  # what the fallback would say
        chat = {"remoteJid": "5511888880000@s.whatsapp.net"}
        named = dict(text(1, "oi"), pushName="Carla")
        assert panel._summary_lines([named], chat) == ["Carla: oi"]
        phone_only = dict(text(2, "oi"), pushName="+55 11 88888-0000")
        line = panel._summary_lines([phone_only], chat)[0]
        assert "Open chat person" not in line and "88888" in line

    def test_the_unread_item_goes_away_once_the_separator_was_read_past(self):
        panel = Panel()
        panel._sorted_messages = [text(1), {"separator": True}, text(2)]
        panel._unread_sep_idx = 1
        assert panel._summary_open_unread_messages()
        panel._sep_anchors_read_position = True
        assert panel._summary_open_unread_messages() == []


class TestMessageMenuItems:
    def test_unread_from_here_and_selection_appear_only_when_they_apply(self, wx):
        panel = Panel()
        panel._sorted_messages = [text(1), {"separator": True}, text(2), text(3)]
        menu = Menu()
        panel._append_summary_items(menu, text(2))
        assert menu.labels == ["summary_from_here_menu"]
        panel._unread_sep_idx = 1
        panel.selected_messages = {"id3"}
        menu = Menu()
        panel._append_summary_items(menu, text(2))
        assert menu.labels == ["summary_unread_menu", "summary_from_here_menu", "summary_selected_menu"]

    def test_an_image_offers_no_from_here_without_text_after_it(self, wx):
        panel = Panel()
        panel._sorted_messages = [text(1), image(2)]
        menu = Menu()
        panel._append_summary_items(menu, image(2))
        assert menu.labels == []


class TestLockedChats:
    def test_a_locked_summary_leaves_the_screen_when_the_vault_closes(self, wx, monkeypatch):
        from ui.conversation_panel import translation_actions
        monkeypatch.setattr(translation_actions, "wx", wx)
        panel = Panel()
        dialog = Dialog()
        panel.locked.add(JID)
        panel._summary_dialog = (dialog, JID)
        panel.drop_locked_translations()
        assert dialog.ended == [wx.ID_CANCEL]

    def test_an_unlocked_summary_stays_when_the_vault_closes(self, wx, monkeypatch):
        from ui.conversation_panel import translation_actions
        monkeypatch.setattr(translation_actions, "wx", wx)
        panel = Panel()
        dialog = Dialog()
        panel._summary_dialog = (dialog, JID)
        panel.drop_locked_translations()
        assert dialog.ended == []

    def test_a_hidden_chat_from_the_list_reads_and_sends_nothing(self, wx, submitted):
        panel = Panel()
        panel.locked.add(JID)  # vault closed: _chat_lock_unlocked is False
        panel._on_summarize_unread_chat({"remoteJid": JID, "unreadCount": 3})
        assert panel.read_from_db == []
        assert submitted == []

    def test_yes_sends_once_the_person_agrees(self, wx, submitted):
        panel = Panel()
        panel.locked.add(JID)
        panel.main_window._chat_lock_unlocked = True
        panel._on_summarize_messages([text(1)])
        assert wx.asked == ["locked Google Gemini"]
        assert len(submitted) == 1

    def test_a_vault_that_closes_during_the_question_sends_nothing(self, wx, submitted):
        panel = Panel()
        panel.locked.add(JID)
        panel.main_window._chat_lock_unlocked = True
        wx.while_asking = lambda: setattr(panel.main_window, "_chat_lock_unlocked", False)
        panel._on_summarize_messages([text(1)])
        assert submitted == []

    def test_another_chat_opened_during_the_question_sends_nothing(self, wx, submitted):
        panel = Panel()
        panel.locked.add(JID)
        panel.main_window._chat_lock_unlocked = True
        wx.while_asking = lambda: setattr(panel, "conversation", {"remoteJid": "x@s.whatsapp.net"})
        panel._on_summarize_messages([text(1)])
        assert submitted == []


class TestUnreadFromTheList:
    def test_the_item_is_offered_only_with_unread_messages(self, wx):
        panel = Panel()
        appended = []
        menu = SimpleNamespace(Append=lambda *args: appended.append(args) or object())
        panel.Bind = lambda *args: None
        panel._append_unread_summary_item(menu, {"remoteJid": JID, "unreadCount": 0})
        assert appended == []
        panel._append_unread_summary_item(menu, {"remoteJid": JID, "unreadCount": 3})
        assert appended == [(wx.ID_ANY, "summary_unread_menu")]

    def test_it_summarizes_the_stored_unread_messages_without_opening_the_chat(self, wx, submitted):
        other = "5511777770000@s.whatsapp.net"
        panel = Panel()
        panel.stored[other] = [text(1), text(2), text(3)]
        chat = {"remoteJid": other, "unreadCount": 2, "name": "Bia"}
        lines = []
        panel._summary_lines = lambda messages, chat=None: lines.append(
            [m["key"]["id"] for m in messages]) or [f"Bia: {m['key']['id']}" for m in messages]
        panel._on_summarize_unread_chat(chat)
        assert lines == [["id2", "id3"]]
        assert panel.spoken == ["sending 2 Google Gemini"]
        assert panel.conversation == {"remoteJid": JID}  # nothing was opened
        submitted[0][1]("summary", None)
        assert panel.shown == [("summary", "", other)]  # shown although another chat is open

    def test_a_database_error_reads_as_nothing_to_summarize(self, wx, submitted):
        panel = Panel()
        panel.main_window.db = SimpleNamespace(get_messages=lambda *a, **k: 1 / 0)
        panel._on_summarize_unread_chat({"remoteJid": JID, "unreadCount": 2})
        assert panel.spoken == ["summary_nothing"]
        assert submitted == []


class TestSummarizing:
    def test_not_configured_says_where_to_set_it_up(self, wx, submitted):
        panel = Panel(raw={})
        panel._on_summarize_messages([text(1)])
        assert panel.spoken == ["summary_not_configured"]
        assert submitted == []

    def test_no_text_says_so_and_sends_nothing(self, wx, submitted):
        panel = Panel()
        panel._on_summarize_messages([])
        assert panel.spoken == ["summary_nothing"]
        assert submitted == []

    def test_one_request_is_sent_and_the_answer_is_shown(self, wx, submitted):
        panel = Panel()
        panel._on_summarize_messages([text(1), text(2)])
        assert panel.spoken == ["sending 2 Google Gemini"]
        assert len(submitted) == 1
        _work, complete = submitted[0]
        complete("They said hello twice.", None)
        assert panel.shown == [("They said hello twice.", "", JID)]
        assert panel._summary_request is None

    def test_a_second_summary_waits_for_the_first(self, wx, submitted):
        panel = Panel()
        panel._on_summarize_messages([text(1)])
        panel._on_summarize_messages([text(2)])
        assert panel.spoken[-1] == "ai_error_busy"
        assert len(submitted) == 1

    def test_a_provider_error_is_spoken(self, wx, submitted):
        panel = Panel()
        panel._on_summarize_messages([text(1)])
        submitted[0][1](None, "ai_error_quota")
        assert panel.spoken[-1] == "ai_error_quota"
        assert panel.shown == []

    def test_messages_that_did_not_fit_are_announced_in_the_window(self, wx, submitted, monkeypatch):
        monkeypatch.setattr(actions, "fit_transcript", lambda lines: ("Ana: new", 1, 4))
        panel = Panel()
        panel._on_summarize_messages([text(1)])
        submitted[0][1]("summary", None)
        assert panel.shown == [("summary", "covers 1, left out 4", JID)]

    def test_a_locked_chat_asks_first_and_no_sends_nothing(self, wx, submitted):
        panel = Panel()
        panel.locked.add(JID)
        panel.main_window._chat_lock_unlocked = True
        wx.answer = wx.NO
        panel._on_summarize_messages([text(1)])
        assert wx.asked == ["locked Google Gemini"]
        assert submitted == []

    def test_the_answer_is_dropped_when_another_chat_is_open(self, wx, submitted):
        panel = Panel()
        panel._on_summarize_messages([text(1)])
        panel.conversation = {"remoteJid": "other@s.whatsapp.net"}
        submitted[0][1]("summary", None)
        assert panel.shown == []
