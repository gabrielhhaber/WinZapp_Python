"""Tests for MainWindow.on_chat_unread_update() not resurrecting already-read
messages as unread.

Reported live: after reading some messages in a conversation, a single new
incoming message could push the unread badge to 3 or 4 instead of 1. Root
cause: WPPConnect's chats.update event reports an ABSOLUTE unread total, and
on_chat_unread_update() unconditionally overwrote the local unreadCount with
it. The one existing guard (_locally_read_at) only protects against a
chats.update whose chat["t"] is not newer than the local read-ack — once a
genuinely new message arrives after the read-ack, that guard is bypassed
entirely and the server's (possibly stale, still counting messages we already
read locally) total was accepted as-is.

The fix tracks _new_since_read[jid] — incremented once per real local
increment in on_new_message() — and clamps the server-reported count to that
local counter whenever the timestamp guard is bypassed, instead of trusting
the raw server value.

MainWindow is a wx.Frame and cannot be instantiated without a running wx.App,
so on_chat_unread_update() is exercised as a plain function against a small
stub — same approach as tests/test_failed_send_preview.py.
"""

from main import MainWindow


class _Stub:
    on_chat_unread_update = MainWindow.on_chat_unread_update
    _normalize_jid = staticmethod(MainWindow._normalize_jid)
    _remote_read_confirmed = staticmethod(MainWindow._remote_read_confirmed)
    # Bridges the @lid / phone identities of the incoming event before the
    # handler looks the chat up — see tests/test_chats_update_lid_resolution.py.
    _resolve_chat_for_event = MainWindow._resolve_chat_for_event
    # _locally_read_at is persisted to DB metadata now (it has to survive a
    # restart — see tests/test_locally_read_at_persisted.py); the real method
    # is a no-op without a `db` attribute, which this stub deliberately lacks.
    _persist_locally_read_at = MainWindow._persist_locally_read_at
    # Says whether _new_since_read counts from a local read of this chat or
    # merely from process start — see TestNeverReadChatKeepsTheServerTotal.
    _anchor_unread_to_local_read = MainWindow._anchor_unread_to_local_read
    _drop_unread_local_read_anchor = MainWindow._drop_unread_local_read_anchor
    _unread_anchored_to_local_read = MainWindow._unread_anchored_to_local_read

    def __init__(self, chat):
        self.chats = {"5511999999999@s.whatsapp.net": chat}
        self._initial_sync_running = False
        self._sync_completed = True
        self.conversations_panel = None
        self._locally_read_at = {}
        self._new_since_read = {}
        self._unread_read_anchors = set()
        self.saved = []
        self.set_chats_calls = 0

    def read_locally(self, jid):
        """What mark_conversation_as_read() does to the tracking state."""
        self._new_since_read[jid] = 0
        self._anchor_unread_to_local_read(jid)

    def open_conversation(self, jid):
        """Put the panel on a chat the way navigate_to_conversation() does.

        Opening always runs mark_conversation_as_read() on the way in
        (ui/conversations.py), and that read is what lets the open branch
        treat the panel showing a chat as proof it has been read. Tests that
        want the other state — open, but the read undone on screen — set the
        panel directly and leave the anchor off.
        """
        self.conversations_panel = _CP(jid)
        self.read_locally(jid)

    def _schedule_save(self, dirty_jid=None):
        self.saved.append(dirty_jid)

    def _schedule_set_chats(self):
        self.set_chats_calls += 1


JID = "5511999999999@s.whatsapp.net"


def _chat(t=1000):
    return {"unreadCount": 0, "t": t, "messages": {"messages": {"records": []}}}


class TestStaleServerCountAfterLocalRead:
    def test_one_new_message_does_not_resurrect_previously_read_ones(self):
        """User read the chat (unreadCount=0, read-ack at t=1000). One new
        message arrives; on_new_message() would have already bumped the
        local count to 1 and recorded it in _new_since_read. The server's
        chats.update for the same event still (staleness) reports 4 — it
        must be clamped down to the locally known 1, not accepted as-is."""
        stub = _Stub(_chat(t=2000))
        stub._locally_read_at[JID] = 1000
        stub._new_since_read[JID] = 1  # on_new_message() already counted this

        stub.on_chat_unread_update(JID, 4)

        assert stub.chats[JID]["unreadCount"] == 1

    def test_server_count_at_or_below_local_is_trusted_unchanged(self):
        stub = _Stub(_chat(t=2000))
        stub._locally_read_at[JID] = 1000
        stub._new_since_read[JID] = 3

        stub.on_chat_unread_update(JID, 2)

        assert stub.chats[JID]["unreadCount"] == 2

    def test_update_not_newer_than_the_read_ack_still_clears_to_zero(self):
        """Unrelated / stale chats.update whose own chat t is not after the
        read-ack must still be fully suppressed, same as before this fix."""
        stub = _Stub(_chat(t=1000))
        stub._locally_read_at[JID] = 1000

        stub.on_chat_unread_update(JID, 5)

        assert stub.chats[JID]["unreadCount"] == 0

    def test_no_local_tracking_falls_back_to_trusting_the_server(self):
        """If _new_since_read has no entry (e.g. the increment path wasn't
        hit for some other reason), there's no better local information —
        keep accepting the server's number rather than zeroing it out."""
        chat = _chat(t=2000)
        stub = _Stub(chat)
        stub._locally_read_at[JID] = 1000

        stub.on_chat_unread_update(JID, 3)

        assert stub.chats[JID]["unreadCount"] == 3

    def test_a_second_stale_update_after_the_read_ack_was_consumed_is_dropped(self):
        """Reported live: a chat read locally, then one genuinely new message
        arrives (on_new_message bumps unreadCount 0 -> 1 and _new_since_read to
        1). The first chats.update after that consumes and pops
        _locally_read_at (see the `elif read_at_t is not None` branch), which
        is correct and expected. But a second, later chats.update for the same
        chat can still arrive — now with no read_at_t left to protect it —
        reporting an even higher total (e.g. 2) that still counts a message
        already read locally days earlier. Before this guard, that inflated
        total was accepted verbatim, which is exactly what pulled an
        already-read message back into "unread" (first_unread_index() places
        the separator by counting backwards from unreadCount)."""
        stub = _Stub(_chat(t=2000))
        stub.read_locally(JID)  # the local read this whole branch stands on
        stub.chats[JID]["unreadCount"] = 1
        stub._new_since_read[JID] = 1
        # _locally_read_at has no entry for JID — already consumed/popped.

        stub.on_chat_unread_update(JID, 2, previous_unread=1)

        assert stub.chats[JID]["unreadCount"] == 1

class _CP:
    """Conversation panel stub.

    The open-conversation check moved from `_last_open_jid` to the panel's live
    `conversation` dict: the old field was never cleared on close, so a chat the
    user had already left kept counting as open forever and every chats-update
    for it was force-zeroed.
    """

    def __init__(self, jid=None):
        self.conversation = {"remoteJid": jid} if jid else None


class TestCurrentlyOpenConversation:
    def test_an_open_conversation_with_nothing_new_clears_to_zero(self):
        stub = _Stub(_chat(t=1000))
        stub.open_conversation(JID)

        stub.on_chat_unread_update(JID, 5)

        assert stub.chats[JID]["unreadCount"] == 0

    def test_an_open_conversation_keeps_what_arrived_while_hidden(self):
        """Deliberately not "open always means zero" any more: messages that
        landed while the window was minimized are tracked in _new_since_read
        and stay counted, clamped down to that locally-known number."""
        stub = _Stub(_chat(t=1000))
        stub.open_conversation(JID)
        stub._new_since_read[JID] = 2

        stub.on_chat_unread_update(JID, 5)

        assert stub.chats[JID]["unreadCount"] == 2

    def test_a_closed_panel_is_not_treated_as_the_open_chat(self):
        """The regression the move away from _last_open_jid fixed."""
        stub = _Stub(_chat(t=2000))
        stub.conversations_panel = _CP(None)
        stub._locally_read_at[JID] = 1000
        stub._new_since_read[JID] = 3

        stub.on_chat_unread_update(JID, 3)

        assert stub.chats[JID]["unreadCount"] == 3


class TestOpenConversationSurvivesSpuriousServerZeros:
    """Reported live: closing with Alt+F4 (which only hides to tray) and
    leaving the app minimized made every notification announce "✉️ 1 mensagem
    não lida" no matter how many had piled up — while reopening the window
    showed the conversation really did hold several.

    A conversation left open stays open with the window gone, so this is the
    _open_now branch, and WA-JS keeps firing chat.unread_count_changed with
    unreadCount=0 for 1:1 chats. Clamping with a plain min() against those
    zeros reset the count after every message, so the next arrival counted up
    from zero again and its toast always read "1".
    """

    def test_a_server_zero_does_not_wipe_what_arrived_while_hidden(self):
        chat = _chat(t=1000)
        # on_new_message() moves both of these together, once per arrival —
        # a chat at 0 with a nonzero _new_since_read is not a reachable state.
        chat["unreadCount"] = 4
        stub = _Stub(chat)
        stub.open_conversation(JID)
        stub._new_since_read[JID] = 4

        stub.on_chat_unread_update(JID, 0)

        assert stub.chats[JID]["unreadCount"] == 4

    def test_the_count_keeps_climbing_across_a_burst(self):
        """The symptom itself: five messages with a spurious zero after each
        must leave five unread, not one. Mirrors what on_new_message() does
        (increment both the chat and _new_since_read) between the events."""
        stub = _Stub(_chat(t=1000))
        stub.open_conversation(JID)
        seen_by_toasts = []

        for _ in range(5):
            chat = stub.chats[JID]
            chat["unreadCount"] = int(chat.get("unreadCount") or 0) + 1
            stub._new_since_read[JID] = stub._new_since_read.get(JID, 0) + 1
            # What the toast reads (NotificationManager._dispatch samples the
            # live chat right before showing the banner).
            seen_by_toasts.append(chat["unreadCount"])
            stub.on_chat_unread_update(JID, 0)

        assert seen_by_toasts == [1, 2, 3, 4, 5]
        assert stub.chats[JID]["unreadCount"] == 5

    def test_a_real_server_count_is_still_clamped_down(self):
        """The spurious-zero rescue must not become a licence to trust the
        server's absolute total, which can still be counting messages already
        read locally — that is the bug this whole function exists to prevent."""
        stub = _Stub(_chat(t=1000))
        stub.open_conversation(JID)
        stub._new_since_read[JID] = 2

        stub.on_chat_unread_update(JID, 7)

        assert stub.chats[JID]["unreadCount"] == 2

    def test_a_read_on_the_phone_does_clear_the_open_chat(self):
        """The other side of the same coin: a zero that really fell from a
        positive count is somebody reading the chat elsewhere, and must clear
        the badge — including the messages counted while hidden, which is
        exactly what the user just read on their phone."""
        chat = _chat(t=1000)
        chat["unreadCount"] = 4
        stub = _Stub(chat)
        stub.open_conversation(JID)
        stub._new_since_read[JID] = 4

        stub.on_chat_unread_update(JID, 0, previous_unread=4)

        assert stub.chats[JID]["unreadCount"] == 0
        # ...and the local counter goes with it, or the next arrival would be
        # clamped against a backlog that no longer exists.
        assert JID not in stub._new_since_read

    def test_a_read_on_the_phone_clears_a_chat_that_is_not_open(self):
        """Same for the closed-chat guard: it exists to reject uninformative
        zeros, not real reads. This used to leave the badge lit until some
        later full sync happened to correct it."""
        chat = _chat(t=1000)
        chat["unreadCount"] = 3
        stub = _Stub(chat)

        stub.on_chat_unread_update(JID, 0, previous_unread=3)

        assert stub.chats[JID]["unreadCount"] == 0

    def test_an_unknown_previous_count_stays_conservative(self):
        """The page could not tell us (None). Keeping the badge risks a stale
        badge until the next sync; dropping it risks silently losing unread
        messages — so the safe reading is 'not confirmed'."""
        chat = _chat(t=1000)
        chat["unreadCount"] = 4
        stub = _Stub(chat)
        stub.open_conversation(JID)
        stub._new_since_read[JID] = 4

        stub.on_chat_unread_update(JID, 0, previous_unread=None)

        assert stub.chats[JID]["unreadCount"] == 4

    def test_a_previous_of_zero_is_the_store_load_not_a_read(self):
        """previousUnreadCount=0 means the count never fell: nothing was read,
        the chat was just loaded into WhatsApp Web's Store."""
        chat = _chat(t=1000)
        chat["unreadCount"] = 2
        stub = _Stub(chat)
        stub.open_conversation(JID)
        stub._new_since_read[JID] = 2

        stub.on_chat_unread_update(JID, 0, previous_unread=0)

        assert stub.chats[JID]["unreadCount"] == 2

    def test_an_open_chat_with_no_local_arrivals_still_clears(self):
        """A zero with nothing counted locally means what it says: the open
        conversation is read. The badge here came from a sync, not from
        arrivals this session, so there is nothing local to defend."""
        chat = _chat(t=1000)
        chat["unreadCount"] = 3
        stub = _Stub(chat)
        stub.open_conversation(JID)

        stub.on_chat_unread_update(JID, 0)

        assert stub.chats[JID]["unreadCount"] == 0


class TestReadOnAnotherDeviceAfterALocalRead:
    """Reported live: a 1:1 chat read locally, then a new audio arrives, then
    the user reads it on the phone — WinZapp kept showing "1 mensagem não
    lida" for it.

    WhatsApp Web reports that read as unreadCount=0 with previousUnreadCount=1,
    which _remote_read_confirmed() recognizes as a genuine read elsewhere. But
    the read-ack branch (chat["t"] newer than _locally_read_at) restored the
    badge from _new_since_read without ever consulting it, treating the
    confirmed read exactly like the uninformative zero WA-JS emits when it
    loads a chat into the Store. Groups looked fine because they take the
    `unread_count < old_count` guard, which already checked _remote_read.
    """

    def test_a_confirmed_remote_read_clears_a_message_newer_than_the_read_ack(self):
        chat = _chat(t=2000)
        chat["unreadCount"] = 1
        stub = _Stub(chat)
        stub._locally_read_at[JID] = 1000  # read here first...
        stub._new_since_read[JID] = 1      # ...then one new message arrived

        # ...and then the phone read it: 1 -> 0.
        stub.on_chat_unread_update(JID, 0, 1)

        assert stub.chats[JID]["unreadCount"] == 0

    def test_an_uninformative_zero_still_keeps_the_badge(self):
        """The protection this branch exists for is untouched: a zero with no
        previous count behind it (WA-JS loading the chat into the Store) must
        still leave what arrived after the read-ack counted."""
        chat = _chat(t=2000)
        chat["unreadCount"] = 1
        stub = _Stub(chat)
        stub._locally_read_at[JID] = 1000
        stub._new_since_read[JID] = 1

        stub.on_chat_unread_update(JID, 0, None)

        assert stub.chats[JID]["unreadCount"] == 1

    def test_a_zero_whose_previous_was_also_zero_keeps_the_badge(self):
        """previousUnreadCount=0 is the load-time signature, not a read: the
        count did not fall from anything."""
        chat = _chat(t=2000)
        chat["unreadCount"] = 3
        stub = _Stub(chat)
        stub._locally_read_at[JID] = 1000
        stub._new_since_read[JID] = 3

        stub.on_chat_unread_update(JID, 0, 0)

        assert stub.chats[JID]["unreadCount"] == 3


class TestNeverReadChatKeepsTheServerTotal:
    """Reported live: a group holding 34 thousand unread messages had its badge
    collapse to 21, climb one at a time, get restored to ~34 thousand by the
    next 60s resync, and collapse again a second later — over and over, in the
    four busiest groups on the account.

    The clamp above is only meaningful when _new_since_read counts from a read
    that really happened. on_new_message() creates that entry for any chat
    receiving a message, so in a chat never read in WinZapp it counts arrivals
    since the process started; clamping WhatsApp Web's absolute total to it
    discards every unread message older than this launch. Straight from the
    log: `[unread] ...@g.us: 34876 -> 21 (previous=34944, open=False,
    read_ack=None)`.
    """

    def test_a_backlog_survives_a_climbing_server_count(self):
        chat = _chat(t=2000)
        chat["unreadCount"] = 34876
        stub = _Stub(chat)
        # 21 messages have arrived since launch; the chat was never read here,
        # so nothing anchors that counter to a read.
        stub._new_since_read[JID] = 21

        stub.on_chat_unread_update(JID, 34945, previous_unread=34944)

        assert stub.chats[JID]["unreadCount"] == 34945

    def test_the_clamp_still_applies_once_the_chat_has_been_read_here(self):
        """The regression this branch exists to prevent is untouched: after a
        real local read, the server's inflated total is still clamped down."""
        chat = _chat(t=2000)
        chat["unreadCount"] = 1
        stub = _Stub(chat)
        stub.read_locally(JID)
        stub._new_since_read[JID] = 1

        stub.on_chat_unread_update(JID, 4, previous_unread=3)

        assert stub.chats[JID]["unreadCount"] == 1

    def test_marking_the_chat_unread_again_drops_the_anchor(self):
        """mark_conversation_as_unread() reverses the read, so the ceiling it
        installed must go too — otherwise the next server total is clamped to
        a read the user has explicitly undone."""
        chat = _chat(t=2000)
        chat["unreadCount"] = 1
        stub = _Stub(chat)
        stub.read_locally(JID)
        stub._drop_unread_local_read_anchor(JID)
        stub._new_since_read[JID] = 1

        stub.on_chat_unread_update(JID, 4, previous_unread=3)

        assert stub.chats[JID]["unreadCount"] == 4

    def test_the_anchor_resolves_both_identities_of_one_chat(self):
        """mark_conversation_as_read() is called with whatever key self.chats
        holds; the handler looks it up normalized. A @c.us read must still
        anchor the @s.whatsapp.net form the handler resolves to."""
        chat = _chat(t=2000)
        chat["unreadCount"] = 1
        stub = _Stub(chat)
        stub.read_locally("5511999999999@c.us")
        stub._new_since_read[JID] = 1

        stub.on_chat_unread_update(JID, 4, previous_unread=3)

        assert stub.chats[JID]["unreadCount"] == 1


class TestTheAnchorSurvivesTheHandlerConsumingItsOwnState:
    """The question the anchor has to answer: does dropping the clamp for
    never-read chats let already-read messages come back as unread?

    The end-to-end sequence, in one test rather than in three separate
    fixtures, because the whole risk is in the transitions: the handler pops
    both _locally_read_at and _new_since_read once it has used them, and the
    protection has to outlive that. It does, because only
    mark_conversation_as_read() sets the anchor and only an explicit reversal
    clears it.
    """

    def test_a_read_then_two_arrivals_then_an_inflated_total_stays_clamped(self):
        chat = _chat(t=1000)
        chat["unreadCount"] = 3  # stale badge from an earlier sync
        stub = _Stub(chat)

        # 1. The user reads the chat here.
        stub._locally_read_at[JID] = 1000
        stub.read_locally(JID)
        chat["unreadCount"] = 0

        # 2. One genuinely new message arrives (on_new_message).
        chat["t"] = 2000
        chat["unreadCount"] = 1
        stub._new_since_read[JID] = 1

        # 3. The chats-update for it carries WhatsApp Web's inflated total and
        #    consumes the read-ack on its way through.
        stub.on_chat_unread_update(JID, 5, previous_unread=4)
        assert stub.chats[JID]["unreadCount"] == 1
        assert JID not in stub._locally_read_at      # ack consumed...
        assert JID not in stub._new_since_read       # ...and counter popped

        # 4. A second message arrives — on_new_message recreates the counter.
        chat["t"] = 3000
        chat["unreadCount"] = 2
        stub._new_since_read[JID] = 1

        # 5. A later chats-update still counting the messages read in step 1.
        stub.on_chat_unread_update(JID, 6, previous_unread=5)

        # One unread, not six: the anchor outlived both pops.
        assert stub.chats[JID]["unreadCount"] == 1
        assert stub._unread_anchored_to_local_read(JID)


OTHER_JID = "5511888888888@s.whatsapp.net"


class TestTheAnchorIsPerChat:
    """The anchor is one process-wide set, so the thing that must not happen
    is a read of one chat authorising the clamp for another — that is the
    collapse of a backlog again, just sourced from the wrong conversation."""

    def test_reading_one_chat_does_not_anchor_another(self):
        stub = _Stub(_chat(t=2000))
        stub.chats[OTHER_JID] = _chat(t=2000)
        stub.chats[OTHER_JID]["unreadCount"] = 34876
        stub.read_locally(JID)
        # Arrivals in the OTHER chat, which was never read here.
        stub._new_since_read[OTHER_JID] = 21

        stub.on_chat_unread_update(OTHER_JID, 34945, previous_unread=34944)

        assert stub.chats[OTHER_JID]["unreadCount"] == 34945
        assert not stub._unread_anchored_to_local_read(OTHER_JID)


class TestAnOpenChatWhoseReadWasUndone:
    """The live-event half of tests/test_resync_open_chat_unread.py's class of
    the same name. "Open" is proof of "read" only until the read is undone
    with the conversation still on screen — Ctrl+Shift+M (_on_accel_toggle_read
    -> mark_conversation_as_unread) and the /send-seen rollback both do that,
    and both leave _new_since_read at 0, which is the exact shape
    reconcile_open_chat_unread() answers 0 for.
    """

    def _open_but_unread(self, unread):
        chat = _chat(t=1000)
        chat["unreadCount"] = unread
        stub = _Stub(chat)
        stub.conversations_panel = _CP(JID)  # open, but no anchor: read undone
        return stub

    def test_a_chat_marked_unread_on_screen_keeps_its_badge(self):
        """mark_conversation_as_unread() set 1; the next chats-update used to
        take the open branch and put it straight back to 0."""
        stub = self._open_but_unread(unread=1)

        stub.on_chat_unread_update(JID, 0, previous_unread=None)

        assert stub.chats[JID]["unreadCount"] == 1

    def test_a_restored_backlog_survives_the_next_arrival(self):
        """The /send-seen rollback case: the backlog is back, the conversation
        is still open, and a new message pushes the server's total up by one.
        It used to be answered with 0 (open, nothing counted locally)."""
        stub = self._open_but_unread(unread=34876)

        stub.on_chat_unread_update(JID, 34877, previous_unread=34876)

        assert stub.chats[JID]["unreadCount"] == 34877

    def test_reading_it_again_restores_the_open_behaviour(self):
        """And the ordinary path is untouched: read the chat again and the
        open branch takes over exactly as before."""
        stub = self._open_but_unread(unread=1)
        stub.read_locally(JID)

        stub.on_chat_unread_update(JID, 5)

        assert stub.chats[JID]["unreadCount"] == 0
