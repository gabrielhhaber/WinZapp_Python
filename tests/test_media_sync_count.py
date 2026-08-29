"""Tests for the media-sync download count and its user-facing announcement.

Reported live (log of 2026-08-08 21:50): WinZapp announced "download de mídias
iniciado em segundo plano" and, 71 ms later, "download de mídias concluído" —
having downloaded nothing at all. Two separate defects produced that:

* ``sync_media_for_all_chats()`` returned ``len(tasks)``, the number of media
  messages it *considered*, not the number it downloaded. With the app offline
  every ``sync_if_media()`` returned at its first line, so all 1579 candidates
  "completed" instantly and the caller — which announced completion on any
  count above zero — spoke a finished download that never happened.
* ``start_sync()``'s Phase 2 ran at all while offline, so the "iniciado"
  announcement fired for a phase that could not do any work.

``MainWindow`` is a wx.Frame and cannot be instantiated without a running
wx.App, so the methods under test are exercised as plain functions against a
small stub — same approach as tests/test_media_failed_ids.py.
"""

import time

import pytest

from main import MainWindow


class _Stub:
    """Minimal stand-in for MainWindow for the media sync phase."""

    _MEDIA_SYNC_TIMEOUT = 60
    _MEDIA_SYNC_WORKERS = 2
    sync_media_for_all_chats = MainWindow.sync_media_for_all_chats

    def __init__(self, chats=None, downloads=None):
        self.chats = chats or {}
        # msg id -> what sync_if_media should report for it
        self._downloads = downloads or {}
        self.seen = []
        self._saved = False

    def sync_if_media(self, msg, timeout=60):
        self.seen.append(msg.get("key", {}).get("id"))
        return self._downloads.get(msg.get("key", {}).get("id"), False)

    def _save_media_failed_ids(self):
        self._saved = True


def _chat(*msgs):
    return {"messages": {"messages": {"records": list(msgs)}}}


def _media_msg(msg_id, message_type="imageMessage"):
    return {"key": {"id": msg_id}, "messageType": message_type}


class TestSyncMediaForAllChatsCount:
    def test_counts_downloads_not_candidates(self):
        """The regression itself: three media messages, one real download."""
        s = _Stub(
            chats={"a@s.whatsapp.net": _chat(
                _media_msg("A"), _media_msg("B"), _media_msg("C"))},
            downloads={"B": True},
        )
        assert s.sync_media_for_all_chats() == 1
        # All three were still *considered* — the count is what changed.
        assert sorted(s.seen) == ["A", "B", "C"]

    def test_offline_run_reports_zero(self):
        """Every sync_if_media() returning False (the offline case) must not
        read as 1579 completed downloads."""
        s = _Stub(chats={"a@s.whatsapp.net": _chat(*(
            _media_msg(str(i)) for i in range(20)))})
        assert s.sync_media_for_all_chats() == 0

    def test_counts_every_successful_download(self):
        s = _Stub(
            chats={
                "a@s.whatsapp.net": _chat(_media_msg("A"), _media_msg("B")),
                "b@s.whatsapp.net": _chat(_media_msg("C", "audioMessage")),
            },
            downloads={"A": True, "B": True, "C": True},
        )
        assert s.sync_media_for_all_chats() == 3

    def test_no_media_messages_returns_zero_without_touching_the_pool(self):
        s = _Stub(chats={"a@s.whatsapp.net": _chat(
            {"key": {"id": "T"}, "messageType": "conversation"})})
        assert s.sync_media_for_all_chats() == 0
        assert s.seen == []
        # Early return — nothing ran, so nothing to persist.
        assert s._saved is False

    def test_a_worker_raising_does_not_count_as_a_download(self):
        class _Boom(_Stub):
            def sync_if_media(self, msg, timeout=60):
                if msg.get("key", {}).get("id") == "B":
                    raise RuntimeError("boom")
                return True

        s = _Boom(chats={"a@s.whatsapp.net": _chat(
            _media_msg("A"), _media_msg("B"))})
        assert s.sync_media_for_all_chats() == 1

    def test_expired_ids_are_persisted_after_a_real_run(self):
        s = _Stub(chats={"a@s.whatsapp.net": _chat(_media_msg("A"))})
        s.sync_media_for_all_chats()
        assert s._saved is True


class _SyncIfMediaStub:
    """Stub for sync_if_media() itself — the per-message download decision."""

    _MEDIA_MAX_AGE_SECONDS = MainWindow._MEDIA_MAX_AGE_SECONDS
    sync_if_media = MainWindow.sync_if_media

    def __init__(self, connected=True, offline=False, allowed_types=None):
        self._wa_connected = connected
        self.offline_mode = offline
        self._media_failed_ids = {}
        self.audio_result = True
        self.media_result = True
        # Configuracoes > Armazenamento > "Tipos de midia a serem baixados
        # automaticamente". No key at all means every category is allowed,
        # which is what a settings.json predating the option looks like.
        self.settings = ({"storage": {"auto_download_media_types": list(allowed_types)}}
                         if allowed_types is not None else {})

    def _media_max_download_days(self):
        return 0

    def _media_max_download_bytes(self):
        return 0

    def _is_conversation_open_for(self, msg):
        return False

    def handle_audio_message(self, msg, timeout=60):
        return self.audio_result

    def handle_media_message(self, msg, progress_callback=None, timeout=60):
        return self.media_result


class TestSyncIfMediaReturnValue:
    def _msg(self, message_type="imageMessage"):
        return {"key": {"id": "3EB0AA"}, "messageType": message_type,
                "messageTimestamp": int(time.time())}

    def test_returns_true_when_a_file_was_downloaded(self):
        assert _SyncIfMediaStub().sync_if_media(self._msg()) is True

    def test_returns_true_for_a_downloaded_audio(self):
        assert _SyncIfMediaStub().sync_if_media(self._msg("audioMessage")) is True

    def test_returns_false_when_the_handler_downloaded_nothing(self):
        """Already on disk / empty API response — the handler reports False and
        so must sync_if_media(), or it inflates the phase's count."""
        s = _SyncIfMediaStub()
        s.media_result = False
        assert s.sync_if_media(self._msg()) is False

    def test_returns_false_while_offline(self):
        assert _SyncIfMediaStub(connected=False).sync_if_media(self._msg()) is False
        assert _SyncIfMediaStub(offline=True).sync_if_media(self._msg()) is False

    def test_returns_false_for_a_non_media_message(self):
        assert _SyncIfMediaStub().sync_if_media(self._msg("conversation")) is False

    def test_returns_false_for_a_known_expired_id(self):
        s = _SyncIfMediaStub()
        s._media_failed_ids = {"3EB0AA": 0}
        assert s.sync_if_media(self._msg()) is False

    def test_returns_false_past_the_cdn_ttl(self):
        s = _SyncIfMediaStub()
        msg = self._msg()
        msg["messageTimestamp"] = 1  # 1970
        assert s.sync_if_media(msg) is False

    def test_returns_false_for_a_still_pending_local_message(self):
        s = _SyncIfMediaStub()
        msg = self._msg()
        msg["_local_pending"] = True
        assert s.sync_if_media(msg) is False


class TestAutoDownloadTypeFilter:
    """Configuracoes > Armazenamento > "Tipos de midia a serem baixados
    automaticamente".

    Checked inside sync_if_media() because that is the single funnel every
    automatic download passes through — the live-message path and the sync
    sweep both land here. Opening the media by hand is unaffected: this is
    about what the app fetches without being asked.
    """

    def _msg(self, message_type="imageMessage"):
        return {"key": {"id": "3EB0AA"}, "messageType": message_type,
                "messageTimestamp": int(time.time())}

    def test_an_unchecked_category_is_not_downloaded(self):
        stub = _SyncIfMediaStub(allowed_types=["videos", "audios", "documents"])
        assert stub.sync_if_media(self._msg("imageMessage")) is False

    def test_a_checked_category_still_is(self):
        stub = _SyncIfMediaStub(allowed_types=["videos", "audios", "documents"])
        assert stub.sync_if_media(self._msg("videoMessage")) is True

    def test_everything_is_allowed_when_the_setting_was_never_written(self):
        """A settings.json predating this option must not read as "nothing
        selected" — that would silently stop all media downloads."""
        assert _SyncIfMediaStub().sync_if_media(self._msg()) is True

    def test_unchecking_everything_is_honoured(self):
        """Not the same as never having chosen."""
        stub = _SyncIfMediaStub(allowed_types=[])
        assert stub.sync_if_media(self._msg("documentMessage")) is False

    def test_stickers_follow_the_photos_checkbox(self):
        """group_media_category() groups them there, and the Media tab shows
        them there — "Fotos" has to mean the same thing in both places."""
        stub = _SyncIfMediaStub(allowed_types=["videos"])
        assert stub.sync_if_media(self._msg("stickerMessage")) is False
        stub = _SyncIfMediaStub(allowed_types=["photos"])
        assert stub.sync_if_media(self._msg("stickerMessage")) is True

    def test_each_category_can_be_switched_off_on_its_own(self):
        from core.utils import AUTO_DOWNLOAD_MEDIA_TYPES
        sample = {"photos": "imageMessage", "videos": "videoMessage",
                  "audios": "audioMessage", "documents": "documentMessage"}
        assert set(sample) == set(AUTO_DOWNLOAD_MEDIA_TYPES), \
            "a new category needs a message type here"
        for category, message_type in sample.items():
            others = [k for k in AUTO_DOWNLOAD_MEDIA_TYPES if k != category]
            assert _SyncIfMediaStub(allowed_types=others).sync_if_media(
                self._msg(message_type)) is False
            assert _SyncIfMediaStub(allowed_types=[category]).sync_if_media(
                self._msg(message_type)) is True
