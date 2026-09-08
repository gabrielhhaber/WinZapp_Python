"""Both progress bars measured the wrong thing, in different ways.

**Download.** The gauge was fed by `iter_content` over the localhost hop
between Node and Python — but WPPConnect fetches the whole file from WhatsApp's
CDN and decrypts it *before* writing a single byte back. So the bar sat at 0%
for the entire real wait, then flashed to 100% as the finished file crossed a
loopback socket at gigabytes per second. It measured the fast part.

**Upload.** The server only emitted when `Number(mediaData.progressiveStage)`
was finite. Grepped, not guessed: `progressiveStage` does not appear anywhere
in @wppconnect/wa-js 4.6.0, and `mediaData.mediaStage` — the field wa-js itself
subscribes to — carries a lifecycle *string* (its own handler logs "message
file <id> is <stage>"). `Number("ENCRYPT")` is NaN, so the guard rejected every
event: the bar was inert, not merely inaccurate, and only moved when the send
completed and something else forced it to 1.0.
"""

import inspect
import re
import types
from pathlib import Path

import pytest

from ui.conversations import ConversationsPanel


API = Path(__file__).resolve().parents[1] / "client" / "api_patches" / "src"


def _panel():
    panel = ConversationsPanel.__new__(ConversationsPanel)
    panel._media_upload_progress = {}
    panel._upload_stages_seen = {}
    panel._download_progress = {}
    panel._media_transfer_started = set()
    panel._sorted_messages = []
    panel.messages_list = types.SimpleNamespace(
        SetItemText=lambda *a: None, Refresh=lambda: None)
    panel._render_message_line = lambda msg: "rendered"
    panel.gauge_values = []
    panel._update_media_transfer_gauge = panel.gauge_values.append
    return panel


class TestUploadMovesWithoutANumber:
    """The whole complaint: on a current build there is no number to move it."""

    def test_a_stage_name_alone_advances_the_bar(self):
        panel = _panel()
        ConversationsPanel.update_media_upload_progress(
            panel, "up-1", None, "ENCRYPT")
        assert panel._media_upload_progress["up-1"] > 0

    def test_each_new_stage_advances_it_further(self):
        panel = _panel()
        for stage in ("RESIZE", "ENCRYPT", "UPLOAD"):
            ConversationsPanel.update_media_upload_progress(
                panel, "up-1", None, stage)
        assert panel._media_upload_progress["up-1"] == pytest.approx(0.45)

    def test_the_same_stage_reported_twice_is_not_progress(self):
        """WhatsApp re-fires the change event; a repeat is not forward motion."""
        panel = _panel()
        ConversationsPanel.update_media_upload_progress(panel, "up-1", None, "UPLOAD")
        first = panel._media_upload_progress["up-1"]
        ConversationsPanel.update_media_upload_progress(panel, "up-1", None, "UPLOAD")
        assert panel._media_upload_progress["up-1"] == first

    def test_stages_never_reach_completion_on_their_own(self):
        """Only the send finishing means done. A bar at 100% while the file is
        still going up is a worse lie than one that stops at 90%."""
        panel = _panel()
        for i in range(50):
            ConversationsPanel.update_media_upload_progress(
                panel, "up-1", None, f"STAGE_{i}")
        assert panel._media_upload_progress["up-1"] <= 0.9

    def test_a_real_number_still_wins_when_a_build_provides_one(self):
        panel = _panel()
        ConversationsPanel.update_media_upload_progress(panel, "up-1", 0.72, "UPLOAD")
        assert panel._media_upload_progress["up-1"] == pytest.approx(0.72)

    def test_neither_a_number_nor_a_stage_does_nothing(self):
        panel = _panel()
        ConversationsPanel.update_media_upload_progress(panel, "up-1", None, "")
        assert "up-1" not in panel._media_upload_progress

    def test_two_uploads_do_not_share_a_stage_history(self):
        panel = _panel()
        ConversationsPanel.update_media_upload_progress(panel, "up-1", None, "UPLOAD")
        ConversationsPanel.update_media_upload_progress(panel, "up-2", None, "UPLOAD")
        assert panel._media_upload_progress["up-2"] > 0


class TestTheServerStopsDiscardingUploadEvents:
    @staticmethod
    def _watch_source():
        source = (API / "controller" / "messageController.ts").read_text(
            encoding="utf-8")
        start = source.index("async function watchMediaUpload(")
        return source[start:source.index("\nexport async function", start)]

    def test_the_stage_name_is_sent(self):
        assert "mediaData?.mediaStage" in self._watch_source()

    def test_emission_is_no_longer_gated_on_a_finite_number(self):
        """`if (Number.isFinite(numeric))` around the emit is what made this
        inert — nothing on a current build ever satisfies it."""
        body = self._watch_source()
        emit = body.index("__winzappMediaProgress({")
        guard = body.rfind("if (Number.isFinite", 0, emit)
        # Either there is no guard before the emit at all, or it is closed
        # before the emit begins (i.e. it guards a value, not the call).
        assert guard == -1 or body.rfind("}", guard, emit) > guard

    def test_a_numeric_source_is_still_preferred_when_present(self):
        assert "Number.isFinite(numeric)" in self._watch_source()


class TestDownloadProgressComesFromTheCdn:
    @staticmethod
    def _download_source():
        source = (API / "controller" / "sessionController.ts").read_text(
            encoding="utf-8")
        start = source.index("async function downloadMediaWithProgress(")
        return source[start:source.index("\n/**", start)]

    def test_the_bytes_are_counted_as_they_arrive(self):
        body = self._download_source()
        assert "onDownloadProgress" in body
        assert "media-download-progress" in body

    def test_it_is_throttled(self):
        """A 200 MB file produces thousands of events, and each one is a
        Socket.IO frame plus a wx.CallAfter on a UI thread whose screen reader
        is already announcing the value."""
        body = self._download_source()
        assert re.search(r"now - lastSent < \d+", body)

    def test_every_failure_falls_back_to_the_original_download(self):
        """A progress bar is worth nothing if wanting one can cost the file."""
        body = self._download_source()
        assert body.count("client.decryptFile(message)") >= 3

    def test_both_decrypt_sites_use_it(self):
        source = (API / "controller" / "sessionController.ts").read_text(
            encoding="utf-8")
        media = source[source.index("export async function getMediaByMessage("):]
        assert media.count("downloadMediaWithProgress(req, client,") == 2


class TestTheGaugeOnlyEverMovesForward:
    """Two sources feed it and they measure different things: the CDN download
    (minutes) and the loopback read of the finished file (instant), which
    restarts from near zero. Without this the bar climbs, drops and climbs."""

    def test_a_lower_reading_is_ignored(self):
        panel = _panel()
        ConversationsPanel.update_message_download_progress(panel, "m1", 0.8)
        ConversationsPanel.update_message_download_progress(panel, "m1", 0.1)
        assert panel._download_progress["m1"] == pytest.approx(0.8)

    def test_a_higher_reading_is_taken(self):
        panel = _panel()
        ConversationsPanel.update_message_download_progress(panel, "m1", 0.3)
        ConversationsPanel.update_message_download_progress(panel, "m1", 0.9)
        assert panel._download_progress["m1"] == pytest.approx(0.9)

    def test_the_loopback_source_is_kept_for_older_servers(self):
        """client/api/ is reinstalled independently of this app; on a server
        that has never heard of media-download-progress it is the only signal
        there is."""
        source = inspect.getsource(ConversationsPanel._on_action_download)
        assert "progress_callback=_update_download_progress" in source

    def test_a_new_attempt_starts_from_zero(self):
        """Monotonic across one download, not across the button being pressed
        twice — otherwise a retry starts wherever the failure stopped."""
        source = inspect.getsource(ConversationsPanel._on_action_download)
        assert "_download_progress.pop(msg_id, None)" in source

    @pytest.mark.parametrize("bad", ["abc", None, float("nan")])
    def test_unreadable_values_are_ignored(self, bad):
        panel = _panel()
        ConversationsPanel.update_message_download_progress(panel, "m1", 0.5)
        ConversationsPanel.update_message_download_progress(panel, "m1", bad)
        assert panel._download_progress["m1"] == pytest.approx(0.5)


class TestTheCorrelationIdIsSentWithTheRequest:
    def test_the_request_carries_one(self):
        import main
        source = inspect.getsource(main.MainWindow.get_base64_from_media)
        assert 'body_data["progressId"]' in source

    def test_the_client_subscribes_to_the_event(self):
        from core import websocket_client
        source = inspect.getsource(websocket_client.WebSocketClient)
        assert '"media-download-progress"' in source
        assert "def on_media_download_progress" in source


class TestNaNCannotCompleteABar:
    """min(1.0, nan) is 1.0 — every comparison with NaN is False, so the clamp
    lets it through as a finished transfer. Found by the parametrised test
    above, on real code: a malformed progress event would have shown 100% over
    a download that had not started."""

    def test_the_download_gauge_rejects_it(self):
        panel = _panel()
        ConversationsPanel.update_message_download_progress(panel, "m1", float("nan"))
        assert "m1" not in panel._download_progress

    def test_the_upload_gauge_rejects_it(self):
        panel = _panel()
        ConversationsPanel.update_media_upload_progress(panel, "up-1", float("nan"))
        assert "up-1" not in panel._media_upload_progress

    def test_infinity_is_still_only_clamped(self):
        """Unlike NaN, an ordering exists — clamping is the right answer."""
        panel = _panel()
        ConversationsPanel.update_message_download_progress(panel, "m1", float("inf"))
        assert panel._download_progress["m1"] == pytest.approx(1.0)
