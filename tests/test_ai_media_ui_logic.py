"""Unbound wx methods against plain stubs: never construct windows or use keys/network."""
from types import SimpleNamespace

import pytest
import wx

from app_settings import AppSettings
from core.i18n import LANGUAGE_NAMES
from core.ai_credentials import CredentialStore
from core.ai_media import config as ai_config
from core.ai_media.payload import Media
from core.ai_media.session import MediaSession
from ui.dialogs.ai_result_dialog import AIResultDialog
from ui.dialogs.ai_settings_page import AIProviderDialog, AISettingsPage
from ui.conversation_panel.ai_actions import AIActionsMixin


def image(data=b"photo"):
    return Media("image", data, "image/jpeg", "image.jpg")


def answered(text, provider="gemini"):
    """What run_chain hands back for a successful action."""
    return image(), (text, provider)


class Control:
    def __init__(self, value="", selection=0):
        self.value = value
        self.selection = selection
        self.enabled = True
        self.shown = False

    def GetValue(self):
        return self.value

    def ChangeValue(self, value):
        self.value = value

    SetValue = ChangeValue
    SetLabel = ChangeValue

    def GetSelection(self):
        return self.selection

    def SetItems(self, items):
        self.items = items

    def SetSelection(self, selection):
        self.selection = selection

    def Freeze(self):
        self.frozen = True

    def Thaw(self):
        self.frozen = False

    def Enable(self, value):
        self.enabled = value

    def SetFocus(self):
        self.focused = True

    def Hide(self):
        self.shown = False

    def Show(self, shown=True):
        self.shown = shown


@pytest.mark.parametrize("modal_fails", [False, True])
def test_technical_help_is_readable_focused_and_destroyed_without_opening_a_window(monkeypatch, modal_fails):
    import ui.dialogs.ai_settings_page as module

    calls = []
    dialog = SimpleNamespace(
        SetSizer=lambda layout: calls.append("layout"),
        SetMinSize=lambda size: calls.append(size),
        Destroy=lambda: calls.append("destroy"),
    )
    text = Control()

    def show_modal():
        assert text.focused
        calls.append("modal")
        if modal_fails:
            raise RuntimeError("synthetic modal failure")

    dialog.ShowModal = show_modal
    page = SimpleNamespace(_t=lambda key: key)

    def make_dialog(parent, **kwargs):
        assert parent is page and kwargs["title"] == "ai_technical_info"
        return dialog

    def make_text(parent, **kwargs):
        assert parent is dialog
        assert kwargs["value"] == "ai_technical_notice"
        assert kwargs["name"] == "ai_technical_info"
        assert kwargs["style"] & wx.TE_READONLY
        assert kwargs["style"] & wx.TE_MULTILINE
        return text

    def make_close(parent, identifier, **kwargs):
        assert parent is dialog and identifier == wx.ID_CANCEL
        assert kwargs["label"] == "close"
        return object()

    fake_wx = SimpleNamespace(
        Dialog=make_dialog, TextCtrl=make_text, Button=make_close,
        BoxSizer=lambda orientation: SimpleNamespace(Add=lambda *args: None),
        **{name: getattr(wx, name) for name in (
            "DEFAULT_DIALOG_STYLE", "RESIZE_BORDER", "VERTICAL", "TE_MULTILINE",
            "TE_READONLY", "EXPAND", "ALL", "ID_CANCEL", "ALIGN_RIGHT")},
    )
    monkeypatch.setattr(module, "wx", fake_wx)
    if modal_fails:
        with pytest.raises(RuntimeError, match="synthetic modal failure"):
            AISettingsPage._technical_info(page, None)
    else:
        AISettingsPage._technical_info(page, None)
    assert calls[-2:] == ["modal", "destroy"]


class DialogStub:
    _complete = AIResultDialog._complete
    _close = AIResultDialog._close
    _dispose = AIResultDialog._dispose
    _finish_close = AIResultDialog._finish_close
    _cancel = AIResultDialog._cancel
    _timeout = AIResultDialog._timeout
    _key = AIResultDialog._key
    _valid = AIResultDialog._valid
    _busy = AIResultDialog._busy
    _show_error = AIResultDialog._show_error
    _speak = AIResultDialog._speak
    _chain = AIResultDialog._chain
    _start = AIResultDialog._start

    def __init__(self, *, locked=False, kind="image"):
        self.kind = kind
        self.mime = "image/jpeg"
        self._provider = None
        self.session = MediaSession("a", "c", "m", kind, locked=locked)
        self.question = Control("a question I am still writing")
        self.result = Control()
        self.status = Control()
        self.ask = Control()
        self.copy = Control()
        self.regenerate = Control()
        self.cancel = Control()
        self.spoken = []
        self.main_window = SimpleNamespace(_chat_lock_unlocked=True, _shutting_down=False,
                                           IsShown=lambda: True, output=self.spoken.append)
        self.i18n = SimpleNamespace(t=lambda key: key)
        self.config = {"read_answers": False, "order": ["gemini", "openai"], "disabled": [],
                       "consented": [], "models": {p: ai_config.PROVIDERS[p].model for p in ai_config.PROVIDERS},
                       "profile": "balanced", "kinds": {k: True for k in ai_config.KINDS}}
        self._latest = ""
        self._consented = False
        self._timer = None
        self._description_first = False
        self._processing = SimpleNamespace(start=lambda: None, stop=lambda: None)
        self._consent_dialog = None
        self.loader = lambda: b"private"
        self.closed = False

    def IsModal(self):
        return True

    def EndModal(self, code):
        self.closed = True


@pytest.fixture
def dialog(monkeypatch):
    import ui.dialogs.ai_result_dialog as module
    monkeypatch.setattr(module, "active_account_id", lambda: "a")
    return DialogStub()


def test_complete_updates_once_without_touching_editor_or_focus(dialog):
    generation, _ = dialog.session.begin("old question")
    dialog._complete(generation, "old question", False, (image(b"photo"), ("answer", "gemini")), None)
    assert dialog.result.value == "ai_history_question\nold question\n\nai_history_answer\nanswer"
    assert dialog.question.value == "a question I am still writing"
    assert not hasattr(dialog.question, "focused")
    assert dialog.spoken == ["ai_ready ai_answered_by"]
    assert dialog.session.media.data == b"photo"


def test_initial_description_omits_internal_prompt_but_keeps_provider_history(dialog):
    prompt = "Internal description instruction"
    generation, _ = dialog.session.begin(prompt)
    dialog._complete(generation, prompt, True, (image(b"photo"), ("A blue rectangle", "gemini")), None)
    assert dialog.result.value == "A blue rectangle"
    assert dialog.session.history == [("user", prompt), ("assistant", "A blue rectangle")]
    assert dialog._latest == "A blue rectangle" and dialog.spoken == ["ai_ready ai_answered_by"]


def test_followup_history_distinguishes_questions_and_answers(dialog):
    picture = image(b"photo")
    generation, _ = dialog.session.begin("Internal instruction")
    dialog._complete(generation, "Internal instruction", True, (picture, ("Three shapes", "gemini")), None)
    generation, _ = dialog.session.begin("What color is the circle?")
    dialog._complete(generation, "What color is the circle?", False, (picture, ("Red", "gemini")), None)
    assert dialog.result.value == ("Three shapes\n\nai_history_question\nWhat color is the circle?"
                                   "\n\nai_history_answer\nRed")
    assert dialog.session.history[0] == ("user", "Internal instruction")
    assert dialog.session.history[-2:] == [("user", "What color is the circle?"), ("assistant", "Red")]
    assert dialog._latest == "Red" and dialog.copy.enabled


def test_first_manual_question_is_not_hidden_as_an_internal_prompt(dialog):
    generation, _ = dialog.session.begin("Read the text")
    dialog._complete(generation, "Read the text", False, (image(b"photo"), ("DEMO", "gemini")), None)
    assert dialog.result.value == "ai_history_question\nRead the text\n\nai_history_answer\nDEMO"


def test_regeneration_replaces_old_transcript_without_exposing_new_prompt(dialog):
    generation, _ = dialog.session.begin("old question")
    dialog._complete(generation, "old question", False, (image(b"photo"), ("old answer", "gemini")), None)
    generation, _ = dialog.session.begin("Internal description instruction")
    dialog._complete(generation, "Internal description instruction", True,
                     (image(b"photo"), ("new description", "gemini")), None)
    assert dialog.result.value == "new description"
    assert dialog.session.history == [("user", "Internal description instruction"), ("assistant", "new description")]


@pytest.mark.parametrize("draft", ["Which shape?", "  Which shape? \n"])
def test_successful_question_clears_only_the_unchanged_submitted_editor(dialog, draft):
    dialog.question.value = draft
    generation, _ = dialog.session.begin("Which shape?")
    dialog._complete(generation, "Which shape?", False, (image(b"photo"), ("A circle", "gemini")), None)
    assert dialog.question.value == ""
    assert not hasattr(dialog.question, "focused")


def test_failed_question_keeps_draft_available_for_explicit_retry(dialog):
    dialog.question.value = "Which shape?"
    generation, _ = dialog.session.begin("Which shape?")
    dialog._complete(generation, "Which shape?", False, None, "ai_error_quota")
    assert dialog.question.value == "Which shape?" and dialog.result.value == ""


def test_cancelled_question_keeps_draft_and_ignores_late_answer(dialog):
    dialog.question.value = "Which shape?"
    generation, _ = dialog.session.begin("Which shape?")
    dialog._cancel()
    dialog._complete(generation, "Which shape?", False, (image(b"photo"), ("late answer", "gemini")), None)
    assert dialog.question.value == "Which shape?" and dialog.result.value == ""
    assert not dialog.spoken


def test_regeneration_never_clears_a_question_draft_even_if_it_matches_prompt(dialog):
    prompt = "Internal description instruction"
    dialog.question.value = prompt
    generation, _ = dialog.session.begin(prompt)
    dialog._complete(generation, prompt, True, (image(b"photo"), ("description", "gemini")), None)
    assert dialog.question.value == prompt and not hasattr(dialog.question, "focused")


@pytest.mark.parametrize("invalid", ["close", "cancel", "lock", "account", "shutdown"])
def test_late_answers_never_resurrect_private_results(dialog, monkeypatch, invalid):
    generation, _ = dialog.session.begin("question")
    if invalid == "close":
        dialog._close()
    elif invalid == "cancel":
        dialog._cancel()
    elif invalid == "lock":
        dialog.session.locked = True
        dialog.main_window._chat_lock_unlocked = False
    elif invalid == "shutdown":
        dialog.main_window._shutting_down = True
    else:
        import ui.dialogs.ai_result_dialog as module
        monkeypatch.setattr(module, "active_account_id", lambda: "different-account")
    dialog._complete(generation, "question", False, (image(b"private"), ("secret answer", "gemini")), None)
    assert dialog.result.value == "" and dialog.spoken == []
    assert dialog.session.media is None


def test_answer_queued_after_deadline_cannot_be_published(dialog):
    generation, token = dialog.session.begin("question")
    token.clock = lambda: token.deadline + 1
    dialog._complete(generation, "question", False, (image(b"photo"), ("late answer", "gemini")), None)
    assert dialog.spoken == ["ai_error_timeout"] and dialog.result.value == ""
    assert dialog.status.value == "ai_error_timeout" and dialog.ask.enabled


@pytest.mark.parametrize("locked,remembered,expected", [(False, True, 0), (True, True, 1), (False, False, 1)])
def test_locked_media_requires_fresh_consent_even_if_provider_was_remembered(dialog, monkeypatch,
                                                                          locked, remembered, expected):
    import ui.dialogs.ai_result_dialog as module
    opened = []
    class ConsentStub:
        def __init__(self, *args):
            opened.append(args[2])
            self.remember = Control(False)
        def ShowModal(self):
            return wx.ID_CANCEL
        def Destroy(self):
            pass
    monkeypatch.setattr(module, "AIConsentDialog", ConsentStub)
    dialog.session.locked = locked
    dialog.config.update(consented=["openai"] if remembered else [])
    dialog._consented = False
    AIResultDialog._consent(dialog, ["openai"])
    assert len(opened) == expected
    assert all(providers == ["openai"] for providers in opened)


def test_consent_covers_every_provider_that_may_receive_the_media_and_is_remembered_for_all(dialog, monkeypatch):
    import ui.dialogs.ai_result_dialog as module
    shown, saved = [], []
    class ConsentStub:
        def __init__(self, parent, i18n, providers, kind, locked):
            shown.append((list(providers), kind, locked))
            self.remember = Control(True)
        def ShowModal(self):
            return wx.ID_OK
        def Destroy(self):
            pass
    monkeypatch.setattr(module, "AIConsentDialog", ConsentStub)
    dialog.app = SimpleNamespace(update=lambda key, merge: saved.append((key, merge({"consented": ["openai"]}))))
    dialog.config["consented"] = ["openai"]
    assert AIResultDialog._consent(dialog, ["openai", "gemini", "groq"]) is True
    assert shown == [(["openai", "gemini", "groq"], "image", False)]
    assert saved == [("ai_media", {"consented": ["gemini", "groq", "openai"]})]
    assert dialog.config["consented"] == ["gemini", "groq", "openai"]


def test_already_consented_providers_ask_nothing(dialog, monkeypatch):
    import ui.dialogs.ai_result_dialog as module
    monkeypatch.setattr(module, "AIConsentDialog", lambda *args: pytest.fail("asked again"))
    dialog.config["consented"] = ["gemini", "openai"]
    assert AIResultDialog._consent(dialog, ["gemini", "openai"]) is True


def test_closing_also_dismisses_nested_consent_and_clears_session(dialog):
    dismissed = []
    dialog._consent_dialog = SimpleNamespace(IsModal=lambda: True, EndModal=dismissed.append)
    generation, _ = dialog.session.begin("private question")
    dialog.session.accept(generation, "private question", "private answer")
    dialog._latest = "private answer"
    dialog._close()
    assert dismissed == [wx.ID_CANCEL]
    # EndModal on the child only requests exit: its ShowModal has not
    # returned yet, so the outer modal loop must not be ended here.
    assert not dialog.closed and dialog.session.closed
    assert dialog.session.history == [] and dialog._latest == "" and dialog.loader is None


@pytest.mark.parametrize("return_code", [wx.ID_CANCEL, wx.ID_OK])
def test_vault_close_unwinds_consent_before_ending_outer_modal(dialog, monkeypatch, return_code):
    import ui.dialogs.ai_result_dialog as module
    events = []
    dialog.config.update(consented=[])
    dialog._consented = False
    dialog.session.locked = True
    dialog.result.value = "private answer"

    class ConsentStub:
        remember = Control(True)
        def __init__(self, *args):
            self.ending = False
        def ShowModal(self):
            events.append("child-loop")
            dialog.main_window._chat_lock_unlocked = False
            dialog._close()
            assert dialog.session.closed and dialog.result.value == ""
            assert not dialog.closed  # No premature exit of the parent loop.
            # A second shutdown/account callback during unwinding is harmless.
            dialog._close()
            return return_code
        def IsModal(self):
            return not self.ending
        def EndModal(self, code):
            assert code == wx.ID_CANCEL
            self.ending = True
            events.append("child-exit-requested")
        def Destroy(self):
            events.append("child-destroyed")

    def end_parent(code):
        if dialog._consent_dialog is not None:
            raise RuntimeError("outer loop is not the active wx modal loop")
        assert code == wx.ID_CANCEL
        dialog.closed = True
        events.append("parent-exit")
    dialog.EndModal = end_parent
    monkeypatch.setattr(module, "AIConsentDialog", ConsentStub)
    assert AIResultDialog._consent(dialog, ["gemini"]) is False
    assert events == ["child-loop", "child-exit-requested", "child-destroyed", "parent-exit"]
    assert dialog.closed and dialog.session.closed and not dialog._consented


def test_close_during_consent_never_dispatches_the_job(dialog, monkeypatch):
    module = _prepare_start(dialog, monkeypatch)
    dialog._consented = False
    dialog.config["consented"] = []
    dialog._consent = lambda providers: AIResultDialog._consent(dialog, providers)
    dispatched = []
    class ConsentStub:
        remember = Control(False)
        def __init__(self, *args):
            self.ending = False
        def ShowModal(self):
            dialog._close()
            return wx.ID_OK  # Even a queued OK cannot resurrect the session.
        def IsModal(self):
            return not self.ending
        def EndModal(self, code):
            self.ending = True
        def Destroy(self):
            pass
    monkeypatch.setattr(module, "AIConsentDialog", ConsentStub)
    monkeypatch.setattr(module, "submit", lambda *args: dispatched.append(True))
    dialog._start("describe", regenerate=True)
    assert dialog.closed and dialog.session.closed
    assert dialog._timer is None and dialog.session.active is None
    assert not dispatched and not dialog.spoken and dialog.session.requests == 0


def test_error_has_safe_readable_status_and_announces_error_without_answer(dialog):
    generation, _ = dialog.session.begin("question")
    dialog._complete(generation, "question", False, None, "ai_error_quota")
    assert dialog.status.value == "ai_error_quota"
    assert dialog.session.active is None and dialog.ask.enabled
    assert dialog.spoken == ["ai_error_quota"]


def test_watchdog_announces_timeout_even_when_no_worker_result_arrives(dialog):
    generation, _ = dialog.session.begin("question")
    dialog._busy(True)
    dialog._timeout(generation)
    assert dialog.session.active is None and dialog.ask.enabled
    assert dialog.status.value == "ai_error_timeout"
    assert dialog.spoken == ["ai_error_timeout"]


def _prepare_start(dialog, monkeypatch):
    import ui.dialogs.ai_result_dialog as module
    dialog.config.update(profile="detailed")
    dialog._consent = lambda providers: True
    dialog.i18n.language = "tr-TR"
    dialog.loader = lambda token: b"synthetic photo"
    monkeypatch.setattr(module, "CredentialStore", lambda path: SimpleNamespace(
        saved=lambda: {"gemini", "openai"}, get=lambda provider: "fake-key"))
    monkeypatch.setattr(module, "prepare_media", lambda kind, data, mime, profile: image(data))
    class TimerStub:
        def __init__(self, milliseconds, callback, generation):
            self.milliseconds, self.callback, self.generation = milliseconds, callback, generation
            self.stopped = False
        def Stop(self):
            self.stopped = True
    monkeypatch.setattr(module.wx, "CallLater", TimerStub)
    return module


@pytest.mark.parametrize("language", list(LANGUAGE_NAMES))
@pytest.mark.parametrize("kind", ["image", "audio", "pdf"])
@pytest.mark.parametrize("regenerate", [False, True])
def test_requests_follow_current_application_language(dialog, monkeypatch, language, kind, regenerate):
    from core.i18n import I18n
    from core.ai_media.prompts import first_question
    module = _prepare_start(dialog, monkeypatch)
    dialog.kind = kind
    dialog.session = MediaSession("a", "c", "m", kind)
    # As in the app: the dialog holds the window's own I18n. The window showed
    # Turkish and has applied `language` since (the Settings dialog's OK, a
    # switch that was pending) -- _start must ask for what it shows now.
    dialog.main_window.settings = {"general": {"language": "tr-TR"}}
    dialog.main_window.i18n = dialog.i18n = I18n(dialog.main_window)
    dialog.i18n.get_language()
    dialog.main_window.settings["general"]["language"] = language
    dialog.i18n.get_language()
    sent = []
    def chain(operation, providers, models, key_for, media, history, question, instruction, profile, **kwargs):
        sent.append((list(providers), question, instruction))
        return "synthetic answer", providers[0]
    monkeypatch.setattr(module, "run_chain", chain)
    monkeypatch.setattr(module, "submit", lambda work, complete: work())
    monkeypatch.setattr(module, "prepare_media", lambda kind, data, mime, profile: Media(kind, data, "x/y", "f"))
    question = first_question(kind) if regenerate else "Question in another language"
    dialog._start(question, regenerate=regenerate)
    assert len(sent) == 1
    providers, asked, instruction = sent[0]
    assert providers == ["gemini", "openai"]  # the person's order, both with a saved key
    assert asked == question
    assert (f"Answer in language {language}" in instruction) or (kind == "audio" and "language that is spoken" in instruction)


@pytest.mark.parametrize("regenerate, held_by", [
    # Ctrl+Shift+I with consent remembered: __init__ queues the first request
    # before ShowModal(), so it runs ahead of the switch the main window
    # queues on losing the focus to this dialog.
    (True, "_main_window_active"),
    # Ask during a call: the switch waits for the call window to close.
    (False, "voice_call_window"),
])
def test_a_request_leaves_the_windows_pending_language_switch_to_the_window(dialog, monkeypatch, regenerate, held_by):
    """self.i18n is the window's own I18n, and _start asked it get_language()
    for the answer's language. On that instance the call reads the settings,
    where a language chosen in another account was waiting for the window to
    be allowed to repaint: it was applied right there, with nothing repainted,
    and MainWindow._apply_pending_language_switch() then found it "already
    applied" -- a window in two languages until the next start."""
    from unittest.mock import Mock
    from core.i18n import I18n
    from core.ai_media.prompts import first_question
    from main import MainWindow
    module = _prepare_start(dialog, monkeypatch)
    window = dialog.main_window
    window.settings = {"general": {"language": "pt-BR"}}
    window.i18n = dialog.i18n = I18n(window)
    window.i18n.get_language()
    window.apply_language_changes = Mock()
    window._incoming_call_dialogs = {}
    window.voice_call_window = Mock(IsShown=Mock(return_value=held_by == "voice_call_window"))
    window._main_window_active = held_by == "_main_window_active"
    window._call_window_on_screen = lambda: MainWindow._call_window_on_screen(window)
    window.settings["general"]["language"] = "en-US"  # pulled from another account
    window._pending_language_switch = True
    MainWindow._apply_pending_language_switch(window)
    assert window._pending_language_switch is True  # held
    sent = []
    def chain(operation, providers, models, key_for, media, history, question, instruction, profile, **kwargs):
        sent.append(instruction)
        return "synthetic answer", providers[0]
    monkeypatch.setattr(module, "run_chain", chain)
    monkeypatch.setattr(module, "submit", lambda work, complete: work())

    dialog._start(first_question("image") if regenerate else "What is on the table?", regenerate=regenerate)

    # Answered in the language the dialog and the window are still in.
    assert len(sent) == 1 and "Answer in language pt-BR" in sent[0]
    assert window.i18n.language == "pt-BR"

    window._main_window_active = False
    window.voice_call_window.IsShown.return_value = False
    MainWindow._apply_pending_language_switch(window)

    assert window.i18n.language == "en-US"
    window.apply_language_changes.assert_called_once()


def test_only_providers_with_a_key_and_the_kind_are_tried_in_the_persons_order(dialog, monkeypatch):
    module = _prepare_start(dialog, monkeypatch)
    dialog.config.update(order=["groq", "claude", "openai", "gemini"], disabled=["openai"])
    monkeypatch.setattr(module, "CredentialStore", lambda path: SimpleNamespace(
        saved=lambda: {"groq", "claude", "openai", "gemini"}, get=lambda provider: "key"))
    chains = []
    monkeypatch.setattr(module, "submit", lambda *args: None)
    original = AIResultDialog._chain
    monkeypatch.setattr(DialogStub, "_chain", lambda self, store: chains.append(original(self, store)) or chains[-1])
    dialog.kind = "video"
    dialog.session = MediaSession("a", "c", "m", "video")
    dialog._start("Describe this.", regenerate=True)
    assert chains == [["groq", "claude", "gemini"]]  # a video is described from a picture; OpenAI is off


def test_no_usable_provider_is_a_clear_error_before_any_consent_or_job(dialog, monkeypatch):
    module = _prepare_start(dialog, monkeypatch)
    monkeypatch.setattr(module, "CredentialStore", lambda path: SimpleNamespace(saved=lambda: set(), get=lambda p: ""))
    monkeypatch.setattr(module, "submit", lambda *args: pytest.fail("dispatched"))
    dialog._consent = lambda providers: pytest.fail("asked consent")
    dialog._start("Describe this.", regenerate=True)
    assert dialog.status.value == "ai_error_providers" and dialog.spoken == ["ai_error_providers"]
    assert dialog.session.active is None and dialog.session.requests == 0


def test_a_failed_chain_lists_each_provider_and_its_reason(dialog):
    from core.ai_media.service import ChainFailed
    error = ChainFailed([("gemini", "quota"), ("openai", "authentication")]).key
    generation, _ = dialog.session.begin("question")
    dialog._complete(generation, "question", False, None, error)
    lines = dialog.status.value.split("\n")
    assert lines[0] == "ai_chain_failed" and len(lines) == 3
    assert dialog.spoken == [dialog.status.value]


def test_a_single_failure_stays_one_plain_sentence(dialog):
    from core.ai_media.service import ChainFailed
    generation, _ = dialog.session.begin("question")
    dialog._complete(generation, "question", False, None, ChainFailed([("gemini", "quota")]).key)
    assert dialog.status.value == "ai_error_quota"


def test_processing_sound_starts_before_the_job_is_dispatched(dialog, monkeypatch):
    module = _prepare_start(dialog, monkeypatch)
    events = []
    dialog._processing = SimpleNamespace(start=lambda: events.append("sound-start"), stop=lambda: events.append("sound-stop"))
    monkeypatch.setattr(module, "submit", lambda *args: events.append("dispatch"))
    dialog._start("question")
    assert events == ["sound-start", "dispatch"]


@pytest.mark.parametrize("error", [None, "ai_error_quota"])
def test_processing_sound_stops_before_answer_or_error_is_spoken(dialog, error):
    events = []
    dialog.config["read_answers"] = True
    dialog._processing = SimpleNamespace(start=lambda: None, stop=lambda: events.append("sound-stop"))
    dialog.main_window.output = lambda text: events.append(("speak", text))
    generation, _ = dialog.session.begin("question")
    result = (image(b"photo"), ("answer", "gemini")) if error is None else None
    dialog._complete(generation, "question", False, result, error)
    assert events == ["sound-stop", ("speak", error or "answer")]


@pytest.mark.parametrize("ending", ["cancel", "close", "timeout"])
def test_processing_sound_stops_on_every_session_ending(dialog, ending):
    events = []
    dialog._processing = SimpleNamespace(start=lambda: None, stop=lambda: events.append("sound-stop"))
    generation, _ = dialog.session.begin("question")
    if ending == "timeout":
        dialog._timeout(generation)
    else:
        getattr(dialog, "_" + ending)()
    assert events == ["sound-stop"]
    assert dialog.session.active is None


def test_late_result_does_not_stop_a_newer_requests_processing_sound(dialog):
    events = []
    dialog._processing = SimpleNamespace(start=lambda: events.append("sound-start"), stop=lambda: events.append("sound-stop"))
    old, _ = dialog.session.begin("old")
    dialog._cancel()
    dialog.session.begin("new")
    dialog._busy(True)
    events.clear()
    dialog._complete(old, "old", False, (image(b"photo"), ("old answer", "gemini")), None)
    assert not events and dialog.session.active is not None and not dialog.spoken


def test_start_arms_watchdog_before_dispatch_and_recovers_without_completion(dialog, monkeypatch):
    module = _prepare_start(dialog, monkeypatch)
    queued = []
    def dispatch(work, complete):
        assert dialog._timer is not None
        queued.append((work, complete))
    monkeypatch.setattr(module, "submit", dispatch)
    dialog._start("question")
    assert len(queued) == 1 and dialog.status.value == "ai_question_loading"
    timer = dialog._timer
    assert timer.milliseconds > dialog.session.active.total_seconds * 1000  # outlasts every attempt
    timer.callback(timer.generation)
    assert timer.stopped and dialog._timer is None
    assert dialog.session.active is None and dialog.ask.enabled
    assert dialog.spoken == ["ai_question_loading", "ai_error_timeout"]


def test_real_worker_error_reaches_gui_queue_and_is_announced(dialog, monkeypatch):
    from core.ai_media import service
    from core.ai_media.errors import DescriptionError
    module = _prepare_start(dialog, monkeypatch)
    future = []
    callbacks = []
    def request(*args, **kwargs):
        raise DescriptionError("quota")
    def dispatch(work, complete):
        future.append(service.submit(work, complete))
    monkeypatch.setattr(module, "run_chain", request)
    monkeypatch.setattr(module, "submit", dispatch)
    monkeypatch.setattr(module.wx, "CallAfter", lambda callback, *args: callbacks.append((callback, args)))
    dialog._start("question")
    future[0].result(timeout=5)
    assert dialog.status.value == "ai_question_loading" and len(callbacks) == 1
    callback, args = callbacks.pop()
    callback(*args)
    assert dialog.status.value == "ai_error_quota" and dialog.ask.enabled
    assert dialog.spoken == ["ai_question_loading", "ai_error_quota"]
    assert dialog.session.active is None and dialog._timer is None


def test_dispatch_failure_clears_busy_state_and_watchdog(dialog, monkeypatch):
    module = _prepare_start(dialog, monkeypatch)
    def dispatch(*args):
        raise RuntimeError("private failure text must never be shown")
    monkeypatch.setattr(module, "submit", dispatch)
    dialog._start("question")
    assert dialog.status.value == "ai_error_response"
    assert dialog.spoken == ["ai_question_loading", "ai_error_response"]
    assert dialog.ask.enabled and dialog.session.active is None and dialog._timer is None


@pytest.mark.parametrize("regenerate,expected", [
    (True, "ai_processing_msg"),
    (False, "ai_question_loading"),
])
def test_waiting_status_names_the_requested_operation_without_moving_focus(dialog, monkeypatch,
                                                                         regenerate, expected):
    module = _prepare_start(dialog, monkeypatch)
    queued = []
    monkeypatch.setattr(module, "submit", lambda *args: queued.append(args))
    dialog._start("synthetic question", regenerate=regenerate)
    assert dialog.status.value == expected and len(queued) == 1
    assert not dialog.ask.enabled and not dialog.regenerate.enabled and dialog.cancel.enabled
    assert dialog.question.value == "a question I am still writing"
    assert not hasattr(dialog.question, "focused")
    assert dialog.spoken == [expected]  # one spoken start; the sound cue and status carry the wait


@pytest.mark.parametrize("error,expected", [(None, "ai_connection_ok"), ("ai_error_authentication", "ai_error_authentication")])
def test_connection_probe_has_its_own_waiting_status_and_ignores_stale_callbacks(monkeypatch, error, expected):
    import ui.dialogs.ai_settings_page as module
    queued = []
    cancelled = []
    window = SimpleNamespace(_probe=None, _probe_generation=0, _alive=True, _provider="gemini",
                             model=Control("gemini-3.8-flash"), _current_key=lambda: "synthetic-key",
                             status=Control(), _t=lambda key: key,
                             _cancel_model_list=lambda: cancelled.append(True))
    window._tested = lambda generation, error: AIProviderDialog._tested(window, generation, error)
    monkeypatch.setattr(module, "submit", lambda *args: queued.append(args))
    AIProviderDialog._test(window, None)
    assert cancelled == [True]
    assert window.status.value == "ai_connection_loading" and len(queued) == 1
    window._tested(window._probe_generation - 1, "ai_error_network")
    assert window.status.value == "ai_connection_loading"
    window._tested(window._probe_generation, error)
    assert window.status.value == expected
    window._alive = False
    window._tested(window._probe_generation, "ai_error_network")
    assert window.status.value == expected


def test_settings_guidance_has_a_distinct_readable_name_and_keeps_profile_selection():
    class NamedControl(Control):
        def SetName(self, value):
            self.name = value
        def SetItems(self, items):
            self.items = items
            self.selection = -1
        def SetSelection(self, value):
            self.selection = value
        def GetCount(self):
            return 0
    from ui.dialogs.ai_auto_options import AIAutoOptionsPanel
    page = SimpleNamespace(_labels=[], _t=lambda key: key, _plain=lambda key: key,
                           _profile_labels=lambda: ["quick", "balanced", "detailed"],
                           _scope_labels=lambda: ["all", "selected"],
                           _age_labels=lambda: ["5", "15", "30", "60"],
                           profile=NamedControl(selection=2), providers=NamedControl(selection=-1),
                           notice=NamedControl(), status=NamedControl(),
                           auto_dialog=SimpleNamespace(SetTitle=lambda title: setattr(page, "title", title)),
                           kinds_dialog=SimpleNamespace(
                               SetTitle=lambda title: setattr(page, "kinds_title", title)),
                           _refresh_list=lambda selection: None)
    # The automatic options live in their own window (ai_auto_options.py).
    panel = SimpleNamespace(_page=page, transcribe=NamedControl(), describe=NamedControl(),
                            scope_label=NamedControl(), scope=NamedControl(selection=1),
                            max_age_label=NamedControl(), max_age=NamedControl(selection=2),
                            limit_label=NamedControl(), limit=NamedControl(),
                            used_label=NamedControl(), used=NamedControl())
    panel.refresh_labels = lambda: AIAutoOptionsPanel.refresh_labels(panel)
    page.auto_panel = panel
    # So do the kinds of media (ai_kinds_options.py).
    from ui.dialogs.ai_kinds_options import AIKindsPanel
    kinds = SimpleNamespace(_page=page, _keys={"image": "ai_describe_images_label"},
                            toggles={"image": NamedControl()})
    kinds.refresh_labels = lambda: AIKindsPanel.refresh_labels(kinds)
    page.kinds_panel = kinds
    AISettingsPage.refresh_labels(page)
    assert kinds.toggles["image"].value == "ai_describe_images_label"
    assert page.kinds_title == "ai_kinds_button"
    assert page.notice.name == "ai_settings_help" and page.notice.value == "ai_settings_notice"
    assert page.status.name == "ai_status_label" and page.profile.selection == 2
    assert page.profile.items == ["quick", "balanced", "detailed"]
    assert panel.scope.items == ["all", "selected"] and panel.scope.selection == 1
    assert panel.max_age.items == ["5", "15", "30", "60"] and panel.max_age.selection == 2
    assert panel.limit.name == "ai_auto_daily_limit" and panel.transcribe.value == "ai_auto_transcribe"
    assert page.title == "ai_auto_button"
    assert page.providers.name == "ai_provider_list_label"



def test_ctrl_enter_asks_only_from_question_editor(dialog, monkeypatch):
    calls, skipped = [], []
    dialog._ask = lambda: calls.append("ask")
    event = SimpleNamespace(GetKeyCode=lambda: wx.WXK_RETURN, ControlDown=lambda: True,
                            Skip=lambda: skipped.append(True))
    monkeypatch.setattr(wx.Window, "FindFocus", lambda: dialog.question)
    dialog._key(event)
    assert calls == ["ask"] and not skipped
    event.ControlDown = lambda: False
    dialog._key(event)
    assert skipped == [True] and calls == ["ask"]
    event.ControlDown = lambda: True
    monkeypatch.setattr(wx.Window, "FindFocus", lambda: dialog.result)
    dialog._key(event)
    assert skipped == [True, True] and calls == ["ask"]


def test_escape_releases_private_state_and_stops_timer(dialog):
    stopped = []
    dialog._timer = SimpleNamespace(Stop=lambda: stopped.append(True))
    event = SimpleNamespace(GetKeyCode=lambda: wx.WXK_ESCAPE, Skip=lambda: None)
    dialog.result.value = "private answer"
    dialog._latest = "private answer"
    dialog._key(event)
    assert dialog.closed and dialog.result.value == "" and dialog.question.value == ""
    assert stopped == [True] and dialog._timer is None


class PageStub:
    """AISettingsPage's state-handling methods over plain controls."""
    _translation = None
    apply = AISettingsPage.apply
    _has_key = AISettingsPage._has_key
    _row = AISettingsPage._row
    _selected = AISettingsPage._selected
    _move = AISettingsPage._move
    _configure = AISettingsPage._configure
    _reset_keys = AISettingsPage._reset_keys
    _destroyed = AISettingsPage._destroyed

    def __init__(self, directory):
        self.store = CredentialStore(directory)
        self.app = AppSettings(str(directory))
        config = ai_config.preferences(self.app)
        self._order = list(config["order"])
        self._disabled = set()
        self._models = dict(config["models"])
        self._auto = set(config["auto_models"])
        self._voice_models = dict(config["transcribe_models"])
        self._drafts = {}
        self._deleted = set()
        self._reset = False
        self._saved = self.store.saved()
        self._alive = True
        self.enabled = Control(True)
        self.toggles = {kind: Control(True) for kind in ai_config.KINDS}
        self.profile = Control(selection=1)
        self.read_answers = Control(False)
        self.hide_action_button = Control(False)
        self.auto_transcribe = Control(False)
        self.auto_describe = Control(False)
        self.auto_scope = Control(selection=0)
        self.auto_max_age = Control(selection=0)
        self.auto_limit = Control(50)
        self._auto_consent = set()
        self.status = Control()
        self.providers = Control(selection=0)
        self.refreshed = []
        self.dirty = []
        self._on_change = lambda: self.dirty.append(True)
        self.main_window = SimpleNamespace(i18n=SimpleNamespace(t=lambda key: key), settings={})
        self.GetTopLevelParent = lambda: None

    def _t(self, key):
        return key

    def _refresh_list(self, selection=-1):
        self.refreshed.append(selection)


def test_apply_saves_order_switches_models_and_kinds_but_never_a_key_in_the_settings_file(tmp_path):
    page = PageStub(tmp_path)
    page._order = ["groq", "gemini", "openai", "claude", "openrouter"]
    page._disabled = {"claude"}
    page._models["openai"] = "gpt-4o"
    page._auto.discard("openai")
    page._drafts = {"gemini": "gemini-secret"}
    page.toggles["audio"].value = False
    page.profile.selection = 2
    assert page.apply()
    saved = page.app.get("ai_media")
    assert saved["order"][:3] == ["groq", "gemini", "openai"] and saved["disabled"] == ["claude"]
    assert saved["models"]["openai"] == "gpt-4o" and saved["models"]["groq"] == ""  # pinned vs automatic
    assert saved["kinds"]["audio"] is False
    assert saved["profile"] == "detailed" and saved["enabled"] is True and saved["read_answers"] is False
    assert "secret" not in str(saved) and page.store.get("gemini") == "gemini-secret"
    assert page._drafts == {} and "gemini" in page._saved


def test_every_apply_resumes_a_paused_automatic_translation(tmp_path):
    """A key re-entered in a provider's window, without opening the
    translation dialog, is the usual fix for what paused it."""
    page = PageStub(tmp_path)
    resumed = []
    page.main_window.conversations_panel = SimpleNamespace(
        resume_automatic_translation=lambda: resumed.append(True))
    page._drafts = {"gemini": "new-key"}
    assert page.apply()
    assert resumed == [True] and page._translation is None


def test_blank_draft_on_apply_preserves_saved_key_and_consent(tmp_path):
    page = PageStub(tmp_path)
    page.store.set("openai", "saved-key")
    page.app.set("ai_media", {"consented": ["openai"]})
    assert page.apply()
    assert page.store.get("openai") == "saved-key"
    assert page.app.get("ai_media")["consented"] == ["openai"]


def test_explicit_delete_revokes_only_this_providers_consent(tmp_path):
    page = PageStub(tmp_path)
    page.store.set("openai", "one")
    page.store.set("gemini", "two")
    page.app.set("ai_media", {"consented": ["openai", "gemini"]})
    page._deleted.add("openai")
    assert page.apply()
    assert not page.store.get("openai") and page.store.get("gemini") == "two"
    assert page.app.get("ai_media")["consented"] == ["gemini"]


@pytest.mark.parametrize("ending", ["apply", "cancel"])
def test_key_removal_is_staged_until_apply(tmp_path, ending):
    page = PageStub(tmp_path)
    page.store.set("openai", "synthetic-key")
    page._saved = page.store.saved()
    assert page._has_key("openai")
    page._deleted.add("openai")
    assert not page._has_key("openai") and page.store.get("openai") == "synthetic-key"
    if ending == "apply":
        assert page.apply()
        assert not page.store.get("openai") and not page._has_key("openai")
    else:
        page._destroyed(SimpleNamespace(GetEventObject=lambda: page, Skip=lambda: None))
        assert CredentialStore(tmp_path).get("openai") == "synthetic-key"
        assert page._drafts == {}


def test_reset_recovers_corruption_and_can_save_new_key(tmp_path, monkeypatch):
    import ui.dialogs.ai_settings_page as module
    page = PageStub(tmp_path)
    page.store.set("openai", "old")
    page.store.key_path.unlink()
    monkeypatch.setattr(module.wx, "MessageBox", lambda *args: wx.YES)
    page._reset_keys(None)
    assert page._reset and page._deleted == set(ai_config.PROVIDERS) and page.dirty == [True]
    assert not any(page._has_key(p) for p in ai_config.PROVIDERS)
    page._drafts["openai"] = "new"
    assert page.apply()
    assert page.store.get("openai") == "new" and not page._reset


def test_declining_the_reset_confirmation_changes_nothing(tmp_path, monkeypatch):
    import ui.dialogs.ai_settings_page as module
    page = PageStub(tmp_path)
    monkeypatch.setattr(module.wx, "MessageBox", lambda *args: wx.NO)
    page._reset_keys(None)
    assert not page._reset and not page._deleted and not page.dirty


@pytest.mark.parametrize("provider", list(ai_config.PROVIDERS))
@pytest.mark.parametrize("ending", ["apply", "cancel"])
@pytest.mark.parametrize("model_mode", ["automatic", "pinned"])
def test_reset_then_configure_new_key_revokes_old_consent_only_on_apply(
        tmp_path, monkeypatch, dialog, provider, ending, model_mode):
    import ui.dialogs.ai_settings_page as settings_module
    import ui.dialogs.ai_result_dialog as result_module
    old_consents = list(ai_config.PROVIDERS)
    old_preferences = {"consented": old_consents, "models": {provider: "synthetic-old-model"}}
    AppSettings(str(tmp_path)).set("ai_media", old_preferences)
    page = PageStub(tmp_path)
    page.store.set(provider, "synthetic-old-key")
    page.providers.selection = page._order.index(provider)
    monkeypatch.setattr(settings_module.wx, "MessageBox", lambda *args: wx.YES)
    new_model = "" if model_mode == "automatic" else "synthetic-new-model"

    class ProviderStub:
        def __init__(self, *args):
            pass
        def ShowModal(self):
            return wx.ID_OK
        def values(self):
            return {"key": "synthetic-new-key", "deleted": False,
                    "model": new_model, "enabled": True}
        def Destroy(self):
            pass

    monkeypatch.setattr(settings_module, "AIProviderDialog", ProviderStub)
    page._reset_keys(None)
    page._configure(None)
    assert page.store.get(provider) == "synthetic-old-key"
    assert page.app.get("ai_media") == old_preferences
    if ending == "cancel":
        page._destroyed(SimpleNamespace(GetEventObject=lambda: page, Skip=lambda: None))
        assert page.store.get(provider) == "synthetic-old-key"
        assert page.app.get("ai_media") == old_preferences
        return
    assert page.apply()
    assert page.store.get(provider) == "synthetic-new-key"
    assert page.app.get("ai_media")["consented"] == []
    assert page.app.get("ai_media")["models"][provider] == new_model
    preferences = ai_config.preferences(page.app)
    assert preferences["models"][provider] == (new_model or ai_config.PROVIDERS[provider].model)
    assert (provider in preferences["auto_models"]) is (model_mode == "automatic")
    asked = []

    class ConsentStub:
        remember = Control(False)
        def __init__(self, parent, i18n, providers, kind, locked):
            asked.append(list(providers))
        def ShowModal(self):
            return wx.ID_CANCEL
        def Destroy(self):
            pass

    monkeypatch.setattr(result_module, "AIConsentDialog", ConsentStub)
    dialog.config = preferences
    assert AIResultDialog._consent(dialog, [provider]) is False
    assert asked == [[provider]]


def test_output_limit_is_announced_as_error_without_publishing_partial_text(dialog):
    generation, _ = dialog.session.begin("convert")
    dialog._complete(generation, "convert", True, None, "ai_error_output_limit")
    assert dialog.status.value == "ai_error_output_limit"
    assert dialog.spoken == ["ai_error_output_limit"]
    assert dialog.result.value == "" and dialog._latest == ""
    assert dialog.session.history == [] and dialog.session.active is None


def test_an_invalid_model_blocks_apply_and_names_the_problem(tmp_path):
    page = PageStub(tmp_path)
    page._models["groq"] = "../escape"
    assert page.apply() is False
    assert page.status.value == "ai_error_request" and getattr(page.providers, "focused", False)
    assert page.app.get("ai_media") == {}


def test_a_failing_credential_store_keeps_settings_open(tmp_path):
    page = PageStub(tmp_path)
    page.store.set("openai", "x")
    page.store.data_path.write_bytes(b"broken")
    page._drafts = {"gemini": "new"}
    assert page.apply() is False and page.status.value == "ai_error_credentials"


def test_move_reorders_the_try_order_and_stops_at_the_ends(tmp_path):
    page = PageStub(tmp_path)
    first, second = page._order[0], page._order[1]
    page.providers.selection = 0
    page._move(-1)
    assert page._order[0] == first and not page.dirty  # already first
    page._move(1)
    assert page._order[:2] == [second, first] and page.refreshed[-1] == 1 and page.dirty == [True]
    page.providers.selection = len(page._order) - 1
    last = page._order[-1]
    page._move(1)
    assert page._order[-1] == last and len(page.dirty) == 1
    page.providers.selection = -1
    page._move(1)
    assert len(page.dirty) == 1


def test_list_row_names_the_provider_its_state_and_whether_a_key_is_saved(tmp_path):
    page = PageStub(tmp_path)
    page.store.set("openai", "saved")
    page._saved = page.store.saved()
    page._disabled = {"groq"}
    page._drafts = {"claude": "typed-not-yet-applied"}
    assert page._row("openai") == "OpenAI, ai_provider_state_enabled, ai_key_saved"
    assert page._row("groq") == "Groq, ai_provider_state_disabled, ai_key_missing"
    assert page._row("claude").endswith("ai_key_saved")  # a staged key counts
    assert page._row("gemini").endswith("ai_key_missing")


@pytest.mark.parametrize("draft,expect_key,expect_deleted,expect_off", [
    ({"key": "new-key", "deleted": False, "model": "m", "enabled": True}, "new-key", False, False),
    ({"key": "", "deleted": True, "model": "m", "enabled": False}, None, True, True),
    ({"key": "", "deleted": False, "model": "m", "enabled": True}, None, False, False),
])
def test_provider_window_result_is_merged_into_the_staged_drafts(tmp_path, monkeypatch, draft,
                                                                  expect_key, expect_deleted, expect_off):
    import ui.dialogs.ai_settings_page as module
    page = PageStub(tmp_path)
    page.providers.selection = 1
    provider = page._order[1]
    seen = {}
    class WindowStub:
        def __init__(self, parent, main_window, name, state, store, reset):
            seen.update(name=name, state=state)
        def ShowModal(self):
            return wx.ID_OK
        def values(self):
            return draft
        def Destroy(self):
            seen["destroyed"] = True
    monkeypatch.setattr(module, "AIProviderDialog", WindowStub)
    page._configure(None)
    assert seen["name"] == provider and seen["destroyed"]
    assert page._drafts.get(provider) == expect_key
    assert (provider in page._deleted) is expect_deleted
    assert (provider in page._disabled) is expect_off
    assert page._models[provider] == "m" and page.dirty == [True]


def test_cancelling_the_provider_window_changes_nothing(tmp_path, monkeypatch):
    import ui.dialogs.ai_settings_page as module
    page = PageStub(tmp_path)
    class WindowStub:
        def __init__(self, *args):
            pass
        def ShowModal(self):
            return wx.ID_CANCEL
        def Destroy(self):
            pass
    monkeypatch.setattr(module, "AIProviderDialog", WindowStub)
    page._configure(None)
    assert not page._drafts and not page.dirty


class ProviderWindowStub:
    values = AIProviderDialog.values
    _current_key = AIProviderDialog._current_key
    _key_changed = AIProviderDialog._key_changed
    _delete_key = AIProviderDialog._delete_key
    _key_status = AIProviderDialog._key_status
    _cancel_probe = AIProviderDialog._cancel_probe
    _cancel_model_list = AIProviderDialog._cancel_model_list
    _refresh_model_choices = AIProviderDialog._refresh_model_choices
    _hide_key = AIProviderDialog._hide_key
    _apply_automatic = AIProviderDialog._apply_automatic
    _show_manual_model_fields = AIProviderDialog._show_manual_model_fields
    _model_pinned = AIProviderDialog._model_pinned
    _automatic_changed = AIProviderDialog._automatic_changed

    def __init__(self, directory, provider="openai"):
        self.store = CredentialStore(directory)
        self._provider = provider
        self._reset = False
        self._deleted = False
        self._alive = True
        self._probe = None
        self._probe_generation = 0
        AIProviderDialog._init_model_catalog(self)
        self.key = Control()
        self.key_state = Control()
        self.revealed = Control()
        self.model = Control("m")
        self.automatic = Control(False)
        self.model_choice = Control(selection=-1)
        self.get_models = Control()
        self.voice_model = None
        self.enabled = Control(True)
        self.status = Control()
        self._reveal = lambda shown: None
        self._labels = [(Control(), "ai_model_choice"), (Control(), "ai_model"), (Control(), "ai_api_key")]
        self.main_window = SimpleNamespace(i18n=SimpleNamespace(t=lambda key: key))

    def _t(self, key):
        return key


def test_provider_window_uses_the_typed_key_before_the_saved_one(tmp_path):
    window = ProviderWindowStub(tmp_path)
    window.store.set("openai", "saved")
    assert window._current_key() == "saved"
    window.key.value = "typed"
    assert window._current_key() == "typed"
    window.key.value = ""
    window._deleted = True
    assert window._current_key() == ""
    window._deleted, window._reset = False, True
    assert window._current_key() == ""


def test_removal_is_announced_as_pending_and_typing_a_new_key_takes_it_back(tmp_path):
    window = ProviderWindowStub(tmp_path)
    window.store.set("openai", "saved")
    window._delete_key(None)
    assert window.key_state.value == "ai_key_removal_pending" and window.status.value == "ai_key_removal_notice"
    assert window.values() == {"key": "", "deleted": True, "model": "m", "enabled": True}
    window.key.value = "replacement"
    window._key_changed(SimpleNamespace(Skip=lambda: None))
    assert window.values()["deleted"] is False and window.values()["key"] == "replacement"
    assert window.key_state.value == "ai_key_saved"


def test_key_state_reports_saved_and_missing_and_an_unreadable_store(tmp_path):
    window = ProviderWindowStub(tmp_path)
    window._key_status()
    assert window.key_state.value == "ai_key_missing"
    window.store.set("openai", "saved")
    window._key_status()
    assert window.key_state.value == "ai_key_saved"
    window.store.data_path.write_bytes(b"broken")
    window._key_status()
    assert window.key_state.value == "ai_error_credentials"


def test_close_filter_uses_original_chat_not_current_panel_conversation():
    closed = []
    session = MediaSession("a", "original-locked-chat", "m", "image", locked=True)
    stub = SimpleNamespace(_ai_dialog=SimpleNamespace(session=session, _close=lambda: closed.append(True)),
                           conversation={"remoteJid": "ordinary-chat"},
                           main_window=SimpleNamespace(is_chat_locked=lambda jid: jid == "original-locked-chat"))
    AIActionsMixin.close_ai_media(stub, locked_only=True)
    assert closed == [True]
    AIActionsMixin.close_ai_media(stub, message_ids={"unrelated"})
    assert closed == [True]
    AIActionsMixin.close_ai_media(stub, message_ids={"m"})
    assert closed == [True, True]


def test_close_does_nothing_when_no_window_is_open():
    AIActionsMixin.close_ai_media(SimpleNamespace(), locked_only=True)


def test_shortcut_does_nothing_outside_message_list(monkeypatch):
    stub = SimpleNamespace(messages_list=object(), _focused_message=lambda: AIActionsMixin._focused_message(stub))
    monkeypatch.setattr(wx.Window, "FindFocus", lambda: object())
    # No main_window/settings accesses, let alone network or dialog construction.
    AIActionsMixin._on_ai_action(stub)


def message(kind, **info):
    return {"messageType": kind, "key": {"id": "m", "remoteJid": "c"}, "message": {kind: info}}


def test_disabled_feature_never_opens_a_window_or_downloads(tmp_path, monkeypatch):
    import ui.conversation_panel.ai_actions as module
    spoken = []
    monkeypatch.setattr(module, "global_dir", lambda: str(tmp_path))
    panel = SimpleNamespace(main_window=SimpleNamespace(_app_settings=AppSettings(str(tmp_path)),
                            i18n=SimpleNamespace(t=lambda key: key), output=spoken.append))
    panel._ai_settings = lambda: AIActionsMixin._ai_settings(panel)
    AIActionsMixin._on_ai_action(panel, message=message("imageMessage"))
    assert spoken == ["ai_disabled"]


def test_a_message_that_is_not_media_is_ignored_silently(tmp_path):
    spoken = []
    panel = SimpleNamespace(main_window=SimpleNamespace(output=spoken.append))
    AIActionsMixin._on_ai_action(panel, message={"messageType": "conversation", "key": {"id": "m"}})
    AIActionsMixin._on_ai_action(panel, message=message("imageMessage", viewOnce=True))
    assert spoken == []


@pytest.fixture
def ready(tmp_path, monkeypatch):
    """A panel with the feature on and a key saved for Gemini and Groq."""
    import ui.conversation_panel.ai_actions as module
    monkeypatch.setattr(module, "global_dir", lambda: str(tmp_path))
    app = AppSettings(str(tmp_path))
    app.set("ai_media", {"enabled": True})
    CredentialStore(tmp_path).set("gemini", "synthetic")
    CredentialStore(tmp_path).set("groq", "synthetic")
    return module, app


def test_the_menu_offers_only_what_a_provider_with_a_key_can_do(ready):
    module, app = ready
    panel = SimpleNamespace(main_window=SimpleNamespace(_app_settings=app))
    panel._ai_settings = lambda: AIActionsMixin._ai_settings(panel)
    i18n = SimpleNamespace(t=lambda key: key)
    label = lambda msg: AIActionsMixin._ai_menu_label(panel, msg, i18n)
    assert label(message("imageMessage")) == "ai_describe_image_menu"
    assert label(message("stickerMessage")) == "ai_describe_sticker_menu"
    assert label(message("videoMessage")) == "ai_describe_video_menu"  # Gemini
    assert label(message("audioMessage")) == "ai_transcribe_audio_menu"
    assert label(message("documentMessage", mimetype="application/pdf")) == "ai_pdf_accessible_menu"  # Gemini
    assert label(message("documentMessage", mimetype="application/zip")) == ""
    assert label(message("imageMessage", viewOnce=True)) == ""
    assert label({"messageType": "conversation", "key": {"id": "m"}}) == ""


def test_the_menu_hides_a_kind_whose_switch_is_off_and_everything_when_the_feature_is_off(ready):
    module, app = ready
    panel = SimpleNamespace(main_window=SimpleNamespace(_app_settings=app))
    panel._ai_settings = lambda: AIActionsMixin._ai_settings(panel)
    i18n = SimpleNamespace(t=lambda key: key)
    app.set("ai_media", {"enabled": True, "kinds": {"audio": False}})
    assert AIActionsMixin._ai_menu_label(panel, message("audioMessage"), i18n) == ""
    assert AIActionsMixin._ai_menu_label(panel, message("imageMessage"), i18n) != ""
    app.set("ai_media", {"enabled": False})
    assert AIActionsMixin._ai_menu_label(panel, message("imageMessage"), i18n) == ""


def test_the_menu_offers_nothing_when_no_provider_has_a_key(tmp_path, monkeypatch):
    import ui.conversation_panel.ai_actions as module
    monkeypatch.setattr(module, "global_dir", lambda: str(tmp_path))
    app = AppSettings(str(tmp_path))
    app.set("ai_media", {"enabled": True})
    panel = SimpleNamespace(main_window=SimpleNamespace(_app_settings=app))
    panel._ai_settings = lambda: AIActionsMixin._ai_settings(panel)
    assert AIActionsMixin._ai_menu_label(panel, message("imageMessage"), SimpleNamespace(t=lambda k: k)) == ""


@pytest.mark.parametrize("kind,msg_type,fetcher,limit", [
    ("image", "imageMessage", "handle_media_message", 20 * 1024 * 1024),
    ("sticker", "stickerMessage", "handle_media_message", 20 * 1024 * 1024),
    ("video", "videoMessage", "handle_media_message", 64 * 1024 * 1024),
    ("pdf", "documentMessage", "handle_media_message", 14 * 1024 * 1024),
    ("audio", "audioMessage", "handle_audio_message", 14 * 1024 * 1024),
])
@pytest.mark.parametrize("cached", [True, False])
def test_loader_reads_the_original_through_the_bounded_download_of_its_kind(tmp_path, kind, msg_type,
                                                                          fetcher, limit, cached):
    from cryptography.fernet import Fernet
    from core.ai_media.service import Operation
    key = Fernet.generate_key()
    path = tmp_path / "media.bin"
    if cached:
        path.write_bytes(Fernet(key).encrypt(b"original bytes"))
    calls = []
    def download(snapshot, **kwargs):
        calls.append((fetcher, kwargs))
        path.write_bytes(Fernet(key).encrypt(b"original bytes"))
    other = "handle_audio_message" if fetcher == "handle_media_message" else "handle_media_message"
    mw = SimpleNamespace(**{fetcher: download, other: lambda *a, **k: pytest.fail("wrong download route")})
    panel = SimpleNamespace(main_window=mw)
    snapshot = message(msg_type, fileLength=14, mimetype="x/y")
    loader = AIActionsMixin._ai_loader(panel, snapshot, kind, str(path), key)
    assert loader(Operation(kind)) == b"original bytes"
    assert len(calls) == (0 if cached else 1)
    if calls:
        assert calls[0][1]["max_bytes"] == limit and callable(calls[0][1]["cancel_check"])
        assert "timeout" not in calls[0][1]  # the size-scaled default of the download applies


def test_loader_refuses_oversized_media_by_metadata_and_by_actual_size(tmp_path):
    from cryptography.fernet import Fernet
    from core.ai_media.errors import DescriptionError
    from core.ai_media.service import Operation
    from ui.conversation_panel.ai_actions import encrypted_limit
    key = Fernet.generate_key()
    path = tmp_path / "media.bin"
    mw = SimpleNamespace(handle_media_message=lambda *a, **k: pytest.fail("downloaded"))
    panel = SimpleNamespace(main_window=mw)
    declared = message("audioMessage", fileLength=ai_config.MAX_SOURCE_BYTES["audio"] + 1)
    with pytest.raises(DescriptionError, match="ai_error_media_size"):
        AIActionsMixin._ai_loader(panel, declared, "audio", str(path), key)(Operation("audio"))
    path.write_bytes(b"x" * (encrypted_limit(ai_config.MAX_SOURCE_BYTES["audio"]) + 1))
    understated = message("audioMessage", fileLength=1)
    with pytest.raises(DescriptionError, match="ai_error_media_size"):
        AIActionsMixin._ai_loader(panel, understated, "audio", str(path), key)(Operation("audio"))


@pytest.mark.parametrize("damage", ["missing", "garbage"])
def test_loader_reports_missing_or_unreadable_cache_as_a_media_error(tmp_path, damage):
    from cryptography.fernet import Fernet
    from core.ai_media.errors import DescriptionError
    from core.ai_media.service import Operation
    path = tmp_path / "media.bin"
    if damage == "garbage":
        path.write_bytes(b"not encrypted")
    mw = SimpleNamespace(handle_media_message=lambda *a, **k: None)
    loader = AIActionsMixin._ai_loader(SimpleNamespace(main_window=mw), message("imageMessage"), "image",
                                       str(path), Fernet.generate_key())
    with pytest.raises(DescriptionError, match="ai_error_media$"):
        loader(Operation("image"))


def test_loader_stops_when_the_action_is_cancelled_before_reading(tmp_path):
    from cryptography.fernet import Fernet
    from core.ai_media.errors import DescriptionError
    from core.ai_media.service import Operation
    operation = Operation("image")
    mw = SimpleNamespace(handle_media_message=lambda *a, **k: operation.cancel())
    loader = AIActionsMixin._ai_loader(SimpleNamespace(main_window=mw), message("imageMessage"), "image",
                                       str(tmp_path / "gone"), Fernet.generate_key())
    with pytest.raises(DescriptionError, match="ai_error_cancelled"):
        loader(operation)


def test_entry_point_opens_one_window_with_the_kind_chat_and_message_and_restores_focus(ready, monkeypatch):
    module, app = ready
    import ui.dialogs.ai_result_dialog as dialog_module
    focused, built = [], []
    monkeypatch.setattr(module, "active_account_id", lambda: "account")
    monkeypatch.setattr(module, "cached_media_path", lambda *args: "unused")
    class WindowStub:
        def __init__(self, panel, identity, kind, locked, config, app, loader, mime):
            built.append((identity, kind, locked, mime, config["enabled"]))
            self.session = MediaSession(*identity, kind, locked=locked)
        def ShowModal(self):
            panel._ai_dialog.Raise()  # the second activation only raises it
            AIActionsMixin._on_ai_action(panel, message=msg)
        def Raise(self):
            built.append("raised")
        def _dispose(self):
            self.session.close()
        def Destroy(self):
            built.append("destroyed")
    monkeypatch.setattr(dialog_module, "AIResultDialog", WindowStub)
    msg = message("audioMessage", mimetype="audio/ogg; codecs=opus")
    mw = SimpleNamespace(_app_settings=app, key=b"k", IsShown=lambda: True, i18n=SimpleNamespace(t=lambda k: k),
                         is_chat_locked=lambda jid: False, output=lambda text: None)
    panel = SimpleNamespace(main_window=mw, conversation={"remoteJid": "c"}, _sorted_messages=[msg],
                            messages_list=SimpleNamespace(Focus=lambda i: focused.append(i), Select=lambda i: None,
                                                          SetFocus=lambda: None))
    for name in ("_ai_settings", "_ai_loader", "_restore_focus_after_ai"):
        setattr(panel, name, getattr(AIActionsMixin, name).__get__(panel))
    AIActionsMixin._on_ai_action(panel, message=msg)
    assert built[0] == (("account", "c", "m"), "audio", False, "audio/ogg; codecs=opus", True)
    assert built.count("raised") == 2 and built[-1] == "destroyed"  # no second window
    assert focused == [0] and panel._ai_dialog is None


def test_a_locked_chat_is_not_processed_until_the_vault_is_unlocked(ready, monkeypatch):
    module, app = ready
    spoken = []
    mw = SimpleNamespace(_app_settings=app, i18n=SimpleNamespace(t=lambda k: k), output=spoken.append,
                         is_chat_locked=lambda jid: True, _chat_lock_unlocked=False)
    panel = SimpleNamespace(main_window=mw, conversation={"remoteJid": "c"})
    panel._ai_settings = lambda: AIActionsMixin._ai_settings(panel)
    monkeypatch.setattr(module, "AIResultDialog", lambda *a: pytest.fail("window opened"), raising=False)
    AIActionsMixin._on_ai_action(panel, message=message("imageMessage"))
    assert spoken == []



def test_loading_media_leaves_no_plain_copy_anywhere_on_disk(tmp_path):
    from cryptography.fernet import Fernet
    from core.ai_media.service import Operation
    key = Fernet.generate_key()
    cache = tmp_path / "cache"
    cache.mkdir()
    path = cache / "media.wzmedia"
    path.write_bytes(Fernet(key).encrypt(b"PLAIN-MEDIA-BYTES"))
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    import tempfile
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    old = tempfile.tempdir
    tempfile.tempdir = str(scratch)  # anything the loader parks in the temp folder lands here
    try:
        panel = SimpleNamespace(main_window=SimpleNamespace())
        got = AIActionsMixin._ai_loader(panel, message("imageMessage"), "image", str(path), key)(Operation("image"))
    finally:
        tempfile.tempdir = old
    after = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert got == b"PLAIN-MEDIA-BYTES" and after == before and not list(scratch.iterdir())
    assert not any(b"PLAIN-MEDIA-BYTES" in data for data in after.values())


def test_consent_text_names_providers_and_warns_about_metadata_only_where_it_is_not_stripped():
    from ui.dialogs.ai_result_dialog import consent_text
    i18n = SimpleNamespace(t=lambda key: key + " {providers}" if key == "ai_consent" else key)
    for kind in ("audio", "pdf"):
        assert "ai_metadata_notice" in consent_text(i18n, ["openai"], kind, False)
    for kind in ("video", "image", "sticker"):
        assert "ai_metadata_notice" not in consent_text(i18n, ["openai"], kind, False)
    # A video goes out as one picture without its sound: the consent says so.
    assert "ai_video_frame_notice" in consent_text(i18n, ["openai"], "video", False)
    assert "ai_video_frame_notice" not in consent_text(i18n, ["openai"], "image", False)
    text = consent_text(i18n, ["gemini", "groq"], "image", True)
    assert "Google Gemini, Groq" in text and "ai_gemini_notice" in text and "ai_locked_consent" in text
    assert "ai_gemini_notice" not in consent_text(i18n, ["groq"], "image", False)
    # Mistral's free plan may train on what it receives: said whenever it may get the media.
    assert "ai_mistral_notice" in consent_text(i18n, ["groq", "mistral"], "audio", False)
    assert "ai_mistral_notice" not in consent_text(i18n, ["gemini", "groq"], "audio", False)


def test_the_describe_button_follows_the_menu_rule(ready):
    shown = []
    button = SimpleNamespace(SetLabel=lambda text: shown.append(("label", text)),
                             Show=lambda: shown.append("show"), Hide=lambda: shown.append("hide"))
    module, app = ready
    panel = SimpleNamespace(main_window=SimpleNamespace(_app_settings=app, i18n=SimpleNamespace(t=lambda key: key)),
                            _action_describe_btn=button)
    panel._ai_settings = lambda: AIActionsMixin._ai_settings(panel)
    panel._ai_menu_label = lambda msg, i18n, config=None: AIActionsMixin._ai_menu_label(
        panel, msg, i18n, config)
    AIActionsMixin._update_ai_describe_button(panel, message("imageMessage"))
    assert shown == [("label", "ai_describe_image_menu"), "show"]
    shown.clear()
    AIActionsMixin._update_ai_describe_button(panel, message("imageMessage", viewOnce=True))
    AIActionsMixin._update_ai_describe_button(panel, {"messageType": "conversation", "key": {"id": "m"}})
    assert shown == ["hide", "hide"]


def test_the_describe_button_can_be_left_out_and_the_menu_keeps_the_item(ready):
    """Settings > Online AI: one Tab stop fewer after the message list."""
    shown = []
    button = SimpleNamespace(SetLabel=lambda text: shown.append(("label", text)),
                             Show=lambda: shown.append("show"), Hide=lambda: shown.append("hide"))
    module, app = ready
    app.update("ai_media", lambda old: dict(old or {}, hide_action_button=True))
    panel = SimpleNamespace(main_window=SimpleNamespace(_app_settings=app, i18n=SimpleNamespace(t=lambda key: key)),
                            _action_describe_btn=button)
    panel._ai_settings = lambda: AIActionsMixin._ai_settings(panel)
    AIActionsMixin._update_ai_describe_button(panel, message("imageMessage"))
    assert shown == ["hide"]
    assert AIActionsMixin._ai_menu_label(panel, message("imageMessage"), panel.main_window.i18n)         == "ai_describe_image_menu"


def test_the_describe_button_option_is_off_by_default_and_saved(tmp_path):
    assert ai_config.preferences(AppSettings(str(tmp_path)))["hide_action_button"] is False
    page = PageStub(tmp_path)
    page.hide_action_button = Control(True)
    assert page.apply()
    assert ai_config.preferences(page.app)["hide_action_button"] is True


def test_the_shortcut_and_the_button_act_on_the_selected_message_while_the_button_has_focus(monkeypatch):
    button, others = object(), object()
    msgs = [{"id": 1}, {"id": 2}, {"id": 3}]
    focus = {"now": button}
    monkeypatch.setattr(wx.Window, "FindFocus", lambda: focus["now"])
    seen = []
    panel = SimpleNamespace(
        messages_list=SimpleNamespace(GetFirstSelected=lambda: 1, GetFocusedItem=lambda: 2),
        _action_describe_btn=button, _sorted_messages=msgs)
    panel._focused_message = lambda: AIActionsMixin._focused_message(panel)
    panel._on_ai_action = lambda message=None: seen.append(message)
    AIActionsMixin._on_ai_describe_button(panel)            # button focused: the selected row
    focus["now"] = panel.messages_list
    assert AIActionsMixin._focused_message(panel) == {"id": 3}  # list focused: the focused row
    focus["now"] = others
    assert AIActionsMixin._focused_message(panel) is None
    focus["now"] = None  # no focus at all must not be mistaken for the button
    panel._action_describe_btn = None
    assert AIActionsMixin._focused_message(panel) is None
    panel._action_describe_btn = button
    panel.messages_list = SimpleNamespace(GetFirstSelected=lambda: -1, GetFocusedItem=lambda: -1)
    focus["now"] = button
    assert AIActionsMixin._focused_message(panel) is None
    assert seen == [{"id": 2}]


def test_automatic_model_follows_the_recommendation_and_pinning_reveals_the_fields(tmp_path):
    window = ProviderWindowStub(tmp_path)
    window.automatic.value = True
    window.model.value = "gpt-4o"
    assert window.values()["model"] == ""
    window._automatic_changed(SimpleNamespace(Skip=lambda: None))
    assert window.model.value == ai_config.PROVIDERS["openai"].model
    assert window.model.shown is False and window.get_models.enabled is False
    window.automatic.value = False
    window._automatic_changed(SimpleNamespace(Skip=lambda: None))
    assert window.model.shown is True and window.get_models.enabled is True
    window.model.value = "gpt-4o"
    assert window.values()["model"] == "gpt-4o"


def test_a_model_pinned_in_the_provider_window_is_kept_and_automatic_clears_it(tmp_path):
    page = PageStub(tmp_path)
    assert page._auto == set(ai_config.PROVIDERS)  # nothing saved: all follow the recommendation
    page.apply()
    assert set(page.app.get("ai_media")["models"].values()) == {""}
    assert ai_config.preferences(page.app)["models"]["openai"] == ai_config.PROVIDERS["openai"].model
    page._auto.discard("openai")
    page._models["openai"] = "gpt-4o"
    page.apply()
    prefs = ai_config.preferences(page.app)
    assert prefs["models"]["openai"] == "gpt-4o" and "openai" not in prefs["auto_models"]


def test_automatic_keeps_the_model_fields_locked_through_key_edits_and_model_lists(tmp_path):
    window = ProviderWindowStub(tmp_path)
    window.automatic.value = True
    window._automatic_changed(SimpleNamespace(Skip=lambda: None))
    window.key.value = "typed"
    window._key_changed(SimpleNamespace(Skip=lambda: None))
    assert window.get_models.enabled is False
    window._model_options = (SimpleNamespace(id="gpt-4o", label="gpt-4o"),)
    window._refresh_model_choices()
    assert window.model_choice.enabled is False
    window.automatic.value = False
    window._refresh_model_choices()
    assert window.model_choice.enabled is True


def test_declining_the_first_consent_closes_the_window_instead_of_leaving_an_empty_result(dialog, monkeypatch):
    module = _prepare_start(dialog, monkeypatch)
    dialog._consented = False
    dialog.config["consented"] = []
    dialog._consent = lambda providers: False
    monkeypatch.setattr(module, "submit", lambda *args: pytest.fail("dispatched without consent"))
    dialog._start("describe", regenerate=True)
    assert dialog.closed and dialog.session.closed and dialog.session.requests == 0


def test_declining_a_later_consent_keeps_the_answer_that_is_already_shown(dialog, monkeypatch):
    module = _prepare_start(dialog, monkeypatch)
    dialog._latest = "an earlier answer"
    dialog._consent = lambda providers: False
    monkeypatch.setattr(module, "submit", lambda *args: pytest.fail("dispatched without consent"))
    dialog._start("another question")
    assert not dialog.closed and not dialog.session.closed


def test_provider_window_opens_with_the_saved_key_unless_it_is_staged_away(tmp_path):
    from ui.dialogs.ai_settings_page import key_for_dialog
    store = CredentialStore(tmp_path)
    store.set("openai", "saved-key")
    assert key_for_dialog(store, "openai", {}, set(), False) == "saved-key"
    assert key_for_dialog(store, "openai", {"openai": "typed"}, set(), False) == "typed"
    assert key_for_dialog(store, "openai", {}, {"openai"}, False) == ""
    assert key_for_dialog(store, "openai", {}, set(), True) == ""
    assert key_for_dialog(store, "gemini", {}, set(), False) == ""
    store.data_path.write_bytes(b"broken")
    assert key_for_dialog(store, "openai", {}, set(), False) == ""


def test_configure_prefills_the_saved_key_and_stages_only_a_changed_one(tmp_path, monkeypatch):
    import ui.dialogs.ai_settings_page as module
    page = PageStub(tmp_path)
    page.providers.selection = 0
    provider = page._order[0]
    page.store.set(provider, "saved-key")
    seen = []
    typed = {"key": "saved-key"}

    class WindowStub:
        def __init__(self, parent, main_window, name, state, store, reset):
            seen.append(state["key"])
        def ShowModal(self):
            return wx.ID_OK
        def values(self):
            return {"key": typed["key"], "deleted": False, "model": "", "enabled": True}
        def Destroy(self):
            pass

    monkeypatch.setattr(module, "AIProviderDialog", WindowStub)
    page._configure(None)
    assert seen == ["saved-key"] and page._drafts == {}  # unchanged: nothing to re-save
    typed["key"] = "replacement"
    page._configure(None)
    assert page._drafts == {provider: "replacement"}
    page._configure(None)
    assert seen[-1] == "replacement"  # a staged key comes back as typed


def test_the_voice_model_list_marks_preview_models():
    from ui.dialogs.ai_settings_page import voice_model_labels
    labels = voice_model_labels(lambda key: "{name} (preview)" if key == "ai_model_preview" else key,
                                "mistral")
    assert labels == ["Voxtral Mini Transcribe 3 (preview)", "Voxtral Small Transcribe 3 (preview)"]
    assert voice_model_labels(lambda key: key, "openai") == []


def test_the_voice_model_chosen_in_the_provider_window_is_saved(tmp_path, monkeypatch):
    import ui.dialogs.ai_settings_page as module
    page = PageStub(tmp_path)
    page._order = ["mistral"] + [p for p in page._order if p != "mistral"]
    page.providers.selection = 0
    seen = []

    class WindowStub:
        def __init__(self, parent, main_window, name, state, store, reset):
            seen.append(state["transcribe_model"])
        def ShowModal(self):
            return wx.ID_OK
        def values(self):
            return {"key": "", "deleted": False, "model": "", "enabled": True,
                    "transcribe_model": "voxtral-small-transcribe-3"}
        def Destroy(self):
            pass

    monkeypatch.setattr(module, "AIProviderDialog", WindowStub)
    page._configure(None)
    assert seen == ["voxtral-mini-transcribe-3"]  # the window opens on the current choice
    assert page.apply()
    assert page.app.get("ai_media")["transcribe_models"] == {"mistral": "voxtral-small-transcribe-3"}
    prefs = ai_config.preferences(page.app)
    assert ai_config.models_for(prefs, "audio")["mistral"] == "voxtral-small-transcribe-3"


def test_the_default_voice_model_is_not_saved_so_it_can_move_later(tmp_path):
    """Like an automatic vision model: pressing OK must not pin the default."""
    page = PageStub(tmp_path)
    assert page.apply()
    assert page.app.get("ai_media")["transcribe_models"] == {}
    page._voice_models["mistral"] = "voxtral-small-transcribe-3"
    assert page.apply()
    page._voice_models["mistral"] = ai_config.PROVIDERS["mistral"].transcribe_model
    assert page.apply()  # chosen back: the default again, not a pin
    assert page.app.get("ai_media")["transcribe_models"] == {}


def test_turning_on_automatic_transcription_says_the_provider_notices(tmp_path, monkeypatch):
    """Every voice message then goes to the provider: its notice belongs in that consent too."""
    import ui.dialogs.ai_settings_page as module

    class Page(PageStub):
        _on_auto_toggle = AISettingsPage._on_auto_toggle
        _plain = AISettingsPage._plain

    page = Page(tmp_path)
    page.confirm_replaces_local = None
    page.auto_dialog = None
    page._drafts = {"mistral": "k", "groq": "k"}
    asked = []
    monkeypatch.setattr(module.wx, "MessageBox", lambda text, *args: asked.append(text) or wx.YES)
    page.auto_transcribe.value = True
    page._on_auto_toggle(SimpleNamespace(GetEventObject=lambda: page.auto_transcribe,
                                         Skip=lambda: None))
    assert len(asked) == 1 and "ai_mistral_notice" in asked[0]
    assert "ai_gemini_notice" not in asked[0] and page._auto_consent == {"mistral", "groq"}


def test_the_provider_list_lands_on_the_first_provider_when_nothing_is_selected():
    selected = []
    providers = SimpleNamespace(Freeze=lambda: None, Thaw=lambda: None, Set=lambda rows: None,
                                GetCount=lambda: 5, SetSelection=selected.append)
    page = SimpleNamespace(providers=providers, _order=["a"] * 5, _row=lambda p: p)
    AISettingsPage._refresh_list(page)
    AISettingsPage._refresh_list(page, 3)
    AISettingsPage._refresh_list(page, 9)
    assert selected == [0, 3, 0]


@pytest.mark.parametrize("automatic", [True, False])
def test_manual_model_fields_exist_only_while_automatic_is_off(tmp_path, automatic):
    window = ProviderWindowStub(tmp_path)
    window.automatic.value = automatic
    window._apply_automatic()
    manual = not automatic
    assert window.model.shown is manual and window.model_choice.shown is manual
    assert window.get_models.shown is manual and window.get_models.enabled is manual
    captions = {key: label.shown for label, key in window._labels}
    assert captions == {"ai_model_choice": manual, "ai_model": manual, "ai_api_key": False}
    assert window.model.enabled is True  # never left disabled: that reads as read-only
    if automatic:
        assert window.model.value == ai_config.PROVIDERS["openai"].model
