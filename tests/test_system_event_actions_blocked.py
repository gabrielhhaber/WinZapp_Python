"""Message actions must refuse WhatsApp's own system events.

Follow-up to the Alt+R fix: replying to a group notice ("Fulano entrou no
grupo", promotes, name/settings changes) was rejected server-side, so
_on_menu_reply started refusing them up front. The same class of problem
applies to every other action offered on a focused message — measured
against a live WPPConnect Store, a system event's serialized id resolves
only sometimes, so forwarding/pinning/reacting either fails outright or
acts on nothing. Starring now delegates to a verified WhatsApp operation,
which a system event must never reach either.

The guard therefore lives in the _on_menu_* handlers, NOT in the context
menu: the accelerators (Ctrl+Shift+E/R/O/P) call those handlers directly
and would sail straight past a menu-only gate — the same structural hole
that once let Ctrl+Shift+S open a Save As dialog for a text message.

Deleting is deliberately the exception: "for me" stays allowed (it just
hides the row locally), only "for everyone" is withdrawn.

ConversationsPanel is a wx.Panel and can't be instantiated without a
running wx.App, so the real unbound methods are bound onto a plain stub —
same approach as tests/test_forward_multiple_targets.py.
"""

import inspect

import pytest

from ui.conversations import ConversationsPanel


SYSTEM_EVENT = {
    "key": {"id": "SYSEVT1", "remoteJid": "group@g.us", "fromMe": False},
    "messageType": "groupNotification",
    "message": {},
}

NORMAL_MESSAGE = {
    "key": {"id": "MSG1", "remoteJid": "group@g.us", "fromMe": False},
    "messageType": "conversation",
    "message": {"conversation": "oi"},
}


class _FakeI18n:
    def t(self, key):
        return key


class _FakeMainWindow:
    def __init__(self):
        self.i18n = _FakeI18n()
        self.announced = []
        self.saves = 0

    def output(self, text):
        self.announced.append(text)

    def _schedule_save(self):
        self.saves += 1


class _Stub:
    # staticmethod on the real class; attribute access unwraps it, so it has
    # to be re-wrapped or the stub would pass itself in as `msg`.
    _is_system_event = staticmethod(ConversationsPanel._is_system_event)
    _reject_system_event_action = ConversationsPanel._reject_system_event_action
    _on_menu_star = ConversationsPanel._on_menu_star

    def __init__(self):
        self.main_window = _FakeMainWindow()
        self.conversation = {"remoteJid": "group@g.us"}
        self.populated = 0
        self.repainted = []
        self.star_requests = []

    def _sync_message_stars(self, jid, messages, desired):
        self.star_requests.append((jid, messages, desired))

    def _repaint_or_repopulate(self, msg_ids):
        self.repainted.append(sorted(i for i in msg_ids if i))

    def populate_messages(self, preserve_focus=False):
        self.populated += 1


class TestRejectHelper:
    def test_system_event_is_refused_and_announced(self):
        stub = _Stub()

        assert stub._reject_system_event_action(SYSTEM_EVENT) is True
        assert stub.main_window.announced == ["system_event_action_unavailable"]

    def test_normal_message_passes_through_silently(self):
        stub = _Stub()

        assert stub._reject_system_event_action(NORMAL_MESSAGE) is False
        assert stub.main_window.announced == []

    def test_non_dict_is_not_treated_as_a_system_event(self):
        stub = _Stub()

        assert stub._reject_system_event_action(None) is False
        assert stub.main_window.announced == []


class TestStarIsGuarded:
    """The guard runs before handing a normal message to remote verification."""

    def test_system_event_is_not_starred(self):
        stub = _Stub()
        msg = dict(SYSTEM_EVENT)

        stub._on_menu_star(msg)

        assert "starred" not in msg
        assert stub.main_window.saves == 0
        assert stub.populated == 0
        assert stub.repainted == []
        assert stub.star_requests == []
        assert stub.main_window.announced == ["system_event_action_unavailable"]

    @pytest.mark.parametrize("old_star", [False, True])
    def test_normal_message_delegates_without_optimistic_mutation(self, old_star):
        stub = _Stub()
        msg = dict(NORMAL_MESSAGE)
        msg["starred"] = old_star

        stub._on_menu_star(msg)

        assert stub.star_requests == [("group@g.us", [msg], not old_star)]
        assert msg["starred"] is old_star
        assert stub.main_window.saves == 0
        assert stub.repainted == []
        assert stub.main_window.announced == []


# The remaining handlers open wx dialogs / spawn threads before anything
# observable happens, so they're covered structurally: the guard must be
# the first statement to run, ahead of any dialog construction.
@pytest.mark.parametrize(
    "method_name",
    ["_on_menu_forward", "_on_menu_react", "_on_menu_pin_message", "_on_menu_star"],
)
class TestGuardRunsFirst:
    def test_handler_calls_the_shared_guard(self, method_name):
        src = inspect.getsource(getattr(ConversationsPanel, method_name))

        assert "_reject_system_event_action" in src, (
            f"{method_name} does not refuse system events — a group notice can "
            f"still reach it through its accelerator."
        )

    def test_guard_precedes_any_dialog_or_mutation(self, method_name):
        src = inspect.getsource(getattr(ConversationsPanel, method_name))
        lines = [ln.strip() for ln in src.splitlines()]
        guard_at = next(
            i for i, ln in enumerate(lines) if "_reject_system_event_action" in ln
        )
        # Nothing that touches the server, the message or the screen may run
        # before the guard has had its say.
        for i, ln in enumerate(lines[:guard_at]):
            assert not ln.startswith(("wx.", "msg[", "self.populate", "threading.")), (
                f"{method_name} acts on the message at line {i} before the "
                f"system-event guard at line {guard_at}: {ln!r}"
            )


class TestDeleteKeepsLocalButDropsForEveryone:
    """Deleting a group notice for yourself is legitimate; revoking it for
    everyone is not, on either the fromMe path or the group-admin path."""

    def test_system_event_never_offers_delete_for_everyone(self):
        src = inspect.getsource(ConversationsPanel._on_menu_delete_message)

        assert "is_system = self._is_system_event(msg)" in src
        assert "can_delete_for_all = from_me and not is_system" in src
        # The admin fallback below must not hand it back.
        assert "if not can_delete_for_all and not is_system" in src

    def test_local_delete_is_not_gated(self):
        src = inspect.getsource(ConversationsPanel._on_menu_delete_message)

        # An early "refuse the whole action" guard would take local delete
        # away too, which is the one thing that should keep working.
        assert "_reject_system_event_action" not in src
