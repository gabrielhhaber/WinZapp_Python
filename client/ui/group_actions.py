"""Group actions that change local UI only after server confirmation."""

import threading
import wx
from core.group_invitation import connection_identity


def finish_leave_group(main_window, jid, ok, message, connection):
    if getattr(main_window, "_shutting_down", False) or connection_identity(main_window) != connection:
        return
    if not ok:
        wx.MessageBox(message, main_window.i18n.t("leave_group"), wx.OK | wx.ICON_ERROR, main_window)
        return
    panel = main_window.conversations_panel
    if panel.conversation and panel.conversation.get("remoteJid") == jid:
        panel.close_conversation()
    # Preserve history locally. Leaving does not need a second remote mutation.
    main_window._set_archived_state(jid, True)


def leave_group_from_ui(main_window, jid):
    connection = connection_identity(main_window)
    def run():
        ok, message = main_window.leave_group(jid, connection=connection)
        wx.CallAfter(finish_leave_group, main_window, jid, ok, message, connection)
    threading.Thread(target=run, daemon=True).start()
