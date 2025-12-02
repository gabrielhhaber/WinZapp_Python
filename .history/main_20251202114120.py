import os
import sys
import wx
from dictionary_translation import dictionary as dt
from connect import Connect

class MainWindow(wx.Frame):
    def __init__(self, title):
        super().__init__(None, title=title)
        #Initialize helper classes
        self.connect = Connect()
        self.nav_list_label = wx.StaticText(self, label=dt["pt"]["main_nav"])
        self.nav_list = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        #Insert an unique column
        self.nav_list.Insert(0, dt["pt"["main_nav"]])
        #Append navigation list items
        self.nav_list.Append((dt["pt"]["conversations"],))
        self.nav_list.Append((dt["pt"]["settings"],))

if __name__ == "__main__":
    app = wx.App()
    frame = MainWindow(title="WinZapp")
    if frame.connect.check_connection_status():
        frame.Show()
        app.MainLoop()
    else:
        frame.show_connection_dial()