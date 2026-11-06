"""One explicit retry never clears automatic blocks or creates a real window."""
from types import SimpleNamespace

import pytest

from main_window.media import MediaMixin
from ui.conversation_panel.media_files import MediaFilesMixin


@pytest.mark.parametrize("kind,method", [("imageMessage", MediaMixin.handle_media_message),
                                        ("audioMessage", MediaMixin.handle_audio_message)])
def test_only_explicit_true_bypasses_a_cleaned_copy_block(tmp_path, monkeypatch, kind, method):
    import main_window.media as module
    monkeypatch.setattr(module, "data_path", lambda *parts: str(tmp_path.joinpath(*parts)))
    calls, announcements = [], []
    def fetch(*args, **kwargs):
        calls.append(args[0]["key"]["id"])
        return b""
    stub = SimpleNamespace(is_media_cleaned=lambda key: True, _wa_connected=True,
        _say_media_cleaned=lambda: announcements.append(1), fetch_media_bytes=fetch,
        get_base64_from_media=fetch)
    msg = {"key": {"id": "one"}, "messageType": kind}
    assert method(stub, msg) is False
    assert method(stub, msg, restore_cleaned=1) is False
    assert calls == []
    assert method(stub, msg, restore_cleaned=True) is False  # Empty response is not success.
    assert calls == ["one"] and len(announcements) == 2
    assert stub.is_media_cleaned("one")


@pytest.mark.parametrize("kind", ["imageMessage", "videoMessage", "documentMessage", "audioMessage"])
@pytest.mark.parametrize("accept", [False, True])
def test_download_action_confirms_one_item_before_starting_a_worker(monkeypatch, kind, accept):
    import ui.conversation_panel.media_files as module
    class Control:
        def __getattr__(self, name): return lambda *args, **kwargs: None
    calls, prompts = [], []
    class Thread:
        def __init__(self, target, **kwargs): self.target = target
        def start(self): self.target()
    def prompt(*args):
        prompts.append(args)
        return module.wx.YES if accept else module.wx.NO
    monkeypatch.setattr(module.wx, "MessageBox", prompt)
    monkeypatch.setattr(module.wx, "CallAfter", lambda fn, *a, **kw: fn(*a, **kw))
    monkeypatch.setattr(module.threading, "Thread", Thread)
    monkeypatch.setattr(module.os.path, "isfile", lambda path: False)
    monkeypatch.setattr(module, "cached_media_path", lambda *a: "fixture")
    mw = SimpleNamespace(i18n=SimpleNamespace(t=lambda key: key), _wa_connected=True,
        is_media_cleaned=lambda key: True, output=lambda *a: None,
        handle_audio_message=lambda msg, **kw: calls.append((msg, kw)),
        handle_media_message=lambda msg, **kw: calls.append((msg, kw)))
    msg = {"key": {"id": "one"}, "messageType": kind}
    stub = SimpleNamespace(main_window=mw, messages_list=SimpleNamespace(GetFirstSelected=lambda: 0),
        _sorted_messages=[msg], _action_download_btn=Control(), _download_progress={},
        _hide_media_transfer_gauge=lambda: None, _show_media_transfer_gauge=lambda: None,
        conversation_panel=Control(), update_message_download_progress=lambda *a: None,
        _sync_media_action_slot_visibility=lambda: None)
    MediaFilesMixin._on_action_download(stub, None)
    assert len(prompts) == 1
    assert len(calls) == int(accept)
    if accept:
        assert calls[0][0] is msg and calls[0][1]["restore_cleaned"] is True
    assert mw.is_media_cleaned("unrelated") and mw.is_media_cleaned("one")
