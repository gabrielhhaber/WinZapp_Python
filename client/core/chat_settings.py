"""Per-chat settings shown in the personal chat-info dialog.

Both come from data the client already holds, so nothing is asked of the
server and nothing in WPPConnect changes:

* Mute is WinZapp's own ``_muted_chats`` map (JID -> expiry, ``-1`` meaning
  for good), kept in step with WhatsApp's ``muteExpiration`` on every chat list
  sync and edited by the mute menu. It is read with the same rule as
  ``is_chat_muted``.
* Disappearing messages is the ``ephemeralDuration`` that wa-js puts on the
  chat, in the ``list-chats`` snapshot. It is only as fresh as that snapshot.

When the data is not there, no line is produced: a missing value is never
turned into "not muted" or "off".
"""

import time

# The durations WhatsApp offers; anything else positive is reported as "on".
_DAY = 86400
_WEEK = 7 * _DAY
_QUARTER = 90 * _DAY


def _number(value):
    """*value* if it is a real number (bools are not), else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def mute_state(muted_chats, jids, now):
    """How a chat's notifications stand: ``("off", None)``,
    ``("forever", None)`` or ``("until", expiry)``.

    *jids* are the forms of the chat's JID the mute may be filed under (phone
    and @lid). Same test as ``is_chat_muted``: an expiry of -1, or one still in
    the future; with several, the one that lasts longest is reported.
    """
    latest = None
    for jid in jids:
        expiry = _number((muted_chats or {}).get(jid))
        if expiry is None:
            continue
        if expiry == -1:
            return ("forever", None)
        if expiry > now and (latest is None or expiry > latest):
            latest = expiry
    if latest is None:
        return ("off", None)
    return ("until", int(latest))


def disappearing_duration(chat):
    """The chat's disappearing-messages timer in seconds (0 = off), or None
    when the chat does not carry the field or carries something unusable."""
    if not isinstance(chat, dict) or "ephemeralDuration" not in chat:
        return None
    value = _number(chat.get("ephemeralDuration"))
    if value is None or value < 0:
        return None
    return int(value)


def disappearing_value(seconds, i18n) -> str:
    """The words for a timer: "Off", one of WhatsApp's three durations, or a
    plain "On" for any other positive value (never an invented duration)."""
    if seconds == 0:
        return i18n.t("chat_disappearing_off")
    if seconds == _DAY:
        return i18n.t("chat_disappearing_24h")
    if seconds == _WEEK:
        return i18n.t("chat_disappearing_7d")
    if seconds == _QUARTER:
        return i18n.t("chat_disappearing_90d")
    return i18n.t("chat_disappearing_on")


def owner_call(owner, name, *args):
    """Call ``owner.name(*args)`` if the owner has it, else None."""
    method = getattr(owner, name, None)
    return method(*args) if callable(method) else None


def chat_settings_lines(owner, jid, i18n, fmt_ts, now=None) -> list:
    """"Label: value" lines for a personal chat's mute and disappearing state.

    *owner* is the main window (read tolerantly: whatever it does not have
    simply yields no line), *fmt_ts* formats a Unix time for display.
    """
    now = time.time() if now is None else now
    lines = []

    muted_chats = getattr(owner, "_muted_chats", None)
    if isinstance(muted_chats, dict):
        jids = owner_call(owner, "_mute_state_jids", jid) or {jid}
        state, expiry = mute_state(muted_chats, jids, now)
        label = i18n.t("chat_notifications_label")
        if state == "forever":
            lines.append(f"{label}: {i18n.t('chat_notifications_muted')}")
        elif state == "until":
            when = fmt_ts(expiry, i18n)
            value = (
                i18n.t("chat_notifications_muted_until").format(when=when)
                if when
                else i18n.t("chat_notifications_muted")
            )
            lines.append(f"{label}: {value}")
        else:
            lines.append(f"{label}: {i18n.t('chat_notifications_not_muted')}")

    seconds = disappearing_duration(owner_call(owner, "get_chat", jid))
    if seconds is not None:
        label = i18n.t("chat_disappearing_label")
        lines.append(f"{label}: {disappearing_value(seconds, i18n)}")

    return lines
