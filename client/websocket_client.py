import sys
import socketio
import wx
import json
from i18n import I18n
from traceback import format_exc

class WebSocketClient:
    def __init__(self, main_window, instance_name):
        self.main_window = main_window
        #Initialize i18n
        self.i18n = I18n(self.main_window)
        self.instance_name = instance_name

        self.sio = socketio.Client(
            ssl_verify=False,
            reconnection=True, reconnection_attempts=5,
            logger=True
        )
        self.sio.on("connect", self.on_connect, namespace=f"/{self.instance_name}")
        self.sio.on("disconnect", self.on_disconnect, namespace=f"/{self.instance_name}")
        self.sio.on("connection.update", self.on_connection_update, namespace=f"/{self.instance_name}")
        self.sio.on("qrcode.updated", self.on_qrcode_update, namespace=f"/{self.instance_name}")

    def on_connect(self):
        print("WebSocket connected.")

    def on_disconnect(self):
        print("WebSocket disconnected.")

    def on_connection_update(self, info):
        print(info)
        #Checks the new connection state
        connection_state = info.get("data", {}).get("state", "")
        if connection_state == "open":
            self.on_pairing_complete()
        else:
            self.main_window.error_sound.play()
            wx.MessageBox(self.i18n.t["instance_state_changed"], self.i18n.t["error"], wx.OK | wx.ICON_ERROR, self.main_window.connect.pairing_dial)

    def on_pairing_complete(self):
        #Saves the new user token in the program directory
        try:
            self.save_token(self.token)
        except Exception as e:
            self.main_window.error_sound.play()
            wx.MessageBox(f"{self.i18n.t('token_save_failed')} {format_exc()}", self.i18n.t("error"), wx.OK | wx.ICON_ERROR)
            sys.exit()

        self.main_window.pairing_dial.Destroy()
        self.main_window.connection_dial.Destroy()
        self.main_window.start_sync()

    def save_token(self, token):
        with open("token.tk", "w") as token_file:
            token_file.write(token)


    def on_qrcode_update(self, info):
        print(info)
        self.main_window.pairing_code_updated_sound.play()
        self.main_window.speak_output.output(self.i18n.t("qrcode_updated"))
        self.main_window.connect.pairing_code_field.SetValue(info.get("data", {}).get("qrcode", {}).get("pairingCode", ""))

