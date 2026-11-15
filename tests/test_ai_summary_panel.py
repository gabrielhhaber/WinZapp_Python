"""The conversation panel's AI summary (SummaryActionsMixin, SummaryVoiceMixin).

Pins: one flow behind the button, Ctrl+Shift+U, the conversations-list menu and
the message menu; no unread threshold; the cheap checks (the unread count) come
before the credential store; a locked chat is summarized with a consent asked
each time (the dialog's job) but never while the vault hides it; a private chat
summarized from the list is never signed with the OPEN chat's name; a voice
message carries its stored or freshly made transcription, only the newest 20 are
transcribed, a missing Whisper leaves markers and is said, cancelling drops the
summary. No window is built: wx goes to a fake module, the result window to a
recorder and the transcription worker runs inline.
"""
import threading
from types import SimpleNamespace as NS

import pytest

from core.transcription import errors
from core.transcription import stored as stored_transcription
from ui.conversation_panel import summary_actions as actions
from ui.conversation_panel import summary_voice as voice
from ui.conversation_panel.ai_actions import AIActionsMixin
from ui.conversation_panel.bulk_transcription import BulkTranscriptionMixin
from ui.conversation_panel.summary_actions import SummaryActionsMixin
from ui.conversation_panel.summary_voice import SummaryVoiceMixin
from core.ai_media.summary_voice_choice import VoiceChoice

JID = "5511999990000@s.whatsapp.net"
GROUP = "120363000000000000@g.us"
TEXTS = {"ai_summary_left_out": "in {count} left {left_out}",
         "ai_summary_voice_missing": "markers {count}",
         "ai_summary_voice_failed": "failed {count}",
         "ai_summary_voice_over_limit": "over {count} of {limit}",
         "ai_summary_voice_progress": "voice {index}/{total}",
         "datetime_fmt": "%H:%M"}


class FakeWx:
    YES, NO, YES_NO, ICON_QUESTION, ID_ANY, EVT_MENU = 1, 2, 4, 8, -1, "menu"

    def __init__(self):
        self.answer = self.YES
        self.asked = []

    def CallAfter(self, function, *args):
        function(*args)

    def MessageBox(self, prompt, *args):
        self.asked.append(prompt)
        return self.answer


class FakeThread:
    def __init__(self, target, args=(), **kwargs):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


class Menu:
    def __init__(self):
        self.labels = []

    def Append(self, item_id, label):
        self.labels.append(label)
        return label


class Store:
    def __init__(self, saved=("gemini",)):
        self.keys = set(saved)
        self.reads = 0

    def saved(self):
        self.reads += 1
        return set(self.keys)


class Dialog:
    """AIResultDialog's recorder."""
    made = []

    def __init__(self, panel, identity, kind, locked, config, app, loader, mime, notice="",
                 preconsented=False):
        self.args = NS(identity=identity, kind=kind, locked=locked, notice=notice, loader=loader, mime=mime,
                       preconsented=preconsented)
        self.disposed = self.destroyed = False
        Dialog.made.append(self)
        panel.events.append("result window")

    def ShowModal(self):
        return 0

    def Raise(self):
        pass

    def _dispose(self):
        self.disposed = True

    def Destroy(self):
        self.destroyed = True


class ProgressWindow:
    """TranscriptionProgressDialog's recorder: the job starts when the modal
    loop would, and its report closes the window (here, run() returns)."""
    made = []
    #: What the person does inside the window before the job starts.
    first_cancel = False
    #: The window's job could not even start.
    fail = False

    def __init__(self, panel, i18n, speak_output, make_job, status, title=None):
        self.panel, self.title, self.statuses, self.ticks = panel, title, [status], []
        self.result = self.error = None
        self.destroyed = False
        self.job = make_job(self.ticks.append, self._finished)
        ProgressWindow.made.append(self)

    def _finished(self, result, error):
        self.result, self.error = result, error

    def set_status(self, text):
        self.statuses.append(text)

    def run(self):
        self.panel.events.append("progress window")
        if self.fail:
            self._finished(None, errors.TranscriptionError(errors.BACKEND_ERROR, "x"))
        else:
            if self.first_cancel:
                self.job.cancel()
            self.job.start()
        return 0

    def Destroy(self):
        self.destroyed = True
        self.panel.events.append("progress window closed")


class Panel(SummaryActionsMixin, SummaryVoiceMixin):
    _bulk_transcription_running = BulkTranscriptionMixin._bulk_transcription_running

    def __init__(self, store, config=None):
        self._bulk_transcription = None
        self.spoken = []
        self.events = []
        self.consents = []
        self.consent_answer = True
        self.voice_choice = VoiceChoice("local")
        self.online_consent_answer = True
        self.locked = set()
        self.unlocked = False
        self.settings_reads = 0
        self._config = {"enabled": True, "order": ["gemini"], "disabled": []} if config is None else config
        self.store = store
        self.main_window = NS(
            i18n=NS(t=lambda key: TEXTS.get(key, key), language="pt-BR"),
            output=lambda text, interrupt=False: self.spoken.append(text),
            speak_output=NS(output=lambda text, interrupt=False: self.spoken.append(text)),
            is_chat_locked=lambda jid: jid in self.locked,
            is_chat_hidden_by_vault=lambda jid: jid in self.locked and not self.unlocked,
            _resolve_contact_name=lambda chat: chat.get("name"),
            find_name_through_messages=lambda chat: None, _lid_to_phone={}, settings={},
            _shutting_down=False)
        self.conversation = {"remoteJid": JID}
        self.selected_messages = set()
        self._sorted_messages = []
        self._unread_sep_idx = -1
        self._first_unread_msg_id = None
        self._sep_anchors_read_position = False
        self._ai_dialog = None
        self.restored = []
        self.transcribed = []
        self.kept = []
        self.transcribe_with = lambda msg: (NS(text="llego tarde", is_empty=False), None)
        self.db_reads = []
        self.stored_rows = {}

        def get_messages(jid, limit):
            self.db_reads.append((jid, limit))
            return list(reversed(self.stored_rows.get(jid, [])))

        self.main_window.db = NS(get_messages=get_messages)

    def Bind(self, *args):
        pass

    def _summary_voice_decide(self, app, config, jid, count):
        self.events.append(f"how to transcribe {count}")
        return self.voice_choice

    def _summary_voice_online_consent(self, app, config, jid, choice, count):
        self.events.append("online voice consent")
        return self.online_consent_answer

    def _ai_settings(self):
        self.settings_reads += 1
        return NS(), self._config

    @staticmethod
    def _is_separator(row):
        return row.get("_type") in ("unread_separator", "empty_placeholder")

    @staticmethod
    def _sender_label(row):
        return "Me" if row["key"].get("fromMe") else "Open chat name"

    @staticmethod
    def _extract_timestamp(row):
        return None

    @staticmethod
    def _message_text_with_names(row):
        return row["message"].get("conversation", "")

    def _restore_focus_after_ai(self, identity, locked):
        self.restored.append(identity)

    def _bulk_transcribe_one(self, run, msg):
        self.transcribed.append(msg["key"]["id"])
        return self.transcribe_with(msg)

    def _store_bulk_transcription(self, jid, msg, result, inline_run=False, item=None):
        self.kept.append((jid, msg["key"]["id"], result.text))


def text(number, body=None, from_me=False, jid=JID, participant=None):
    key = {"remoteJid": jid, "id": f"id{number}", "fromMe": from_me}
    if participant:
        key["participant"] = participant
    return {"key": key, "messageType": "conversation", "message": {"conversation": body or f"hello {number}"}}


def audio(number):
    return {"key": {"remoteJid": JID, "id": f"id{number}", "fromMe": False}, "messageType": "audioMessage",
            "message": {"audioMessage": {}}}


SEPARATOR = {"_type": "unread_separator", "count": 2}


@pytest.fixture
def wx(monkeypatch):
    fake = FakeWx()
    monkeypatch.setattr(actions, "wx", fake)
    monkeypatch.setattr(voice, "wx", fake)
    monkeypatch.setattr(voice.threading, "Thread", FakeThread)
    monkeypatch.setattr(voice, "TranscriptionProgressDialog", ProgressWindow)
    ProgressWindow.made = []
    ProgressWindow.first_cancel = ProgressWindow.fail = False
    return fake


@pytest.fixture
def panel(wx, monkeypatch):
    import ui.dialogs.ai_result_dialog as dialog_module
    store = Store()
    monkeypatch.setattr(actions, "CredentialStore", lambda path: store)
    monkeypatch.setattr(actions, "global_dir", lambda: "unused")
    monkeypatch.setattr(actions, "active_account_id", lambda: "account")
    monkeypatch.setattr(dialog_module, "AIResultDialog", Dialog)
    Dialog.made = []
    the_panel = Panel(store)

    def ask_consent(parent, i18n, app, config, providers, kind, locked, valid=lambda: True, track=None):
        the_panel.events.append("consent")
        the_panel.consents.append(NS(providers=providers, kind=kind, locked=locked, valid=valid))
        return the_panel.consent_answer
    monkeypatch.setattr(dialog_module, "ask_consent", ask_consent)
    return the_panel


def with_unread(panel, rows=None):
    """Two read messages, the separator, then the unread ones."""
    unread = rows if rows is not None else [text(3), text(4)]
    panel._sorted_messages = [text(1), text(2), dict(SEPARATOR), *unread]
    panel._unread_sep_idx = 2
    panel._first_unread_msg_id = unread[0]["key"]["id"] if unread else None
    return panel


class TestUnreadMessages:
    def test_the_rows_below_the_separator_even_with_the_counter_zeroed(self, panel):
        with_unread(panel)
        panel.conversation["unreadCount"] = 0
        assert [m["key"]["id"] for m in panel._summary_unread_messages()] == ["id3", "id4"]

    def test_nothing_once_focus_stepped_past_the_separator(self, panel):
        with_unread(panel)
        panel._sep_anchors_read_position = True
        assert panel._summary_unread_messages() == []

    def test_the_anchor_id_finds_them_mid_rebuild(self, panel):
        with_unread(panel)
        panel._unread_sep_idx = -1
        assert [m["key"]["id"] for m in panel._summary_unread_messages()] == ["id3", "id4"]

    def test_the_anchor_may_be_the_first_loaded_row(self, panel):
        panel._sorted_messages = [text(7), text(8)]
        panel._first_unread_msg_id = "id7"
        assert len(panel._summary_unread_messages()) == 2

    def test_no_separator_and_no_anchor_is_nothing(self, panel):
        panel._sorted_messages = [text(1)]
        assert panel._summary_unread_messages() == []

    def test_no_conversation_is_nothing(self, panel):
        with_unread(panel)
        panel.conversation = None
        assert panel._summary_unread_messages() == []

    def test_sentinels_and_reactions_are_not_messages(self, panel):
        reaction = {"key": {"id": "r", "remoteJid": JID}, "messageType": "reactionMessage", "message": {}}
        with_unread(panel, [text(3), {"_type": "empty_placeholder"}, reaction])
        assert [m["key"]["id"] for m in panel._summary_unread_messages()] == ["id3"]


class TestOffered:
    def test_one_unread_message_is_enough(self, panel):
        with_unread(panel, [text(3)])
        assert panel._summary_offered() is True

    def test_a_locked_chat_is_offered_while_the_vault_is_open(self, panel):
        with_unread(panel)
        panel.locked.add(JID)
        panel.unlocked = True
        assert panel._summary_offered() is True

    def test_a_locked_chat_is_never_offered_while_the_vault_hides_it(self, panel):
        with_unread(panel)
        panel.locked.add(JID)
        assert panel._summary_offered() is False

    def test_nothing_unread_never_reads_settings_or_the_credential_store(self, panel):
        panel._sorted_messages = [text(1), text(2)]
        assert panel._summary_offered() is False
        assert panel.settings_reads == 0 and panel.store.reads == 0

    def test_the_credential_store_is_read_last(self, panel):
        with_unread(panel)
        panel._config = {"enabled": False, "order": ["gemini"], "disabled": []}
        assert panel._summary_offered() is False
        assert panel.store.reads == 0

    def test_no_key_for_a_provider_that_takes_text_means_no_offer(self, panel):
        with_unread(panel)
        panel.store.keys = set()
        assert panel._summary_offered() is False

    def test_the_button_follows_the_rule(self, panel):
        shown = []
        panel._summary_btn = NS(SetLabel=lambda label: None, Show=lambda: shown.append(True),
                                Hide=lambda: shown.append(False))
        panel._sync_media_action_slot_visibility = lambda: shown.append("sync")
        with_unread(panel, [text(n) for n in range(3, 13)])
        panel._update_summary_button()
        panel._sorted_messages = []
        panel._update_summary_button()
        assert shown == [True, "sync", False, "sync"]

    def test_below_ten_unread_there_is_no_button_but_the_shortcut_works(self, panel):
        shown = []
        panel._summary_btn = NS(SetLabel=lambda label: None, Show=lambda: shown.append(True),
                                Hide=lambda: shown.append(False))
        panel._sync_media_action_slot_visibility = lambda: None
        with_unread(panel, [text(n) for n in range(3, 12)])
        panel._update_summary_button()
        assert shown == [False]
        assert panel._summary_offered() is True
        panel._on_summary_action()
        assert Dialog.made


class TestMenus:
    def _message_menu(self, panel, msg):
        menu = Menu()
        panel._append_summary_items(menu, msg)
        return menu.labels

    def test_unread_from_here_and_selection_appear_only_when_they_apply(self, panel):
        with_unread(panel)
        assert self._message_menu(panel, panel._sorted_messages[0]) == [
            "ai_summary_unread_menu\tCtrl+Shift+U", "ai_summary_from_here_menu\tAlt+Shift+I"]
        panel.selected_messages = {"id1"}
        assert self._message_menu(panel, panel._sorted_messages[0]) == [
            "ai_summary_unread_menu\tCtrl+Shift+U", "ai_summary_from_here_menu\tAlt+Shift+I", "ai_summary_selected_menu\tCtrl+Alt+Shift+I"]

    def test_with_nothing_unread_from_here_and_selected_still_work(self, panel):
        panel._sorted_messages = [text(1), text(2)]
        panel.selected_messages = {"id2"}
        assert self._message_menu(panel, panel._sorted_messages[0]) == [
            "ai_summary_from_here_menu\tAlt+Shift+I", "ai_summary_selected_menu\tCtrl+Alt+Shift+I"]

    def test_an_image_with_nothing_after_it_offers_no_from_here(self, panel):
        image = {"key": {"id": "i", "remoteJid": JID}, "messageType": "imageMessage", "message": {"imageMessage": {}}}
        panel._sorted_messages = [image]
        assert self._message_menu(panel, image) == ["ai_summary_from_here_menu\tAlt+Shift+I"]
        panel._sorted_messages = [image, {"key": {"id": "r"}, "messageType": "reactionMessage", "message": {}}]
        assert self._message_menu(panel, image) == ["ai_summary_from_here_menu\tAlt+Shift+I"]

    def test_nothing_is_offered_without_a_configured_provider(self, panel):
        with_unread(panel)
        panel.store.keys = set()
        assert self._message_menu(panel, panel._sorted_messages[0]) == []

    def test_the_list_item_needs_unread_messages(self, panel):
        menu = Menu()
        panel._append_unread_summary_item(menu, {"remoteJid": JID, "unreadCount": 0})
        assert menu.labels == [] and panel.store.reads == 0
        panel._append_unread_summary_item(menu, {"remoteJid": JID, "unreadCount": 1})
        assert menu.labels == ["ai_summary_unread_menu"]

    def test_the_list_item_is_not_offered_for_a_hidden_locked_chat(self, panel):
        panel.locked.add(JID)
        menu = Menu()
        panel._append_unread_summary_item(menu, {"remoteJid": JID, "unreadCount": 4})
        assert menu.labels == []


class TestSender:
    def test_a_private_chat_from_the_list_is_named_after_that_chat(self, panel):
        row = text(1)
        assert panel._summary_sender(row, {"remoteJid": JID, "name": "Bia"}) == "Bia"

    def test_it_never_borrows_the_open_chat_name_when_the_chat_has_none(self, panel):
        row = text(1)
        assert panel._summary_sender(row, {"remoteJid": JID}) not in ("", "Open chat name")

    def test_a_pushname_is_better_than_a_number(self, panel):
        row = dict(text(1), pushName="Beatriz")
        assert panel._summary_sender(row, {"remoteJid": JID}) == "Beatriz"

    def test_groups_my_messages_and_the_open_chat_use_the_panels_own_label(self, panel):
        assert panel._summary_sender(text(1, participant="x@s.whatsapp.net"), {"remoteJid": GROUP}) == "Open chat name"
        assert panel._summary_sender(text(1, from_me=True), {"remoteJid": JID, "name": "Bia"}) == "Me"
        assert panel._summary_sender(text(1), None) == "Open chat name"


class TestSummarizing:
    def test_the_open_chat_summary_opens_one_window_with_the_unread_lines(self, panel):
        with_unread(panel)
        panel._on_summary_action()
        (dialog,) = Dialog.made
        assert dialog.args.kind == "text" and dialog.args.locked is False
        assert dialog.args.identity == ("account", JID, "summary")
        body = dialog.args.loader(None).decode()
        assert body == "Open chat name: hello 3\nOpen chat name: hello 4"
        assert dialog.args.notice == "" and dialog.disposed and dialog.destroyed
        assert panel.restored == [dialog.args.identity]

    def test_the_window_can_be_reopened_after_it_closes(self, panel):
        with_unread(panel)
        panel._on_summary_action()
        assert panel._ai_dialog is None
        panel._on_summary_action()  # a new window, not a silent Raise
        assert len(Dialog.made) == 2

    def test_the_lines_carry_the_time_and_the_sender_like_copy_selected(self, panel, monkeypatch):
        with_unread(panel, [text(3), text(4, from_me=True)])
        monkeypatch.setattr(Panel, "_extract_timestamp", staticmethod(lambda row: 3600 * 12))
        panel._on_summary_action()
        body = Dialog.made[0].args.loader(None).decode().splitlines()
        assert body[1].endswith(" - Me: hello 4") and ":" in body[1].split(" - ")[0]

    def test_a_locked_chat_is_summarized_with_the_locked_flag(self, panel):
        with_unread(panel)
        panel.locked.add(JID)
        panel.unlocked = True
        panel._on_summary_action()
        assert Dialog.made[0].args.locked is True

    def test_nothing_unread_says_so_and_opens_nothing(self, panel):
        panel._on_summary_action()
        assert panel.spoken == ["ai_summary_not_available"] and Dialog.made == []

    def test_no_provider_says_so_and_opens_nothing(self, panel):
        panel.store.keys = set()
        panel._on_summarize_messages([text(1)])
        assert panel.spoken == ["ai_summary_not_available"] and Dialog.made == []

    def test_messages_without_content_say_there_is_nothing(self, panel):
        reaction = {"key": {"id": "r", "remoteJid": JID}, "messageType": "reactionMessage", "message": {}}
        panel._on_summarize_messages([reaction])
        assert panel.spoken == ["ai_summary_nothing"] and Dialog.made == []

    def test_a_second_window_is_not_opened_over_the_first(self, panel):
        raised = []
        panel._ai_dialog = NS(Raise=lambda: raised.append(True))
        panel._on_summarize_messages([text(1)])
        assert raised == [True] and Dialog.made == []

    def test_a_hidden_locked_chat_reads_and_sends_nothing(self, panel):
        panel.locked.add(JID)
        panel._on_summarize_messages([text(1)])
        assert panel.spoken == [] and Dialog.made == [] and panel.store.reads == 0

    def test_messages_that_did_not_fit_are_announced_in_the_window(self, panel, monkeypatch):
        monkeypatch.setattr("core.ai_media.summary.MAX_MESSAGES", 200)
        rows = [text(n) for n in range(250)]
        panel._on_summarize_messages(rows)
        assert Dialog.made[0].args.notice == "in 200 left 50"

    def test_from_the_list_it_names_the_chat_and_leaves_the_focus_alone(self, panel):
        panel._on_summarize_messages([text(1)], jid=JID, chat={"remoteJid": JID, "name": "Bia"})
        dialog = Dialog.made[0]
        assert dialog.args.loader(None).decode() == "Bia: hello 1"
        assert dialog.args.identity[1] == JID and panel.restored == []

    def test_the_vault_closing_before_the_window_opens_drops_it(self, panel):
        panel._summary_voice_step = lambda messages, jid, on_done, follows, choice=None: (
            panel.locked.add(JID), on_done(NS(texts={})))
        panel._on_summarize_messages([text(1)])
        assert Dialog.made == []

    def test_another_chat_opened_while_voice_was_transcribed_drops_it(self, panel):
        panel._summary_voice_step = lambda messages, jid, on_done, follows, choice=None: (
            panel.conversation.update(remoteJid="other@s.whatsapp.net"), on_done(NS(texts={})))
        panel._on_summarize_messages([text(1)])
        assert Dialog.made == []

    def test_a_summary_from_the_list_survives_another_chat_opening(self, panel):
        panel._summary_voice_step = lambda messages, jid, on_done, follows, choice=None: (
            panel.conversation.update(remoteJid="other@s.whatsapp.net"), on_done(NS(texts={})))
        panel._on_summarize_messages([text(1)], jid=JID, chat={"remoteJid": JID, "name": "Bia"})
        assert len(Dialog.made) == 1

    def test_no_summary_text_reaches_a_log(self, panel, caplog):
        with_unread(panel, [text(3, "a secret plan")])
        with caplog.at_level("DEBUG"):
            panel._on_summary_action()
        assert "secret" not in caplog.text


class TestFromTheList:
    def test_it_summarizes_the_stored_unread_messages_without_opening_the_chat(self, panel):
        panel.stored_rows[JID] = [text(n) for n in range(1, 7)]
        panel._on_summarize_unread_chat({"remoteJid": JID, "unreadCount": 2, "name": "Bia"})
        assert panel.db_reads == [(JID, 52)]
        assert Dialog.made[0].args.loader(None).decode() == "Bia: hello 5\nBia: hello 6"

    def test_fewer_stored_than_unread_summarizes_what_is_stored(self, panel):
        panel.stored_rows[JID] = [text(1), text(2)]
        panel._on_summarize_unread_chat({"remoteJid": JID, "unreadCount": 30, "name": "Bia"})
        assert Dialog.made[0].args.loader(None).decode() == "Bia: hello 1\nBia: hello 2"

    def test_a_database_error_reads_as_nothing_to_summarize(self, panel):
        def broken(jid, limit):
            raise OSError("synthetic")
        panel.main_window.db = NS(get_messages=broken)
        panel._on_summarize_unread_chat({"remoteJid": JID, "unreadCount": 3})
        assert panel.spoken == ["ai_summary_nothing"] and Dialog.made == []

    def test_a_hidden_chat_is_not_read_from_the_database(self, panel):
        panel.locked.add(JID)
        panel._on_summarize_unread_chat({"remoteJid": JID, "unreadCount": 3})
        assert panel.db_reads == []


def stored_voice(msg, words):
    result = NS(text=words, language="pt", language_probability=0.9, model_id="small",
                backend="faster-whisper", vad_used=True)
    msg[stored_transcription.TRANSCRIPTION_KEY] = stored_transcription.with_result(
        None, stored_transcription.record_from_result(result, 1.0))
    return msg


class TestVoiceMessages:
    def test_a_stored_transcription_is_used_without_a_run(self, panel):
        panel._on_summarize_messages([text(1), stored_voice(audio(2), "llego tarde")])
        assert panel.transcribed == []
        body = Dialog.made[0].args.loader(None).decode()
        assert body.splitlines()[1] == "Open chat name: [Voice message] llego tarde"
        assert Dialog.made[0].args.notice == ""

    def test_a_voice_message_without_words_is_transcribed_before_the_summary(self, panel):
        panel._on_summarize_messages([text(1), audio(2)])
        assert panel.transcribed == ["id2"]
        assert "[Voice message] llego tarde" in Dialog.made[0].args.loader(None).decode()
        assert panel.kept == [(JID, "id2", "llego tarde")]
        assert ProgressWindow.made[0].statuses == ["voice 1/1"]

    def test_only_the_newest_twenty_are_transcribed_and_the_rest_are_said(self, panel):
        rows = [audio(n) for n in range(25)]
        panel._on_summarize_messages(rows)
        assert panel.transcribed == [f"id{n}" for n in range(5, 25)]
        assert Dialog.made[0].args.notice == "markers 5\nover 5 of 20"
        assert ProgressWindow.made[0].statuses[-1] == "voice 20/20"
        assert len(ProgressWindow.made) == 1

    def test_a_transcription_that_is_not_set_up_is_said_once_and_leaves_markers(self, panel):
        panel.transcribe_with = lambda msg: (None, errors.TranscriptionError(errors.MODEL_NOT_INSTALLED, "x"))
        panel._on_summarize_messages([audio(1), audio(2), audio(3)])
        assert panel.transcribed == ["id1"]  # it would fail the same way for each one
        assert errors.error_i18n_key(errors.MODEL_NOT_INSTALLED) in panel.spoken
        assert Dialog.made[0].args.notice == "markers 3\nfailed 1"

    def test_a_failing_message_does_not_stop_the_others(self, panel):
        def one_fails(msg):
            if msg["key"]["id"] == "id1":
                return None, errors.TranscriptionError(errors.FFMPEG_FAILED, "x")
            return NS(text="hola", is_empty=False), None
        panel.transcribe_with = one_fails
        panel._on_summarize_messages([audio(1), audio(2)])
        assert panel.transcribed == ["id1", "id2"]
        assert Dialog.made[0].args.notice == "markers 1\nfailed 1"

    def test_a_message_with_no_speech_stays_a_marker(self, panel):
        panel.transcribe_with = lambda msg: (NS(text="", is_empty=True), None)
        panel._on_summarize_messages([audio(1)])
        assert panel.kept == [] and Dialog.made[0].args.notice == "markers 1"

    def test_cancelling_drops_the_whole_summary_and_says_so(self, panel):
        panel.transcribe_with = lambda msg: (None, errors.TranscriptionError(errors.CANCELLED, "x"))
        panel._on_summarize_messages([audio(1)])
        assert Dialog.made == [] and panel.spoken[-1] == "ai_summary_cancelled"

    def test_the_vault_closing_stops_the_run_without_a_word_about_the_messages(self, panel):
        def lock_then_answer(msg):
            panel.unlocked = False  # the vault closes under the run
            return NS(text="hola", is_empty=False), None
        panel.locked.add(JID)
        panel.unlocked = True
        panel.transcribe_with = lock_then_answer
        panel._on_summarize_messages([audio(1), audio(2)])
        assert panel.transcribed == ["id1"] and Dialog.made == []
        assert panel.spoken == [] and ProgressWindow.made[0].statuses == ["voice 1/2"]

    def test_a_second_summary_while_it_runs_starts_nothing_and_asks_nothing(self, panel, wx):
        run = NS(running=True, index=2, total=5, cancelled=False)
        run.cancel = lambda: setattr(run, "cancelled", True)
        panel._summary_voice_run = run
        with_unread(panel)
        panel._on_summary_action()
        panel._on_summarize_messages([text(1)])
        assert run.cancelled is False and Dialog.made == [] and wx.asked == []
        assert panel.consents == [] and panel.spoken == []

    def test_cancel_in_the_progress_window_drops_the_summary_without_a_question(self, panel, wx):
        ProgressWindow.first_cancel = True
        panel._on_summarize_messages([audio(1), audio(2)])
        assert panel.transcribed == [] and Dialog.made == [] and wx.asked == []
        assert panel.spoken == ["ai_summary_cancelled"] and panel._summary_voice_run.running is False

    def test_cancel_during_a_message_stops_before_the_next_and_drops_the_summary(self, panel):
        def cancel_then_answer(msg):
            ProgressWindow.made[0].job.cancel()
            return NS(text="hola", is_empty=False), None
        panel.transcribe_with = cancel_then_answer
        panel._on_summarize_messages([audio(1), audio(2), audio(3)])
        assert panel.transcribed == ["id1"] and Dialog.made == []
        assert panel.spoken[-1] == "ai_summary_cancelled"

    def test_the_window_is_gone_before_the_result_opens(self, panel):
        panel._on_summarize_messages([audio(1)])
        assert panel.events == ["how to transcribe 1", "consent", "progress window",
                                "progress window closed", "result window"]
        assert ProgressWindow.made[0].destroyed and ProgressWindow.made[0].title == "ai_result_summary_title"

    def test_the_window_shows_each_message_and_a_growing_gauge(self, panel):
        panel._on_summarize_messages([audio(1), audio(2), audio(3), audio(4)])
        window = ProgressWindow.made[0]
        assert window.statuses == ["voice 1/4", "voice 2/4", "voice 3/4", "voice 4/4"]
        assert [tick.percent for tick in window.ticks] == [0, 25, 50, 75]
        assert all(tick.update_bar and not tick.speak for tick in window.ticks)

    def test_no_window_when_there_is_nothing_to_transcribe(self, panel):
        panel._on_summarize_messages([text(1), stored_voice(audio(2), "hola")])
        assert ProgressWindow.made == []

    def test_a_job_that_cannot_start_says_why_and_the_summary_goes_on_with_markers(self, panel):
        ProgressWindow.fail = True
        panel._on_summarize_messages([audio(1)])
        assert panel.spoken == [errors.error_i18n_key(errors.BACKEND_ERROR)]
        assert Dialog.made[0].args.notice == "markers 1" and panel._summary_voice_run.running is False

    def test_a_window_that_raises_leaves_no_run_marked_as_running(self, panel, monkeypatch):
        def broken(*args):
            raise RuntimeError("synthetic")
        monkeypatch.setattr(voice, "TranscriptionProgressDialog", broken)
        with pytest.raises(RuntimeError):
            panel._on_summarize_messages([audio(1)])
        assert panel._summary_voice_run.running is False and panel._summary_voice_running() is False

    def test_leaving_the_chat_stops_only_a_run_started_from_it(self, panel):
        stopped = []
        from_chat = NS(running=True, follows_open_chat=True, cancel=lambda: stopped.append("chat"))
        from_list = NS(running=True, follows_open_chat=False, cancel=lambda: stopped.append("list"))
        panel._summary_voice_run = from_list
        panel.cancel_summary_voice()
        panel._summary_voice_run = from_chat
        panel.cancel_summary_voice()
        assert stopped == ["chat"]

    def test_quitting_cancels_the_run_and_hands_back_its_job(self, panel):
        job = object()
        stopped = []
        panel._summary_voice_run = NS(running=True, lock=threading.Lock(), current=job,
                                      cancel=lambda: stopped.append(True))
        assert panel._cancel_summary_voice_now() is job and stopped == [True]
        panel._summary_voice_run = None
        assert panel._cancel_summary_voice_now() is None


class TestClosingWithTheChat:
    def test_closing_ai_media_cancels_the_voice_step_of_the_open_chat(self):
        calls = []
        stub = NS(cancel_summary_voice=lambda: calls.append("voice"), _ai_dialog=None,
                  close_summary_consent=lambda locked_only=False: calls.append(("consent", locked_only)),
                  cancel_summary_voice_locked=lambda: calls.append("voice locked"))
        import ui.conversation_panel.ai_actions as ai_actions
        original = ai_actions.close_open_dialog
        ai_actions.close_open_dialog = lambda: calls.append("status")
        try:
            AIActionsMixin.close_ai_media(stub)
            AIActionsMixin.close_ai_media(stub, locked_only=True)
        finally:
            ai_actions.close_open_dialog = original
        # Leaving or closing: everything. Vault locking: only what a locked chat owns.
        assert calls == [("consent", False), "status", "voice", ("consent", True), "voice locked"]


class TestOneLocalTranscriptionAtATime:
    def test_a_bulk_run_refuses_the_voice_step_with_a_spoken_reason(self, panel):
        panel._bulk_transcription = NS(running=True)
        panel._on_summarize_messages([audio(1)])
        assert panel.spoken == ["ai_summary_voice_busy"]
        assert panel.transcribed == [] and Dialog.made == []

    def test_a_manual_run_without_the_progress_window_refuses_it_too(self, panel):
        panel.main_window._background_transcription = object()
        panel._on_summarize_messages([audio(1)])
        assert panel.spoken == ["ai_summary_voice_busy"] and Dialog.made == []

    def test_a_manual_run_waiting_its_turn_counts(self, panel):
        panel.main_window._waiting_transcriptions = [object()]
        panel._on_summarize_messages([audio(1)])
        assert panel.spoken == ["ai_summary_voice_busy"] and Dialog.made == []

    def test_a_summary_with_nothing_to_transcribe_is_not_blocked(self, panel):
        panel._bulk_transcription = NS(running=True)
        panel._on_summarize_messages([text(1), stored_voice(audio(2), "hola")])
        assert len(Dialog.made) == 1

    def test_a_bulk_transcription_refuses_while_the_summary_transcribes(self):
        spoken = []
        stub = NS(main_window=NS(i18n=NS(t=lambda key: key), output=lambda text, interrupt=False: spoken.append(text)),
                  _bulk_transcription_running=lambda: False, _summary_voice_running=lambda: True,
                  selected_messages={"a"}, conversation={"remoteJid": JID})
        BulkTranscriptionMixin._start_bulk_transcription(stub, inline=False)
        assert spoken == ["ai_summary_voice_running"]

    def test_a_manual_transcription_refuses_while_the_summary_transcribes(self, monkeypatch):
        from ui import transcription_flow
        spoken = []
        flow = NS(_hidden_by_vault=lambda: False, _msg={"messageType": "audioMessage"},
                  _main_window=NS(output=spoken.append), _i18n=NS(t=lambda key: key),
                  _panel=NS(_summary_voice_running=lambda: True),
                  _runs_without_dialog=lambda: pytest.fail("started a run"))
        transcription_flow.MessageTranscriptionFlow.start(flow)
        assert spoken == ["ai_summary_voice_running"]


class TestEndingTheVoiceStep:
    def test_nothing_is_said_when_the_app_is_closing(self, panel):
        panel.main_window._shutting_down = True
        run = NS(running=True, cancelled=NS(is_set=lambda: True), outcome=NS(texts={}), on_done=lambda o: pytest.fail("opened"))
        panel._finish_summary_voice(run, None)
        assert panel.spoken == []


class TestStoringAfterTheVault:
    def _stub(self, hidden):
        stored = []
        stub = NS(main_window=NS(is_chat_hidden_by_vault=lambda jid: hidden,
                                 store_message_transcription=lambda *a, **k: stored.append(a) or "stored"),
                  conversation=None)
        return stub, stored

    def test_a_chat_locked_in_the_meantime_is_not_stored(self):
        stub, stored = self._stub(hidden=True)
        BulkTranscriptionMixin._store_bulk_transcription(
            stub, JID, audio(1), NS(text="secreto", language=None, language_probability=None,
                                    model_id="small", backend="faster-whisper", vad_used=True))
        assert stored == []

    def test_a_visible_chat_is_stored(self):
        stub, stored = self._stub(hidden=False)
        BulkTranscriptionMixin._store_bulk_transcription(
            stub, JID, audio(1), NS(text="hola", language=None, language_probability=None,
                                    model_id="small", backend="faster-whisper", vad_used=True))
        assert len(stored) == 1


class TestProgressJob:
    def test_the_gauge_is_the_share_already_transcribed(self):
        assert [voice.voice_progress_tick(n, 4).percent for n in (1, 2, 3, 4)] == [0, 25, 50, 75]
        tick = voice.voice_progress_tick(1, 1)
        assert (tick.percent, tick.update_bar, tick.speak) == (0, True, False)

    def test_start_runs_the_work_on_its_thread_and_reports_its_sentence(self, wx):
        reported = []
        run = NS(cancel=lambda: None)
        job = voice._SummaryVoiceJob(lambda r, announce: ("stopped" if r is run else "?"), run, None,
                                     lambda result, error: reported.append((result, error)))
        job.start()
        assert reported == [("stopped", None)]

    def test_cancel_cancels_the_run(self):
        stopped = []
        voice._SummaryVoiceJob(None, NS(cancel=lambda: stopped.append(True)), None, None).cancel()
        assert stopped == [True]


class TestConsentComesFirst:
    def test_a_refused_online_voice_consent_transcribes_and_sends_nothing(self, panel):
        panel.voice_choice = VoiceChoice("online", "openai", "")
        panel.online_consent_answer = False
        panel._on_summarize_messages([text(1), audio(2)])
        assert panel.events == ["how to transcribe 1", "consent", "online voice consent"]
        assert ProgressWindow.made == [] and Dialog.made == [] and panel.transcribed == []
        assert panel.spoken == ["ai_summary_cancelled"]

    def test_none_asks_no_voice_consent_and_runs_no_worker(self, panel):
        panel.voice_choice = VoiceChoice("none")
        panel.online_consent_answer = False
        panel._on_summarize_messages([text(1), audio(2)])
        assert panel.events == ["how to transcribe 1", "consent", "result window"]
        assert ProgressWindow.made == [] and panel.transcribed == []

    def test_a_refusal_transcribes_nothing_sends_nothing_and_says_so(self, panel):
        panel.consent_answer = False
        panel._on_summarize_messages([text(1), audio(2)])
        assert panel.transcribed == [] and ProgressWindow.made == [] and Dialog.made == []
        assert panel.spoken == ["ai_summary_cancelled"]
        assert panel.events == ["how to transcribe 1", "consent"]

    def test_a_refusal_counts_without_voice_messages_too(self, panel):
        panel.consent_answer = False
        panel._on_summarize_messages([text(1), text(2)])
        assert Dialog.made == [] and panel.spoken == ["ai_summary_cancelled"]

    def test_the_question_is_asked_before_the_transcription_and_without_voice_too(self, panel):
        panel._on_summarize_messages([text(1), audio(2)])
        assert panel.events[:3] == ["how to transcribe 1", "consent", "progress window"]
        panel.events.clear()
        panel._on_summarize_messages([text(1)])
        assert panel.events == ["consent", "result window"]

    def test_the_result_window_is_told_not_to_ask_again(self, panel):
        panel._on_summarize_messages([audio(1)])
        assert Dialog.made[0].args.preconsented is True

    def test_it_asks_for_the_text_providers_with_a_saved_key(self, panel):
        panel._config = {"enabled": True, "order": ["gemini", "openai"], "disabled": []}
        panel._on_summarize_messages([text(1)])
        (asked,) = panel.consents
        assert asked.providers == ["gemini"] and asked.kind == "text" and asked.locked is False

    def test_a_locked_chat_asks_with_the_locked_flag_every_time(self, panel):
        panel.locked.add(JID)
        panel.unlocked = True
        panel._on_summarize_messages([text(1)])
        panel._on_summarize_messages([text(1)])
        assert [c.locked for c in panel.consents] == [True, True]
        assert all(d.args.locked and d.args.preconsented for d in Dialog.made)

    def test_the_answer_only_counts_while_the_chat_is_visible_and_the_app_runs(self, panel):
        panel._on_summarize_messages([text(1)])
        valid = panel.consents[0].valid
        assert valid() is True
        panel.main_window._shutting_down = True
        assert valid() is False
        panel.main_window._shutting_down = False
        panel.locked.add(JID)
        assert valid() is False


class TestVaultLockingDuringTheSummary:
    def _asking(self, panel, ended):
        dialog = NS(IsModal=lambda: True, EndModal=ended.append)
        panel._summary_consent_dialog, panel._summary_consent_jid = dialog, JID
        return dialog

    def test_the_consent_window_is_dismissed_for_a_locked_chat(self, panel, wx):
        wx.ID_CANCEL = 7
        ended = []
        self._asking(panel, ended)
        panel.locked.add(JID)
        panel.close_summary_consent(locked_only=True)
        assert ended == [7]

    def test_a_vault_lock_leaves_the_consent_of_an_ordinary_chat_alone(self, panel, wx):
        wx.ID_CANCEL = 7
        ended = []
        self._asking(panel, ended)
        panel.close_summary_consent(locked_only=True)
        assert ended == []
        panel.close_summary_consent()  # closing the app dismisses any
        assert ended == [7]

    def test_no_window_and_a_window_already_closing_are_harmless(self, panel, wx):
        panel.close_summary_consent()
        panel._summary_consent_dialog = NS(IsModal=lambda: False, EndModal=lambda code: pytest.fail("ended"))
        panel.close_summary_consent()

    def test_the_consent_window_is_tracked_for_the_helper(self, panel, monkeypatch):
        import ui.dialogs.ai_result_dialog as dialog_module
        seen = []

        def ask(parent, i18n, app, config, providers, kind, locked, valid=None, track=None):
            track("window")
            seen.append(parent._summary_consent_dialog)
            track(None)
            return True
        monkeypatch.setattr(dialog_module, "ask_consent", ask)
        panel._on_summarize_messages([text(1)])
        assert seen == ["window"] and panel._summary_consent_dialog is None

    def test_a_refusal_caused_by_the_vault_or_the_app_closing_says_nothing(self, panel):
        panel.consent_answer = False
        panel.locked.add(JID)  # hidden once the question ends
        panel._on_summarize_messages([text(1)])
        assert panel.spoken == []
        panel.locked.clear()
        panel.main_window._shutting_down = True
        panel._on_summarize_messages([text(1)])
        assert panel.spoken == []

    def test_a_refusal_by_the_person_is_said(self, panel):
        panel.consent_answer = False
        panel._on_summarize_messages([text(1)])
        assert panel.spoken == ["ai_summary_cancelled"]

    def test_the_vault_locking_cancels_the_run_of_a_locked_chat_only(self, panel):
        stopped = []
        panel._summary_voice_run = NS(running=True, jid=JID, cancel=lambda: stopped.append(True))
        panel.cancel_summary_voice_locked()
        assert stopped == []
        panel.locked.add(JID)
        panel.cancel_summary_voice_locked()
        assert stopped == [True]
        panel._summary_voice_run = NS(running=False, jid=JID, cancel=lambda: stopped.append("late"))
        panel.cancel_summary_voice_locked()
        assert stopped == [True]

    def test_a_vault_lock_cancel_ends_the_run_silently_and_drops_the_summary(self, panel):
        panel.locked.add(JID)
        panel.unlocked = True

        def lock_then_cancel(msg):
            panel.unlocked = False  # the vault closes...
            panel.cancel_summary_voice_locked()  # ...and cancels the run
            return None, errors.TranscriptionError(errors.CANCELLED, "x")
        panel.transcribe_with = lock_then_cancel
        panel._on_summarize_messages([audio(1)])
        assert Dialog.made == [] and panel.spoken == []
        assert panel._summary_voice_running() is False


class TestBusyBeforeConsent:
    def test_a_busy_transcription_with_audio_to_run_refuses_before_asking(self, panel):
        panel._bulk_transcription = NS(running=True)
        panel._on_summarize_messages([audio(1)])
        assert panel.spoken == ["ai_summary_voice_busy"] and panel.consents == []

    def test_a_busy_transcription_does_not_stop_a_summary_with_nothing_to_run(self, panel):
        panel._bulk_transcription = NS(running=True)
        panel._on_summarize_messages([text(1), stored_voice(audio(2), "hola")])
        assert len(panel.consents) == 1 and len(Dialog.made) == 1
        assert panel.spoken == []
