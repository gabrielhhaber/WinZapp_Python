"""Choose visible local histories when an entire account exceeds scan limits."""
import wx

from core.storage_manager.references import chat_token
from .chat_picker import ChatCheckList


class StorageMatchingDialog(wx.Dialog):
    def __init__(self, parent, owner, preferences):
        super().__init__(parent, title=owner.i18n.t("storage_choose_matching_chats"),
                         size=(620, 540), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.SetMinSize((360, 320))
        self.privacy_epoch = owner.service.visibility_epoch
        box = wx.BoxSizer(wx.VERTICAL)
        self.all = wx.CheckBox(self, label=owner.i18n.t("storage_match_all_visible_chats"))
        self.all.SetValue(preferences.match_chats is None)
        box.Add(self.all, 0, wx.EXPAND | wx.ALL, 8)
        self.chats = ChatCheckList(self, owner.i18n)
        context = owner.main_window._storage_context
        context.capture_chats()
        options = [(chat_token(jid), name or owner.i18n.t("storage_private_item").format(number=n + 1),
                    jid.endswith("@g.us")) for n, (jid, name) in enumerate(context.allowed_chats().items())]
        self.chats.set_options(options)
        self.chats.set_checked(preferences.match_chats or set())
        self.chats.Enable(not self.all.GetValue())
        self.all.Bind(wx.EVT_CHECKBOX, lambda e: self.chats.Enable(not self.all.GetValue()))
        box.Add(self.chats, 1, wx.EXPAND | wx.ALL, 8)
        box.Add(self.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.EXPAND | wx.ALL, 8)
        self.SetSizer(box)

    def selection(self):
        return None if self.all.GetValue() else set(self.chats.checked_jids())

    def clear_private_state(self):
        self.chats.set_options([])
        if self.IsModal():
            self.EndModal(wx.ID_CANCEL)
