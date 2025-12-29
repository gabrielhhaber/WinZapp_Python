import os
import sys
import socketio
from accessible_output2 import outputs
from websocket_client import WebSocketClient
from sound_system import SoundSystem, Sound
from i18n import I18n
import wx
from connect import Connect
import json
from traceback import format_exc

class MainWindow(wx.Frame):
    def __init__(self, title):
        super().__init__(None, title=title)

        #Initialize screen reader/sapi output
        self.speak_output = outputs.auto.Auto()

        #Initialize sound system
        self.sound_system = SoundSystem(sound_dir=os.path.join(os.getcwd(), "sounds"))
        self.sound_system.start()
        self.load_sounds()

        self.settings = {}

        #Initialize helper classes
        self.connect = Connect(self)
        #Load client settings
        self.load_settings()
        #Initialize i18n
        self.i18n = I18n(self)
        self.i18n.get_language()
        #Play startup sound
        self.startup_sound.play()

        self.nav_list_label = wx.StaticText(self, label=self.i18n.t("main_nav"))
        self.nav_list = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        #Insert an unique column
        self.nav_list.InsertColumn(0, self.i18n.t("main_nav"), width=200)
        #Append navigation list items
        self.nav_list.Append((self.i18n.t("conversations"),))

    def output(self, text, interrupt=False):
        self.speak_output.output(text, interrupt=interrupt)

    def load_settings(self):
        try:
            self.settings = json.load(open(os.path.join(os.getcwd(), "data", "settings.json"), "r"))
        except Exception as e:
            wx.MessageBox(f"Erro ao carregar o arquivo de configuração: {format_exc()}", "Erro do WinZapp", wx.OK | wx.ICON_ERROR)
            sys.exit()

    def save_settings(self):
        try:
            json.dump(self.settings, open(os.path.join(os.getcwd(), "data", "settings.json"), "w"), indent=4)
        except Exception as e:
            wx.MessageBox(f"Erro ao salvar o arquivo de configuração: {format_exc()}", "Erro do WinZapp", wx.OK | wx.ICON_ERROR)

    def load_sounds(self):
        self.startup_sound = Sound(self.sound_system, "startup.ogg")
        self.waiting_pairing_sound = Sound(self.sound_system, "waiting_pairing.ogg")
        self.pairing_code_updated_sound = Sound(self.sound_system, "pairing_code_updated.ogg")
        self.connected_sound = Sound(self.sound_system, "connected.ogg")
        self.synchronizing_sound = Sound(self.sound_system, "synchronizing.ogg")


if __name__ == "__main__":
    app = wx.App()
    frame = MainWindow(title="WinZapp")
    if frame.connect.check_connection_status():
        frame.Show()
        app.MainLoop()
    else:
        frame.connect.show_connection_dial()