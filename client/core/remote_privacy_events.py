"""Remote privacy observations update untouched choices, never send settings."""
import weakref
import wx

from core.sync_lifecycle import capture_sync_context, sync_context_is_current
from core.whatsapp_privacy import (PRIVACY_FIELDS, jid_for_status_list, option_index,
                                   parse_status_privacy, status_option_index)

ALLOWED = {
    "lastSeen": {"all", "contacts", "contact_blacklist", "none"},
    "online": {"all", "match_last_seen"}, "about": {"all", "contacts", "contact_blacklist", "none"},
    "profilePicture": {"all", "contacts", "contact_blacklist", "none"},
    "readReceipts": {"all", "none"}, "groupAdd": {"all", "contacts", "contact_blacklist", "known"},
}
STATUS_SETTINGS = {"contact", "deny-list", "allow-list"}


def normalize_status(value):
    """Validate a remote status audience without displaying or logging its IDs."""
    if (not isinstance(value, dict) or not isinstance(value.get("setting"), str)
            or value["setting"] not in STATUS_SETTINGS):
        return None
    people = value.get("list")
    if not isinstance(people, list) or len(people) > 2000:
        return None
    if value["setting"] == "contact":
        return {"setting": "contact", "list": []} if not people else None
    if any(not isinstance(person, str) for person in people):
        return None
    normalized = [jid_for_status_list(person) for person in people]
    if any(not person for person in normalized):
        return None
    return {"setting": value["setting"], "list": list(dict.fromkeys(normalized))}


def event_state(mw):
    context = capture_sync_context(mw)
    state = getattr(mw, "_privacy_event_state", None)
    if state is None or state["context"] != context:
        state = mw._privacy_event_state = {"context": context, "revision": 0, "fields": {},
                                           "keep_archived": None, "status": None}
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


def keep_archived_since(mw, revision):
    seen, value = event_state(mw)["keep_archived"] or (0, None)
    return value if seen > revision else None


def status_since(mw, revision):
    seen, value = event_state(mw)["status"] or (0, None)
    return value if seen > revision else None


def keep_archived_for_load(mw, revision, fetched):
    """Keep a newer phone observation over a slow initial HTTP read."""
    observed = keep_archived_since(mw, revision)
    return observed if observed is not None else fetched


def update_panel(panel, fields, keep_archived=None, status=None):
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
    if isinstance(keep_archived, bool) and panel._keep_loaded is not None:
        checkbox = panel._keep_archived_check
        untouched = checkbox.GetValue() == panel._keep_loaded
        panel._keep_loaded = keep_archived
        if untouched:
            checkbox.SetValue(keep_archived)
    if status is not None:
        incoming = parse_status_privacy(status)
        if incoming is not None:
            loaded = panel._status_loaded
            # Status choices use their own option table, not the six account fields.
            untouched = loaded is None or (
                panel._status_choice.GetSelection() == status_option_index(loaded.setting)
                and panel._status_people is None)
            panel._status_loaded = incoming
            if untouched:
                panel._status_choice.SetSelection(status_option_index(incoming.setting))
                panel._status_choice.Enable()
                panel._refresh_status_controls()


def apply_privacy_event(mw, fields, keep_archived=None, status=None, context=None):
    if getattr(mw, "_shutting_down", False):
        return
    if context is not None and not sync_context_is_current(mw, context):
        return
    state = event_state(mw)
    state["revision"] += 1
    state["fields"].update({name: (state["revision"], value) for name, value in fields.items()})
    if isinstance(keep_archived, bool):
        state["keep_archived"] = (state["revision"], keep_archived)
    if status is not None:
        state["status"] = (state["revision"], status)
    for panel in list(getattr(mw, "_privacy_event_panels", [])):
        if sync_context_is_current(mw, panel._privacy_event_context):
            update_panel(panel, fields, keep_archived, status)


def on_privacy_event(client, data):
    if not isinstance(data, dict) or not client._belongs_to_this_session(data):
        return
    settings = data.get("settings", {})
    if not isinstance(settings, dict):
        return
    fields = {key: value for key, value in settings.items()
              if key in ALLOWED and isinstance(value, str) and value in ALLOWED[key]}
    keep_archived = data.get("keepArchived")
    status = normalize_status(data.get("status"))
    if fields or isinstance(keep_archived, bool) or status is not None:
        mw = client.main_window
        wx.CallAfter(apply_privacy_event, mw, fields, keep_archived, status,
                     capture_sync_context(mw))
