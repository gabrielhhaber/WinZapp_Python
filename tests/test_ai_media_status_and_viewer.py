"""Describe a photo or video status and a video opened in the media viewer.

Logic against plain stubs plus source-level wiring checks: no window is built.
"""
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app_settings import AppSettings
from core.ai_credentials import CredentialStore
from core.ai_media import config
from core.ai_media.errors import DescriptionError
from core.ai_media.service import Operation
from status_tab.status_ai import StatusAIMixin
from ui import ai_media_launch

CLIENT = Path(__file__).resolve().parents[1] / "client"


def read(*parts):
    return CLIENT.joinpath(*parts).read_text(encoding="utf-8")


def status(msg_type="videoMessage", **info):
    return {"key": {"id": "STATUS1", "remoteJid": "status@broadcast", "fromMe": False},
            "messageType": msg_type, "message": {msg_type: info}}


@pytest.fixture
def ready(tmp_path, monkeypatch):
    """Feature on, one provider with a saved key."""
    monkeypatch.setattr(ai_media_launch, "global_dir", lambda: str(tmp_path))
    app = AppSettings(str(tmp_path))
    app.set("ai_media", {"enabled": True})
    CredentialStore(str(tmp_path)).set("gemini", "key")
    outputs = []
    mw = SimpleNamespace(_app_settings=app, output=outputs.append, i18n=SimpleNamespace(t=lambda k: k))
    return mw, app, outputs


def test_photo_and_video_statuses_get_the_describe_button(ready):
    mw, _app, _out = ready
    assert ai_media_launch.describe_label(mw, status("imageMessage")) == "ai_describe_image_menu"
    assert ai_media_launch.describe_label(mw, status("videoMessage")) == "ai_describe_video_menu"


def test_a_text_status_gets_no_button(ready):
    mw, _app, _out = ready
    assert ai_media_launch.describe_label(mw, status("conversation")) == ""
    assert ai_media_launch.describe_label(mw, status("extendedTextMessage")) == ""


def test_no_button_when_the_feature_is_off_the_kind_is_off_or_no_provider_has_a_key(ready, monkeypatch):
    from core.ai_media import local_text
    monkeypatch.setattr(local_text, "ocr_available", lambda: False)
    mw, app, _out = ready
    app.set("ai_media", {"enabled": True, "kinds": {"video": False}})
    assert ai_media_launch.describe_label(mw, status("videoMessage")) == ""
    assert ai_media_launch.describe_label(mw, status("imageMessage")) != ""
    app.set("ai_media", {"enabled": False})
    assert ai_media_launch.describe_label(mw, status("imageMessage")) == ""
    app.set("ai_media", {"enabled": True, "disabled": ["gemini"]})
    assert ai_media_launch.describe_label(mw, status("imageMessage")) == ""


def test_a_view_once_or_idless_status_is_never_offered(ready):
    mw, _app, _out = ready
    assert ai_media_launch.describe_label(mw, status("imageMessage", viewOnce=True)) == ""
    broken = status("imageMessage")
    broken["key"]["id"] = ""
    assert ai_media_launch.describe_label(mw, broken) == ""
    assert ai_media_launch.describe_label(mw, None) == ""


def test_the_panels_label_rule_ignores_a_missing_status():
    panel = SimpleNamespace(main_window=None)
    assert StatusAIMixin._status_describe_label(panel, None) == ""


def test_describing_while_disabled_says_so_and_opens_nothing(ready, monkeypatch):
    from core.ai_media import local_text
    monkeypatch.setattr(local_text, "ocr_available", lambda: False)
    mw, app, outputs = ready
    app.set("ai_media", {"enabled": False})
    built = []
    ai_media_launch.describe(object(), mw, status("imageMessage"), lambda *a: built.append(a))
    assert outputs == ["ai_disabled"] and built == []


def test_status_offline_ocr_opens_when_ai_is_disabled(ready, monkeypatch):
    from core.ai_media import local_text
    import ui.dialogs.ai_result_dialog as dialog_module
    monkeypatch.setattr(local_text, "ocr_available", lambda: True)
    monkeypatch.setattr(ai_media_launch, "active_account_id", lambda: "account")
    mw, app, outputs = ready
    app.set("ai_media", {"enabled": False})
    assert ai_media_launch.describe_label(mw, status("imageMessage")) == "ai_read_text_offline"
    built = []

    class WindowStub:
        def __init__(self, parent, identity, kind, locked, cfg, app_s, loader, mime, **more):
            built.append((kind, more))
        def ShowModal(self):
            return 0
        def _dispose(self):
            pass
        def Destroy(self):
            pass

    monkeypatch.setattr(dialog_module, "AIResultDialog", WindowStub)
    ai_media_launch.describe(object(), mw, status("imageMessage"), lambda *a: lambda t: b"img")
    assert outputs == [] and built == [("image", {"offline_only": True})]


def loader_for(snapshot, kind, download, tmp=None):
    panel = SimpleNamespace(main_window=SimpleNamespace(get_base64_from_media=download))
    return StatusAIMixin._status_ai_loader(panel, snapshot, kind)


def test_the_status_original_is_downloaded_as_bytes():
    import base64
    calls = []
    def download(snapshot):
        calls.append(snapshot)
        return base64.b64encode(b"status-bytes").decode()
    snapshot = status("videoMessage", fileLength=12)
    assert loader_for(snapshot, "video", download)(Operation("video")) == b"status-bytes"
    assert calls == [snapshot]


def test_an_oversized_status_is_refused_by_metadata_before_any_download():
    def download(snapshot):
        pytest.fail("downloaded")
    big = status("videoMessage", fileLength=config.MAX_SOURCE_BYTES["video"] + 1)
    with pytest.raises(DescriptionError, match="ai_error_media_size"):
        loader_for(big, "video", download)(Operation("video"))


def test_an_oversized_status_is_refused_by_its_real_size(monkeypatch):
    import base64
    from status_tab import status_ai
    monkeypatch.setitem(status_ai.MAX_SOURCE_BYTES, "image", 4)
    snapshot = status("imageMessage", fileLength=1)  # understates
    with pytest.raises(DescriptionError, match="ai_error_media_size"):
        loader_for(snapshot, "image", lambda s: base64.b64encode(b"12345").decode())(Operation("image"))


def test_a_failed_status_download_is_a_media_error_without_the_underlying_text(monkeypatch):
    from status_tab import status_ai
    monkeypatch.setattr(status_ai, "_download_status_media",
                        lambda mw, st: (_ for _ in ()).throw(ValueError("secret detail")))
    with pytest.raises(DescriptionError, match="ai_error_media$"):
        loader_for(status("imageMessage"), "image", None)(Operation("image"))


def test_a_cancelled_action_stops_before_the_status_download():
    operation = Operation("image")
    operation.cancel()
    with pytest.raises(DescriptionError):
        loader_for(status("imageMessage"), "image", lambda s: pytest.fail("downloaded"))(operation)


# ── wiring ───────────────────────────────────────────────────────────────

def test_the_inline_status_button_follows_save_media_and_has_a_shortcut():
    panel = read("status_panel.py")
    assert panel.index("self._save_media_btn = ") < panel.index("self._describe_status_btn = ") < panel.index("self._reply_label = ")
    assert "AccessibleDescribeButton()" in panel and 'ord("I")' in panel
    assert "self._on_describe_status" in panel and "StatusAIMixin" in panel
    assert "self._update_status_describe_button()" in read("status_tab", "status_viewer.py")


def test_the_status_media_viewer_is_given_the_describe_callbacks():
    viewer = read("status_tab", "status_viewer.py")
    assert "describe_label=self._viewer_describe_label" in viewer
    assert "on_describe=self._viewer_describe_status" in viewer


def test_a_conversation_video_opened_in_the_viewer_can_be_described():
    files = read("ui", "conversation_panel", "media_files.py")
    assert "self._ai_menu_label(msg, self.main_window.i18n)" in files
    assert "self._on_ai_action(message=msg, parent=parent)" in files


def test_the_viewer_button_sits_after_save_as_and_before_close_with_its_shortcut():
    viewer = read("ui", "media_viewer.py")
    assert viewer.index("self._save_btn = ") < viewer.index("self._describe_btn = ") < viewer.index("self._close_btn = ")
    assert "AccessibleDescribeButton()" in viewer
    assert re.search(r'ACCEL_CTRL \| wx\.ACCEL_SHIFT, ord\("I"\), self\.ID_CTRL_SHIFT_I', viewer)


def test_only_pictures_and_videos_offer_describe_in_the_viewer():
    from ui.media_viewer import MediaViewerDialog
    cases = {"image": "Describe", "video": "Describe", "audio": "", "text": ""}
    for kind, expected in cases.items():
        stub = SimpleNamespace(_describe_label_cb=lambda item: "Describe", _on_describe_cb=lambda *a: None)
        assert MediaViewerDialog._describe_text(stub, {"kind": kind}) == expected
    nothing = SimpleNamespace(_describe_label_cb=None, _on_describe_cb=None)
    assert MediaViewerDialog._describe_text(nothing, {"kind": "video"}) == ""


def test_leaving_or_hiding_the_app_also_closes_a_status_or_viewer_window():
    actions = read("ui", "conversation_panel", "ai_actions.py")
    assert "close_open_dialog()" in actions
    assert "def close_open_dialog" in read("ui", "ai_media_launch.py")
