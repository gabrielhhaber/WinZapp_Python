import os
import sys
import json

class I18n:
    def __init__(self, main_window):
        self.main_window = main_window
        self.language = "pt-BR" #default

    def get_language(self):
        #Gets the current language setting from main window settings
        self.language = self.main_window.settings.get("language", "pt-BR")
        return self.language

    def t(self, key):
        #Translates a given key based on the current language
        try:
            with open(os.path.join(os.getcwd(), "languages", f"{self.language}.json"), "r", encoding="utf-8") as f:
                translations = json.load(f)
            return translations.get(key, key)
        except Exception as e:
            return key