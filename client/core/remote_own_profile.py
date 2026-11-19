"""Refresh only the open own-profile photo baseline after a photo notice."""
import threading
import weakref

import wx

from core.sync_lifecycle import capture_sync_context, sync_context_is_current


def _state(mw):
    context = capture_sync_context(mw)
    state = getattr(mw, "_own_profile_photo_events", None)
    if state is None or state["context"] != context:
        state = mw._own_profile_photo_events = {
            "context": context, "panels": weakref.WeakSet(), "revision": 0,
            "latest_photo": None, "running": False, "pending": False,
        }
    return state


def register_panel(panel):
    state = _state(panel.main_window)
    panel._own_profile_photo_context = state["context"]
    state["panels"].add(panel)


def revision(mw):
    return _state(mw)["revision"]


def merge_read(mw, current, revision):
    """A slower initial profile read must keep a newer photo observation."""
    if current is None:
        return current
    state = _state(mw)
    latest = state["latest_photo"]
    if latest is not None and latest[0] > revision:
        from dataclasses import replace
        return replace(current, photo=latest[1])
    return current


def _apply_photo(panel, photo):
    current = getattr(panel, "_current", None)
    if current is None or photo is None:
        return False
    from dataclasses import replace
    panel._current = replace(current, photo=photo)
    panel._show_photo_state()
    return True


def apply_photo(mw, photo, context, revision):
    if (getattr(mw, "_shutting_down", False) or
            not sync_context_is_current(mw, context)):
        return
    state = _state(mw)
    if state["revision"] != revision:
        return
    state["latest_photo"] = (revision, photo)
    for panel in list(state["panels"]):
        if (panel and getattr(panel, "_current", None) is not None and
                sync_context_is_current(mw, panel._own_profile_photo_context)):
            _apply_photo(panel, photo)


def _finish(mw, context, revision, answer):
    state = getattr(mw, "_own_profile_photo_events", None)
    if state is None or state["context"] != context:
        return
    if sync_context_is_current(mw, context) and answer is not None:
        photo = getattr(answer, "photo", None)
        if isinstance(photo, bool):
            apply_photo(mw, photo, context, revision)
    state["running"] = False
    if state["pending"] and sync_context_is_current(mw, context):
        state["pending"] = False
        _start(mw, state)


def _start(mw, state):
    if (getattr(mw, "_shutting_down", False)
            or not sync_context_is_current(mw, state["context"])
            or not any(p for p in list(state["panels"]))):
        return
    state["running"] = True
    context, revision = state["context"], state["revision"]

    def read():
        try:
            answer = (mw.fetch_my_profile() if sync_context_is_current(mw, context)
                      and not getattr(mw, "_shutting_down", False) else None)
        except Exception:
            answer = None
        wx.CallAfter(_finish, mw, context, revision, answer)

    try:
        threading.Thread(target=read, daemon=True, name="wa-profile-photo-refresh").start()
    except Exception:
        state["running"] = False
        state["pending"] = False


def photo_changed(mw, jid):
    """One bounded read for a burst, and only for an open profile panel on self."""
    if getattr(mw, "_shutting_down", False):
        return
    is_self = getattr(mw, "_is_self_jid", None)
    if not callable(is_self) or not is_self(jid):
        return
    state = _state(mw)
    if not any(p and sync_context_is_current(mw, p._own_profile_photo_context)
               for p in list(state["panels"])):
        return
    state["revision"] += 1
    if state["running"]:
        state["pending"] = True
        return
    _start(mw, state)
