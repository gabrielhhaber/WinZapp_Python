"""Unbound wx methods against plain stubs: never construct windows or use keys/network."""
from types import SimpleNamespace

import pytest
import wx

from app_settings import AppSettings
from core.i18n import LANGUAGE_NAMES
from core.ai_credentials import CredentialStore
from core.image_description.session import PhotoSession
from core.image_description.image_input import ImageInput
from ui.dialogs.image_description_dialog import ImageDescriptionDialog
from ui.dialogs.image_description_settings import ImageDescriptionSettingsPage
from ui.conversation_panel.image_description import ImageDescriptionMixin


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


@pytest.mark.parametrize("modal_fails", [False, True])
def test_technical_help_is_readable_focused_and_destroyed_without_opening_a_window(monkeypatch, modal_fails):
    import ui.dialogs.image_description_settings as module

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
            ImageDescriptionSettingsPage._technical_info(page, None)
    else:
        ImageDescriptionSettingsPage._technical_info(page, None)
    assert calls[-2:] == ["modal", "destroy"]


class DialogStub:
    _complete = ImageDescriptionDialog._complete
    _close = ImageDescriptionDialog._close
    _dispose = ImageDescriptionDialog._dispose
    _finish_close = ImageDescriptionDialog._finish_close
    _cancel = ImageDescriptionDialog._cancel
    _timeout = ImageDescriptionDialog._timeout
    _key = ImageDescriptionDialog._key
    _valid = ImageDescriptionDialog._valid
    _busy = ImageDescriptionDialog._busy
    _show_error = ImageDescriptionDialog._show_error
    _start = ImageDescriptionDialog._start

    def __init__(self, *, locked=False):
        self.session = PhotoSession("a", "c", "m", locked=locked)
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
        self.config = {"read_answers": False}
        self._latest = ""
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
    import ui.dialogs.image_description_dialog as module
    monkeypatch.setattr(module, "active_account_id", lambda: "a")
    return DialogStub()


def test_complete_updates_once_without_touching_editor_or_focus(dialog):
    generation, _ = dialog.session.begin("old question")
    dialog._complete(generation, "old question", False, (ImageInput(b"photo", "image/jpeg", 1, 1), "answer"), None)
    assert dialog.result.value == "ai_history_question\nold question\n\nai_history_answer\nanswer"
    assert dialog.question.value == "a question I am still writing"
    assert not hasattr(dialog.question, "focused")
    assert dialog.spoken == ["ai_ready"]
    assert dialog.session.image.data == b"photo"


def test_initial_description_omits_internal_prompt_but_keeps_provider_history(dialog):
    prompt = "Internal description instruction"
    generation, _ = dialog.session.begin(prompt)
    dialog._complete(generation, prompt, True, (ImageInput(b"photo", "image/jpeg", 1, 1), "A blue rectangle"), None)
    assert dialog.result.value == "A blue rectangle"
    assert dialog.session.history == [("user", prompt), ("assistant", "A blue rectangle")]
    assert dialog._latest == "A blue rectangle" and dialog.spoken == ["ai_ready"]


def test_followup_history_distinguishes_questions_and_answers(dialog):
    image = ImageInput(b"photo", "image/jpeg", 1, 1)
    generation, _ = dialog.session.begin("Internal instruction")
    dialog._complete(generation, "Internal instruction", True, (image, "Three shapes"), None)
    generation, _ = dialog.session.begin("What color is the circle?")
    dialog._complete(generation, "What color is the circle?", False, (image, "Red"), None)
    assert dialog.result.value == ("Three shapes\n\nai_history_question\nWhat color is the circle?"
                                   "\n\nai_history_answer\nRed")
    assert dialog.session.history[0] == ("user", "Internal instruction")
    assert dialog.session.history[-2:] == [("user", "What color is the circle?"), ("assistant", "Red")]
    assert dialog._latest == "Red" and dialog.copy.enabled


def test_first_manual_question_is_not_hidden_as_an_internal_prompt(dialog):
    generation, _ = dialog.session.begin("Read the text")
    dialog._complete(generation, "Read the text", False, (ImageInput(b"photo", "image/jpeg", 1, 1), "DEMO"), None)
    assert dialog.result.value == "ai_history_question\nRead the text\n\nai_history_answer\nDEMO"


def test_regeneration_replaces_old_transcript_without_exposing_new_prompt(dialog):
    generation, _ = dialog.session.begin("old question")
    dialog._complete(generation, "old question", False, (ImageInput(b"photo", "image/jpeg", 1, 1), "old answer"), None)
    generation, _ = dialog.session.begin("Internal description instruction")
    dialog._complete(generation, "Internal description instruction", True,
                     (ImageInput(b"photo", "image/jpeg", 1, 1), "new description"), None)
    assert dialog.result.value == "new description"
    assert dialog.session.history == [("user", "Internal description instruction"), ("assistant", "new description")]


@pytest.mark.parametrize("draft", ["Which shape?", "  Which shape? \n"])
def test_successful_question_clears_only_the_unchanged_submitted_editor(dialog, draft):
    dialog.question.value = draft
    generation, _ = dialog.session.begin("Which shape?")
    dialog._complete(generation, "Which shape?", False, (ImageInput(b"photo", "image/jpeg", 1, 1), "A circle"), None)
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
    dialog._complete(generation, "Which shape?", False, (ImageInput(b"photo", "image/jpeg", 1, 1), "late answer"), None)
    assert dialog.question.value == "Which shape?" and dialog.result.value == ""
    assert not dialog.spoken


def test_regeneration_never_clears_a_question_draft_even_if_it_matches_prompt(dialog):
    prompt = "Internal description instruction"
    dialog.question.value = prompt
    generation, _ = dialog.session.begin(prompt)
    dialog._complete(generation, prompt, True, (ImageInput(b"photo", "image/jpeg", 1, 1), "description"), None)
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
        import ui.dialogs.image_description_dialog as module
        monkeypatch.setattr(module, "active_account_id", lambda: "different-account")
    dialog._complete(generation, "question", False, (ImageInput(b"private", "image/jpeg", 1, 1), "secret answer"), None)
    assert dialog.result.value == "" and dialog.spoken == []
    assert dialog.session.image is None


def test_answer_queued_after_deadline_cannot_be_published(dialog):
    generation, token = dialog.session.begin("question")
    token.clock = lambda: token.deadline + 1
    dialog._complete(generation, "question", False, (ImageInput(b"photo", "image/jpeg", 1, 1), "late answer"), None)
    assert dialog.spoken == ["ai_error_timeout"] and dialog.result.value == ""
    assert dialog.status.value == "ai_error_timeout" and dialog.ask.enabled


@pytest.mark.parametrize("locked,remembered,expected", [(False, True, 0), (True, True, 1), (False, False, 1)])
def test_locked_photo_requires_fresh_consent_even_if_provider_was_remembered(dialog, monkeypatch,
                                                                          locked, remembered, expected):
    import ui.dialogs.image_description_dialog as module
    opened = []
    class ConsentStub:
        def __init__(self, *args):
            opened.append(True)
            self.remember = Control(False)
        def ShowModal(self):
            return wx.ID_CANCEL
        def Destroy(self):
            pass
    monkeypatch.setattr(module, "PhotoConsentDialog", ConsentStub)
    dialog.session.locked = locked
    dialog.config.update(consented=remembered, provider="openai")
    dialog._consented = False
    ImageDescriptionDialog._consent(dialog)
    assert len(opened) == expected


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
    import ui.dialogs.image_description_dialog as module
    events = []
    dialog.config.update(provider="gemini", consented=False)
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
    monkeypatch.setattr(module, "PhotoConsentDialog", ConsentStub)
    assert ImageDescriptionDialog._consent(dialog) is False
    assert events == ["child-loop", "child-exit-requested", "child-destroyed", "parent-exit"]
    assert dialog.closed and dialog.session.closed and not dialog._consented


def test_close_during_consent_never_dispatches_photo_job(dialog, monkeypatch):
    module = _prepare_start(dialog, monkeypatch)
    dialog._consented = False
    dialog.config["consented"] = False
    dialog._consent = lambda: ImageDescriptionDialog._consent(dialog)
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
    monkeypatch.setattr(module, "PhotoConsentDialog", ConsentStub)
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
    import ui.dialogs.image_description_dialog as module
    dialog.config.update(provider="gemini", model="gemini-3.8-flash", profile="detailed")
    dialog._consent = lambda: True
    dialog.i18n.get_language = lambda: "tr-TR"
    dialog.loader = lambda token: b"synthetic photo"
    monkeypatch.setattr(module, "CredentialStore", lambda path: SimpleNamespace(get=lambda provider: "fake-key"))
    monkeypatch.setattr(module, "prepare_image", lambda data, profile: ImageInput(data, "image/jpeg", 1, 1))
    class TimerStub:
        def __init__(self, milliseconds, callback, generation):
            self.milliseconds, self.callback, self.generation = milliseconds, callback, generation
            self.stopped = False
        def Stop(self):
            self.stopped = True
    monkeypatch.setattr(module.wx, "CallLater", TimerStub)
    return module


@pytest.mark.parametrize("language", list(LANGUAGE_NAMES))
@pytest.mark.parametrize("provider", ["openai", "gemini"])
@pytest.mark.parametrize("regenerate", [False, True])
def test_photo_requests_follow_current_application_language(dialog, monkeypatch, language, provider, regenerate):
    from core.i18n import I18n
    from core.image_description.config import PROVIDERS
    from core.image_description.providers import build_request
    module = _prepare_start(dialog, monkeypatch)
    dialog.main_window.settings = {"general": {"language": language}}
    dialog.i18n = I18n(dialog.main_window)
    dialog.config.update(provider=provider, model=PROVIDERS[provider].model)
    requests = []
    def answer(provider, model, key, image, history, question, instruction, profile, token):
        _, _, body = build_request(provider, model, key, image, history, question, instruction, profile)
        requests.append(body)
        return "synthetic answer"
    monkeypatch.setattr(module, "request_answer", answer)
    monkeypatch.setattr(module, "submit", lambda work, complete: work())
    # _start must re-read settings, not rely on a cached Turkish demo locale.
    dialog.i18n.language = "tr-TR"
    question = dialog.i18n.t("ai_describe_prompt") if regenerate else "Question in another language"
    dialog._start(question, regenerate=regenerate)
    assert len(requests) == 1
    body = requests[0]
    instruction = body["instructions"] if provider == "openai" else body["systemInstruction"]["parts"][0]["text"]
    assert f"Answer in language {language}." in instruction
    if language == "ro" and regenerate:
        sent = body["input"][-1]["content"][-1]["text"] if provider == "openai" else body["contents"][-1]["parts"][-1]["text"]
        assert sent.startswith("Descrie") and "Bu fotoğrafı" not in sent


def test_processing_sound_starts_before_photo_job_is_dispatched(dialog, monkeypatch):
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
    result = (ImageInput(b"photo", "image/jpeg", 1, 1), "answer") if error is None else None
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
    dialog._complete(old, "old", False, (ImageInput(b"photo", "image/jpeg", 1, 1), "old answer"), None)
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
    timer.callback(timer.generation)
    assert timer.stopped and dialog._timer is None
    assert dialog.session.active is None and dialog.ask.enabled
    assert dialog.spoken == ["ai_error_timeout"]


def test_real_worker_error_reaches_gui_queue_and_is_announced(dialog, monkeypatch):
    from core.image_description import service
    from core.image_description.errors import DescriptionError
    module = _prepare_start(dialog, monkeypatch)
    future = []
    callbacks = []
    def request(*args):
        raise DescriptionError("quota")
    def dispatch(work, complete):
        future.append(service.submit(work, complete))
    monkeypatch.setattr(module, "request_answer", request)
    monkeypatch.setattr(module, "submit", dispatch)
    monkeypatch.setattr(module.wx, "CallAfter", lambda callback, *args: callbacks.append((callback, args)))
    dialog._start("question")
    future[0].result(timeout=5)
    assert dialog.status.value == "ai_question_loading" and len(callbacks) == 1
    callback, args = callbacks.pop()
    callback(*args)
    assert dialog.status.value == "ai_error_quota" and dialog.ask.enabled
    assert dialog.spoken == ["ai_error_quota"]
    assert dialog.session.active is None and dialog._timer is None


def test_dispatch_failure_clears_busy_state_and_watchdog(dialog, monkeypatch):
    module = _prepare_start(dialog, monkeypatch)
    def dispatch(*args):
        raise RuntimeError("private failure text must never be shown")
    monkeypatch.setattr(module, "submit", dispatch)
    dialog._start("question")
    assert dialog.status.value == "ai_error_response"
    assert dialog.spoken == ["ai_error_response"]
    assert dialog.ask.enabled and dialog.session.active is None and dialog._timer is None


@pytest.mark.parametrize("regenerate,expected", [
    (True, "ai_description_loading"),
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
    assert not hasattr(dialog.question, "focused") and not dialog.spoken


@pytest.mark.parametrize("error,expected", [(None, "ai_connection_ok"), ("ai_error_authentication", "ai_error_authentication")])
def test_connection_probe_has_its_own_waiting_status_and_ignores_stale_callbacks(monkeypatch, error, expected):
    import ui.dialogs.image_description_settings as module
    queued = []
    cancelled = []
    page = SimpleNamespace(_probe=None, _probe_generation=0, _alive=True, _provider="gemini",
                           model=Control("gemini-3.8-flash"), _current_key=lambda: "synthetic-key",
                           status=Control(), _t=lambda key: key,
                           _cancel_model_list=lambda: cancelled.append(True))
    page._tested = lambda generation, error: ImageDescriptionSettingsPage._tested(page, generation, error)
    monkeypatch.setattr(module, "submit", lambda *args: queued.append(args))
    ImageDescriptionSettingsPage._test(page, None)
    assert cancelled == [True]
    assert page.status.value == "ai_connection_loading" and len(queued) == 1
    page._tested(page._probe_generation - 1, "ai_error_network")
    assert page.status.value == "ai_connection_loading"
    page._tested(page._probe_generation, error)
    assert page.status.value == expected
    page._alive = False
    page._tested(page._probe_generation, "ai_error_network")
    assert page.status.value == expected


def test_settings_guidance_has_a_distinct_readable_name_and_keeps_profile_selection():
    class NamedControl(Control):
        def SetName(self, value):
            self.name = value
        def SetItems(self, items):
            self.items = items
            self.selection = -1
        def SetSelection(self, value):
            self.selection = value
    page = SimpleNamespace(_labels=[], _t=lambda key: key, _profile_labels=lambda: ["quick", "balanced", "detailed"],
                           key=NamedControl(), revealed=NamedControl(), provider=NamedControl(),
                           profile=NamedControl(selection=2), model=NamedControl(), model_choice=NamedControl(),
                           notice=NamedControl(), status=NamedControl())
    ImageDescriptionSettingsPage.refresh_labels(page)
    assert page.notice.name == "ai_settings_help" and page.notice.value == "ai_settings_notice"
    assert page.status.name == "ai_status" and page.profile.selection == 2
    assert page.profile.items == ["quick", "balanced", "detailed"]


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


class SettingsStub:
    apply = ImageDescriptionSettingsPage.apply
    _current_key = ImageDescriptionSettingsPage._current_key
    _change_provider = ImageDescriptionSettingsPage._change_provider
    _hide_key = ImageDescriptionSettingsPage._hide_key
    _cancel_model_list = ImageDescriptionSettingsPage._cancel_model_list
    _refresh_model_choices = ImageDescriptionSettingsPage._refresh_model_choices
    _cancel_probe = ImageDescriptionSettingsPage._cancel_probe

    def __init__(self, directory):
        self.store = CredentialStore(directory)
        self.app = AppSettings(str(directory))
        self._provider = "openai"
        self._alive = True
        ImageDescriptionSettingsPage._init_model_catalog(self)
        self.get_models = Control()
        self.model_choice = Control(selection=-1)
        self._drafts = {}
        self._models = {}
        self._deleted = set()
        self._reset = False
        self.key = Control()
        self.revealed = Control()
        self.model = Control("gpt-4.1-mini")
        self.provider = Control(selection=0)
        self.profile = Control(selection=1)
        self.enabled = Control(True)
        self.read_answers = Control(False)
        self.status = Control()
        self._labels = []
        self._probe_generation = 0
        self._probe = None

    def _t(self, key):
        return key

    def _key_status(self):
        pass


def test_provider_switch_never_relabels_one_key_as_another(tmp_path):
    page = SettingsStub(tmp_path)
    page.key.value = "openai-secret"
    page.provider.selection = 1
    page._change_provider(SimpleNamespace(Skip=lambda: None))
    assert page.key.value == "" and page._provider == "gemini"
    page.key.value = "gemini-secret"
    assert page.apply()
    assert page.store.get("openai") == "openai-secret"
    assert page.store.get("gemini") == "gemini-secret"
    saved = page.app.get("image_description")
    assert saved["provider"] == "gemini" and saved["enabled"]
    assert "secret" not in str(saved)


def test_blank_key_on_apply_preserves_saved_key_and_consent(tmp_path):
    page = SettingsStub(tmp_path)
    page.store.set("openai", "saved-key")
    page.app.set("image_description", {"consented": ["openai"]})
    assert page.apply()
    assert page.store.get("openai") == "saved-key"
    assert page.app.get("image_description")["consented"] == ["openai"]


def test_explicit_delete_revokes_only_this_providers_consent(tmp_path):
    page = SettingsStub(tmp_path)
    page.store.set("openai", "one")
    page.store.set("gemini", "two")
    page.app.set("image_description", {"consented": ["openai", "gemini"]})
    page._deleted.add("openai")
    assert page.apply()
    assert not page.store.get("openai") and page.store.get("gemini") == "two"
    assert page.app.get("image_description")["consented"] == ["gemini"]


@pytest.mark.parametrize("ending", ["apply", "cancel"])
def test_key_removal_is_explained_as_pending_and_changes_only_the_local_store_on_save(tmp_path, ending):
    page = SettingsStub(tmp_path)
    page.store.set("openai", "synthetic-key")
    page.key_state = Control()
    page._key_status = lambda: ImageDescriptionSettingsPage._key_status(page)
    dirty = []
    parent = SimpleNamespace(_mark_dirty=lambda: dirty.append(True))
    page.GetParent = lambda: SimpleNamespace(GetParent=lambda: parent)
    ImageDescriptionSettingsPage._delete_key(page, None)
    assert page.key_state.value == "ai_key_removal_pending"
    assert page.status.value == "ai_key_removal_notice" and dirty == [True]
    assert page.store.get("openai") == "synthetic-key"
    if ending == "apply":
        assert page.apply()
        assert not page.store.get("openai") and page.key_state.value == "ai_key_missing"
        assert page.status.value == ""
    else:
        page._alive = True
        ImageDescriptionSettingsPage._destroyed(page, SimpleNamespace(GetEventObject=lambda: page, Skip=lambda: None))
        assert CredentialStore(tmp_path).get("openai") == "synthetic-key"


def test_reset_recovers_corruption_and_can_save_new_key(tmp_path):
    page = SettingsStub(tmp_path)
    page.store.set("openai", "old")
    page.store.key_path.unlink()
    page._reset = True
    page._deleted = {"openai", "gemini"}
    page.key.value = "new"
    assert page.apply()
    assert page.store.get("openai") == "new" and not page._reset


def test_close_filter_uses_original_chat_not_current_panel_conversation():
    closed = []
    session = PhotoSession("a", "original-locked-chat", "m", locked=True)
    stub = SimpleNamespace(_image_description_dialog=SimpleNamespace(session=session, _close=lambda: closed.append(True)),
                           conversation={"remoteJid": "ordinary-chat"},
                           main_window=SimpleNamespace(is_chat_locked=lambda jid: jid == "original-locked-chat"))
    ImageDescriptionMixin.close_image_description(stub, locked_only=True)
    assert closed == [True]
    ImageDescriptionMixin.close_image_description(stub, message_ids={"unrelated"})
    assert closed == [True]
    ImageDescriptionMixin.close_image_description(stub, message_ids={"m"})
    assert closed == [True, True]


def test_shortcut_does_nothing_outside_message_list(monkeypatch):
    stub = SimpleNamespace(messages_list=object())
    monkeypatch.setattr(wx.Window, "FindFocus", lambda: object())
    # No main_window/settings accesses, let alone network or dialog construction.
    ImageDescriptionMixin._on_describe_photo(stub)


@pytest.mark.parametrize("cached", [True, False])
def test_entrypoint_reads_original_encrypted_photo_and_uses_bounded_download(tmp_path, monkeypatch, cached):
    from cryptography.fernet import Fernet
    from core.image_description.service import RequestToken
    import ui.conversation_panel.image_description as module
    import ui.dialogs.image_description_dialog as dialog_module
    data, downloaded, focused = [], [], []
    key = Fernet.generate_key()
    path = tmp_path / "photo.wzmedia"
    if cached:
        path.write_bytes(Fernet(key).encrypt(b"original photo"))
    message = {"messageType": "imageMessage", "key": {"id": "m", "remoteJid": "c"},
               "message": {"imageMessage": {"caption": "private caption", "fileLength": 14}}}
    app = AppSettings(str(tmp_path))
    app.set("image_description", {"enabled": True})
    def download(snapshot, **kwargs):
        downloaded.append(kwargs)
        path.write_bytes(Fernet(key).encrypt(b"original photo"))
    mw = SimpleNamespace(app_settings=app, key=key, IsShown=lambda: True,
                         is_chat_locked=lambda jid: False, handle_media_message=download)
    panel = SimpleNamespace(main_window=mw, conversation={"remoteJid": "c"},
                            _sorted_messages=[message], messages_list=SimpleNamespace(
                                Focus=lambda index: focused.append(index), Select=lambda index: None,
                                SetFocus=lambda: None))
    class WindowStub:
        def __init__(self, panel, identity, locked, config, app, loader):
            self.session = PhotoSession(*identity, locked=locked)
            self.loader = loader
        def ShowModal(self):
            data.append(self.loader(RequestToken()))
        def _dispose(self):
            self.session.close()
        def Destroy(self):
            pass
    monkeypatch.setattr(dialog_module, "ImageDescriptionDialog", WindowStub)
    monkeypatch.setattr(module, "cached_media_path", lambda *args: str(path))
    monkeypatch.setattr(module, "active_account_id", lambda: "account")
    ImageDescriptionMixin._on_describe_photo(panel, message=message)
    assert data == [b"original photo"]
    assert focused == [0] and panel._image_description_dialog is None
    assert len(downloaded) == (0 if cached else 1)
    if downloaded:
        assert downloaded[0]["max_bytes"] == 20 * 1024 * 1024
        assert callable(downloaded[0]["cancel_check"])


def test_disabled_feature_never_opens_a_dialog_or_downloads(tmp_path):
    spoken = []
    panel = SimpleNamespace(main_window=SimpleNamespace(app_settings=AppSettings(str(tmp_path)),
                           i18n=SimpleNamespace(t=lambda key: key), output=spoken.append))
    ImageDescriptionMixin._on_describe_photo(panel, message={"messageType": "imageMessage",
                             "key": {"id": "m"}, "message": {"imageMessage": {}}})
    assert spoken == ["ai_disabled"]
