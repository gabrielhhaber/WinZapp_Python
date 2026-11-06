"""Opt-in scheduling has no dependency on a wx window, clock waits or network."""
from types import SimpleNamespace

from core.storage_manager.policy import Preferences, Rule
from core.storage_manager.types import Snapshot
from main_window.storage_runtime import StorageRuntime


class Service:
    def __init__(self, preferences):
        self.preferences = preferences
        self.jobs = SimpleNamespace(busy=False, close=lambda: None)
        self.scans, self.applies = [], []
        self.pause_downloads = False
        self.target_exceeded = False
    def valid(self): return True
    def scan(self, callback): self.scans.append(callback)
    def effective_preferences(self): return self.preferences
    def warning(self): return False
    def automatic_selection(self, **kwargs): return []
    def apply(self, plan, callback): self.applies.append(plan)


def runtime(preferences):
    value = Service(preferences)
    speech = []
    window = SimpleNamespace(speak_output=SimpleNamespace(output=speech.append),
                             i18n=SimpleNamespace(t=lambda key: key))
    captures = []
    result = StorageRuntime(window, value, SimpleNamespace(capture_chats=lambda: captures.append(1)))
    return result, value, captures, speech


def test_defaults_never_scan_or_act():
    value, service, captures, speech = runtime(Preferences())
    value.tick()
    assert not service.scans and not captures and not speech


def test_startup_is_one_owned_evaluation_and_dialog_suspends_the_scheduler():
    value, service, captures, _ = runtime(Preferences(scan_on_start=True))
    value.window._storage_dialog_open = True
    value.tick()
    assert not service.scans
    value.window._storage_dialog_open = False
    value.tick()
    value.tick()
    assert len(service.scans) == len(captures) == 1


def test_closed_paused_partial_and_failed_results_never_automatically_apply():
    value, service, _, _ = runtime(Preferences(scan_on_start=True))
    service.automatic_selection = lambda **kwargs: ["would-have-been-eligible"]
    snapshot = Snapshot()
    snapshot.issue("unreadable")
    value.scanned(snapshot, None)
    value.scanned(Snapshot(), "Failure")
    service.preferences.paused_until = 10**12
    value.scanned(Snapshot(), None)
    value.tick()
    value.close()
    value.scanned(Snapshot(), None)
    assert not service.applies and not service.scans


def test_automatic_cache_work_uses_an_explicit_automatic_plan():
    value, service, _, _ = runtime(Preferences(scan_on_start=True,
                                             rules=[Rule("compile_cache", mode="automatic", enabled=True)]))
    service.automatic_selection = lambda **kwargs: ["cache"]
    requests = []
    def preview(keys, method, **kwargs):
        requests.append((keys, method, kwargs))
        return "fixed-plan"
    service.preview = preview
    value.scanned(Snapshot(), None)
    assert requests == [(["cache"], "remove", {"automatic": True})]
    assert service.applies == ["fixed-plan"]


def test_notify_only_target_notifies_without_pausing_or_starting_file_actions():
    from core.storage_manager.service import StorageService
    prefs = Preferences(interval_minutes=1, target_bytes=1, target_action="notify", warning_repeat_minutes=0)
    service = StorageService(SimpleNamespace(preferences=lambda **kw: prefs), None,
        lambda prefs: [], lambda cancel: None, lambda fn: fn())
    snapshot = service.snapshot = Snapshot()
    service.measured_usage = {("all", "all"): 20}
    speech, actions = [], []
    window = SimpleNamespace(i18n=SimpleNamespace(t=lambda key: "." if key == "decimal_separator" else "{total} {count}"),
        speak_output=SimpleNamespace(output=speech.append))
    service.apply = lambda *a: actions.append(1)
    value = StorageRuntime(window, service, None)
    value.scanned(snapshot, None)
    value.scanned(snapshot, None)
    assert len(speech) == 1 and service.target_exceeded and not service.pause_downloads and not actions
    service.measured_usage = {}
    value.scanned(snapshot, None)
    service.measured_usage = {("all", "all"): 20}
    value.scanned(snapshot, None)
    assert len(speech) == 2 and not actions
