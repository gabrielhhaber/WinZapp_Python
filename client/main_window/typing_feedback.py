"""Engine-independent typing sound delivery on the existing wx UI thread."""
import wx

from core.conversation_view import conversation_in_view
from core.typing_feedback import TypingCadence, interval, mode, silence_during_capture


def canonical(window, jid):
    jid = window._normalize_jid(jid)
    return getattr(window, "_lid_to_phone", {}).get(jid, jid)


def context(window):
    """No authority to play outside the active, visible conversation visit."""
    try:
        if (mode(window.settings) not in ("sound", "both")
                or getattr(window, "_shutting_down", False)
                or getattr(window, "_window_hidden", False)
                or not getattr(window, "_wa_connected", True)
                or not window.IsShown() or window.IsIconized() or not window.IsActive()):
            return None
        panel = getattr(window, "conversations_panel", None)
        conv = getattr(panel, "conversation", None)
        if not conv or not conversation_in_view(panel):
            return None
        if silence_during_capture(window.settings) and (
                getattr(panel, "_is_recording", False)
                or getattr(panel, "_recording_starting", False)
                or getattr(panel, "_recording_system_audio", False)
                or getattr(window, "_active_voice_call", None)
                or getattr(window, "_call_audio_session", None)):
            return None
        chat = canonical(window, conv.get("remoteJid", ""))
        if not chat or not chat.endswith(("@s.whatsapp.net", "@lid", "@g.us")):
            return None
        pack = window.get_active_sound_pack()
        pack_id = pack.get("id", "default") if pack else "default"
        cfg = window.settings.get("sound_events", {}).get(pack_id, {}).get("typing", {})
        if not cfg.get("enabled", True):
            return None
        return (panel, chat, getattr(panel, "_contact_presence_visit", 0),
                getattr(window, "_contact_presence_epoch", 0), pack_id,
                cfg.get("path", ""), id(getattr(window, "typing_sound", None)))
    except (AttributeError, RuntimeError, TypeError):
        return None


def stop_typing_sound(window):
    controller = getattr(window, "_typing_sound_cadence", None)
    if controller is not None:
        if wx.IsMainThread():
            controller.stop()
        else:
            retired = controller.retire()
            try:
                wx.CallAfter(controller.finish_stop, *retired)
            except RuntimeError:
                # wx is already gone. No old callback is authorized to play.
                pass


def stop_typing_for_capture(window):
    """UI-thread preflight only; does not change capture, call or speech state."""
    if silence_during_capture(getattr(window, "settings", {})):
        stop_typing_sound(window)


def _make_controller(window):
    owned_sound = getattr(window, "typing_sound", None)
    def stop():
        sound = owned_sound
        if sound is not None:
            try:
                sound.stop()
            except Exception:
                pass

    def play():
        sound = owned_sound
        if sound is not None:
            # Long custom files finish before replaying; never overlap/restart.
            if not getattr(sound, "is_playing", False):
                sound.play()

    controller = TypingCadence(wx.CallLater, lambda: context(window), play, stop,
                              lambda: interval(window.settings))
    window._typing_sound_cadence = controller
    controller.owned_sound = owned_sound

    if getattr(window, "_typing_sound_handlers_bound", False):
        return controller
    window._typing_sound_handlers_bound = True

    def on_activate(event):
        if not event.GetActive():
            stop_typing_sound(window)
        event.Skip()

    def on_hide(event):
        if not event.IsShown():
            stop_typing_sound(window)
        event.Skip()

    def on_destroy(event):
        if event.GetEventObject() is window:
            stop_typing_sound(window)
        event.Skip()

    def on_iconize(event):
        if event.IsIconized():
            stop_typing_sound(window)
        event.Skip()

    window.Bind(wx.EVT_ACTIVATE, on_activate)
    window.Bind(wx.EVT_SHOW, on_hide)
    window.Bind(wx.EVT_ICONIZE, on_iconize)
    window.Bind(wx.EVT_WINDOW_DESTROY, on_destroy)
    panel = window.conversations_panel
    for widget in (panel, getattr(panel, "conversation_panel", None)):
        if widget is not None and hasattr(widget, "Bind"):
            widget.Bind(wx.EVT_SHOW, on_hide)
    return controller


def observe_typing_batch(window, chat, changes):
    """Sound alone batches participants; cache, speech and provider order stay intact."""
    current = context(window)
    controller = getattr(window, "_typing_sound_cadence", None)
    if current is None:
        if controller is not None:
            controller.stop()
        return
    if canonical(window, chat) != current[1]:
        return  # Another chat must not interrupt the open chat's cadence.
    if controller is not None and controller.owned_sound is not getattr(window, "typing_sound", None):
        controller.stop()
        controller = None
    if controller is None:
        if "composing" not in changes.values():
            return
        controller = _make_controller(window)
    controller.observe_many(current, {canonical(window, person): state == "composing"
                                      for person, state in changes.items()})


def dismiss_typing_message(window, chat, msg):
    controller = getattr(window, "_typing_sound_cadence", None)
    if controller is None or controller.context is None:
        return
    key = msg.get("key") or {}
    if key.get("fromMe") or canonical(window, chat) != controller.context[1]:
        return
    if chat.endswith("@g.us"):
        sender = key.get("participant") or msg.get("participant")
        if sender:
            controller.dismiss(canonical(window, sender))
    else:
        controller.dismiss()
