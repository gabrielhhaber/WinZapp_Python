import socketio
import wx
import json
from dictionary_translation import dictionary as dt
from traceback import format_exc

class WebSocketClient:
    def __init__(self, main_window, instance_name):
        self.main_window = main_window
        self.instance_name = instance_name

        self.sio = socketio.Client(
            ssl_verify=False,
            reconnection=True, reconnection_attempts=5,
            logger=True, engineio_logger=True
        )
        self.sio.on("connect", self.on_connect, namespace=f"/{self.instance_name}")
        self.sio.on("disconnect", self.on_disconnect, namespace=f"/{self.instance_name}")
        self.sio.on("connection.update", self.on_connection_update, namespace=f"/{self.instance_name}")
        self.sio.on("qrcode.updated", self.on_qrcode_update, namespace=f"/{self.instance_name}")

    def on_connect(self):
        print("WebSocket connected.")

    def on_disconnect(self):
        print("WebSocket disconnected.")

    def on_connection_update(self, data):
        print(data)

    def on_qrcode_update(self, data):
        self.main_window.speak_output.output(dt["pt"]["qrcode_updated"])
        self.main_window.connect.pairing_code_field.SetValue(data.get("qrcode", {}).get("pairingCode", ""))

