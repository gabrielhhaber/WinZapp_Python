import os
import sys
import wx
from dictionary_translation import dictionary as dt
from connect import Connect
import json
from traceback import format_exc

class MainWindow(wx.Frame):
    def __init__(self, title):
        super().__init__(None, title=title)
        self.settings = {}
        #Initialize helper classes
        self.connect = Connect(self)
        #Load client settings
        self.load_settings()
        self.nav_list_label = wx.StaticText(self, label=dt["pt"]["main_nav"])
        self.nav_list = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        #Insert an unique column
        self.nav_list.Insert(0, dt["pt"["main_nav"]])
        #Append navigation list items
        self.nav_list.Append((dt["pt"]["conversations"],))
        self.nav_list.Append((dt["pt"]["settings"],))

    def load_settings(self):
        try:
            self.settings = json.load(open(os.path.join(os.getcwd(), "data", "settings.json"), "r"))
        except Exception as e:
            wx.MessageBox(f"Erro ao carregar o arquivo de configuração: {format_exc(e)}", "Erro do WinZapp", wx.OK | wx.ICON_ERROR)
            sys.exit()


if __name__ == "__main__":
    app = wx.App()
    frame = MainWindow(title="WinZapp")
    if frame.connect.check_connection_status():
        frame.Show()
        app.MainLoop()
    else:
        frame.connect.show_connection_dial()