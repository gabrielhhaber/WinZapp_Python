"""macOS compatibility layer for WinZapp.

Everything here is installed by macos/launcher.py before WinZapp's own
client/main.py is imported, so upstream files need as few Mac-specific
edits as possible and upstream merges stay clean.
"""

import sys


def install():
    if sys.platform != "darwin":
        return
    from . import paths_mac
    paths_mac.install()  # first: other modules bind app_paths names on import
    from . import accessibility_mac, audio_mac, autostart_mac, button_keys_mac, camera_mac, combo_mac, coord_mac, diagnostics_mac, focus_mac, hotkey_mac, keymap_mac, launch_mac, layout_mac, lifecycle_mac, listctrl, menubar_mac, notify_mac, platform_mac, server_mac, sound_mac, sparkle_mac, speech, spell_mac, strings_mac, updater_mac, version_mac, wake_mac, vo_focus_mac
    version_mac.install()  # before anything imports `from version import __version__`
    coord_mac.install()  # before main.py's start-up gate reads any lease
    diagnostics_mac.install()
    launch_mac.install()
    accessibility_mac.install()
    listctrl.install()
    combo_mac.install()
    speech.install()
    autostart_mac.install()
    sound_mac.install()
    audio_mac.install()
    keymap_mac.install()
    button_keys_mac.install()
    menubar_mac.install()
    strings_mac.install()
    notify_mac.install()
    lifecycle_mac.install()
    server_mac.install()
    platform_mac.install()
    layout_mac.install()
    spell_mac.install()
    camera_mac.install()
    hotkey_mac.install()
    focus_mac.install()
    wake_mac.install()
    vo_focus_mac.install()
    updater_mac.install()
    sparkle_mac.install()  # after updater_mac, which stays the fallback
