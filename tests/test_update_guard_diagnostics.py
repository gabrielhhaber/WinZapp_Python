"""Only temporary receipt files and generated text; no native/child calls."""
import pytest
from pathlib import Path


@pytest.mark.parametrize("value,expected", [
    ("stage:compile", "stage:compile"), ("stage:running", "stage:running"),
    ("error:job_assign:5", "error:job_assign:5"),
    ("error:guard_lock:32", "error:guard_lock:32"),
    ("completed:0", "completed:0"), ("completed:5", "completed:5"),
    ("error:compile:0", "error:compile:0"),
    ("error:job_assign:5\nprivate data", "unavailable"),
    ("error:job_assign:4294967296", "unavailable"),
    ("error:unknown:1", "unavailable"), ("stage:arbitrary_path", "unavailable"),
    ("C:\\Users\\private\\file", "unavailable"), ("", "unavailable"),
])
def test_only_known_stages_and_bounded_codes_can_enter_logs(tmp_path, value, expected):
    from core.update_guard_diagnostics import diagnostic_summary
    path = tmp_path / "diagnostic"
    path.write_text(value, encoding="ascii")
    assert diagnostic_summary(str(path)) == expected


def test_missing_diagnostic_never_becomes_a_permission_or_an_exception(tmp_path):
    from core.update_guard_diagnostics import diagnostic_summary
    assert diagnostic_summary(str(tmp_path / "missing")) == "unavailable"


def test_diagnostic_failures_cannot_hide_an_already_visible_commit(tmp_path, monkeypatch):
    from core import update_guard_launch as launch
    from core import update_install_guard as guard
    paths = guard.guard_paths(str(tmp_path), "a" * 32)
    guard.write_signal(paths["commit"], "commit")
    monkeypatch.setattr(launch, "write_recovery_note", lambda *args: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(launch.logging, "error", lambda *args: (_ for _ in ()).throw(OSError()))
    assert launch.launch_guarded(str(tmp_path / "run.bat"), paths, "", "handoff", "exe", "log",
                                  111, False, None, None, None, None, {}, recovery_text="Recovery")
    assert not Path(paths["abort"]).exists()


def test_guardian_reports_stage_without_serializing_exception_messages():
    from core.update_guard_process import guardian_script
    from core.update_install_guard import guard_paths
    script = guardian_script("batch", guard_paths("root", "a" * 32))
    assert "Write-Diagnostic 'stage:compile'" in script
    assert "Write-Diagnostic ('error:' + $phase + ':0')" in script
    assert "exception.Message" not in script and "$_" not in script
