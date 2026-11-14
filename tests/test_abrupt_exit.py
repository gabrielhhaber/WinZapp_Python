"""core/abrupt_exit.py: a run that ended without closing is said by the next.

Reported 2026-11-12 (the shape below is that user's shutdown_audit.log,
pids kept, nothing else): two runs ended without any close line, the
WPPConnect Node was left running, and switching to Neonize then found the
port taken with nothing in the logs to say why.
"""
import faulthandler

from core import abrupt_exit as ae

REPORTED = """\
2026-11-12 11:51:37 [pid=8808] _stop_wpp_server START account=a session='s' port=6300 pre_close_status='CONNECTED'
2026-11-12 11:51:44 [pid=8808] taskkill /F /T node pid=23188 (flush done above)
2026-11-12 11:51:57 [pid=18780] STARTUP account=a active_session='s' paired=True
2026-11-12 14:49:38 [pid=3540] STARTUP account=a active_session='s' paired=True
""".splitlines()


def test_the_reported_runs_are_both_seen_as_ended_without_closing():
    assert ae.abrupt_previous_run(REPORTED[:3], my_pid=3540) == (18780, "2026-11-12 11:51:57")
    assert ae.abrupt_previous_run(REPORTED, my_pid=22848) == (3540, "2026-11-12 14:49:38")


def test_a_run_that_closed_or_was_forced_out_is_not_reported():
    assert ae.abrupt_previous_run(REPORTED[:2], my_pid=18780) is None
    forced = REPORTED[:3] + ["2026-11-12 12:00:00 [pid=18780] HARD EXIT: quit did not finish"]
    assert ae.abrupt_previous_run(forced, my_pid=3540) is None
    assert ae.abrupt_previous_run([], my_pid=1) is None
    assert ae.abrupt_previous_run(["junk", "  "], my_pid=1) is None


def test_this_runs_own_lines_are_not_the_previous_run():
    lines = REPORTED[:2] + ["2026-11-12 15:00:00 [pid=99] STARTUP account=a"]
    assert ae.abrupt_previous_run(lines, my_pid=99) is None


def test_the_note_goes_to_both_logs(tmp_path, caplog):
    audit_file = tmp_path / "shutdown_audit.log"
    audit_file.write_text("\n".join(REPORTED) + "\n", encoding="utf-8")
    written = []
    assert ae.note_abrupt_previous_run(str(audit_file), 22848, written.append)
    assert written == ["previous run pid=3540 ended without closing (last line 2026-11-12 14:49:38)"]
    assert "ended without closing" in caplog.text
    assert not ae.note_abrupt_previous_run(str(tmp_path / "missing.log"), 1, written.append)


def test_only_the_tail_of_a_long_audit_is_read(tmp_path):
    path = tmp_path / "shutdown_audit.log"
    path.write_text("x" * 100 + "\nsecond\nthird\n", encoding="utf-8")
    assert ae.read_tail(str(path), size=12) == ["third"]      # the cut first line dropped
    assert ae.read_tail(str(path)) == ["x" * 100, "second", "third"]


def test_the_fatal_log_is_kept_open_for_faulthandler(tmp_path, monkeypatch):
    enabled = {}
    monkeypatch.setattr(faulthandler, "enable",
                        lambda file, all_threads: enabled.update(file=file, all=all_threads))
    monkeypatch.setattr(ae, "_fatal_file", None)
    assert ae.enable_fatal_log(str(tmp_path / "fatal.log"))
    assert enabled["all"] is True and not enabled["file"].closed
    enabled["file"].close()
    assert "started" in (tmp_path / "fatal.log").read_text(encoding="utf-8")
