"""Mac notifications for WinZapp (UserNotifications framework).

On Windows WinZapp shows toasts with a reply box and quick-reaction
buttons; clicking one opens the chat. Off Windows it had no toaster and
only spoke new messages. This gives the Mac the same notification, as a
native one: VoiceOver reads it when it appears, the reply box and reactions
are in its actions (VO-Command-Space), and activating it opens the chat.
macOS Focus modes apply to it automatically.

Only in the built app: UserNotifications needs a bundle identifier, which a
plain `python3 launcher.py` process does not have (dev runs keep the spoken
announcement).
"""

import json
import logging
import sys
import time

import objc
import wx
from Foundation import NSObject

_center = None
_delegate = None
_managers = []   # the NotificationManager (one per process)
_plain_actions = {}   # notification identifier -> what activating it does (post_plain)

CAT_INTERACTIVE = "wz.message"
CAT_PLAIN = "wz.plain"


def _un():
    import UserNotifications as UN
    return UN


class _Delegate(NSObject, protocols=[objc.protocolNamed("UNUserNotificationCenterDelegate")]):
    def userNotificationCenter_willPresentNotification_withCompletionHandler_(self, center, note, handler):
        UN = _un()
        handler(UN.UNNotificationPresentationOptionBanner | UN.UNNotificationPresentationOptionList)

    def userNotificationCenter_didReceiveNotificationResponse_withCompletionHandler_(self, center, response, handler):
        try:
            _handle(response)
        except Exception:
            logging.exception("[notify_mac] response failed")
        handler()


def _handle(response):
    UN = _un()
    action_for = _plain_actions.get(str(response.notification().request().identifier()))
    if action_for is not None:
        wx.CallAfter(action_for)
        return
    info = response.notification().request().content().userInfo()
    jid = str(info.get("jid", ""))
    key = json.loads(str(info.get("key") or "null"))
    action = str(response.actionIdentifier())
    mgr = _managers[-1] if _managers else None
    if mgr is None or not jid:
        return
    if action == "reply" and hasattr(response, "userText"):
        text = str(response.userText() or "").strip()
        if text:
            wx.CallAfter(mgr._do_reply, jid, text, key)
            return
    if action.startswith("react:") and key:
        wx.CallAfter(mgr._do_react, jid, key, action.split(":", 1)[1])
        return
    if action in (str(UN.UNNotificationDefaultActionIdentifier), "reply"):
        wx.CallAfter(mgr._do_open, jid)


def _ensure_center(mgr):
    global _center, _delegate
    if _center is not None:
        return _center
    from . import focus_mac
    focus_mac.request_permission()
    UN = _un()
    _center = UN.UNUserNotificationCenter.currentNotificationCenter()
    _delegate = _Delegate.alloc().init()
    _center.setDelegate_(_delegate)
    _center.requestAuthorizationWithOptions_completionHandler_(
        UN.UNAuthorizationOptionAlert | UN.UNAuthorizationOptionSound,
        lambda granted, err: logging.info("[notify_mac] notifications allowed: %s", bool(granted)))
    _register_categories(mgr)
    return _center


def _register_categories(mgr):
    UN = _un()
    i18n = mgr.i18n
    reply = UN.UNTextInputNotificationAction.actionWithIdentifier_title_options_textInputButtonTitle_textInputPlaceholder_(
        "reply", i18n.t("notif_send_reply"), 0, i18n.t("notif_send_reply"), i18n.t("notif_reply_hint"))
    reacts = [
        UN.UNNotificationAction.actionWithIdentifier_title_options_(
            f"react:{e}", i18n.t("notif_react_with").format(emoji=e), 0)
        for e in getattr(mgr, "_TOAST_REACTIONS", ())
    ]
    cats = {
        UN.UNNotificationCategory.categoryWithIdentifier_actions_intentIdentifiers_options_(
            CAT_INTERACTIVE, [reply] + reacts, [], 0),
        UN.UNNotificationCategory.categoryWithIdentifier_actions_intentIdentifiers_options_(
            CAT_PLAIN, [], [], 0),
    }
    _center.setNotificationCategories_(cats)


def _dispatch(self, title, body, remote_jid, msg_key=None, *, sound_event=None):
    """Mirror of NotificationManager._dispatch for the Mac notifier."""
    from core.quiet_hours import is_quiet_hours_active
    from core.notification_manager import (
        EDIT_SOUND_EVENT, REACTION_SOUND_EVENT, format_locked_notification,
        format_toast_unread_suffix, reaction_silenced_now,
    )
    from core.utils import effective_unread_count
    # A Focus silences WinZapp's own sound, but the notification is still
    # posted: macOS files it quietly or lets it through if the Focus allows
    # WinZapp or this person (Windows' Do Not Disturb drops it instead).
    quiet = is_quiet_hours_active()
    mw = getattr(self, "main_window", None)
    if (sound_event in (REACTION_SOUND_EVENT, EDIT_SOUND_EVENT)
            and reaction_silenced_now(mw, remote_jid)):
        return
    sound_args = (remote_jid,) if sound_event is None else (remote_jid, sound_event)
    if title is None:
        if not quiet:
            wx.CallAfter(self._play_sound, *sound_args)
        return
    get_chat = getattr(mw, "get_chat", None)
    chat = get_chat(remote_jid) if callable(get_chat) else getattr(mw, "chats", {}).get(remote_jid)
    locked = bool(getattr(mw, "is_chat_locked", lambda _j: False)(remote_jid))
    if locked:
        title, body = format_locked_notification(
            effective_unread_count(chat or {}), self.i18n, getattr(mw, "app_name", "WinZapp"))
    try:
        center = _ensure_center(self)
        UN = _un()
        self.i18n.get_language()
        suffix = ""
        if not locked and chat is not None and not chat.get("_unread_count_unsynced"):
            suffix = format_toast_unread_suffix(effective_unread_count(chat), self.i18n)
        if not quiet:
            wx.CallAfter(self._play_sound, *sound_args)
        content = UN.UNMutableNotificationContent.alloc().init()
        content.setTitle_(str(title))
        content.setBody_(f"{body}\n{suffix}".strip() if suffix else str(body))
        content.setThreadIdentifier_(remote_jid)
        content.setCategoryIdentifier_(CAT_PLAIN if locked else CAT_INTERACTIVE)
        content.setUserInfo_({"jid": remote_jid, "key": json.dumps(msg_key if not locked else None)})
        # Silent: WinZapp plays its own per-chat sound (above).
        req = UN.UNNotificationRequest.requestWithIdentifier_content_trigger_(
            remote_jid, content, None)
        center.addNotificationRequest_withCompletionHandler_(req, None)
        self._last_shown_at = time.monotonic()
    except Exception:
        logging.exception("[notify_mac] could not post a notification")
        if not quiet:
            self._announce_unshown(title, body)


def post_plain(identifier, title, body, on_open=None):
    """A notification that is not about a chat (no reply box, no reactions):
    VoiceOver reads it when it appears; activating it brings WinZapp forward
    and calls *on_open* on the main thread. False when it could not be posted
    (a development run has no bundle identifier; nothing is posted then)."""
    if not getattr(sys, "frozen", False) or not _managers:
        return False
    try:
        center = _ensure_center(_managers[-1])
        UN = _un()
        content = UN.UNMutableNotificationContent.alloc().init()
        content.setTitle_(str(title))
        content.setBody_(str(body))
        content.setCategoryIdentifier_(CAT_PLAIN)
        if on_open is not None:
            _plain_actions[identifier] = on_open
        req = UN.UNNotificationRequest.requestWithIdentifier_content_trigger_(identifier, content, None)
        center.addNotificationRequest_withCompletionHandler_(req, None)
        return True
    except Exception:
        logging.exception("[notify_mac] could not post a notification")
        return False


def remove_plain(identifier):
    """Take a post_plain() notification away (from the screen and from
    Notification Center) once it no longer applies."""
    _plain_actions.pop(identifier, None)
    if _center is None:
        return
    try:
        _center.removeDeliveredNotificationsWithIdentifiers_([identifier])
        _center.removePendingNotificationRequestsWithIdentifiers_([identifier])
    except Exception:
        logging.debug("[notify_mac] could not remove %s", identifier, exc_info=True)


def install():
    if not getattr(sys, "frozen", False):
        return
    from core import notification_manager as nm
    cls = nm.NotificationManager
    orig_init = cls.__init__

    def init(self, *a, **k):
        orig_init(self, *a, **k)
        _managers.append(self)
        # Ask for notification and Focus permission at startup, while the
        # user is at the Mac, rather than on the first incoming message.
        wx.CallAfter(_ensure_center, self)

    cls.__init__ = init
    cls._setup_toaster = lambda self: None
    cls._clear_active_toasts = lambda self: None
    cls._dispatch = _dispatch
