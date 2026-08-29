"""Tests for toast notification behaviour.

Covers the "pile of notifications appears at once" bug: Windows queues toasts it
cannot display (screen off, Focus Assist) and pops them in sequence on wake, so
WinZapp must keep at most one notification alive and collapse any backlog of its
own instead of handing Windows a burst of banners.

Also covers the "sound plays well before the banner appears" complaint:
_dispatch() used to run a blocking WinRT/COM remove_toast_group() call in
front of EVERY show_toast(), and queued the sound only after show_toast()
returned — both added latency between "message arrived" and the sound/
banner actually happening that had nothing to do with Windows' own
(uncontrollable) toast-rendering pipeline.

NotificationManager starts a worker thread and touches the registry in
__init__, so the logic under test is exercised against a stub carrying only the
attributes those methods use.
"""

import queue
import time

import pytest
import wx

from core.notification_manager import NotificationManager


class _FakeToaster:
    """Records the toast calls a real WinRT toaster would receive."""

    def __init__(self, fail_group_removal=False):
        self.removed_groups = []
        self.removed_toasts = []
        self.shown_toasts = []
        self.fail_group_removal = fail_group_removal

    def remove_toast_group(self, group):
        if self.fail_group_removal:
            raise RuntimeError("not supported by this toaster")
        self.removed_groups.append(group)

    def remove_toast(self, toast):
        self.removed_toasts.append(toast)

    def show_toast(self, toast):
        self.shown_toasts.append(toast)


class _FakeI18n:
    def get_language(self):
        pass

    def t(self, key):
        if key == "unread_sep_plural":
            return "{count} [unread_sep_plural]"
        return f"[{key}]"


class _FakeMessageQueue:
    def __init__(self):
        self.enqueued = []

    def enqueue(self, pm):
        self.enqueued.append(pm)


class _FakeMainWindow:
    def __init__(self, chats=None):
        self.chats = chats if chats is not None else {}
        self.message_queue = _FakeMessageQueue()


def _chat(unread=0, records=1):
    return {
        "unreadCount": unread,
        "messages": {"messages": {"records": [{"key": {"id": f"M{i}"}}
                                              for i in range(records)]}},
    }


class _Stub:
    """Minimal stand-in for NotificationManager."""

    TOAST_TAG = NotificationManager.TOAST_TAG
    TOAST_GRP = NotificationManager.TOAST_GRP
    _TOAST_LIKELY_GONE_SECONDS = NotificationManager._TOAST_LIKELY_GONE_SECONDS
    _TOAST_REACTIONS = NotificationManager._TOAST_REACTIONS
    # 0 by default so every existing test below (none of which care about the
    # near-simultaneous-arrival settle window) stays instant; TestCoalesce
    # SettleWindow overrides this per-instance to actually exercise it.
    _COALESCE_SETTLE_SECONDS = 0

    _coalesce_pending     = NotificationManager._coalesce_pending
    _clear_active_toasts  = NotificationManager._clear_active_toasts
    _dispatch             = NotificationManager._dispatch
    _do_reply             = NotificationManager._do_reply

    def _play_sound(self, remote_jid=""):
        pass

    def __init__(self, toaster=None, interactable=False, chats=None):
        self._queue = queue.Queue()
        self._toaster = toaster
        self._last_toast = None
        self._last_shown_at = None
        self._interactable = interactable
        self.i18n = _FakeI18n()
        self.main_window = _FakeMainWindow(chats)


class TestCoalescePending:
    def test_empty_queue_keeps_the_current_item(self):
        mgr = _Stub()
        item, dropped = mgr._coalesce_pending(("Ana", "oi", "j@g.us"))
        assert item == ("Ana", "oi", "j@g.us")
        assert dropped == 0

    def test_burst_collapses_to_the_newest(self):
        """The exact screen-off scenario: 30 messages must not become 30 banners."""
        mgr = _Stub()
        for i in range(29):
            mgr._queue.put((f"Sender {i}", f"body {i}", "j@g.us"))
        mgr._queue.put(("Newest", "last body", "j@g.us"))

        item, dropped = mgr._coalesce_pending(("Oldest", "first body", "j@g.us"))

        assert item == ("Newest", "last body", "j@g.us")
        assert dropped == 30
        assert mgr._queue.empty()

    def test_shutdown_signal_wins_over_pending_items(self):
        mgr = _Stub()
        mgr._queue.put(("Ana", "oi", "j@g.us"))
        mgr._queue.put(None)
        item, _ = mgr._coalesce_pending(("Bruno", "ola", "j@g.us"))
        assert item is None

    def test_items_after_a_shutdown_signal_are_left_alone(self):
        mgr = _Stub()
        mgr._queue.put(None)
        mgr._queue.put(("Ana", "oi", "j@g.us"))
        item, _ = mgr._coalesce_pending(("Bruno", "ola", "j@g.us"))
        assert item is None
        assert not mgr._queue.empty()


class TestCoalesceSettleWindow:
    """Reported live: two messages arriving within the same instant (e.g. two
    chats each getting a message in the same live burst) raced past the old
    _coalesce_pending(), which only ever looked at whatever was ALREADY
    sitting in the queue at that exact moment — a window measured in
    microseconds. The first one nearly always won that race, started its
    (occasionally slow, several-second) WinRT round-trip, and the second sat
    queued behind it for the whole duration instead of super­seding it. A
    short, bounded settle window closes that race: _coalesce_pending() now
    also waits briefly for an imminent arrival before committing.
    """

    def test_an_item_arriving_during_the_settle_window_supersedes_the_first(self):
        mgr = _Stub()
        mgr._COALESCE_SETTLE_SECONDS = 0.2

        def _deliver_second_soon():
            time.sleep(0.05)
            mgr._queue.put(("Bruno", "quase junto", "j2@g.us"))

        import threading
        threading.Thread(target=_deliver_second_soon, daemon=True).start()

        item, dropped = mgr._coalesce_pending(("Ana", "primeira", "j1@g.us"))

        assert item == ("Bruno", "quase junto", "j2@g.us")
        assert dropped == 1

    def test_nothing_arriving_returns_the_original_item_after_the_window(self):
        mgr = _Stub()
        mgr._COALESCE_SETTLE_SECONDS = 0.05

        started = time.monotonic()
        item, dropped = mgr._coalesce_pending(("Ana", "sozinha", "j@g.us"))
        elapsed = time.monotonic() - started

        assert item == ("Ana", "sozinha", "j@g.us")
        assert dropped == 0
        assert elapsed >= 0.05

    def test_a_shutdown_signal_during_the_settle_window_is_honoured(self):
        mgr = _Stub()
        mgr._COALESCE_SETTLE_SECONDS = 0.2

        def _shutdown_soon():
            time.sleep(0.05)
            mgr._queue.put(None)

        import threading
        threading.Thread(target=_shutdown_soon, daemon=True).start()

        item, _ = mgr._coalesce_pending(("Ana", "oi", "j@g.us"))
        assert item is None

    def test_total_wait_is_bounded_to_one_window_even_with_multiple_arrivals(self):
        """Two messages several settle-windows apart in wall-clock terms must
        not each restart the clock — otherwise a steady trickle could delay
        the toast indefinitely."""
        mgr = _Stub()
        mgr._COALESCE_SETTLE_SECONDS = 0.30

        def _deliver_two():
            time.sleep(0.04)
            mgr._queue.put(("Bruno", "segunda", "j2@g.us"))
            time.sleep(0.04)
            mgr._queue.put(("Carla", "terceira", "j3@g.us"))

        import threading
        threading.Thread(target=_deliver_two, daemon=True).start()

        started = time.monotonic()
        item, dropped = mgr._coalesce_pending(("Ana", "primeira", "j1@g.us"))
        elapsed = time.monotonic() - started

        assert item == ("Carla", "terceira", "j3@g.us")
        assert dropped == 2
        # Bounded by the single deadline set at call time, not extended by
        # each arrival — well under 2x the settle window.
        assert elapsed < 0.30 * 2


class TestClearActiveToasts:
    def test_removes_the_whole_group(self):
        toaster = _FakeToaster()
        mgr = _Stub(toaster)
        mgr._clear_active_toasts()
        assert toaster.removed_groups == [NotificationManager.TOAST_GRP]

    def test_falls_back_to_removing_the_last_toast(self):
        """Basic (non-interactable) toasters may not support group removal."""
        toaster = _FakeToaster(fail_group_removal=True)
        mgr = _Stub(toaster)
        sentinel = object()
        mgr._last_toast = sentinel
        mgr._clear_active_toasts()
        assert toaster.removed_toasts == [sentinel]

    def test_no_toaster_is_a_no_op(self):
        mgr = _Stub(None)
        mgr._clear_active_toasts()  # must not raise

    def test_nothing_to_remove_is_a_no_op(self):
        toaster = _FakeToaster(fail_group_removal=True)
        mgr = _Stub(toaster)
        mgr._clear_active_toasts()  # no _last_toast yet
        assert toaster.removed_toasts == []

    def test_removal_failure_never_propagates(self):
        """A failed cleanup must never stop the new notification from showing."""

        class _Broken:
            def remove_toast_group(self, group):
                raise RuntimeError("winrt unavailable")

            def remove_toast(self, toast):
                raise RuntimeError("winrt unavailable")

        mgr = _Stub(_Broken())
        mgr._last_toast = object()
        mgr._clear_active_toasts()  # must not raise


class TestToastIdentity:
    def test_tag_and_group_are_stable_constants(self):
        """A per-notification tag is what let banners stack up; the whole fix
        depends on every toast reusing one identity."""
        assert isinstance(NotificationManager.TOAST_TAG, str)
        assert isinstance(NotificationManager.TOAST_GRP, str)
        assert NotificationManager.TOAST_TAG
        assert NotificationManager.TOAST_GRP


class TestSetupToasterAumid:
    """_setup_toaster() builds the AUMID candidate list show_toast() calls
    live under. Dev mode used to skip straight to sys.executable (the
    venv's own python.exe — an AUMID shared by every unrelated script run
    from that same venv, with no registered DisplayName/icon of its own)
    instead of trying the registered "WinZapp" identity first like a frozen
    build does. Windows can silently decline to show a banner for an AUMID
    it has no real registered app identity for — reported live running
    from source: the custom sound played, but the toast never appeared on
    screen at all, with no exception anywhere to explain why."""

    class _ToasterStub:
        _setup_toaster  = NotificationManager._setup_toaster
        _outer_exe_path = staticmethod(NotificationManager._outer_exe_path)
        APP_ID          = NotificationManager.APP_ID

        def __init__(self):
            self._toaster      = None
            self._interactable = False

        def _register_aumid_registry(self):
            pass  # touches the real Windows registry — irrelevant here

    def test_dev_mode_tries_the_registered_app_id_first(self, monkeypatch):
        monkeypatch.setattr("core.notification_manager._is_frozen", lambda: False)
        attempts = []

        class FakeInteractable:
            def __init__(self, app_id, notifierAUMID=None):
                attempts.append(app_id)

        monkeypatch.setattr("windows_toasts.InteractableWindowsToaster", FakeInteractable)

        stub = self._ToasterStub()
        stub._setup_toaster()

        assert attempts == ["WinZapp"]
        assert isinstance(stub._toaster, FakeInteractable)
        assert stub._interactable is True

    def test_dev_mode_falls_back_to_the_interpreter_path_if_app_id_fails(self, monkeypatch):
        monkeypatch.setattr("core.notification_manager._is_frozen", lambda: False)
        monkeypatch.setattr("core.notification_manager.sys.executable", "C:\\venv\\Scripts\\python.exe")
        attempts = []

        class AlwaysFails:
            def __init__(self, app_id, notifierAUMID=None):
                attempts.append(app_id)
                raise RuntimeError("simulated: no registered app identity for this AUMID")

        monkeypatch.setattr("windows_toasts.InteractableWindowsToaster", AlwaysFails)
        monkeypatch.setattr("windows_toasts.WindowsToaster", AlwaysFails)

        stub = self._ToasterStub()
        stub._setup_toaster()

        assert attempts == [
            "WinZapp", "WinZapp",
            "C:\\venv\\Scripts\\python.exe", "C:\\venv\\Scripts\\python.exe",
        ]
        assert stub._toaster is None

    def test_frozen_mode_also_tries_app_id_before_the_outer_exe(self, monkeypatch):
        monkeypatch.setattr("core.notification_manager._is_frozen", lambda: True)
        attempts = []

        class FakeInteractable:
            def __init__(self, app_id, notifierAUMID=None):
                attempts.append(app_id)

        monkeypatch.setattr("windows_toasts.InteractableWindowsToaster", FakeInteractable)

        stub = self._ToasterStub()
        stub._outer_exe_path = lambda: "C:\\Program Files\\WinZapp\\WinZapp.exe"
        stub._setup_toaster()

        assert attempts == ["WinZapp"]


class TestDispatchLatency:
    """_dispatch() is called on the worker thread for every notification.
    These cover the two latency fixes: skipping the blocking clear when
    nothing is likely still showing, and firing the sound before (not
    after) the WinRT/COM work."""

    def test_sound_is_queued_before_the_toast_is_shown(self, monkeypatch):
        """The whole point of the reorder: by the time show_toast() has even
        been called, the sound must already be queued."""
        calls = []
        monkeypatch.setattr("core.quiet_hours.is_quiet_hours_active", lambda: False)
        monkeypatch.setattr(wx, "CallAfter", lambda fn, *a, **kw: calls.append("sound"))
        toaster = _FakeToaster()
        real_show = toaster.show_toast
        toaster.show_toast = lambda t: (calls.append("toast"), real_show(t))[1]
        mgr = _Stub(toaster)

        mgr._dispatch("title", "body", "j@g.us")

        assert calls == ["sound", "toast"]

    def test_first_ever_notification_still_clears(self, monkeypatch):
        """_last_shown_at is None (nothing shown yet this session) — must
        still attempt the clear, same as before this change."""
        monkeypatch.setattr(wx, "CallAfter", lambda fn, *a, **kw: None)
        toaster = _FakeToaster()
        mgr = _Stub(toaster)
        assert mgr._last_shown_at is None

        mgr._dispatch("title", "body", "j@g.us")

        assert toaster.removed_groups == [NotificationManager.TOAST_GRP]
        assert mgr._last_shown_at is not None  # recorded after showing

    def test_recent_toast_still_clears(self, monkeypatch):
        """A burst arriving within the "likely still visible" window must
        keep clearing — this is the actual case the clear exists to fix."""
        monkeypatch.setattr(wx, "CallAfter", lambda fn, *a, **kw: None)
        toaster = _FakeToaster()
        mgr = _Stub(toaster)
        mgr._last_shown_at = time.monotonic()  # "just shown"

        mgr._dispatch("title", "body", "j@g.us")

        assert toaster.removed_groups == [NotificationManager.TOAST_GRP]

    def test_stale_toast_skips_the_clear(self, monkeypatch):
        """Once the previous toast has almost certainly auto-dismissed,
        skip the blocking clear — this is the actual latency fix."""
        monkeypatch.setattr(wx, "CallAfter", lambda fn, *a, **kw: None)
        toaster = _FakeToaster()
        mgr = _Stub(toaster)
        mgr._last_shown_at = time.monotonic() - (mgr._TOAST_LIKELY_GONE_SECONDS + 1)

        mgr._dispatch("title", "body", "j@g.us")

        assert toaster.removed_groups == []
        assert len(toaster.shown_toasts) == 1  # the new toast is still shown

    def test_last_shown_at_updates_after_each_dispatch(self, monkeypatch):
        monkeypatch.setattr(wx, "CallAfter", lambda fn, *a, **kw: None)
        mgr = _Stub(_FakeToaster())
        before = time.monotonic()

        mgr._dispatch("title", "body", "j@g.us")

        assert mgr._last_shown_at >= before


class TestUnreadSuffix:
    """The unread line is appended at display time, from the live chat map.

    It must land as its OWN text_fields entry (a real second line in the
    toast), not concatenated with "\\n" onto the body — Windows' toast
    renderer does not honour an embedded newline inside a single <text>
    element, so the "\\n"-joined version used to visually run the "✉️ N não
    lidas" line straight into the message body instead of showing it below.
    """

    def _body_of(self, toaster):
        assert len(toaster.shown_toasts) == 1
        return toaster.shown_toasts[0].text_fields[1]

    def _suffix_of(self, toaster):
        fields = toaster.shown_toasts[0].text_fields
        return fields[2] if len(fields) > 2 else ""

    def test_suffix_is_appended_from_the_live_chat(self, monkeypatch):
        monkeypatch.setattr(wx, "CallAfter", lambda fn, *a, **kw: None)
        toaster = _FakeToaster()
        mgr = _Stub(toaster, chats={"j@g.us": _chat(unread=7, records=20)})

        mgr._dispatch("title", "body", "j@g.us")

        assert self._body_of(toaster) == "body"
        assert "7" in self._suffix_of(toaster)

    def test_singular_and_plural_take_different_strings(self, monkeypatch):
        monkeypatch.setattr(wx, "CallAfter", lambda fn, *a, **kw: None)
        toaster = _FakeToaster()
        mgr = _Stub(toaster, chats={"j@g.us": _chat(unread=1, records=5)})

        mgr._dispatch("title", "body", "j@g.us")

        assert "unread_sep_singular" in self._suffix_of(toaster)

    def test_a_chat_we_do_not_know_yet_still_notifies(self, monkeypatch):
        monkeypatch.setattr(wx, "CallAfter", lambda fn, *a, **kw: None)
        toaster = _FakeToaster()
        mgr = _Stub(toaster, chats={})

        mgr._dispatch("title", "body", "j@g.us")

        assert self._body_of(toaster) == "body"
        assert self._suffix_of(toaster) == ""

    def test_a_count_over_an_empty_chat_is_suppressed(self, monkeypatch):
        monkeypatch.setattr(wx, "CallAfter", lambda fn, *a, **kw: None)
        toaster = _FakeToaster()
        mgr = _Stub(toaster, chats={"j@g.us": _chat(unread=9, records=0)})

        mgr._dispatch("title", "body", "j@g.us")

        assert "9" not in self._suffix_of(toaster)

    def test_a_freshly_discovered_chats_low_count_is_suppressed(self, monkeypatch):
        """on_new_message() creates a brand-new chat entry with unreadCount=0
        and counts up from there live — that assumed 0 can be badly wrong if
        the phone already had a real backlog (e.g. 230 unread) this session
        just hasn't synced yet. A toast announcing "1 unread"/"2 unread" for
        that chat is actively misleading, not just imprecise, until a real
        chat-list sync backs the number (clears _unread_count_unsynced — see
        get_remote_chats())."""
        monkeypatch.setattr(wx, "CallAfter", lambda fn, *a, **kw: None)
        toaster = _FakeToaster()
        chat = _chat(unread=2, records=2)
        chat["_unread_count_unsynced"] = True
        mgr = _Stub(toaster, chats={"j@g.us": chat})

        mgr._dispatch("title", "body", "j@g.us")

        assert self._body_of(toaster) == "body"
        assert self._suffix_of(toaster) == ""

    def test_the_count_reappears_once_a_real_sync_clears_the_flag(self, monkeypatch):
        monkeypatch.setattr(wx, "CallAfter", lambda fn, *a, **kw: None)
        toaster = _FakeToaster()
        chat = _chat(unread=230, records=200)
        mgr = _Stub(toaster, chats={"j@g.us": chat})

        mgr._dispatch("title", "body", "j@g.us")

        assert "230" in self._suffix_of(toaster)


class TestInteractableAccessibility:
    """The reply box had no accessible label (empty placeholder, the field
    NVDA actually reads) and no real "Enviar" button (no explicit
    ToastButton at all, so nothing for Tab to land on). Also covers the new
    "Reagir" quick-action, added beside it when a msg_key is available."""

    def test_reply_box_placeholder_is_set_and_caption_is_not(self, monkeypatch):
        """placeholder is what Windows exposes as the field's accessible
        Name — that's the actual NVDA fix. caption renders as a separate
        visible line above the field; setting it to the same text doubled
        up "Responder..." on screen once placeholder was no longer empty."""
        monkeypatch.setattr(wx, "CallAfter", lambda fn, *a, **kw: None)
        toaster = _FakeToaster()
        mgr = _Stub(toaster, interactable=True)

        mgr._dispatch("title", "body", "j@g.us")

        toast = toaster.shown_toasts[0]
        reply_inputs = [i for i in toast.inputs if i.input_id == "reply_box"]
        assert len(reply_inputs) == 1
        assert reply_inputs[0].placeholder
        assert not reply_inputs[0].caption

    def test_reply_box_has_a_related_send_button(self, monkeypatch):
        monkeypatch.setattr(wx, "CallAfter", lambda fn, *a, **kw: None)
        toaster = _FakeToaster()
        mgr = _Stub(toaster, interactable=True)

        mgr._dispatch("title", "body", "j@g.us")

        toast = toaster.shown_toasts[0]
        send_buttons = [a for a in toast.actions if a.arguments == "do_reply"]
        assert len(send_buttons) == 1
        reply_box = next(i for i in toast.inputs if i.input_id == "reply_box")
        assert send_buttons[0].relatedInput is reply_box

    def test_no_react_buttons_without_a_message_key(self, monkeypatch):
        """_maybe_notify_reaction() (reacting to your own message) has no
        message to quick-react to — msg_key stays None there."""
        monkeypatch.setattr(wx, "CallAfter", lambda fn, *a, **kw: None)
        toaster = _FakeToaster()
        mgr = _Stub(toaster, interactable=True)

        mgr._dispatch("title", "body", "j@g.us", None)

        toast = toaster.shown_toasts[0]
        assert not any(a.arguments.startswith("do_react:") for a in toast.actions)

    def test_react_buttons_present_with_a_message_key(self, monkeypatch):
        """One plain ToastButton per emoji — not a second input control
        (a selection dropdown) alongside the reply text box. Combining two
        different input types in one toast was the prime suspect for a live
        regression (notifications losing all accessible content and playing
        Windows' default sound instead of the app's), so reactions are kept
        to independent buttons, same shape as the reply button."""
        monkeypatch.setattr(wx, "CallAfter", lambda fn, *a, **kw: None)
        toaster = _FakeToaster()
        mgr = _Stub(toaster, interactable=True)

        mgr._dispatch("title", "body", "j@g.us", {"id": "ABC123", "fromMe": False})

        toast = toaster.shown_toasts[0]
        assert not any(i.input_id == "react_box" for i in toast.inputs)
        react_buttons = [a for a in toast.actions if a.arguments.startswith("do_react:")]
        assert len(react_buttons) == len(NotificationManager._TOAST_REACTIONS)
        assert {a.arguments.split(":", 1)[1] for a in react_buttons} == set(NotificationManager._TOAST_REACTIONS)

    def test_total_action_count_stays_within_the_toast_budget(self, monkeypatch):
        """Windows toasts realistically render at most ~5 actions (buttons +
        inputs combined) reliably — 1 reply input + 1 send button + the
        reaction buttons must stay comfortably under that."""
        monkeypatch.setattr(wx, "CallAfter", lambda fn, *a, **kw: None)
        toaster = _FakeToaster()
        mgr = _Stub(toaster, interactable=True)

        mgr._dispatch("title", "body", "j@g.us", {"id": "ABC123"})

        toast = toaster.shown_toasts[0]
        assert len(toast.inputs) + len(toast.actions) <= 5

    def test_activating_with_do_react_arguments_sends_the_chosen_emoji(self, monkeypatch):
        monkeypatch.setattr(wx, "CallAfter", lambda fn, *a, **kw: fn(*a, **kw))
        toaster = _FakeToaster()
        mgr = _Stub(toaster, interactable=True)
        reacted = []
        mgr._do_react = lambda jid, msg_key, emoji: reacted.append((jid, msg_key, emoji))

        mgr._dispatch("title", "body", "j@g.us", {"id": "ABC123"})

        toast = toaster.shown_toasts[0]

        class _Event:
            arguments = "do_react:👍"
            inputs = {}

        toast.on_activated(_Event())
        assert reacted == [("j@g.us", {"id": "ABC123"}, "👍")]

    def test_activating_with_reply_text_quotes_the_triggering_message(self, monkeypatch):
        """A reply from the toast used to always send as a brand new
        message — msg_key never made it into the enqueued PendingMessage's
        "quoted" field, so WhatsApp had nothing to attach the reply to."""
        monkeypatch.setattr(wx, "CallAfter", lambda fn, *a, **kw: fn(*a, **kw))
        toaster = _FakeToaster()
        mgr = _Stub(toaster, interactable=True)

        mgr._dispatch("title", "body", "j@g.us", {"id": "ABC123", "fromMe": False})

        toast = toaster.shown_toasts[0]

        class _Event:
            arguments = "do_reply"
            inputs = {"reply_box": "valeu!"}

        toast.on_activated(_Event())

        [pm] = mgr.main_window.message_queue.enqueued
        assert pm.text == "valeu!"
        assert pm.quoted == {"key": {"id": "ABC123", "fromMe": False}}


class TestDoReply:
    def test_quotes_the_message_key_when_given(self):
        mgr = _Stub()
        mgr._do_reply("j@g.us", "oi", {"id": "M1", "fromMe": False})

        [pm] = mgr.main_window.message_queue.enqueued
        assert pm.jid == "j@g.us"
        assert pm.text == "oi"
        assert pm.quoted == {"key": {"id": "M1", "fromMe": False}}

    def test_sends_plain_when_no_message_key(self):
        """_maybe_notify_reaction() notifications carry no msg_key — a
        reply from one of those has nothing to quote."""
        mgr = _Stub()
        mgr._do_reply("j@g.us", "oi")

        [pm] = mgr.main_window.message_queue.enqueued
        assert pm.quoted is None

    def test_empty_text_is_a_no_op(self):
        mgr = _Stub()
        mgr._do_reply("j@g.us", "", {"id": "M1"})
        assert mgr.main_window.message_queue.enqueued == []
