import os
import wx

def check_connection_status():
    #Look for a valid user token file
    user_token_path = os.path.join(os.getcwd(), "token.tk")
    return os.path.isfile(user_token_path)

def show_connection_dial():
    connection_dial = wx.Dialog(None, title=dt["pt"]["connect_winzapp"], size=(300, 150))