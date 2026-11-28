"""Describe a picture or video outside a conversation's message list.

The Status tab (inline player) and the separate media viewer end here; the
message list has its own entry point (ui/conversation_panel/ai_actions.py).
Which provider answers, under which consent and in which window is
core/ai_media's and ui/dialogs/ai_result_dialog.py's business; this module only
decides whether the action is offered, builds the window and tears it down.
"""
import copy

from app_paths import active_account_id, global_dir
from app_settings import AppSettings
from core.ai_credentials import CredentialStore
from core.ai_media import config as ai_config
from core.ai_media.config import MENU_KEY, preferences
from core.ai_media.local_text import offline_ocr_kind

#: Statuses are not chats; they carry this in the window's identity.
STATUS_CHAT = "status@broadcast"

_open = []


def _settings(main_window):
    app = getattr(main_window, "_app_settings", None) or AppSettings(global_dir())
    return app, preferences(app)


def describe_label(main_window, message):
    """Label of the Describe button for ``message``, or "" when none is
    offered: the same rule as the message menu (feature on, a provider with a
    key that takes the kind, not view-once; or Windows OCR when available)."""
    _app, config = _settings(main_window)
    kind = ai_config.offered_kind(message, config, CredentialStore(global_dir()).saved())
    if kind:
        return main_window.i18n.t(MENU_KEY[kind])
    if offline_ocr_kind(message):
        return main_window.i18n.t("ai_read_text_offline")
    return ""


def describe(parent, main_window, message, make_loader, chat=STATUS_CHAT):
    """Open the result window for ``message`` over ``parent`` (modal).

    ``make_loader(snapshot, kind)`` returns the callable that fetches the
    original bytes. One window at a time: a second request raises the open one
    instead of starting a duplicate billed request."""
    kind = ai_config.eligible_kind(message)
    if kind is None:
        return
    app, config = _settings(main_window)
    offered = ai_config.offered_kind(message, config, CredentialStore(global_dir()).saved())
    offline_only = offered is None and offline_ocr_kind(message) is not None
    if offered is None and not offline_only:
        main_window.output(main_window.i18n.t("ai_disabled"))
        return
    if _open:
        _open[0].Raise()
        return
    identity = (active_account_id(), chat, message["key"]["id"])
    snapshot = copy.deepcopy(message)
    info = (snapshot.get("message") or {}).get(snapshot["messageType"]) or {}
    from ui.dialogs.ai_result_dialog import AIResultDialog
    dialog = AIResultDialog(parent, identity, kind, False, config, app, make_loader(snapshot, kind),
                            str(info.get("mimetype") or ""),
                            **({"offline_only": True} if offline_only else {}))
    _open.append(dialog)
    try:
        dialog.ShowModal()
    finally:
        dialog._dispose()
        _open.remove(dialog)
        dialog.Destroy()


def close_open_dialog():
    """Close the open window, dropping its media and answers: on hiding or
    closing the app and on leaving the account."""
    for dialog in list(_open):
        dialog._close()
