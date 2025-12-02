import os
import wx

def check_connection_status():
    #Look for a valid user token file
    user_token_path = os.path.join(os.getcwd(), "token.tk")
    return os.path.isfile(user_token_path)

def show_connection_dial():
    connection_dial = wx.Dialog(None, title=dt["pt"]["connect_winzapp"], size=(300, 150))
    phone_number_label = wx.StaticText(connection_dial, label=dt["pt"]["enter_phone"])
    phone_field = wx.TextCtrl(connection_dial, style=wx.TE_CENTER | wx.TE_DONTWRAP)
    continue_btn = wx.Button(connection_dial, label=dt["pt"]["continue"])

    connection_dial.ShowModal()