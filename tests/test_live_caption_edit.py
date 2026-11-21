"""A caption edited live must show at once, not only after the next sync.

_apply_possible_edit() compared text only. A media record has no text, so the
edit echo of an image, video or document caption — arriving under the
message's own id, the same way a text edit does — was ignored: the old caption
and no "Editada" marker stayed until a later sync replaced the whole record.

WhatsApp does allow it: WAWebMessageEditUtils.getMsgEditType() maps IMAGE,
VIDEO and DOCUMENT to CaptionEdit, and 2 of the 14 edited messages read from
the running store were image captions.

A live caption edit has not been observed end to end, so the fix is narrow and
these tests pin exactly how narrow: only a copy WhatsApp marks as edited, only
the caption field, never an erase.
"""

import pytest

from core.message_edit import apply_caption_edit
from main import MainWindow

GROUP = "120363427511142886@g.us"


def _media(kind="imageMessage", caption="antes", edited=False, **extra):
    media = {"caption": caption, "url": "https://mmg.example/x", "mediaKey": "KEY"}
    media.update(extra)
    rec = {"key": {"id": "M1"}, "messageType": kind, "message": {kind: media}}
    if edited:
        rec["_edited"] = True
    return rec


class TestApplyCaptionEdit:
    @pytest.mark.parametrize("kind", ["imageMessage", "videoMessage", "documentMessage"])
    def test_an_edited_caption_replaces_only_the_caption(self, kind):
        existing = _media(kind, caption="antes", seconds_measured=42)
        incoming = _media(kind, caption="depois", edited=True, url="https://other", mediaKey="")

        assert apply_caption_edit(existing, incoming) == "changed"

        media = existing["message"][kind]
        assert media["caption"] == "depois"
        assert existing["_edited"] is True
        # Everything else on the stored media stays as it was.
        assert media["url"] == "https://mmg.example/x"
        assert media["mediaKey"] == "KEY"
        assert media["seconds_measured"] == 42

    def test_a_copy_not_marked_edited_never_rewrites(self):
        """An ordinary redelivery with a different caption field is not an edit."""
        existing = _media(caption="antes")
        assert apply_caption_edit(existing, _media(caption="outra")) is None
        assert existing["message"]["imageMessage"]["caption"] == "antes"
        assert "_edited" not in existing

    def test_an_empty_incoming_caption_never_erases(self):
        existing = _media(caption="antes")
        assert apply_caption_edit(existing, _media(caption="", edited=True)) is None
        assert existing["message"]["imageMessage"]["caption"] == "antes"

    def test_same_caption_only_sets_a_missing_marker(self):
        existing = _media(caption="depois")
        assert apply_caption_edit(existing, _media(caption="depois", edited=True)) == "marked"
        assert existing["_edited"] is True
        # Already marked: nothing more to do.
        assert apply_caption_edit(existing, _media(caption="depois", edited=True)) is None

    def test_a_different_media_kind_is_left_alone(self):
        existing = _media("imageMessage")
        assert apply_caption_edit(existing, _media("videoMessage", caption="x", edited=True)) is None

    def test_text_records_are_not_its_business(self):
        existing = {"key": {"id": "T"}, "messageType": "conversation",
                    "message": {"conversation": "oi"}}
        incoming = dict(existing, _edited=True)
        assert apply_caption_edit(existing, incoming) is None

    def test_a_caption_blanked_before_still_takes_a_real_edit(self):
        """The normaliser blanks a caption that looks like thumbnail data; that
        must never stop a genuine edited caption from landing later."""
        existing = _media(caption="")
        assert apply_caption_edit(existing, _media(caption="depois", edited=True)) == "changed"
        assert existing["message"]["imageMessage"]["caption"] == "depois"

    def test_a_videos_measured_duration_survives(self):
        from core.utils import MEASURED_SECONDS_KEY

        existing = _media("videoMessage", caption="antes", **{MEASURED_SECONDS_KEY: 37})
        incoming = _media("videoMessage", caption="depois", edited=True)

        assert apply_caption_edit(existing, incoming) == "changed"
        assert existing["message"]["videoMessage"][MEASURED_SECONDS_KEY] == 37


class TestThroughTheNormaliser:
    def test_thumbnail_data_in_an_edited_copy_never_erases_the_caption(self):
        """End to end: a raw image WhatsApp marks edited, whose caption field
        holds base64 thumbnail data. The normaliser blanks it and marks the
        copy edited; the stored caption must stay."""
        from core.websocket_client import WebSocketClient

        class _Normalizer:
            _normalize_wpp_message = WebSocketClient._normalize_wpp_message
            _clean_jid = WebSocketClient._clean_jid

        raw = {
            "id": f"false_{GROUP}_M1", "from": "5511888888888@c.us", "to": GROUP,
            "fromMe": False, "timestamp": 1789360394, "type": "image",
            "caption": "/9j/4AAQSkZJRgABAQAAAQABAAD" + "A" * 200,
            "latestEditMsgKey": {"id": "EDIT1", "_serialized": f"false_{GROUP}_EDIT1"},
            "latestEditSenderTimestampMs": 1789360406,
        }
        incoming = _Normalizer()._normalize_wpp_message(raw)
        assert incoming["_edited"] is True
        assert incoming["message"]["imageMessage"]["caption"] == ""

        existing = _media(caption="legenda real")
        assert apply_caption_edit(existing, incoming) is None
        assert existing["message"]["imageMessage"]["caption"] == "legenda real"


class _Executor:
    def submit(self, fn, *a, **kw):
        fn(*a, **kw)


class _Db:
    def __init__(self):
        self.inserted = []

    def insert_message(self, jid, record):
        self.inserted.append((jid, record["key"]["id"]))


class _Stub:
    _apply_possible_edit = MainWindow._apply_possible_edit
    _apply_remote_revoke = MainWindow._apply_remote_revoke
    _revoke_stored_copies = MainWindow._revoke_stored_copies
    _persist_and_repaint_edit = MainWindow._persist_and_repaint_edit

    def _maybe_notify_edit(self, remote_jid, record):
        pass

    def __init__(self):
        self.db = _Db()
        self._msg_bg_executor = _Executor()
        self.repaints = 0
        self.revoked = []
        stub = self

        class _Panel:
            def refresh_active_conversation_messages(self):
                stub.repaints += 1

            def on_message_revoked(self, msg_id):
                stub.revoked.append(msg_id)

        self.conversations_panel = _Panel()

    def _schedule_set_chats(self):
        pass


@pytest.fixture(autouse=True)
def _inline_call_after(monkeypatch):
    monkeypatch.setattr("main.wx.CallAfter", lambda fn, *a, **kw: fn(*a, **kw))


class TestApplyPossibleEdit:
    def test_a_live_caption_edit_shows_at_once(self):
        stub = _Stub()
        existing = _media(caption="antes")

        stub._apply_possible_edit(existing, _media(caption="depois", edited=True), GROUP)

        assert existing["message"]["imageMessage"]["caption"] == "depois"
        assert existing["_edited"] is True
        assert stub.db.inserted == [(GROUP, "M1")]
        assert stub.repaints == 1

    def test_a_plain_media_redelivery_still_does_nothing(self):
        stub = _Stub()
        existing = _media(caption="antes")

        stub._apply_possible_edit(existing, _media(caption="antes"), GROUP)

        assert stub.db.inserted == [] and stub.repaints == 0
        assert "_edited" not in existing

    def test_a_revoke_still_wins_over_a_caption(self):
        stub = _Stub()
        existing = _media(caption="antes")
        revoke = {"key": {"id": "M1"}, "messageType": "protocolMessage",
                  "message": {"protocolMessage": {"type": 3}}, "_edited": True}

        stub._apply_possible_edit(existing, revoke, GROUP)

        assert existing["messageType"] == "protocolMessage"
        assert stub.revoked == ["M1"]

    def test_text_edits_are_unchanged(self):
        stub = _Stub()
        existing = {"key": {"id": "T1"}, "messageType": "conversation",
                    "message": {"conversation": "antes"}}
        incoming = {"key": {"id": "T1"}, "messageType": "conversation",
                    "message": {"conversation": "depois"}}

        stub._apply_possible_edit(existing, incoming, GROUP)

        assert existing["message"] == {"conversation": "depois"}
        assert existing["_edited"] is True
