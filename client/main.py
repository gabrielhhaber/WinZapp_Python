import os
import sys
import requests
import socketio
from accessible_output2 import outputs
from websocket_client import WebSocketClient
from sound_system import SoundSystem, Sound
from i18n import I18n
from utils import encrypt_json, decrypt_json, generate_and_save_key, retrieve_key
import wx
from connect import Connect
from navigation import NavigationPanel
from conversations import ConversationsPanel
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
        self.init_UI()

    def init_UI(self):
        self.main_panel = wx.Panel(self)
        self    .SetSize((400, 300))
        self.navigation_panel = NavigationPanel(self, self.main_panel)
        self.conversations_panel = ConversationsPanel(self, self.main_panel)

    def output(self, text, interrupt=False):
        self.speak_output.output(text, interrupt=interrupt)

    def load_settings(self):
        try:
            self.settings = json.load(open(os.path.join(os.getcwd(), "data", "settings.json"), "r"))
        except Exception as e:
            self.error_sound.play()
            wx.MessageBox(f"{self.i18n.t["settings_load_failed"]} {format_exc()}", self.i18n.t["error"], wx.OK | wx.ICON_ERROR)
            sys.exit()

    def save_settings(self):
        try:
            json.dump(self.settings, open(os.path.join(os.getcwd(), "data", "settings.json"), "w"), indent=4)
        except Exception as e:
            self.error_sound.play()
            wx.MessageBox(f"{self.i18n.t["settings_save_failed"]} {format_exc()}", self.i18n.t["error"], wx.OK | wx.ICON_ERROR)

    def load_sounds(self):
        self.startup_sound = Sound(self.sound_system, "startup.ogg")
        self.error_sound = Sound(self.sound_system, "error.ogg")
        self.waiting_pairing_sound = Sound(self.sound_system, "waiting_pairing.ogg")
        self.pairing_code_updated_sound = Sound(self.sound_system, "pairing_code_updated.ogg")
        self.connected_sound = Sound(self.sound_system, "connected.ogg")
        self.synchronizing_sound = Sound(self.sound_system, "synchronizing.ogg")

    def retrieve_token(self):
        try:
            with open(os.path.join(os.getcwd(), "data", "token.tk"), "r") as token_file:
                self.token = token_file.read().strip()
        except Exception as e:
            self.error_sound.play()
            wx.MessageBox(f"{self.i18n.t('token_retrieval_failed')} {format_exc()}", self.i18n.t("error"), wx.OK | wx.ICON_ERROR)
            sys.exit()

    def start_sync(self):
        self.create_basic_files()
        self.generate_secret_key()
        self.connected_sound.play()
        #Get connection settings
        self.authentication_server = self.settings.get("connection", {}).get("authentication_server", "127.0.0.1")
        self.authentication_port = self.settings.get("connection", {}).get("authentication_port", 8081)
        self.evolution_server = self.settings.get("connection", {}).get("evolution_server", "127.0.0.1")
        self.evolution_port = self.settings.get("connection", {}).get("evolution_port", 8080)
        self.chats = self.get_chats()
        self.contacts = self.get_contacts()
        self.set_chats()
        self.synchronizing_sound.play()
        self.output(self.i18n.t("synchronization_started"), interrupt=True)

    def show_window(self):
        self.Show()
        app.MainLoop()

    def create_basic_files(self):
        data_dir = os.path.join(os.getcwd(), "data")
        if not os.path.exists(data_dir):
            os.makedirs(data_dir)

        #Create empty messages.dat if not exists
        messages_file = os.path.join(data_dir, "messages.dat")
        if not os.path.isfile(messages_file):
            with open(messages_file, "w") as f:
                json.dump({}, f)

    def get_chats(self):
        url = f"https://{self.evolution_server}:{self.evolution_port}/chat/findChats/{self.token}"
        headers = {
            "apikey": self.token,
            "Content-Type": "application/json"
        }
        try:
            response = requests.post(url, headers=headers, verify=False)
            response_data = response.json()
            print(response_data)
            for chat in response_data:
                print(chat.get("pushName", ""))
            self.chat_ids = [chat.get("remoteJid", "") for chat in response_data]
            return response_data
        except Exception as e:
            self.error_sound.play()
            wx.MessageBox(f"{self.i18n.t('chat_retrieval_failed')} {format_exc()}", self.i18n.t("error"), wx.OK | wx.ICON_ERROR, self)

    def get_contacts(self):
        url = f"https://{self.evolution_server}:{self.evolution_port}/chat/findContacts/{self.token}"
        headers = {
            "apikey": self.token,
            "Content-Type": "application/json"
        }
        try:
            response = requests.post(url, headers=headers, verify=False)
            response_data = response.json()
            print(response_data)
            for contact in response_data:
                print(contact)
            return response_data
        except Exception as e:
            self.error_sound.play()
            wx.MessageBox(f"{self.i18n.t('contact_retrieval_failed')} {format_exc()}", self.i18n.t("error"), wx.OK | wx.ICON_ERROR, self)

    def set_chats(self):
        for chat in self.chats:
            for contact in self.contacts:
                if contact.get("remoteJid", "") == chat.get("remoteJid", ""):
                    print(contact.get("pushName", ""))
                    break
            else:
                print(chat.get("remoteJid", ""))

    def generate_secret_key(self):
        key_file = os.path.join(os.getcwd(), "data", "secret.key")
        if not os.path.isfile(key_file):
            generate_and_save_key(key_file)
    
    def retrieve_secret_key(self):
        key_file = os.path.join(os.getcwd(), "data", "secret.key")
        return retrieve_key(key_file)


if __name__ == "__main__":
    app = wx.App()
    frame = MainWindow(title="WinZapp")
    if frame.connect.check_connection_status():
        frame.retrieve_token()
        frame.start_sync()
        frame.show_window()
    else:
        frame.connect.show_connection_dial()
