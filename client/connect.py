import os
import sys
import wx
import requests
from websocket import websocket_client
import asyncio
from dictionary_translation import dictionary as dt
from traceback import format_exc
import json

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
        self.phone_field = wx.TextCtrl(self.connection_dial, style=wx.TE_CENTER | wx.TE_PROCESS_ENTER | wx.TE_DONTWRAP)
        self.continue_btn = wx.Button(self.connection_dial, label=dt["pt"]["continue"])
        self.continue_btn.Bind(wx.EVT_BUTTON, self.on_continue)
        self.phone_field.Bind(wx.EVT_TEXT_ENTER, self.on_continue)

        self.connection_dial.ShowModal()

    def on_continue(self, event):
        #Load connection settings
        self.authentication_server = self.main_window.settings.get("connection", {}).get("authentication_server", "127.0.0.1")
        self.authentication_port = self.main_window.settings.get("connection", {}).get("authentication_port", 8081)
        self.evolution_server = self.main_window.settings.get("connection", {}).get("evolution_server", "127.0.0.1")
        self.evolution_port = self.main_window.settings.get("connection", {}).get("evolution_port", "8080")

        url = f"https://{self.authentication_server}:{self.authentication_port}/create_instance/"
        self.phone_number = self.phone_field.GetValue()
        self.token = self.generate_random_token()
        data = {
            "name": self.phone_number,
            "number": self.phone_number,
            "token": self.token
        }
        try:
            response = requests.post(url, json=data, verify=False)
            response_data = response.json()
            if response_data.get("qrcode", {}).get("pairingCode"):
                self.show_pairing_dial(response_data["qrcode"]["pairingCode"])
            else:
                wx.MessageBox(f"{dt["pt"]["connection_failed"]}{response.text}", dt["pt"]["connection_error"], wx.OK | wx.ICON_ERROR)
        except requests.exceptions.RequestException as e:
            wx.MessageBox(f"{dt["pt"]["connection_failed"]} {format_exc()}", dt["pt"]["connection_error"], wx.OK | wx.ICON_ERROR)

    def generate_random_token(self):
        return os.urandom(16).hex()

    def show_pairing_dial(self, pairing_code):
        self.pairing_dial = wx.Dialog(self.connection_dial, title=dt["pt"]["pairing_dial_intro"], size=(300, 150))
        self.pairing_instructions = wx.StaticText(self.pairing_dial, label=dt["pt"]["pairing_instructions"])
        self.pairing_code_label = wx.StaticText(self.pairing_dial, label=dt["pt"]["pairing_code_label"])
        self.pairing_code_field = wx.TextCtrl(self.pairing_dial, style=wx.TE_CENTER | wx.TE_READONLY | wx.TE_DONTWRAP, value=pairing_code)
        self.cancel_btn = wx.Button(self.pairing_dial, label=dt["pt"]["cancel_pairing"])
        self.cancel_btn.Bind(wx.EVT_BUTTON, self.on_cancel_pairing)

        self.ws = websocket_client
        self._socket_thread = threading.Thread(target=self.connect_websocket, daemon=True)
        self._socket_thread.start()

        self.pairing_dial.ShowModal()

    def connect_websocket(self):
        self.ws.connect(f"wss://{self.evolution_server}:{self.evolution_port}/{self.phone_number}", socketio_path="socket.io", headers={"apikey": self.token})
        self.ws.wait()

    def on_cancel_pairing(self, event):
        self.pairing_dial.Destroy()
        self.ws.close()