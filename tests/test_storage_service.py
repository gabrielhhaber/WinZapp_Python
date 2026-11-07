"""Storage service ties scope, plan and optional rules to one owned job."""
from contextlib import nullcontext
from dataclasses import replace

from core.media_cleaned import CleanedMediaIndex
from core.storage_manager.policy import Preferences, Rule
from core.storage_manager.references import References
from core.storage_manager.schedule import Schedule
from core.storage_manager.service import StorageService
from core.storage_manager.store import StorageStore
from core.storage_manager.types import Root


def service(tmp_path, monkeypatch):
    folder = tmp_path / "account"
    cache = folder / "cache"
    cache.mkdir(parents=True)
    version = cache / "v22.22.2-x64-1234abcd"
    version.mkdir()
    (version / "aabbccdd").write_bytes(b"12345")
    store = StorageStore(str(folder))
    monkeypatch.setattr(store, "lock", nullcontext)
    workers, queue = [], []
    value = StorageService(store, CleanedMediaIndex(lambda: str(folder / "cleaned.json")),
                           lambda prefs: [Root("cache", str(cache), "compile_cache", managed=True)],
                           lambda cancel: References({}), queue.append, launch=workers.append)
    return value, workers, queue


def complete(workers, queue):
    workers.pop()()
    queue.pop()()


def test_scan_does_not_publish_a_result_until_dispatched(tmp_path, monkeypatch):
    value, workers, queue = service(tmp_path, monkeypatch)
    results = []
    assert value.scan(lambda result, error: results.append((result, error)))
    assert value.snapshot is None
    complete(workers, queue)
    assert value.snapshot.total_bytes == 5 and results[0][1] is None


def test_scope_change_discards_a_scan_result(tmp_path, monkeypatch):
    value, workers, queue = service(tmp_path, monkeypatch)
    errors = []
    value.scan(lambda result, error: errors.append(error))
    value.valid = lambda: False
    complete(workers, queue)
    assert value.snapshot is None and errors == ["ScopeChanged"]


def test_saved_soft_target_only_pauses_when_user_chose_it(tmp_path, monkeypatch):
    value, workers, queue = service(tmp_path, monkeypatch)
    value.scan(lambda *args: None)
    complete(workers, queue)
    value.save_preferences(Preferences(target_bytes=4))
    assert not value.pause_downloads
    value.save_preferences(Preferences(target_bytes=4, target_action="pause_downloads"))
    assert value.pause_downloads


def test_suggestion_cannot_turn_into_automatic_action(tmp_path, monkeypatch):
    value, workers, queue = service(tmp_path, monkeypatch)
    value.scan(lambda *args: None)
    complete(workers, queue)
    value.preferences.rules = [Rule("compile_cache", enabled=True, mode="suggest")]
    assert len(value.suggested()) == 1 and value.automatic_selection() == []
    value.preferences.rules = [Rule("compile_cache", enabled=True, mode="automatic")]
    assert len(value.automatic_selection()) == 1
    value.preferences.protected.add(value.snapshot.entries[0].key)
    assert value.automatic_selection() == []


def test_preferences_can_change_without_any_file_action(tmp_path, monkeypatch):
    value, workers, queue = service(tmp_path, monkeypatch)
    value.save_preferences(Preferences(warning_bytes=10))
    assert value.snapshot is None and workers == [] and queue == []


def test_schedule_is_off_by_default_and_never_catches_up_multiple_jobs():
    schedule = Schedule()
    prefs = Preferences()
    assert not schedule.due(prefs, 0)
    assert not schedule.due(prefs, 100000)
    prefs.interval_minutes = 10
    assert not schedule.due(prefs, 100001)
    assert schedule.due(prefs, 200000)
    assert not schedule.due(prefs, 200001)


def test_startup_evaluation_waits_until_the_other_job_releases_ownership():
    schedule = Schedule()
    prefs = Preferences(scan_on_start=True)
    assert not schedule.due(prefs, 0, busy=True)
    assert schedule.due(prefs, 1)
    assert not schedule.due(prefs, 2)


def test_pausing_interval_does_not_replay_missed_evaluations():
    schedule = Schedule()
    prefs = Preferences(interval_minutes=1)
    assert not schedule.due(prefs, 0)
    prefs.interval_minutes = 0
    assert not schedule.due(prefs, 500)
    prefs.interval_minutes = 1
    assert not schedule.due(prefs, 1000)
    assert schedule.due(prefs, 1061)


def test_other_account_consent_freezes_the_current_set_until_reenabled(tmp_path, monkeypatch):
    value, _, _ = service(tmp_path, monkeypatch)
    paths = ["first"]
    value.capture_accounts = lambda: list(paths)
    value.save_preferences(Preferences(include_other_accounts=True))
    paths.append("new")
    value.save_preferences(Preferences(include_other_accounts=True))
    assert value.preferences.other_accounts == ["first"]
    value.save_preferences(Preferences(include_other_accounts=False))
    value.save_preferences(Preferences(include_other_accounts=True))
    assert value.preferences.other_accounts == ["first", "new"]


def test_a_stale_options_form_cannot_erase_new_protections(tmp_path, monkeypatch):
    value, _, _ = service(tmp_path, monkeypatch)
    stale = Preferences(warning_bytes=100)
    value.change_protection("protected", {"keep"}, True)
    value.save_preferences(stale)
    assert value.preferences.protected == {"keep"}
    value.change_protection("protected", {"keep"}, False)
    assert value.preferences.protected == set()


def test_scoped_soft_target_uses_only_the_requested_category_and_keeps_its_last_measurement(tmp_path, monkeypatch):
    value, workers, queue = service(tmp_path, monkeypatch)
    value.scan(lambda *args: None)
    complete(workers, queue)
    value.save_preferences(Preferences(target_bytes=1, target_category="media", target_action="pause_downloads"))
    assert not value.pause_downloads
    value.save_preferences(Preferences(target_bytes=1, target_category="compile_cache", target_action="pause_downloads"))
    assert value.pause_downloads
    value.snapshot = None
    assert value.pause_downloads


def test_download_gate_never_walks_the_snapshot(tmp_path, monkeypatch):
    from types import SimpleNamespace
    value, _, _ = service(tmp_path, monkeypatch)
    class UnavailableEntries:
        def __iter__(self): raise AssertionError("download gate walked storage files")
    value.snapshot = SimpleNamespace(entries=UnavailableEntries())
    value.measured_usage = {("account", "media"): 20}
    value.preferences = Preferences(target_bytes=10, target_scope="account", target_category="media",
                                     target_action="pause_downloads")
    for _ in range(100):
        assert value.pause_downloads


def test_disabled_automatic_rule_is_rechecked_after_preview(tmp_path, monkeypatch):
    value, workers, queue = service(tmp_path, monkeypatch)
    value.save_preferences(Preferences(scan_on_start=True,
        rules=[Rule("compile_cache", mode="automatic", enabled=True)]))
    value.scan(lambda *args: None)
    complete(workers, queue)
    plan = value.preview(value.automatic_selection(), "remove", automatic=True)
    value.save_preferences(Preferences(scan_on_start=True))
    probes = []
    def execute(plan, snapshot, preferences, store, index, **kwargs):
        from core.storage_manager.transactions import Result
        probes.extend(kwargs["busy"](entry) for entry in plan.entries)
        return Result(skipped=len(plan.entries))
    monkeypatch.setattr("core.storage_manager.service.execute", execute)
    value.apply(plan, lambda *args: None)
    complete(workers, queue)
    assert probes == [True]
