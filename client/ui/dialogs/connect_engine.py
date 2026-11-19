"""The connection-engine choice in the pairing dialog. Someone who has not
paired cannot reach Settings, so the dialog offers the choice itself; a new
engine is only picked up by a fresh start, so choosing one stores it and
restarts WinZapp (core/app_restart.py)."""

import logging

import wx

from app_paths import data_path, log_path
from core import connection_engine as engines


def offered_engines(bridge_available: bool, current: str, other_account_running: bool = False) -> list:
    """The engines the pairing dialog lists: Neonize first, when a bridge is
    installed (or it is the running one), then WPPConnect. A custom server is the
    user's own setup, edited in Settings: not listed, and no list at all when
    it is the engine in use, or while another account runs (the engine is
    install-wide)."""
    if current == engines.CUSTOM or other_account_running:
        return []
    return [e for e in (engines.NEONIZE, engines.WPPCONNECT)
            if engines.can_choose(e, bridge_available, current)]


def stored_settings(engine: str) -> dict:
    """The ``connection`` settings that make ``engine`` the one the next start
    uses (what MainWindow._apply_engine_choice stores, without touching the
    running process)."""
    changes = {"wpp_custom_api": engines.uses_custom_api(engine), "wpp_engine": engine}
    fixed = engines.fixed_address(engine)
    if fixed is not None:
        changes["wpp_server"], changes["wpp_ws_server"], changes["wpp_port"] = fixed
    return changes


def build_row(parent, sizer, i18n, offered, current):
    """Add the label and the list to ``sizer``; return the list. Empty when
    there is nothing to choose between."""
    if len(offered) < 2:
        return None
    label = wx.StaticText(parent, label=i18n.t("connection_engine_label"))
    sizer.Add(label, 0, wx.LEFT | wx.TOP | wx.RIGHT, 10)
    choice = wx.Choice(parent, choices=[i18n.t(engines.LABEL_KEYS[e]) for e in offered],
                       name=i18n.t("connection_engine_label").replace("&", ""))
    choice.SetSelection(offered.index(current) if current in offered else 0)
    sizer.Add(choice, 0, wx.EXPAND | wx.ALL, 10)
    return choice


def switch_and_restart(main_window, i18n, engine: str, restart) -> bool:
    """Store ``engine`` and start the restart. True when both worked; on a
    failure the previous settings are back and the user was told."""
    connection = main_window.settings.setdefault("connection", {})
    previous = dict(connection)
    connection.update(stored_settings(engine))
    main_window.save_settings()
    if restart(data_path("winzapp_restart.bat"), log_path("restart.log")):
        return True
    logging.error("[engine] could not arrange the restart for the %s engine", engine)
    connection.clear()
    connection.update(previous)
    main_window.save_settings()
    wx.MessageBox(i18n.t("pairing_engine_restart_failed"), i18n.t("pairing_engine_switch_title"),
                  wx.OK | wx.ICON_ERROR)
    return False
