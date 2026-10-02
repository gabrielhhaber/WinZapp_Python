"""User-operated manual demo: real photo controls, no WhatsApp runtime.

Not a GUI probe or an automated test. Only photo_demo.py's explicit manual
entry point constructs these windows; importing this module shows nothing.
"""
import wx
import logging
from accessible_output2 import outputs
from cryptography.fernet import Fernet
from pathlib import Path

from app_paths import global_dir, resource_path
from app_settings import AppSettings
from core.accessible_speech import AccessibleSpeechOutput
from core.i18n import I18n
from core.sound_system import SoundSystem, discover_sound_packs, DEFAULT_PACK_ID
from core.image_description.demo_fixture import DEMO_PHOTO_ID, demo_messages, make_demo_photo
from core.image_description.errors import DescriptionError
from ui.conversation_panel.image_description import ImageDescriptionMixin
from ui.conversation_panel.media_paths import cached_media_path
from ui.dialogs.image_description_settings import ImageDescriptionSettingsPage


class DemoSettingsDialog(wx.Dialog):
    def __init__(self, frame):
        super().__init__(frame, title=frame.i18n.t("ai_title"), size=(600, 660),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        layout = wx.BoxSizer(wx.VERTICAL)
        notebook = wx.Notebook(self)
        self.page = ImageDescriptionSettingsPage(notebook, frame)
        notebook.AddPage(self.page, frame.i18n.t("ai_title"))
        layout.Add(notebook, 1, wx.EXPAND | wx.ALL, 8)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        for identifier, label in ((wx.ID_APPLY, "apply"), (wx.ID_OK, "ok"),
                                  (wx.ID_CANCEL, "cancel")):
            button = wx.Button(self, identifier, frame.i18n.t(label))
            buttons.Add(button, 0, wx.ALL, 6)
        layout.Add(buttons, 0, wx.ALIGN_RIGHT)
        self.SetSizer(layout)
        self.Bind(wx.EVT_BUTTON, self._apply, id=wx.ID_APPLY)
        self.Bind(wx.EVT_BUTTON, self._ok, id=wx.ID_OK)

    def _mark_dirty(self):
        pass  # Apply is always available in the small manual-demo host.

    def _apply(self, event):
        self.page.apply()

    def _ok(self, event):
        if self.page.apply():
            self.EndModal(wx.ID_OK)


class DemoConversationPanel(wx.Panel, ImageDescriptionMixin):
    def __init__(self, frame):
        super().__init__(frame)
        self.main_window = frame
        self.conversation = {"remoteJid": "demo-chat"}
        self._sorted_messages = demo_messages()
        self._image_description_dialog = None
        self.messages_list = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL,
                                        name=frame.i18n.t("messages").replace("&", ""))
        self.messages_list.InsertColumn(0, frame.i18n.t("messages").replace("&", ""), width=650)
        self.messages_list.Freeze()
        try:
            self.messages_list.InsertItem(0, frame.i18n.t("ai_demo_text"))
            self.messages_list.InsertItem(1, frame.i18n.t("ai_demo_photo"))
        finally:
            self.messages_list.Thaw()
        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(self.messages_list, 1, wx.EXPAND)
        self.SetSizer(layout)
        describe_id = wx.NewIdRef()
        self.messages_list.SetAcceleratorTable(wx.AcceleratorTable([
            (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord("Y"), describe_id),
        ]))
        self.Bind(wx.EVT_MENU, self._describe, id=describe_id)
        self.messages_list.Bind(wx.EVT_CONTEXT_MENU, self._menu)

    def focus_photo(self):
        self.messages_list.Focus(1)
        self.messages_list.Select(1)
        self.messages_list.SetFocus()

    def _describe(self, event=None, message=None):
        frame = self.main_window
        frame._chat_lock_unlocked = True  # Simulated vault, never a real PIN.
        if frame.lock_test.GetValue():
            frame._lock_timer = wx.CallLater(15_000, frame._simulate_lock)
        try:
            self._on_describe_photo(event, message)
        finally:
            if frame._lock_timer:
                frame._lock_timer.Stop()
                frame._lock_timer = None

    def describe_selected(self, event):
        index = self.messages_list.GetFocusedItem()
        if 0 <= index < len(self._sorted_messages):
            self._describe(message=self._sorted_messages[index])

    def _menu(self, event):
        if self.messages_list.GetFocusedItem() != 1:
            return
        menu = wx.Menu()
        try:
            item = menu.Append(wx.ID_ANY, self.main_window.i18n.t("ai_describe_photo") + "\tCtrl+Shift+Y")
            menu.Bind(wx.EVT_MENU, lambda e: self._describe(message=self._sorted_messages[1]), id=item.GetId())
            self.messages_list.PopupMenu(menu)
        finally:
            menu.Destroy()


class PhotoDemoFrame(wx.Frame):
    def __init__(self):
        self.settings = {"general": {"language": "tr-TR"},
                         "accessibility": {"sapi_fallback_enabled": False}}
        self.i18n = I18n(self)
        super().__init__(None, title=self.i18n.t("ai_demo_title"), size=(820, 600))
        self._shutting_down = False
        self._chat_lock_unlocked = True
        self._lock_timer = None
        self.key = Fernet.generate_key()
        self.app_settings = AppSettings(global_dir())
        self.speak_output = AccessibleSpeechOutput(outputs.auto.Auto(), lambda: self.settings)
        self._initialize_sounds()
        layout = wx.BoxSizer(wx.VERTICAL)
        note = wx.TextCtrl(self, value=self.i18n.t("ai_demo_notice"),
                           style=wx.TE_READONLY | wx.TE_MULTILINE,
                           name=self.i18n.t("ai_demo_title"), size=(-1, 120))
        layout.Add(note, 0, wx.EXPAND | wx.ALL, 8)
        self.panel = DemoConversationPanel(self)
        layout.Add(self.panel, 1, wx.EXPAND | wx.ALL, 8)
        self.lock_test = wx.CheckBox(self, label=self.i18n.t("ai_demo_lock_test"))
        layout.Add(self.lock_test, 0, wx.ALL, 8)
        buttons = wx.WrapSizer(wx.HORIZONTAL)
        for label, handler in (("settings", self._settings),
                               ("ai_describe_photo", self.panel.describe_selected),
                               ("close", lambda e: self.Close())):
            button = wx.Button(self, label=self.i18n.t(label))
            button.Bind(wx.EVT_BUTTON, handler)
            buttons.Add(button, 0, wx.ALL, 6)
        layout.Add(buttons, 0, wx.ALL, 4)
        self.status = wx.TextCtrl(self, style=wx.TE_READONLY, name=self.i18n.t("ai_status"))
        layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 8)
        self.SetSizer(layout)
        self.Bind(wx.EVT_CLOSE, self._close)
        media = Path(cached_media_path("imageMessage", DEMO_PHOTO_ID))
        media.parent.mkdir(parents=True, exist_ok=True)
        media.write_bytes(Fernet(self.key).encrypt(make_demo_photo()))

    def output(self, text):
        self.status.ChangeValue(text)
        self.speak_output.output(text)

    def _initialize_sounds(self):
        self.sound_system = None
        self._default_sound_pack = None
        try:
            system = SoundSystem(self, resource_path("sounds"))
            system.start()
            self.sound_system = system
            self._default_sound_pack = discover_sound_packs(system.sound_dir).get(DEFAULT_PACK_ID)
        except Exception as exc:
            logging.warning("[photo-demo] audio unavailable (%s)", type(exc).__name__)

    def get_active_sound_pack(self):
        return self._default_sound_pack

    def is_chat_locked(self, chat):
        return self.lock_test.GetValue()

    def handle_media_message(self, *args, **kwargs):
        # No network fallback, even if someone deletes the generated cache.
        raise DescriptionError("media")

    def _settings(self, event):
        dialog = DemoSettingsDialog(self)
        try:
            dialog.ShowModal()
        finally:
            dialog.Destroy()

    def _simulate_lock(self):
        self._chat_lock_unlocked = False
        self.panel.close_image_description(locked_only=True)
        self.output(self.i18n.t("ai_demo_locked"))

    def _close(self, event):
        self._shutting_down = True
        if self._lock_timer:
            self._lock_timer.Stop()
        self.panel.close_image_description()
        event.Skip()
