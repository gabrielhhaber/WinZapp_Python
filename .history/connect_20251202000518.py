import os
import wx

def check_connection_status():
    #Look for a valid user token file
    user_token_path = os.path.join(os.getcwd(), "token.tk")
    return os.path.isfile(user_token_path)