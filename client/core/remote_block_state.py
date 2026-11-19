"""Block notices reuse local state; identity-free changes request a fresh list."""
import logging
import wx

from core.phone_contacts import other_digit_form
from core.remote_message_state import chat_target
from core.sync_lifecycle import capture_sync_context


def apply_block_changed(mw, jid, blocked):
    if getattr(mw, "_shutting_down", False):
        return
    if jid.endswith("@lid"):
        jid = getattr(mw, "_lid_to_phone", {}).get(jid, "")
    jid = chat_target(jid)
    if not jid or not jid.endswith("@s.whatsapp.net") or not jid.split("@")[0].isdigit():
        return  # An unresolved LID is not a phone number.
    if not hasattr(mw, "_blocked_contacts"):
        mw._blocked_contacts = set()
    if not blocked:
        twin = other_digit_form(jid)
        if twin:
            mw._blocked_contacts.discard(twin.split("@")[0])
    mw._apply_block_state(jid, blocked)
    if getattr(mw, "_block_event_refreshing", False):
        mw._block_event_dirty = True


def refresh_block_list(mw):
    if getattr(mw, "_shutting_down", False):
        return
    if getattr(mw, "_block_event_refreshing", False):
        mw._block_event_dirty = True
        return
    context = capture_sync_context(mw)
    mw._block_event_refreshing = True
    def finished():
        mw._block_event_refreshing = False
        if getattr(mw, "_block_event_dirty", False):
            mw._block_event_dirty = False
            refresh_block_list(mw)
    def fetch():
        try:
            mw.get_block_list(expected_context=context)
        except Exception:
            logging.exception("[block-state-changed] could not refresh block list")
        finally:
            wx.CallAfter(finished)
    try:
        mw._msg_bg_executor.submit(fetch)
    except RuntimeError:
        mw._block_event_refreshing = False  # executor was closed during shutdown


def on_block_state(client, data):
    if not isinstance(data, dict) or not client._belongs_to_this_session(data):
        return
    if data.get("kind") == "refresh":
        wx.CallAfter(refresh_block_list, client.main_window)
        return
    jid = chat_target(data.get("contactId"))
    if data.get("kind") != "contact" or not jid or jid.endswith("@g.us") or type(data.get("blocked")) is not bool:
        return
    wx.CallAfter(apply_block_changed, client.main_window, jid, data["blocked"])
