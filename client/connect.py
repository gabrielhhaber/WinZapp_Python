import os
import wx
from dictionary_translation import dictionary as dt

class Connect:
    def __init__(self, main_window):
        self.main_window = main_window
    def check_connection_status(self):
        #Look for a valid user token file
        user_token_path = os.path.join(os.getcwd(), "token.tk")
        return os.path.isfile(user_token_path)

    def show_connection_dial(self):
        self.connection_dial = wx.Dialog(None, title=dt["pt"]["connect_winzapp"], size=(300, 150))
        self.phone_number_label = wx.StaticText(self.connection_dial, label=dt["pt"]["enter_phone"])
        self.phone_field = wx.TextCtrl(self.connection_dial, style=wx.TE_CENTER | wx.TE_DONTWRAP)
        self.continue_btn = wx.Button(self.connection_dial, label=dt["pt"]["continue"])
        self.continue_btn.Bind(wx.EVT_BUTTON, self.on_continue)

        self.connection_dial.ShowModal()

    def on_continue(self, event):
        self.evolution_server = self.main_window.settings.get("connection", {}).get("evolution_server", "localhost:8080")