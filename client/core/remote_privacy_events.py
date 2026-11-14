"""Remote privacy observations update untouched choices, never send settings."""
import weakref
import wx

from core.sync_lifecycle import capture_sync_context, sync_context_is_current
from core.whatsapp_privacy import PRIVACY_FIELDS, option_index

ALLOWED = {
    "lastSeen": {"all", "contacts", "contact_blacklist", "none"},
    "online": {"all", "match_last_seen"}, "about": {"all", "contacts", "contact_blacklist", "none"},
    "profilePicture": {"all", "contacts", "contact_blacklist", "none"},
    "readReceipts": {"all", "none"}, "groupAdd": {"all", "contacts", "contact_blacklist", "known"},
}


def event_state(mw):
    context = capture_sync_context(mw)
    state = getattr(mw, "_privacy_event_state", None)
    if state is None or state["context"] != context:
        state = mw._privacy_event_state = {"context": context, "revision": 0, "fields": {}}
    return state


def register_panel(panel):
    mw = panel.main_window
    panels = getattr(mw, "_privacy_event_panels", None)
    if panels is None:
        panels = mw._privacy_event_panels = weakref.WeakSet()
    panel._privacy_event_context = capture_sync_context(mw)
    panels.add(panel)


def observations_since(mw, revision):
    return {name: value for name, (seen, value) in event_state(mw)["fields"].items() if seen > revision}


def update_panel(panel, fields):
    if not panel or panel._loaded is None:
        return
    for field in PRIVACY_FIELDS:
        name = field.setting
        if name not in fields:
            continue
        choice = panel._choices[name]
        untouched = choice.GetSelection() == option_index(field, panel._loaded.get(name))
        panel._loaded[name] = fields[name]
        if untouched:
            choice.SetSelection(option_index(field, fields[name]))


def apply_privacy_event(mw, fields, context=None):
    if getattr(mw, "_shutting_down", False):
        return
    if context is not None and not sync_context_is_current(mw, context):
        return
    state = event_state(mw)
    state["revision"] += 1
    state["fields"].update({name: (state["revision"], value) for name, value in fields.items()})
    for panel in list(getattr(mw, "_privacy_event_panels", [])):
        if sync_context_is_current(mw, panel._privacy_event_context):
            update_panel(panel, fields)


def on_privacy_event(client, data):
    if not isinstance(data, dict) or not client._belongs_to_this_session(data):
        return
    settings = data.get("settings")
    if not isinstance(settings, dict):
        return
    fields = {key: value for key, value in settings.items()
              if key in ALLOWED and isinstance(value, str) and value in ALLOWED[key]}
    if fields:
        mw = client.main_window
        wx.CallAfter(apply_privacy_event, mw, fields, capture_sync_context(mw))
