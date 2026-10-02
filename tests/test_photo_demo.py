"""Manual-demo fixtures/handlers, never constructing an application window."""
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
import pytest

from core.image_description.config import eligible_photo
from core.image_description.demo_fixture import demo_directory, demo_messages, make_demo_photo
from core.image_description.errors import DescriptionError
from core.image_description.image_input import prepare_image


def test_demo_data_is_outside_checkout():
    root = Path(__file__).resolve().parents[1]
    path = demo_directory(root / "client" / "photo_demo.py")
    assert path == root.parent / "_MANUEL_TEST" / "fotograf-betimleme"
    assert not path.is_relative_to(root)


def test_generated_photo_is_small_and_contains_known_shapes_without_metadata():
    data = make_demo_photo()
    assert len(data) < 100_000
    with Image.open(BytesIO(data)) as image:
        assert image.size == (800, 500)
        assert not image.getexif()
        assert image.getpixel((100, 200)) == (0, 0, 255)
        assert image.getpixel((430, 230)) == (255, 0, 0)
        assert image.getpixel((660, 260)) == (255, 255, 0)
    assert prepare_image(data, "balanced").data


def test_demo_messages_are_fresh_and_only_photo_is_eligible():
    messages = demo_messages()
    assert [eligible_photo(m) for m in messages] == [False, True]
    messages[1]["key"]["id"] = "changed"
    assert demo_messages()[1]["key"]["id"] == "demophoto"


def test_missing_demo_media_never_downloads_from_whatsapp():
    from ui.photo_description_demo import PhotoDemoFrame
    with pytest.raises(DescriptionError, match="ai_error_media"):
        PhotoDemoFrame.handle_media_message(SimpleNamespace(), {})


def test_simulated_vault_closes_only_locked_photo_sessions():
    from ui.photo_description_demo import PhotoDemoFrame
    calls = []
    stub = SimpleNamespace(_chat_lock_unlocked=True,
                           panel=SimpleNamespace(close_image_description=lambda **kw: calls.append(kw)),
                           output=calls.append, i18n=SimpleNamespace(t=lambda key: key))
    PhotoDemoFrame._simulate_lock(stub)
    assert stub._chat_lock_unlocked is False
    assert calls == [{"locked_only": True}, "ai_demo_locked"]


def test_context_menu_does_not_require_unsupported_wx_context_manager(monkeypatch):
    import ui.photo_description_demo as module
    calls = []
    class MenuStub:
        def Append(self, identifier, label):
            calls.append(label)
            return SimpleNamespace(GetId=lambda: 123)
        def Bind(self, *args, **kwargs):
            pass
        def Destroy(self):
            calls.append("destroyed")
    monkeypatch.setattr(module.wx, "Menu", MenuStub)
    stub = SimpleNamespace(main_window=SimpleNamespace(i18n=SimpleNamespace(t=lambda key: key)),
                           _sorted_messages=demo_messages(),
                           messages_list=SimpleNamespace(GetFocusedItem=lambda: 1,
                                                         PopupMenu=lambda menu: calls.append("shown")))
    module.DemoConversationPanel._menu(stub, None)
    assert calls == ["ai_describe_photo\tCtrl+Shift+Y", "shown", "destroyed"]


def test_demo_initializes_its_own_existing_sound_system_without_account_runtime(monkeypatch):
    import ui.photo_description_demo as module
    calls = []
    class FakeSystem:
        def __init__(self, window, path):
            self.sound_dir = path
            calls.append("created")
        def start(self):
            calls.append("started")
    monkeypatch.setattr(module, "SoundSystem", FakeSystem)
    monkeypatch.setattr(module, "discover_sound_packs", lambda path: {"default": {"id": "default"}})
    window = SimpleNamespace()
    module.PhotoDemoFrame._initialize_sounds(window)
    assert calls == ["created", "started"]
    assert module.PhotoDemoFrame.get_active_sound_pack(window) == {"id": "default"}


def test_demo_audio_initialization_failure_does_not_abort_the_demo(monkeypatch, caplog):
    import ui.photo_description_demo as module
    def fail(*args):
        raise RuntimeError("private native error text")
    monkeypatch.setattr(module, "SoundSystem", fail)
    window = SimpleNamespace()
    module.PhotoDemoFrame._initialize_sounds(window)
    assert window.sound_system is None
    assert "RuntimeError" in caplog.text and "private native error text" not in caplog.text
