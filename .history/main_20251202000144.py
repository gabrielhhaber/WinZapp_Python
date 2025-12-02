import os
import sys
import wx
from dictionary_translation import dictionary as dt
from connect import check_connection_status

class MainWindow(wx.Frame):
    def __init__(self, title):
        super().__init__(None, title=title)
        self.nav_list_label = wx.StaticText(self, label=dt["pt"]["main_nav"])
        self.nav_list = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        #Insert an unique column
        self.nav_list.Insert(0, dt["pt"["main_nav"]])
        #Append navigation list items
        self.nav_list.Append((dt["pt"]["conversations"],))
        self.nav_list.Append((dt["pt"]["settings"],))

if __name__ == "__main__":
    app = wx.App()
    if check_connection_status():
        frame = MainWindow(title="WinZapp")
        frame.Show()
        app.MainLoop()