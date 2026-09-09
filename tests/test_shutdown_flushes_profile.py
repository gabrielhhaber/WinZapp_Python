"""Tests for letting Chrome finish writing its profile before the tree kill.

`_stop_wpp_server()` polls `status-session` until CLOSED and then runs
`taskkill /F /T` on the Node process, which takes Chrome down with it. The poll
replaced a fixed `sleep(2)` that had been landing mid-flush and corrupting
leveldb — "Session Unpaired" on the next launch. But CLOSED is a
WPPConnect-level state, not a promise that the browser has finished writing to
disk, so swapping the sleep for that poll fixed the timing only by luck.

A user's shutdown_audit.log showed the entire clean sequence in one second:

    17:37:38  _stop_wpp_server START  pre_close_status='CONNECTED'
    17:37:38  close-session POSTed — waiting for flush
    17:37:38  flush poll #1 status='CLOSED' elapsed=0.2s
    17:37:38  FLUSH OK — session reached CLOSED
    17:37:38  taskkill /F /T node pid=6192 (flush done above)
    17:43:06  STARTUP  active_session='0766109c05fe...' paired=True

and the session came back dead, needing a re-pair — with the phone still
listing the device under Linked Devices. Server-side the link was intact; the
local profile had not survived. Waiting on the process itself is the only
signal that means "nothing is writing to that profile any more".

The same misplaced signal, in a third place: it also made status-session the
wrong gate for /start-session (see test_pairing_waits_for_profile_release.py).
"""

import inspect

from main import MainWindow


def _source():
    return inspect.getsource(MainWindow._stop_wpp_server)


class TestTheKillWaitsForChrome:
    def test_it_waits_for_the_profile_before_killing(self):
        assert "wait_for_profile_release" in _source()

    def test_the_wait_comes_before_the_taskkill(self):
        source = _source()
        wait = source.index("wait_for_profile_release")
        kill = source.index('"taskkill", "/F", "/T"')
        assert wait < kill

    def test_it_waits_after_the_closed_poll_not_instead_of_it(self):
        """Both signals matter: CLOSED means WPPConnect finished its own
        teardown, the process check means the browser did. Dropping the first
        would kill a session mid-close."""
        source = _source()
        # Anchor on the calls, not on bare names: the comments in between now
        # discuss both waits by name, which made a plain index() compare prose.
        flush = source.index("self._wait_for_session_flushed(")
        wait = source.index("self.wait_for_profile_release(")
        assert flush < wait

    def test_both_outcomes_are_recorded_in_the_audit(self):
        """shutdown_audit.log is the only file that survives to the next run,
        so whether the profile was released has to be written there — that is
        exactly the question the next launch's failure raises."""
        source = _source()
        assert "Chrome released the profile before the kill" in source
        assert "leveldb may be incomplete" in source

    def test_a_missing_session_name_skips_the_wait(self):
        """A shutdown with no token has no profile to wait on, and
        wait_for_profile_release would have nothing to poll for."""
        source = _source()
        wait_at = source.index("self.wait_for_profile_release")
        preceding = source[:wait_at].rstrip().splitlines()[-2:]
        assert any("if session_name:" in line for line in preceding)

    def test_the_wait_is_bounded(self):
        """Shutdown must not hang: a profile that never releases still gets
        killed, it just gets said out loud."""
        source = _source()
        # The call spans lines now, so look at its argument list rather than a
        # single source line.
        call_at = source.index("self.wait_for_profile_release(")
        assert "timeout=" in source[call_at:call_at + 200]


class TestWppconnectLogKeepsOneGeneration:
    """The Node log was opened "w", so it only ever held the current run — the
    wrong run for "it worked, I closed it, it came back dead". Reopening the
    app to investigate destroyed the evidence, twice."""

    @staticmethod
    def _startup_source():
        return inspect.getsource(MainWindow._start_wpp_background)

    def test_the_previous_run_is_kept(self):
        source = self._startup_source()
        assert '.1"' in source or "'.1'" in source
        assert "os.replace" in source

    def test_the_rotation_happens_before_the_truncating_open(self):
        source = self._startup_source()
        rotate = source.index("os.replace")
        open_w = source.index('open(self._wpp_log_path, "w"')
        assert rotate < open_w

    def test_only_one_generation_is_kept(self):
        """These reach several MB on a busy account, and the run before last
        has never been the interesting one."""
        source = self._startup_source()
        assert '".2"' not in source

    def test_a_rotation_failure_never_blocks_startup(self):
        """Log housekeeping must not be able to stop the server from coming
        up — a locked or read-only file would otherwise strand the user."""
        source = self._startup_source()
        rotate = source.index("os.replace")
        following = source[rotate : rotate + 400]
        assert "except Exception" in following
        assert "could not rotate wppconnect.log" in following


class TestAnUnreadableProcessListIsNotAnAnswer:
    """"I could not tell" must never reach the caller as "nothing holds it".

    The only thing after wait_for_profile_release() is `taskkill /F /T` on the
    process tree Chrome is in, so reading a failed query as a release turns a
    slow or refused PowerShell spawn into a hard kill of a browser that is
    still writing its LevelDB — and audits it as "Chrome released the profile
    before the kill". Its only trace today is a logging.info into log.log,
    which the launch that would report the damage truncates.

    Measured on the reporting machine before changing this: 0.21 s idle, 0.31 s
    under six competing PowerShell processes, zero failures in 65 runs. So this
    is a latent fault rather than the cause of the losses that prompted the
    review — and still the wrong default.
    """

    class _Stub:
        wait_for_profile_release = MainWindow.wait_for_profile_release

        def __init__(self, answers):
            self._answers = list(answers)
            self.audits = []
            self.killed = 0

        def _chrome_pids_owning_session(self, session_name):
            return self._answers.pop(0) if self._answers else []

        def _shutdown_audit(self, msg):
            self.audits.append(msg)

        def _kill_orphaned_chrome_for_session(self, session_name):
            self.killed += 1

    def test_an_empty_list_still_means_released(self):
        stub = self._Stub([[]])
        assert stub.wait_for_profile_release("sess", timeout=2.0) is True
        assert stub.killed == 0

    def test_an_unreadable_answer_keeps_waiting(self):
        # None first, then a real empty answer: it must not have concluded on
        # the None.
        stub = self._Stub([None, []])
        assert stub.wait_for_profile_release("sess", timeout=5.0) is True
        assert any("unreadable" in a for a in stub.audits)

    def test_a_held_profile_is_still_reported_as_held(self):
        stub = self._Stub([["1234"]] * 50)
        assert stub.wait_for_profile_release("sess", timeout=1.0) is False
        assert stub.killed == 1

    def test_the_audit_records_how_long_the_wait_actually_took(self):
        """"Chrome exited after 6 s of real waiting" and "the first poll said
        nothing was there" are opposite diagnoses and looked identical."""
        stub = self._Stub([["1234"], []])
        stub.wait_for_profile_release("sess", timeout=5.0)
        released = [a for a in stub.audits if "profile released" in a]
        assert released and "poll(s)" in released[0]
