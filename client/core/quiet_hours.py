"""Windows notification-suppression detection for background notifications.

WinZapp plays its background message sound itself, outside the Windows toast
audio pipeline. Before doing that it must mirror the Windows notification state:

* Windows 11 Do Not Disturb / Focus Assist: WNF active quiet-hours profile,
  current CloudStore profile, and SHQueryUserNotificationState fallbacks.
* Global/app notification blocking: ToastNotifier.setting plus registry/policy
  fallbacks for the Windows Notifications switches.
* Full-screen, presentation, and busy states: SHQueryUserNotificationState.

This gate covers the *background* notification path only — both its sound and
its spoken announcement. For a blind user the spoken announcement IS the
notification, so gating one without the other would have left Do Not Disturb
half-honoured; both are silenced together, and only on the path taken after
main.py's `if window_active: ... return`. Sounds and speech for the currently
focused conversation, foreground sounds for other conversations, and incoming
call alerts never reach this gate and are deliberately unchanged.

Every probe below is written to fail OPEN. A false negative here costs one
notification sound the user did not want; a false positive silences WinZapp
indefinitely for a reason nothing in the UI explains, which for this app's
users means silently not knowing that messages are arriving at all. Where a
signal is ambiguous or undocumented, it must yield None (unknown), never True.
"""

import importlib
import logging
import sys
import threading
import time
from typing import Optional

# QUERY_USER_NOTIFICATION_STATE values (shellapi.h)
_QUNS_NOT_PRESENT = 1
_QUNS_BUSY = 2
_QUNS_RUNNING_D3D_FULL_SCREEN = 3
_QUNS_PRESENTATION_MODE = 4
_QUNS_ACCEPTS_NOTIFICATIONS = 5
_QUNS_QUIET_TIME = 6
_QUNS_APP = 7

# States in which Windows would itself suppress a toast's sound/banner: a
# fullscreen app/game, a presentation, or Focus Assist (both "Priority only"
# and "Alarms only" surface here as QUNS_QUIET_TIME — Windows doesn't expose
# a finer-grained value through this API).
_SUPPRESSED_STATES = {
    _QUNS_BUSY, _QUNS_RUNNING_D3D_FULL_SCREEN,
    _QUNS_PRESENTATION_MODE, _QUNS_QUIET_TIME,
}

# WNF_SHEL_QUIETHOURS_ACTIVE_PROFILE_CHANGED
#
# This is the state Windows Shell updates when the active Focus Assist / Do
# Not Disturb profile changes. Its DWORD payload is the restriction level:
# 0 = unrestricted/off, non-zero = a restrictive profile is active.
#
# WNF is an undocumented Windows implementation detail, so this remains a
# best-effort fallback. The state name and NtQueryWnfStateData ABI are stable
# across the Windows 10/11 builds WinZapp targets, and unlike the old values
# below they actually describe the active quiet-hours profile.
_WNF_QUIET_HOURS_ACTIVE_PROFILE = (0xA3BF1C75, 0x0D83063E)


def should_suppress_notification_sound(state: int) -> bool:
    """Pure mapping from a QUERY_USER_NOTIFICATION_STATE value to whether the
    background-notification sound should be skipped. Split out from
    is_quiet_hours_active() so the decision table is testable without
    touching the real Win32 API."""
    return state in _SUPPRESSED_STATES


def _query_notification_state():
    """Raw SHQueryUserNotificationState call, isolated in its own function so
    tests can stub it without reaching into ctypes.windll. Returns None on
    any failure (off-Windows, API error, missing DLL, ...)."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        state = ctypes.c_int()
        result = ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state))
        if result != 0:  # S_OK == 0
            return None
        return state.value
    except Exception:
        return None


def _query_wnf_state(low: int, high: int) -> Optional[int]:
    """Query a single WNF state from ntdll.dll."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        ntdll = ctypes.WinDLL("ntdll")
        query = getattr(ntdll, "NtQueryWnfStateData", None)
        if query is None:
            return None

        class WNF_STATE_NAME(ctypes.Structure):
            _fields_ = [("Data", ctypes.c_uint32 * 2)]

        state_name = WNF_STATE_NAME((ctypes.c_uint32 * 2)(low, high))
        change_stamp = ctypes.c_uint32(0)
        value = ctypes.c_uint32(0)
        buffer_size = ctypes.c_uint32(ctypes.sizeof(value))

        query.argtypes = (
            ctypes.POINTER(WNF_STATE_NAME),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
        )
        query.restype = ctypes.c_int32

        status = query(
            ctypes.byref(state_name),
            None,
            None,
            ctypes.byref(change_stamp),
            ctypes.byref(value),
            ctypes.byref(buffer_size),
        )
        if status == 0 and buffer_size.value >= ctypes.sizeof(value):
            return int(value.value)
    except Exception as exc:
        logging.debug("[quiet_hours] WNF query failed: %s", exc)
    return None


def _query_wnf_quiet_hours() -> Optional[bool]:
    """Query Windows Notification Facility (WNF) for real-time Focus Assist /
    Do Not Disturb state in Windows 10 (1803+) and Windows 11.

    WNF_SHEL_QUIETHOURS_ACTIVE_PROFILE_CHANGED = 0x0D83063EA3BF1C75
    Values: 0 = Off/unrestricted; non-zero = a restrictive profile is active.
    """
    if sys.platform != "win32":
        return None
    low, high = _WNF_QUIET_HOURS_ACTIVE_PROFILE
    value = _query_wnf_state(low, high)
    if value is None:
        return None
    return value > 0


def _parse_cloudstore_quiet_hours(data) -> Optional[bool]:
    """Parse the named quiet-hours profile from a Windows 11 CloudStore blob.

    Returns None — not a decision — whenever the blob names more than one
    profile. This is the difference between a probe and a guess: the blob is
    documented nowhere, and a payload that happens to carry the *list* of
    available profiles alongside (or instead of) the active one contains
    "PriorityOnly" whether or not Do Not Disturb is on. Picking the first
    restrictive name out of such a blob silences WinZapp permanently on a
    machine with DND off, which is exactly the failure this module must not
    have. One name is a reading; several names are noise.
    """
    if not isinstance(data, (bytes, bytearray)):
        return None

    profiles = {
        "Microsoft.QuietHoursProfile.PriorityOnly": True,
        "Microsoft.QuietHoursProfile.AlarmsOnly": True,
        "Microsoft.QuietHoursProfile.Unrestricted": False,
    }
    raw = bytes(data)
    found = [
        is_restricted for profile, is_restricted in profiles.items()
        if profile.encode("utf-16-le") in raw or profile.encode("utf-8") in raw
    ]
    if len(found) != 1:
        return None
    return found[0]


def _query_registry_notifications_disabled() -> bool:
    """Registry fallbacks for Windows notification blocking and DND.

    The primary WinRT/WNF checks are preferred, but registry fallbacks cover
    systems where those APIs are unavailable to the Python runtime. Windows 11
    stores the current DND profile in CloudStore; that payload is parsed by
    profile name rather than by guessing byte offsets.
    """
    if sys.platform != "win32":
        return False
    try:
        import winreg

        # 1. Global toggle: "Obter notificações de apps e outros remetentes"
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\PushNotifications") as k:
                val, _ = winreg.QueryValueEx(k, "ToastEnabled")
                if val == 0:
                    return True
        except OSError:
            pass

        # 2. Group Policy
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(root, r"Software\Policies\Microsoft\Windows\CurrentVersion\PushNotifications") as k:
                    val, _ = winreg.QueryValueEx(k, "NoToastApplicationNotification")
                    if val == 1:
                        return True
            except OSError:
                pass

        # 3. Action Center global setting
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Notifications\Settings") as k:
                val, _ = winreg.QueryValueEx(k, "NOC_GLOBAL_SETTING_TOASTS_ENABLED")
                if val == 0:
                    return True
        except OSError:
            pass

        # 4. App-specific toggle in Windows Settings.
        #
        # WinZapp's own AUMID only. The "python"/"pythonw" keys were tried here
        # and removed: in a dev run WinZapp *is* python.exe, so any unrelated
        # Python script the user had ever silenced in Windows Settings silenced
        # WinZapp too — a per-app switch the user never set for this app.
        for app_key in (
            r"Software\Microsoft\Windows\CurrentVersion\Notifications\Settings\WinZapp",
        ):
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, app_key) as k:
                    val, _ = winreg.QueryValueEx(k, "Enabled")
                    if val == 0:
                        return True
            except OSError:
                pass

    except Exception:
        pass
    return False


def _query_registry_dnd_active() -> Optional[bool]:
    """Best-effort Do Not Disturb / Focus Assist state read out of the registry.

    Split out from _query_registry_notifications_disabled() because the two
    have very different standing. The keys above are the documented Windows
    notification switches: reading one means the user turned notifications off.
    The keys here are undocumented shell implementation detail, and their
    meaning drifts between builds — so this is only ever consulted as a
    *fallback*, when WNF (the state the shell itself updates when the active
    quiet-hours profile changes) could not answer at all. Returns None for
    "don't know", which is not the same as False and must not collapse into it.
    """
    if sys.platform != "win32":
        return None
    try:
        import winreg

        # Focus Assist Mode: 0 = off, 1 = priority only, 2 = alarms only.
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\FocusAssist") as k:
                val, _ = winreg.QueryValueEx(k, "FocusAssistMode")
                if val in (1, 2):
                    return True
                if val == 0:
                    return False
        except OSError:
            pass

        # QuietHours profile/enabled.
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Notifications\QuietHours") as k:
                try:
                    profile, _ = winreg.QueryValueEx(k, "Profile")
                    if profile > 0:
                        return True
                except OSError:
                    pass
                try:
                    enabled, _ = winreg.QueryValueEx(k, "Enabled")
                    if enabled == 1:
                        return True
                except OSError:
                    pass
        except OSError:
            pass

        # Windows 11 DND profile in CloudStore.
        cloud_paths = (
            r"Software\Microsoft\Windows\CurrentVersion\CloudStore\Store\Cache\DefaultAccount"
            r"\$$windows.data.notifications.quiethourssettings\Current",
            r"Software\Microsoft\Windows\CurrentVersion\CloudStore\Store\DefaultAccount\Current"
            r"\default$windows.data.notifications.quiethourssettings"
            r"\windows.data.notifications.quiethourssettings",
        )
        for cloud_path in cloud_paths:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, cloud_path) as k:
                    data, _ = winreg.QueryValueEx(k, "Data")
            except OSError:
                continue
            cloud_state = _parse_cloudstore_quiet_hours(data)
            if cloud_state is not None:
                return cloud_state
    except Exception:
        pass
    return None


def _query_winrt_notification_policy() -> Optional[bool]:
    """Return whether WinRT says WinZapp toast notifications are blocked.

    ``ToastNotifier.setting`` is the supported Windows API for app/user/system
    notification blocking. 0 means Enabled; any non-zero NotificationSetting
    means notifications are disabled for the app, the user, by policy, or by
    the manifest.

    Do Not Disturb itself is intentionally detected through WNF above because
    DND can still allow priority applications, so ToastNotifier.setting may
    remain Enabled while the shell suppresses ordinary notification banners.
    """
    if sys.platform != "win32":
        return None
    for module_name in (
        "winrt.windows.ui.notifications",
        "winsdk.windows.ui.notifications",
    ):
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            logging.debug(
                "[quiet_hours] WinRT notifications module unavailable via %s: %s",
                module_name,
                exc,
            )
            continue

        manager = getattr(module, "ToastNotificationManager", None)
        if manager is None:
            continue
        try:
            notifier = manager.create_toast_notifier("WinZapp")
            if notifier is None:
                continue
            setting = notifier.setting
            setting_value = int(getattr(setting, "value", setting))
            # NotificationSetting: 0 Enabled, 1 DisabledForApplication,
            # 2 DisabledForUser, 3 DisabledByGroupPolicy, 4 DisabledByManifest.
            #
            # Only 2 and 3 are read as suppression. They are user- and
            # machine-wide, so they hold whatever AUMID this call resolved —
            # which matters, because the AUMID here is a literal while
            # NotificationManager._setup_toaster() picks its own from a list of
            # candidates and may well have registered a different one. 1 would
            # be an answer about an app that isn't necessarily us, and 4
            # (DisabledByManifest) is meaningless for an unpackaged app like
            # WinZapp and has been seen reported spuriously. Treating either as
            # "suppress" silences the app permanently for no visible reason.
            if setting_value in (2, 3):
                return True
            return False
        except Exception as exc:
            logging.debug(
                "[quiet_hours] WinRT notification setting query failed via %s: %s",
                module_name,
                exc,
            )
    return None


def _compute_quiet_hours_active() -> bool:
    """Uncached probe sequence behind is_quiet_hours_active(). See that.

    Every "suppressed" branch logs at INFO, not DEBUG, and that level is
    deliberate: log.log runs at INFO, so a decision taken here is otherwise
    invisible in the only artefact a user sends when reporting a problem. What
    this gate silences is a background notification's sound AND its spoken
    announcement — for a user on a screen reader, the whole notification — and
    two of the probes below read undocumented Windows state (the WNF state name
    and its value semantics are reverse-engineered). A misreading therefore
    presents as "WinZapp stopped telling me messages arrive", with no visible
    cause. One line in the log is the difference between diagnosing that in
    minutes and not diagnosing it at all."""
    # 1. Documented Windows notification switches (global, policy, per-app).
    if _query_registry_notifications_disabled():
        logging.info("[quiet_hours] Suppressed via registry check (notifications disabled)")
        return True

    # 2. WNF: the state the shell itself updates when the active quiet-hours
    #    profile changes. When it answers it is authoritative for DND, and the
    #    undocumented registry heuristics below are not consulted at all — a
    #    stale FocusAssistMode value or a CloudStore blob must never override
    #    the live state saying Do Not Disturb is off.
    wnf_state = _query_wnf_quiet_hours()
    if wnf_state is True:
        logging.info("[quiet_hours] Suppressed via WNF state (Focus Assist / DND active)")
        return True
    if wnf_state is None and _query_registry_dnd_active() is True:
        logging.info("[quiet_hours] Suppressed via registry DND fallback (WNF unavailable)")
        return True

    # 3. WinRT ToastNotifier setting (user-wide / group policy only).
    if _query_winrt_notification_policy() is True:
        logging.info("[quiet_hours] Suppressed via WinRT ToastNotifier setting")
        return True

    # 4. Legacy Win32 state: fullscreen game, presentation mode, quiet time.
    state = _query_notification_state()
    if state is not None and should_suppress_notification_sound(state):
        logging.info("[quiet_hours] Suppressed via SHQueryUserNotificationState: %d", state)
        return True

    return False


# The probe sequence above costs up to seven registry opens plus a WNF query,
# a WinRT/COM round-trip and a shell call. It runs per notification, and
# _play_sound() reaches it from the wx main thread — a burst of messages was
# enough to show up as UI stutter. Windows' own DND state does not change on a
# sub-second scale, so one reading is reused for a short window; the ceiling on
# how stale an answer can be is _CACHE_TTL_SECONDS, which is far below the time
# it takes a user to toggle Do Not Disturb and expect the next message to obey.
_CACHE_TTL_SECONDS = 1.0
_cache_lock = threading.Lock()
_cached_answer: Optional[bool] = None
_cached_at: float = 0.0


def invalidate_cache() -> None:
    """Drop the memoized reading. For tests, and for any caller that knows the
    Windows notification state just changed."""
    global _cached_answer, _cached_at
    with _cache_lock:
        _cached_answer = None
        _cached_at = 0.0


def is_quiet_hours_active() -> bool:
    """True if Windows is currently in a state where the *background*
    notification (its sound and its spoken announcement) should be suppressed:
    Do Not Disturb / "Não Incomodar", Focus Assist, notifications toggled off
    in Windows Settings, fullscreen games, presentation mode.

    Fails open (False) off-Windows or on any API failure — an API failure is
    a reason to fall back to the pre-existing behavior (always play), not to
    go silent for a reason nobody can see. Memoized for _CACHE_TTL_SECONDS.
    """
    global _cached_answer, _cached_at
    now = time.monotonic()
    with _cache_lock:
        if _cached_answer is not None and (now - _cached_at) < _CACHE_TTL_SECONDS:
            return _cached_answer
    answer = _compute_quiet_hours_active()
    with _cache_lock:
        _cached_answer = answer
        _cached_at = time.monotonic()
    return answer


