import socketio
import wx
import json
from traceback import format_exc

class WebSocketClient:
    def __init__(self, main_window):
        self.main_window = main_window

        self.sio = socketio.Client(
            ssl_verify=False
        )
        self.sio.on("connection.update", self.on_connection_update)
        self.sio.on("qrcode.updated", self.on_qrcode_update)

    def on_connection_update(self, data):
        try:
            message = json.loads(data)
        except json.JSONDecodeError as e:
            wx.MessageBox(f"Erro ao decodificar a mensagem do WebSocket: {format_exc()}", "Erro do WinZapp", wx.OK | wx.ICON_ERROR, self.main_window.connect.connection_dial)
            return

        print(message)

    def on_qrcode_update(self, data):
        try:
            message = json.loads(data)
        except json.JSONDecodeError as e:
            wx.MessageBox(f"Erro ao decodificar a mensagem do WebSocket: {format_exc()}", "Erro do WinZapp", wx.OK | wx.ICON_ERROR, self.main_window.connect.connection_dial)
            return

        print(message)