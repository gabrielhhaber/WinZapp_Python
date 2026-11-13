"""Pure call-state helpers shared by the wx UI and headless tests."""


# WhatsApp only places a group call for groups of up to this many participants
# (the account itself included, as the group-info participant list counts it).
MAX_GROUP_CALL_PARTICIPANTS = 32


def group_too_large_for_call(participant_count: int | None) -> bool:
    """Return whether a group has too many participants for a call.

    An unknown count (the group info has not arrived yet) is not "too large":
    the call buttons stay, and WhatsApp answers for itself.
    """
    return bool(participant_count) and participant_count > MAX_GROUP_CALL_PARTICIPANTS


def incoming_call_can_answer(details: dict | None) -> bool:
    """Return whether this incoming call can be answered.

    Group offers are answerable: WhatsApp Web accepts a group call through the
    same native ``voipStack.acceptCall`` its own Answer button uses, and
    WinZapp attaches Python audio to the resulting call. This stays the single
    decision point for the alert UI and the global answer key.
    """
    return True


def engine_refuses_call(uses_neonize: bool, bridge_has_calls: bool, *,
                        placing: bool, is_video: bool = False,
                        is_group: bool = False, bridge_has_video: bool = False,
                        bridge_has_group_calls: bool = False) -> bool:
    """Whether the connection engine cannot take this call, so WinZapp says so
    before opening the microphone and the speakers.

    WPPConnect takes every call. The Neonize bridge takes calls only when its
    /send-capabilities says ``calls: true`` (WinZapp's neonize build with the
    call layer); it places a video call only when it also says
    ``videoCalls: true`` (the build's video, and an ffmpeg to encode it) and a
    group call only with ``groupCalls: true``. An incoming video or group call
    it does answer (with voice only when it has no video).
    """
    if not uses_neonize:
        return False
    if not bridge_has_calls:
        return True
    if not placing:
        return False
    return (is_video and not bridge_has_video) or (is_group and not bridge_has_group_calls)


def active_call_label_key(active: dict | None) -> str:
    """Return the call-window label for the current phase."""
    active = active or {}
    if active.get("outgoing"):
        phase = active.get("phase")
        if phase == "CALLING":
            return "voice_call_starting"
        if phase == "RINGING":
            return "voice_call_ringing"
        if phase == "CONNECTING":
            return "voice_call_connecting"
    return "video_call_active_label" if active.get("is_video") else "voice_call_active_label"


# Windows RegisterHotKey modifier bits and virtual-key codes.
_MOD_ALT, _MOD_CONTROL, _MOD_SHIFT = 0x0001, 0x0002, 0x0004
_VK_BACK, _VK_RETURN = 0x08, 0x0D

# System-wide keys that answer, and reject / hang up, from any program. They
# are changeable in Settings > Keyboard shortcuts; these are the shipped keys.
# Ctrl+Alt+Shift matches the app's other all-windows commands and avoids
# AltGr (Ctrl+Alt) letters, which type characters on several keyboard layouts.
CALL_ANSWER_HOTKEY = (_VK_RETURN, _MOD_CONTROL | _MOD_ALT | _MOD_SHIFT)
CALL_END_HOTKEY = (_VK_BACK, _MOD_CONTROL | _MOD_ALT | _MOD_SHIFT)

#: general.<key> -> shipped (vk, mod). A stored {"vk": 0, "mod": 0} means the
#: user removed the shortcut.
CALL_HOTKEY_DEFAULTS = {
    "call_answer_hotkey": CALL_ANSWER_HOTKEY,
    "call_end_hotkey": CALL_END_HOTKEY,
}


def call_hotkey_is_valid(value) -> bool:
    """A key with Ctrl or Alt (as for the "show WinZapp" key), or the
    {"vk": 0, "mod": 0} that records a removed shortcut."""
    if not isinstance(value, dict):
        return False
    vk, mod = value.get("vk"), value.get("mod")
    if isinstance(vk, bool) or isinstance(mod, bool):
        return False
    if not isinstance(vk, int) or not isinstance(mod, int):
        return False
    if vk == 0 and mod == 0:
        return True
    if not 0 < vk < 256:
        return False
    if mod & ~(_MOD_ALT | _MOD_CONTROL | _MOD_SHIFT):
        return False
    return bool(mod & (_MOD_CONTROL | _MOD_ALT))


def saved_call_hotkey(general, key):
    """The (vk, mod) saved under ``general[key]``; the shipped key when nothing
    usable is saved; None once the user removed the shortcut."""
    value = (general or {}).get(key)
    if not call_hotkey_is_valid(value):
        return CALL_HOTKEY_DEFAULTS[key]
    if not value["vk"]:
        return None
    return value["vk"], value["mod"]


def call_hotkey_setting(general, key):
    """The call key in use: ``saved_call_hotkey``, but None while the "show
    WinZapp" key is that same combination.

    Someone could have put the show key on Ctrl+Alt+Shift+Enter before the call
    keys shipped on it. The older key keeps it: Windows gives a combination to
    one registration, so the call key could never work, and Settings would
    refuse every OK over the clash.
    """
    pair = saved_call_hotkey(general, key)
    show = (general or {}).get("global_hotkey")
    if pair and isinstance(show, dict) and (show.get("vk"), show.get("mod")) == pair:
        return None
    return pair


def first_answerable_call(active_calls, details_map) -> str:
    """Return the identity of the oldest ringing call that can be answered.

    Group offers ring and are answerable too, so they take part in the same
    oldest-first order as one-to-one calls rather than being skipped.
    """
    for identity in active_calls or ():
        if incoming_call_can_answer((details_map or {}).get(identity)):
            return identity
    return ""
