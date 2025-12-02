import os
import sys
import wx
from . import dictionary_translation.dictionary as dt

class MainWindow(wx.Frame):
    def __init__(self, title):
        super.__init__(self, title=title)
        self.nav_list_label = wx.StaticText(self, label=dt["pt"]["main_nav"])
        self.nav_list = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        #Append navigation list items
        nav_list.Append((dt["pt"]["conversations"],))
        nav_list.Append((dt["pt"]["settings"],))
