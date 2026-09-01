"""The history-sync progress line has to say why nothing is arriving.

A captured session that never synced logged, every fifteen seconds for ten
minutes:

    [history-sync] Restarted RECENT wait: messages=0 chats=0 unprocessed=0
    recent_complete=False initial_complete=False — 584s of budget left.

Every one of those fields reads the same whether history is merely slow or
WhatsApp Web cannot ingest history at all. /history-sync-status had answered
``backendWorkerBridgeReady`` all along — the field its own controller documents
as the one that matters — and nothing on the Python side ever wrote it down, so
the log could not settle the question it was captured to settle.

describe_history_sync_health() is the part that can be tested without a wx.App:
it is a module-level function in main.py precisely so it can be called here
directly rather than through a stub frame.
"""

import pytest

from main import describe_history_sync_health


class TestTheDecisiveFieldsAreInTheLine:
    def test_reports_a_dead_bridge(self):
        line = describe_history_sync_health(
            {"backendWorkerBridgeReady": False, "storeCounts": {"chat": 0}}
        )
        assert "bridge=False" in line

    def test_reports_a_live_bridge(self):
        line = describe_history_sync_health({"backendWorkerBridgeReady": True})
        assert "bridge=True" in line

    def test_reports_stored_but_undecoded_chunks(self):
        # The signature of the previously fixed bug: chunks delivered by the
        # phone and parked at 'notification_stored'. Distinct from
        # unprocessedChunks, which the caller's own line already carries.
        line = describe_history_sync_health(
            {"storeCounts": {"message": 12, "history-sync-notification": 22}}
        )
        assert "stored_chunks=22" in line

    def test_reports_page_visibility_focus_and_version(self):
        line = describe_history_sync_health(
            {
                "pageVisibility": "visible",
                "pageHasFocus": False,
                "webVersion": "2.3000.1046208945",
            }
        )
        assert "page=visible/focus=False" in line
        assert "web=2.3000.1046208945" in line

    def test_reports_the_service_worker_state_not_the_whole_object(self):
        line = describe_history_sync_health(
            {
                "serviceWorker": {
                    "scriptURL": "https://web.whatsapp.com/serviceworker.js",
                    "state": "activated",
                }
            }
        )
        assert "sw=activated" in line

    def test_no_controlling_service_worker_reads_as_none(self):
        assert "sw=none" in describe_history_sync_health({"serviceWorker": None})


class TestItSurvivesAnOlderServer:
    """A WPPConnect Server built before these fields existed simply omits
    them; the line must stay readable instead of raising."""

    def test_missing_fields_become_question_marks(self):
        line = describe_history_sync_health({})
        assert "bridge=?" in line
        assert "web=?" in line

    def test_store_counts_of_the_wrong_shape_are_tolerated(self):
        # safe() writes an "err: ..." string into a field it could not read,
        # so storeCounts is not always a dict.
        line = describe_history_sync_health({"storeCounts": "cannot open model-storage"})
        assert "stored_chunks=?" in line

    @pytest.mark.parametrize("bad", [None, "", [], 0])
    def test_a_non_dict_status_never_raises(self, bad):
        assert describe_history_sync_health(bad) == "no status"
