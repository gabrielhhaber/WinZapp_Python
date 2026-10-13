"""Spoken "X of Y" progress for the long sync sweeps.

A sync round (a full one on first pairing or F5 most of all, but also the
update when WinZapp opens after days away) and the media phase after it can
take minutes; between "synchronization started" and "finished" the user heard
nothing, so a long sweep could not be told apart from a stuck one. Both sweeps
now report every few seconds (core/sync_progress.py), through
MainWindow._report_sync_progress(), which follows the same mute as every other
sync announcement and stays silent during a voice call. The once-a-minute
safety poll stays quiet (its side is in tests/test_periodic_poll_delta.py).
A round's full and incremental passes share one count (its side is in
tests/test_run_sync_warm_path.py).

MainWindow is a wx.Frame, so the methods run unbound against small stubs.
"""

import threading
import time
import types

import pytest

import core.sync_progress as sync_progress
import main
from core.sync_progress import PROGRESS_INTERVAL_SECONDS, ProgressThrottle
from main import MainWindow

A = "5511900000000@s.whatsapp.net"


class _Clock:
    def __init__(self, start=100.0):
        self.now = start

    def __call__(self):
        return self.now


class TestProgressThrottle:
    def test_nothing_is_due_before_the_interval(self):
        clock = _Clock()
        progress = ProgressThrottle(10, clock=clock)
        clock.now += PROGRESS_INTERVAL_SECONDS - 0.1
        assert progress.step() is False
        assert progress.done == 1

    def test_a_report_is_due_once_the_interval_passed(self):
        clock = _Clock()
        progress = ProgressThrottle(10, clock=clock)
        clock.now += PROGRESS_INTERVAL_SECONDS
        assert progress.step() is True

    def test_the_interval_restarts_after_each_report(self):
        clock = _Clock()
        progress = ProgressThrottle(10, clock=clock)
        clock.now += PROGRESS_INTERVAL_SECONDS
        assert progress.step() is True
        clock.now += 1
        assert progress.step() is False
        clock.now += PROGRESS_INTERVAL_SECONDS
        assert progress.step() is True
        assert progress.done == 3

    def test_the_last_item_is_never_reported(self):
        """The sweep's own completion announcement follows right after it;
        "10 of 10" first would only say the same thing twice."""
        clock = _Clock()
        progress = ProgressThrottle(2, clock=clock)
        clock.now += 60
        assert progress.step() is True
        clock.now += 60
        assert progress.step() is False

    def test_an_empty_sweep_reports_nothing(self):
        progress = ProgressThrottle(0, clock=_Clock())
        assert progress.total == 0
        assert progress.step() is False


class _ReportStub:
    _announce_sync_events_enabled = MainWindow._announce_sync_events_enabled
    _report_sync_progress = MainWindow._report_sync_progress
    _voice_call_in_progress = MainWindow._voice_call_in_progress
    _VOICE_CALL_PAUSE_MAX_SECONDS = MainWindow._VOICE_CALL_PAUSE_MAX_SECONDS
    _active_voice_call = None
    _voice_call_pause_since = 0.0

    def __init__(self, settings=None, background_mode=False):
        self.settings = settings or {}
        self.background_mode = background_mode
        self.statuses = []
        self.spoken = []

    def _set_status(self, text):
        self.statuses.append(text)

    def output(self, text, interrupt=False):
        self.spoken.append((text, interrupt))


class TestReportSyncProgress:
    def test_shown_and_spoken_without_interrupting(self):
        stub = _ReportStub()
        stub._report_sync_progress("Syncing chats: 3 of 10", "announce_sync_progress")
        assert stub.statuses == ["Syncing chats: 3 of 10"]
        assert stub.spoken == [("Syncing chats: 3 of 10", False)]

    def test_the_sync_announcement_mute_silences_it(self):
        stub = _ReportStub({"general": {"announce_sync_events": False}})
        stub._report_sync_progress("Syncing chats: 3 of 10", "announce_sync_progress")
        assert stub.statuses == ["Syncing chats: 3 of 10"]
        assert stub.spoken == []

    def test_background_mode_silences_it(self):
        stub = _ReportStub(background_mode=True)
        stub._report_sync_progress("Syncing chats: 3 of 10", "announce_sync_progress")
        assert stub.statuses == ["Syncing chats: 3 of 10"]
        assert stub.spoken == []

    def test_a_voice_call_silences_it(self):
        """The chat sweep keeps running through a call on purpose (its failure
        set must stay honest); its progress must not talk over the call."""
        stub = _ReportStub()
        stub._active_voice_call = {"id": "CALL1"}
        stub._report_sync_progress("Syncing chats: 3 of 10", "announce_sync_progress")
        assert stub.statuses == ["Syncing chats: 3 of 10"]
        assert stub.spoken == []

    @pytest.mark.parametrize("key", ["announce_sync_progress", "announce_media_progress"])
    def test_its_own_spoken_content_box_silences_only_it(self, key):
        """Settings > Conteúdo falado has one box per sweep: unticking the
        chats one keeps the media progress, and the other way round."""
        other = ({"announce_sync_progress", "announce_media_progress"} - {key}).pop()
        stub = _ReportStub({"speech_content": {key: False}})
        stub._report_sync_progress("muted", key)
        stub._report_sync_progress("spoken", other)
        assert stub.statuses == ["muted", "spoken"]
        assert stub.spoken == [("spoken", False)]


class TestTheSpokenContentBoxes:
    def test_both_default_to_on(self):
        import json
        from pathlib import Path

        from core.utils import DEFAULT_SETTINGS

        defaults = json.loads((Path(__file__).resolve().parents[1] / "client" / "data"
                               / "settings_default.json").read_text(encoding="utf-8"))
        for key in ("announce_sync_progress", "announce_media_progress"):
            assert defaults["speech_content"][key] is True
            assert DEFAULT_SETTINGS["speech_content"][key] is True


class _I18n:
    def t(self, key):
        return {
            "sync_chats_progress": "chats {done}/{total}",
            "sync_media_progress": "media {done}/{total} ({downloaded})",
        }[key]


@pytest.fixture
def every_step_is_due(monkeypatch):
    """A clock that jumps a whole interval per reading, and a CallAfter that
    runs at once, so each finished item (but the last) reports."""
    ticks = iter(range(0, 10_000, int(PROGRESS_INTERVAL_SECONDS)))
    monkeypatch.setattr(sync_progress, "time",
                        types.SimpleNamespace(monotonic=lambda: float(next(ticks))))
    monkeypatch.setattr(main.wx, "CallAfter", lambda fn, *a, **kw: fn(*a, **kw))


class _ChatSweepStub:
    """Just what the real sync_remote_chats() reads."""

    _normalize_jid = staticmethod(MainWindow._normalize_jid)
    _voice_call_in_progress = MainWindow._voice_call_in_progress
    _VOICE_CALL_PAUSE_MAX_SECONDS = MainWindow._VOICE_CALL_PAUSE_MAX_SECONDS
    _active_voice_call = None
    _voice_call_pause_since = 0.0

    def __init__(self):
        self.chats = {}
        self.i18n = _I18n()
        self._sync_run_id = 1
        self._sync_failures_lock = threading.Lock()
        self._sync_failed_chats = set()
        self._delta_unsatisfied_chats = set()
        self._absent_chats = set()
        self._message_retry_jids = set()
        self._history_still_landing = False
        self.reports = []
        self.speech_keys = set()

    def sync_chat_messages(self, chat, expected_run_id=None, sync_mode="full"):
        return True

    def _report_sync_progress(self, text, speech_key):
        self.reports.append(text)
        self.speech_keys.add(speech_key)

    def _persist_message_retry_jids(self):
        pass

    def _persist_backfill_pending_state(self):
        pass

    def _persist_history_gap_jids(self):
        pass


def _chats(n):
    return [{"remoteJid": f"55119{i:08d}@s.whatsapp.net", "t": 1000 - i} for i in range(n)]


class TestTheChatSweepReportsProgress:
    def test_a_long_pass_reports_x_of_y_but_not_the_end(self, every_step_is_due):
        stub = _ChatSweepStub()
        MainWindow.sync_remote_chats(stub, _chats(4), incremental=False, expected_run_id=1)
        assert stub.reports == ["chats 1/4", "chats 2/4", "chats 3/4"]
        # Settings > Conteúdo falado: the chats box, not the media one.
        assert stub.speech_keys == {"announce_sync_progress"}

    def test_a_shared_count_runs_on_across_passes(self, every_step_is_due):
        """_run_sync() hands its full and incremental passes one count, so
        the second pass goes on from where the first stopped."""
        stub = _ChatSweepStub()
        progress = ProgressThrottle(5)
        MainWindow.sync_remote_chats(stub, _chats(2), incremental=False,
                                     expected_run_id=1, progress=progress)
        MainWindow.sync_remote_chats(stub, _chats(3), incremental=True,
                                     expected_run_id=1, progress=progress)
        assert stub.reports == ["chats 1/5", "chats 2/5", "chats 3/5", "chats 4/5"]

    def test_quiet_wins_over_a_shared_count(self, every_step_is_due):
        stub = _ChatSweepStub()
        MainWindow.sync_remote_chats(stub, _chats(4), incremental=True, expected_run_id=1,
                                     quiet=True, progress=ProgressThrottle(4))
        assert stub.reports == []

    def test_quiet_reports_nothing(self, every_step_is_due):
        stub = _ChatSweepStub()
        MainWindow.sync_remote_chats(stub, _chats(4), incremental=True,
                                     expected_run_id=1, quiet=True)
        assert stub.reports == []

    def test_a_superseded_pass_reports_nothing(self, every_step_is_due):
        """F5 or a wipe mid-pass: these numbers belong to a round that is
        gone, and the round that replaced it announces its own start."""
        stub = _ChatSweepStub()
        stub._sync_run_id = 2
        MainWindow.sync_remote_chats(stub, _chats(4), incremental=False, expected_run_id=1)
        assert stub.reports == []


def _media_msg(msg_id, message_type="imageMessage"):
    return {"key": {"id": msg_id}, "messageType": message_type,
            "messageTimestamp": int(time.time())}


class _MediaSweepStub:
    _MEDIA_SYNC_TIMEOUT = 60
    _MEDIA_SYNC_WORKERS = 1
    sync_media_for_all_chats = MainWindow.sync_media_for_all_chats
    _voice_call_in_progress = MainWindow._voice_call_in_progress
    _VOICE_CALL_PAUSE_MAX_SECONDS = MainWindow._VOICE_CALL_PAUSE_MAX_SECONDS
    _active_voice_call = None
    _voice_call_pause_since = 0.0

    def __init__(self, msgs, eligible, downloads=()):
        self.chats = {A: {"messages": {"messages": {"records": list(msgs)}}}}
        self.i18n = _I18n()
        self._eligible = set(eligible)
        self._downloads = set(downloads)
        self.fetched = []
        self.reports = []
        self.speech_keys = set()

    def _media_sync_candidate(self, msg, explicit=False):
        return msg["messageType"] if msg["key"]["id"] in self._eligible else None

    def sync_if_media(self, msg, timeout=60, explicit=False):
        self.fetched.append(msg["key"]["id"])
        return msg["key"]["id"] in self._downloads

    def _report_sync_progress(self, text, speech_key):
        self.reports.append(text)
        self.speech_keys.add(speech_key)

    def _save_media_failed_ids(self):
        pass


class TestTheSweepCountsOnlyCandidates:
    def test_messages_that_would_be_skipped_are_not_queued(self, every_step_is_due):
        """The total is what can still be downloaded. Counting every media
        message in memory (years past the CDN TTL and the caps) made an
        honest count sound stuck: it never got near its total."""
        msgs = [_media_msg(str(i)) for i in range(10)]
        stub = _MediaSweepStub(msgs, eligible={"1", "4", "7", "9"}, downloads={"4"})

        assert stub.sync_media_for_all_chats() == 1

        assert sorted(stub.fetched) == ["1", "4", "7", "9"]
        assert stub.reports[-1] == "media 3/4 (1)"
        assert all("/4 " in report for report in stub.reports)
        assert stub.speech_keys == {"announce_media_progress"}

    def test_nothing_eligible_returns_at_once(self, every_step_is_due):
        stub = _MediaSweepStub([_media_msg("1")], eligible=set())
        assert stub.sync_media_for_all_chats() == 0
        assert stub.fetched == []
        assert stub.reports == []

    def test_quiet_reports_nothing(self, every_step_is_due):
        msgs = [_media_msg(str(i)) for i in range(5)]
        stub = _MediaSweepStub(msgs, eligible={m["key"]["id"] for m in msgs})
        stub.sync_media_for_all_chats(quiet=True)
        assert len(stub.fetched) == 5
        assert stub.reports == []

    def test_a_stopped_sweep_reports_nothing(self, every_step_is_due):
        msgs = [_media_msg(str(i)) for i in range(5)]
        stub = _MediaSweepStub(msgs, eligible={m["key"]["id"] for m in msgs})
        stub.sync_media_for_all_chats(should_stop=lambda: bool(stub.fetched))
        assert stub.reports == []


class _CandidateStub:
    _MEDIA_MAX_AGE_SECONDS = MainWindow._MEDIA_MAX_AGE_SECONDS
    _media_sync_candidate = MainWindow._media_sync_candidate

    def __init__(self, auto_download=True):
        self.settings = {"storage": {"auto_download_media": auto_download}}
        self._media_failed_ids = {}

    def _media_max_download_days(self):
        return 0

    def _media_max_download_bytes(self):
        return 0


class TestTheCandidateCheck:
    def test_a_fresh_media_message_is_a_candidate(self):
        assert _CandidateStub()._media_sync_candidate(_media_msg("3EB0AA")) == "imageMessage"

    def test_past_the_cdn_ttl_is_not(self):
        msg = _media_msg("3EB0AA")
        msg["messageTimestamp"] = int(time.time()) - MainWindow._MEDIA_MAX_AGE_SECONDS - 60
        assert _CandidateStub()._media_sync_candidate(msg) is None

    def test_a_known_expired_id_is_not(self):
        stub = _CandidateStub()
        stub._media_failed_ids = {"3EB0AA": 0}
        assert stub._media_sync_candidate(_media_msg("3EB0AA")) is None

    def test_the_switch_off_leaves_only_explicit_requests(self):
        stub = _CandidateStub(auto_download=False)
        assert stub._media_sync_candidate(_media_msg("3EB0AA")) is None
        assert stub._media_sync_candidate(_media_msg("3EB0AA"), explicit=True) == "imageMessage"

