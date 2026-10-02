"""Native settings methods on recording stubs: no windows, keys, network or speech."""
from types import SimpleNamespace

import pytest

from tests.test_image_description_ui_logic import Control


class Choice(Control):
    def SetItems(self, items):
        self.items = items

    def SetSelection(self, selection):
        self.selection = selection

    def Freeze(self):
        self.frozen = True

    def Thaw(self):
        self.frozen = False


@pytest.fixture
def page(monkeypatch):
    from ui.dialogs.image_description_models import ModelSelectionMixin
    import ui.dialogs.image_description_models as module
    class Stub(ModelSelectionMixin):
        def __init__(self):
            self._alive = True
            self._provider = "openai"
            self._init_model_catalog()
            self.model = Control("gpt-4.1-mini")
            self.model_choice = Choice(selection=-1)
            self.get_models = Control()
            self.status = Control()
            self.key = Control("synthetic")
            self.dirty = []
            self.main_window = SimpleNamespace(output=lambda text: self.dirty.append(("speak", text)))

        def _t(self, key):
            return key

        def _current_key(self):
            return self.key.value

        def _cancel_probe(self):
            pass

        def GetParent(self):
            return SimpleNamespace(GetParent=lambda: SimpleNamespace(_mark_dirty=lambda: self.dirty.append(True)))

    class Timer:
        def __init__(self, milliseconds, callback, generation):
            self.callback, self.generation, self.stopped = callback, generation, False
        def Stop(self):
            self.stopped = True
    monkeypatch.setattr(module.wx, "CallLater", Timer)
    monkeypatch.setattr(module.wx, "CallAfter", lambda f, *args: f(*args))
    return Stub(), module


def choices():
    from core.image_description.model_catalog import ModelOption
    return (ModelOption("gpt-4.1-mini", "GPT-4.1 Mini"), ModelOption("gpt-4.1", "GPT-4.1"))


def start(page, monkeypatch):
    page, module = page
    jobs = []
    monkeypatch.setattr(module, "submit", lambda work, complete: jobs.append((work, complete)))
    page._fetch_models(None)
    assert page.status.value == "ai_models_loading" and not page.get_models.enabled
    return page, module, jobs


def test_success_keeps_existing_model_and_focus_and_changes_no_saved_settings(page, monkeypatch):
    page, _, jobs = start(page, monkeypatch)
    jobs[0][1](choices(), None)
    assert page.model.value == "gpt-4.1-mini"
    assert page.model_choice.GetSelection() == 0 and page.model_choice.enabled
    assert not page.model_choice.frozen and not hasattr(page.model_choice, "focused")
    assert page.get_models.enabled and page._model_list_timer is None
    assert page.dirty == [("speak", "ai_models_ready")]


def test_unknown_manual_model_is_never_replaced_by_first_list_item(page, monkeypatch):
    page, _, jobs = start(page, monkeypatch)
    page.model.value = "custom-model"
    jobs[0][1](choices(), None)
    assert page.model.value == "custom-model" and page.model_choice.GetSelection() == -1


def test_explicit_selection_updates_the_model_and_marks_apply_dirty(page, monkeypatch):
    page, _, jobs = start(page, monkeypatch)
    jobs[0][1](choices(), None)
    page.model_choice.selection = 1
    page._select_model(SimpleNamespace(Skip=lambda: None))
    assert page.model.value == "gpt-4.1" and page.dirty[-1] is True


@pytest.mark.parametrize("ending", ["provider", "key", "destroy", "timeout"])
def test_old_list_cannot_change_ui_after_invalidation(page, monkeypatch, ending):
    page, _, jobs = start(page, monkeypatch)
    token, timer = page._model_list_token, page._model_list_timer
    if ending == "provider":
        page._provider = "gemini"
    elif ending == "destroy":
        page._alive = False
    elif ending == "timeout":
        timer.callback(timer.generation)
    else:
        page._cancel_model_list(clear=True)
    previous = page.status.value
    jobs[0][1](choices(), None)
    assert page.status.value == previous
    assert page.model.value == "gpt-4.1-mini"
    if ending in ("key", "timeout"):
        assert token.cancelled.is_set() and timer.stopped and page.get_models.enabled
    if ending == "key":
        assert page.status.value == ""


@pytest.mark.parametrize("result,error", [((), None), (None, "ai_error_quota")])
def test_empty_or_failed_list_keeps_manual_model_available(page, monkeypatch, result, error):
    page, _, jobs = start(page, monkeypatch)
    jobs[0][1](result, error)
    assert page.model.value == "gpt-4.1-mini" and page.get_models.enabled
    assert not page.model_choice.enabled
    assert page.status.value == (error or "ai_models_empty")


def test_watchdog_recovers_without_any_worker_completion(page, monkeypatch):
    page, _, jobs = start(page, monkeypatch)
    timer = page._model_list_timer
    timer.callback(timer.generation)
    assert page.get_models.enabled and page._model_list_timer is None
    assert page.status.value == "ai_error_timeout" and timer.stopped


def test_double_click_does_not_queue_another_request(page, monkeypatch):
    page, _, jobs = start(page, monkeypatch)
    page._fetch_models(None)
    assert len(jobs) == 1


def test_list_uses_draft_key_but_does_not_save_it(page, monkeypatch):
    page, module, jobs = start(page, monkeypatch)
    calls = []
    monkeypatch.setattr(module, "fetch_models", lambda provider, key, token:
                        calls.append((provider, key)) or choices())
    assert jobs[0][0]() == choices() and calls == [("openai", "synthetic")]
    assert not page.dirty


def test_missing_key_and_dispatch_failure_restore_readable_status(page, monkeypatch):
    from core.image_description.errors import DescriptionError
    page, module = page
    jobs = []
    monkeypatch.setattr(module, "submit", lambda *args: jobs.append(True))
    page.key.value = ""
    page._fetch_models(None)
    assert not jobs and page.status.value == "ai_error_credentials"
    page.key.value = "synthetic"
    def fail(*args):
        raise DescriptionError("busy")
    monkeypatch.setattr(module, "submit", fail)
    page._fetch_models(None)
    assert page.get_models.enabled and page.status.value == "ai_error_busy"
    assert page._model_list_token is None and page._model_list_timer is None


def test_unexpected_dispatch_error_is_safe_and_does_not_leave_loading(page, monkeypatch):
    page, module = page
    def fail(*args):
        raise RuntimeError("private synthetic failure")
    monkeypatch.setattr(module, "submit", fail)
    page._fetch_models(None)
    assert page.get_models.enabled and page.status.value == "ai_error_response"
    assert page._model_list_timer is None and page._model_list_token is None


@pytest.mark.parametrize("action", ["key_changed", "delete_key", "destroyed"])
def test_real_settings_actions_cancel_metadata_before_stale_completion(page, monkeypatch, action):
    from ui.dialogs.image_description_settings import ImageDescriptionSettingsPage
    page, _, jobs = start(page, monkeypatch)
    token = page._model_list_token
    page._hide_key = lambda: None
    page._key_status = lambda: None
    page._deleted = set()
    page._drafts = {}
    page._probe = None
    event = SimpleNamespace(Skip=lambda: None, GetEventObject=lambda: page)
    getattr(ImageDescriptionSettingsPage, "_" + action)(page, event)
    previous = page.status.value
    jobs[0][1](choices(), None)
    assert token.cancelled.is_set() and page.status.value == previous
    assert page.model.value == "gpt-4.1-mini"


def test_manual_identifier_updates_list_selection_without_touching_focus(page, monkeypatch):
    page, _, jobs = start(page, monkeypatch)
    jobs[0][1](choices(), None)
    page.model.value = "custom-model"
    page._manual_model_changed(SimpleNamespace(Skip=lambda: None))
    assert page.model_choice.selection == -1 and page.model.value == "custom-model"
    assert not hasattr(page.model_choice, "focused")


def test_provider_change_discards_previous_provider_catalog(page, monkeypatch):
    from ui.dialogs.image_description_settings import ImageDescriptionSettingsPage
    page, _, jobs = start(page, monkeypatch)
    page._drafts, page._models = {}, {}
    page.provider = Control(selection=1)
    page._hide_key = lambda: None
    page._key_status = lambda: None
    token = page._model_list_token
    ImageDescriptionSettingsPage._change_provider(page, SimpleNamespace(Skip=lambda: None))
    jobs[0][1](choices(), None)
    assert page._provider == "gemini" and token.cancelled.is_set()
    assert page.model.value == "gemini-3.8-flash"
    assert not page.model_choice.enabled and not page.model_choice.items


def test_late_old_catalog_does_not_release_new_requests_watchdog(page, monkeypatch):
    page, _, jobs = start(page, monkeypatch)
    page._cancel_model_list(clear=True)
    page._fetch_models(None)
    timer = page._model_list_timer
    jobs[0][1](choices(), None)
    assert page._model_list_timer is timer and not timer.stopped
    assert not page.get_models.enabled and page.status.value == "ai_models_loading"
    jobs[1][1](choices(), None)
    assert timer.stopped and page.get_models.enabled


def test_expired_result_is_rejected_even_before_the_gui_watchdog_fires(page, monkeypatch):
    page, _, jobs = start(page, monkeypatch)
    token = page._model_list_token
    token.clock = lambda: token.deadline + 1
    jobs[0][1](choices(), None)
    assert page.status.value == "ai_error_timeout"
    assert not page.model_choice.enabled and page.get_models.enabled


def test_disposal_cancels_without_accessing_destroyed_native_controls(page, monkeypatch):
    from ui.dialogs.image_description_settings import ImageDescriptionSettingsPage
    page, _, jobs = start(page, monkeypatch)
    token = page._model_list_token
    def forbidden(*args):
        raise AssertionError("destroyed widgets must not be touched")
    page.get_models.Enable = forbidden
    page.model_choice.SetItems = forbidden
    page._drafts, page._probe = {}, None
    ImageDescriptionSettingsPage._destroyed(page, SimpleNamespace(GetEventObject=lambda: page, Skip=lambda: None))
    jobs[0][1](choices(), None)
    assert not page._alive and token.cancelled.is_set() and page._model_list_timer is None
