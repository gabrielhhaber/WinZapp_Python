"""Tests for the post-pairing "is this even the same phone?" check.

start_qrcode_connection(preserve_local_data=...) keys its wipe on "this
installation had a working history a moment ago", which is not the same
question as "is this the same number". Pair account A, open the dialog, switch
to QR mode and scan with phone B and the wipe is skipped: A's messages.db,
media/ and voice_messages/ survive while B's sync merges on top of them — the
merge clear_local_data() exists to prevent.

MainWindow._wipe_local_data_if_another_number_linked() closes that gap by
asking WPPConnect, once pairing has closed, which phone actually linked. It
deletes the user's history, so every test below is really about the same
property: it may act on proof of divergence and on nothing else. In particular
a freshly created multi-account entry is `pending` with an empty privateinfo
and no session token, and must never so much as reach the probe.

**Both sides of the comparison are a phone WhatsApp itself confirmed.** The
first version of this check read privateinfo["WA_phone_number"], which holds
what the user typed into the pairing dialog — written by connect.py the moment
a pairing code arrives, before the pairing concludes, and never restored when
the attempt is abandoned. So this sequence, in one session, deleted the whole
history of a user who had paired their own phone: session drops, repair dialog
opens, "connect with phone number", a digit typed wrong, the code arrives (
WhatsApp mints one for any number), the mistake is noticed, back to QR, scanned
with the right phone. The check then compared the mistyped number against the
real one and wiped. The confirmed number now lives under its own key,
WA_phone_number_linked, written by this method and nothing else.

The mirror-image half is here too: an install that has never been through this
check carries no such key, and that absence means "learn it now, delete
nothing" — which is also the migration path for every existing install.
"""

import inspect
import threading

import connection_state as cs
import main as main_module
from main import MainWindow, linked_number_differs, linked_phone_digits
from ui.dialogs.connect import Connect


class _List:
    def __init__(self, events):
        self._events = events

    def DeleteAllItems(self):
        self._events.append("list-cleared")


class _Panel:
    """Just enough of ConversationsPanel for the pre-wipe teardown."""

    def __init__(self, events):
        self._events = events
        self.conversations_list = _List(events)
        self.chats_list = ["5511988887777@s.whatsapp.net"]
        self.chat_names = ["Ana"]
        self._all_chats_list = ["5511988887777@s.whatsapp.net"]
        self._all_chat_names = ["Ana"]
        self._displayed_jids = {"5511988887777@s.whatsapp.net"}

    def _stop_audio(self):
        self._events.append("audio-stopped")

    def close_conversation(self):
        self._events.append("conversation-closed")


class _Stub:
    """Minimal stand-in for MainWindow for the divergence check.

    Carries only what the method under test touches — the settings it reads
    the recorded number from, the token/db that gate the probe, the JID helper
    the comparison goes through, and (for the mid-session path) the sync flags
    and the conversation panel it tears down before deleting anything.
    """

    def __init__(self, recorded_number="5511999999999", typed_number=None,
                 paired=True, probe=(cs.LINK_PROBE_LINKED, ""),
                 token="sess1:hash1", db=object(), lid_to_phone=None,
                 ui_ready=False):
        privateinfo = {}
        if recorded_number is not None:
            privateinfo["WA_phone_number_linked"] = recorded_number
        if typed_number is not None:
            privateinfo["WA_phone_number"] = typed_number
        if paired:
            privateinfo["paired"] = True
        self.settings = {"privateinfo": privateinfo}
        self.token = token
        self.db = db
        self._lid_to_phone = lid_to_phone or {}
        self._probe = probe
        self.probe_calls = 0
        self.wipe_calls = 0
        self.saved = 0
        # Ordered trace of everything whose relative order matters: the claim
        # on the sync slot, the UI teardown, the wipe, the sync restart.
        self.events = []
        self._ui_ready_event = threading.Event()
        if ui_ready:
            self._ui_ready_event.set()
        self.conversations_panel = _Panel(self.events)
        self._initial_sync_running = False
        self._sync_completed = True
        self._force_full_sync = False
        self.sync_starts = 0

    def _host_device_link_probe(self):
        self.probe_calls += 1
        self.events.append(("probe", self._initial_sync_running))
        return self._probe

    def clear_local_data(self):
        self.wipe_calls += 1
        self.events.append(("wipe", self._initial_sync_running))

    def save_settings(self):
        self.saved += 1

    def _try_start_sync_thread(self):
        self.sync_starts += 1
        self.events.append(
            ("sync", self._sync_completed, self._force_full_sync))
        return True

    _normalize_jid = staticmethod(MainWindow._normalize_jid)
    _wipe_local_data_if_another_number_linked = (
        MainWindow._wipe_local_data_if_another_number_linked
    )

    @property
    def recorded_number(self):
        return self.settings["privateinfo"].get("WA_phone_number_linked")

    @property
    def typed_number(self):
        return self.settings["privateinfo"].get("WA_phone_number")


def _install_inline_call_after(monkeypatch):
    """Stand in for the MainLoop that is not running here.

    wx.CallAfter marshals the teardown to the main thread and swallows
    whatever the callback raises; running it inline where it is queued keeps
    both halves of that, and keeps the ordering assertions honest, since the
    method waits on that callback before going near clear_local_data().
    """
    def _call_after(fn, *a, **kw):
        try:
            fn(*a, **kw)
        except Exception:
            pass

    monkeypatch.setattr(main_module.wx, "CallAfter", _call_after)


def _run_live(stub, monkeypatch):
    """Run the check with the UI up."""
    _install_inline_call_after(monkeypatch)
    stub._wipe_local_data_if_another_number_linked()


class TestLinkedPhoneDigits:
    def test_reads_a_plain_phone_jid(self):
        assert linked_phone_digits(_Stub(), "5511999999999@c.us") == "5511999999999"

    def test_reads_a_bare_digit_string(self):
        assert linked_phone_digits(_Stub(), "5511999999999") == "5511999999999"

    def test_strips_the_device_suffix(self):
        assert linked_phone_digits(
            _Stub(), "5511999999999:12@s.whatsapp.net") == "5511999999999"

    def test_an_unbridged_lid_is_not_a_phone_number(self):
        """Its digits are an internal identifier, so comparing them against a
        stored number would "prove" a difference for every single @lid."""
        assert linked_phone_digits(_Stub(), "182736450192837@lid") == ""

    def test_a_bridged_lid_resolves_to_its_phone(self):
        stub = _Stub(lid_to_phone={"182736450192837@lid": "5511999999999@s.whatsapp.net"})
        assert linked_phone_digits(stub, "182736450192837@lid") == "5511999999999"

    def test_nothing_readable_yields_nothing(self):
        stub = _Stub()
        for value in (None, "", "   ", 12345, "not-a-number@s.whatsapp.net",
                      "1234@s.whatsapp.net", "120363000000000000@g.us",
                      "status@broadcast"):
            assert linked_phone_digits(stub, value) == "", value


class TestLinkedNumberDiffers:
    def test_the_same_number_never_differs(self):
        assert not linked_number_differs("5511999999999", "5511999999999")

    def test_the_brazilian_eight_nine_digit_variant_is_the_same_number(self):
        """5511999999999 ↔ 551199999999: the collapse
        MainWindow._phone_digits_equivalent() already does everywhere else in
        the app. Comparing raw strings here would wipe the history of a user
        whose recorded number predates a change in what getWid() reports."""
        assert not linked_number_differs("5511999999999", "551199999999")
        assert not linked_number_differs("551199999999", "5511999999999")

    def test_a_genuinely_different_number_differs(self):
        assert linked_number_differs("5511999999999", "5521988887777")

    def test_no_recorded_number_never_differs(self):
        """The migration path: an install that predates this check has no
        recorded number at all, and that must read as "learn it", never as
        "it diverged"."""
        for recorded in (None, "", "   ", 5511999999999):
            assert not linked_number_differs(
                recorded, "5521988887777"), recorded

    def test_nothing_read_from_the_server_never_differs(self):
        for linked in (None, "", 0):
            assert not linked_number_differs("5511999999999", linked), linked


class TestNumbersThatAreNotTheSameNumber:
    """Real, distinct subscribers that a "one extra digit in the prefix"
    tolerance collapsed into one. The reviewer obtained every one of these by
    running that heuristic; three of the four are in countries whose dialling
    code is three digits long, where "the first two digits are the country
    code" is simply false.

    They matter in both directions. Here, treating them as equal means never
    wiping data that belongs to somebody else — which sounds like the safe
    side and is not, because it is the merge this whole check exists to stop.
    In Connect._can_reuse_existing_session() (tests/test_pairing_session_reuse
    .py) it meant resuming a stranger's live session on the strength of a
    typo.
    """

    PAIRS = [
        # +49 211 1234567 vs +49 211 234567 — two Düsseldorf subscribers.
        ("492111234567", "49211234567"),
        # +43 1 ... — two Vienna landlines.
        ("431123456789", "43123456789"),
        # Italian mobiles, 10 and 9 digits.
        ("393331234567", "39331234567"),
        # A Portuguese mobile against a Sofia landline: different countries.
        ("351924567890", "35924567890"),
    ]

    def test_each_pair_reads_as_a_different_number(self):
        for a, b in self.PAIRS:
            assert linked_number_differs(a, b), (a, b)
            assert linked_number_differs(b, a), (b, a)

    def test_each_pair_wipes_when_it_is_the_one_that_linked(self):
        for a, b in self.PAIRS:
            stub = _Stub(recorded_number=a,
                         probe=(cs.LINK_PROBE_LINKED, f"{b}@c.us"))

            stub._wipe_local_data_if_another_number_linked()

            assert stub.wipe_calls == 1, (a, b)
            assert stub.recorded_number == b, (a, b)


class TestWipeOnlyOnProvenDivergence:
    def test_a_different_number_wipes_and_takes_over_the_recorded_number(self):
        stub = _Stub(probe=(cs.LINK_PROBE_LINKED, "5521988887777@c.us"))

        stub._wipe_local_data_if_another_number_linked()

        assert stub.wipe_calls == 1
        # Left at the old number, the next pairing of THIS one would look
        # like another divergence and wipe a second time.
        assert stub.recorded_number == "5521988887777"
        assert stub.saved == 1

    def test_the_same_number_keeps_everything(self):
        stub = _Stub(probe=(cs.LINK_PROBE_LINKED, "5511999999999@c.us"))

        stub._wipe_local_data_if_another_number_linked()

        assert stub.wipe_calls == 0
        assert stub.recorded_number == "5511999999999"

    def test_the_brazilian_variant_of_the_same_number_keeps_everything(self):
        stub = _Stub(recorded_number="5511999999999",
                     probe=(cs.LINK_PROBE_LINKED, "551199999999@s.whatsapp.net"))

        stub._wipe_local_data_if_another_number_linked()

        assert stub.wipe_calls == 0

    def test_a_new_multi_account_entry_is_never_touched_or_even_probed(self):
        """`pending` state: empty privateinfo, no recorded number, no
        `paired`, and — what actually stops it here — no session token.
        Adding an account to the manager must not be able to delete anything,
        in this account or any other, nor go asking a server it holds no
        session on."""
        stub = _Stub(recorded_number=None, paired=False, token="")

        stub._wipe_local_data_if_another_number_linked()

        assert stub.wipe_calls == 0
        assert stub.probe_calls == 0
        assert stub.recorded_number is None

    def test_a_server_answer_we_cannot_read_keeps_everything(self):
        for linked in ("", None, "not-a-number", "182736450192837@lid"):
            stub = _Stub(probe=(cs.LINK_PROBE_LINKED, linked))

            stub._wipe_local_data_if_another_number_linked()

            assert stub.wipe_calls == 0, linked
            assert stub.recorded_number == "5511999999999"

    def test_a_probe_without_a_verdict_keeps_everything(self):
        for outcome in (cs.LINK_PROBE_UNKNOWN, cs.LINK_PROBE_UNLINKED):
            # A number does ride along in the UNKNOWN case only in this test;
            # the real probe returns "" with it. Passing one anyway proves the
            # outcome alone is what gates the wipe.
            stub = _Stub(probe=(outcome, "5521988887777@c.us"))

            stub._wipe_local_data_if_another_number_linked()

            assert stub.wipe_calls == 0, outcome

    def test_no_open_database_defers_instead_of_half_wiping(self):
        """The startup dialog closes before prepare_sync() opens the DB, and
        clear_local_data() only clears it when it exists — a wipe there would
        delete media/ and voice_messages/ and leave every message behind.
        MainWindow.__init__ runs the check again right after prepare_sync()."""
        stub = _Stub(db=None, probe=(cs.LINK_PROBE_LINKED, "5521988887777@c.us"))

        stub._wipe_local_data_if_another_number_linked()

        assert stub.wipe_calls == 0
        assert stub.probe_calls == 0

    def test_no_token_keeps_everything(self):
        stub = _Stub(token="", probe=(cs.LINK_PROBE_LINKED, "5521988887777@c.us"))

        stub._wipe_local_data_if_another_number_linked()

        assert stub.wipe_calls == 0
        assert stub.probe_calls == 0

    def test_a_comparison_that_raises_keeps_everything(self, monkeypatch):
        """A bug in the comparison must not be able to delete anything."""
        def _boom(*a, **kw):
            raise RuntimeError("comparison blew up")

        monkeypatch.setattr(main_module, "linked_number_differs", _boom)
        stub = _Stub(probe=(cs.LINK_PROBE_LINKED, "5521988887777@c.us"))

        stub._wipe_local_data_if_another_number_linked()

        assert stub.wipe_calls == 0


class TestAMistypedNumberCannotDeleteAnything:
    """The regression this key exists for, start to finish.

    Account A is paired and synced. The session drops, _show_repair_dialog()
    reopens the pairing dialog, the user clicks "connect with phone number"
    and gets one digit wrong. WhatsApp mints a code for that number like it
    would for any other, and connect.py writes it into
    privateinfo["WA_phone_number"] the moment it arrives — before pairing has
    concluded, and _on_pairing_code_error() only ever clears the token, never
    that field. The user notices, goes back to QR and scans with their own
    phone. Everything about that is correct behaviour by the user, and it used
    to end in messages.db, media/ and voice_messages/ being deleted.
    """

    TYPED_WRONG = "5511977776666"
    REALLY_MINE = "5511999999999"

    def test_the_typed_number_is_not_what_the_check_compares(self):
        stub = _Stub(recorded_number=self.REALLY_MINE,
                     typed_number=self.TYPED_WRONG,
                     probe=(cs.LINK_PROBE_LINKED, f"{self.REALLY_MINE}@c.us"))

        stub._wipe_local_data_if_another_number_linked()

        assert stub.wipe_calls == 0
        assert stub.recorded_number == self.REALLY_MINE
        # And it is left exactly as the dialog left it: this method has no
        # business rewriting what the user typed.
        assert stub.typed_number == self.TYPED_WRONG

    def test_a_qr_only_install_learns_instead_of_deleting(self):
        """Same sequence on an install that had never used the phone-code
        flow before: there is no recorded number yet, so the abandoned attempt
        is the only number in privateinfo. Reading it would have been the
        worst version of this bug — the wipe with nothing at all to justify
        it."""
        stub = _Stub(recorded_number=None, typed_number=self.TYPED_WRONG,
                     probe=(cs.LINK_PROBE_LINKED, f"{self.REALLY_MINE}@c.us"))

        stub._wipe_local_data_if_another_number_linked()

        assert stub.wipe_calls == 0
        assert stub.recorded_number == self.REALLY_MINE

    def test_a_genuinely_other_phone_is_still_caught_through_the_typo(self):
        """The protection must survive the fix: the mistyped number sitting in
        privateinfo changes nothing about somebody else's phone scanning."""
        stub = _Stub(recorded_number=self.REALLY_MINE,
                     typed_number=self.TYPED_WRONG,
                     probe=(cs.LINK_PROBE_LINKED, "5521988887777@c.us"))

        stub._wipe_local_data_if_another_number_linked()

        assert stub.wipe_calls == 1
        assert stub.recorded_number == "5521988887777"


class TestHostDeviceProbeReportsThePhoneItSaw:
    """_still_linked_on_server() keeps its three-way outcome; the phone it
    read is what the divergence check above needs, so the probe now returns
    both and the old method is the outcome half of it."""

    class _Resp:
        def __init__(self, status_code, body):
            self.status_code = status_code
            self._body = body

        def json(self):
            return self._body

    class _ProbeStub:
        wpp_server = "http://127.0.0.1"
        wpp_port = 6300
        token = "sess1:hash1"

        _host_device_link_probe = MainWindow._host_device_link_probe
        _still_linked_on_server = MainWindow._still_linked_on_server

    def _stub_api(self, monkeypatch, resp):
        monkeypatch.setattr(main_module, "api_get", lambda *a, **kw: resp)
        return self._ProbeStub()

    def test_a_linked_session_reports_its_phone(self, monkeypatch):
        stub = self._stub_api(monkeypatch, self._Resp(
            200, {"response": {"phoneNumber": "5511999999999@c.us"}}))

        assert stub._host_device_link_probe() == (
            cs.LINK_PROBE_LINKED, "5511999999999@c.us")
        assert stub._still_linked_on_server() == cs.LINK_PROBE_LINKED

    def test_the_serialized_shape_is_unwrapped(self, monkeypatch):
        stub = self._stub_api(monkeypatch, self._Resp(
            200, {"response": {"phoneNumber": {"_serialized": "5511999999999@c.us"}}}))

        assert stub._host_device_link_probe() == (
            cs.LINK_PROBE_LINKED, "5511999999999@c.us")

    def test_a_missing_phone_number_key_is_an_unlink_with_no_phone(self, monkeypatch):
        stub = self._stub_api(monkeypatch, self._Resp(200, {"response": {}}))

        assert stub._host_device_link_probe() == (cs.LINK_PROBE_UNLINKED, "")
        assert stub._still_linked_on_server() == cs.LINK_PROBE_UNLINKED

    def test_a_refused_probe_carries_no_phone(self, monkeypatch):
        stub = self._stub_api(monkeypatch, self._Resp(401, {}))

        assert stub._host_device_link_probe() == (cs.LINK_PROBE_UNKNOWN, "")
        assert stub._still_linked_on_server() == cs.LINK_PROBE_UNKNOWN


class TestLearningTheNumberOfAnInstallThatNeverRanThisCheck:
    """Every existing install is in this state on the launch it first sees
    this code, and so is one that has only ever paired by QR.

    Its session drops, _show_repair_dialog() reopens the pairing dialog with
    `paired` and WA_token intact, somebody else's phone scans the code, and
    B's sync merges straight onto A's database: the scenario this whole check
    exists for, and the one it could not see, because an empty recorded number
    returned before anything was compared. Recording what the probe reports
    deletes nothing by itself and arms the comparison from the second pairing
    onwards.
    """

    def test_a_linked_number_is_recorded_without_deleting_anything(self):
        stub = _Stub(recorded_number=None,
                     probe=(cs.LINK_PROBE_LINKED, "5511999999999@c.us"))

        stub._wipe_local_data_if_another_number_linked()

        assert stub.wipe_calls == 0
        assert stub.recorded_number == "5511999999999"
        assert stub.saved == 1

    def test_an_empty_recorded_number_is_treated_the_same(self):
        stub = _Stub(recorded_number="",
                     probe=(cs.LINK_PROBE_LINKED, "5511999999999@c.us"))

        stub._wipe_local_data_if_another_number_linked()

        assert stub.wipe_calls == 0
        assert stub.recorded_number == "5511999999999"

    def test_the_next_pairing_can_then_see_a_different_phone(self):
        """The point of recording it: the very next divergence is caught."""
        stub = _Stub(recorded_number=None,
                     probe=(cs.LINK_PROBE_LINKED, "5511999999999@c.us"))
        stub._wipe_local_data_if_another_number_linked()
        assert stub.wipe_calls == 0

        stub._probe = (cs.LINK_PROBE_LINKED, "5521988887777@c.us")
        stub._wipe_local_data_if_another_number_linked()

        assert stub.wipe_calls == 1
        assert stub.recorded_number == "5521988887777"

    def test_nothing_is_recorded_when_the_probe_has_no_verdict(self):
        for outcome in (cs.LINK_PROBE_UNKNOWN, cs.LINK_PROBE_UNLINKED):
            stub = _Stub(recorded_number=None,
                         probe=(outcome, "5511999999999@c.us"))

            stub._wipe_local_data_if_another_number_linked()

            assert stub.recorded_number is None, outcome
            assert stub.saved == 0, outcome

    def test_nothing_is_recorded_from_an_answer_we_cannot_read(self):
        """An unbridged @lid's digits are not a phone number — storing them
        would make the NEXT pairing of the real number look like a divergence,
        and wipe."""
        for linked in ("182736450192837@lid", "1234", "not-a-number",
                       "120363000000000000@g.us"):
            stub = _Stub(recorded_number=None, probe=(cs.LINK_PROBE_LINKED, linked))

            stub._wipe_local_data_if_another_number_linked()

            assert stub.recorded_number is None, linked
            assert stub.saved == 0, linked


class TestTheMidSessionWipeDoesNotRaceTheAppAroundIt:
    """The dialog this check runs behind is usually _show_repair_dialog()'s,
    which reopens over a fully running app: a chat list on screen, an audio
    player that may hold a .msv open, and — the moment the socket reconnects —
    websocket_client's _recheck_connection_after_connect() setting
    _sync_completed = False and calling trigger_sync_if_needed().

    That sync can start inside the probe's 10 s window, capture self.chats
    while it still holds account A's chats, and write them into account B's
    database after the wipe: the merge this method exists to prevent,
    happening while the user is told it was prevented. _resync_all_worker() is
    the only other caller of clear_local_data() with a live MainLoop and it
    already solves all of this — claim the sync slot first, marshal the UI
    teardown and wait for it, then ask for a fresh full sync afterwards.
    """

    def test_the_sync_slot_is_claimed_before_the_probe_and_across_the_wipe(
            self, monkeypatch):
        stub = _Stub(ui_ready=True,
                     probe=(cs.LINK_PROBE_LINKED, "5521988887777@c.us"))

        _run_live(stub, monkeypatch)

        claimed = [running for name, running, *_ in stub.events
                   if name in ("probe", "wipe")]
        assert claimed == [True, True]

    def test_the_list_and_the_audio_go_before_the_files_are_deleted(
            self, monkeypatch):
        """Not cosmetic on either count: Enter on a leftover row opens a
        conversation that no longer exists, and a voice note still playing
        holds its .msv open, so clear_local_data()'s os.unlink raises
        PermissionError on it."""
        stub = _Stub(ui_ready=True,
                     probe=(cs.LINK_PROBE_LINKED, "5521988887777@c.us"))

        _run_live(stub, monkeypatch)

        names = [e if isinstance(e, str) else e[0] for e in stub.events]
        for step in ("audio-stopped", "conversation-closed", "list-cleared"):
            assert names.index(step) < names.index("wipe"), step
        panel = stub.conversations_panel
        assert panel.chats_list == []
        assert panel.chat_names == []
        assert panel._all_chats_list == []
        assert panel._all_chat_names == []
        assert panel._displayed_jids is None

    def test_a_fresh_full_sync_is_requested_once_the_database_is_empty(
            self, monkeypatch):
        """Whatever the in-flight round managed to write, the round asked for
        here refetches the linked account's own chat list over it."""
        stub = _Stub(ui_ready=True,
                     probe=(cs.LINK_PROBE_LINKED, "5521988887777@c.us"))

        _run_live(stub, monkeypatch)

        names = [e if isinstance(e, str) else e[0] for e in stub.events]
        assert names.index("wipe") < names.index("sync")
        assert stub.sync_starts == 1
        assert stub._sync_completed is False
        assert stub._force_full_sync is True

    def test_the_claim_is_released_when_nothing_was_wiped(self, monkeypatch):
        """The overwhelmingly common outcome. Holding _initial_sync_running
        after it would block every later sync for the rest of the session."""
        stub = _Stub(ui_ready=True,
                     probe=(cs.LINK_PROBE_LINKED, "5511999999999@c.us"))

        _run_live(stub, monkeypatch)

        assert stub.wipe_calls == 0
        assert stub._initial_sync_running is False
        assert stub.sync_starts == 0

    def test_the_claim_is_released_when_the_probe_says_nothing(self, monkeypatch):
        stub = _Stub(ui_ready=True, probe=(cs.LINK_PROBE_UNKNOWN, ""))

        _run_live(stub, monkeypatch)

        assert stub._initial_sync_running is False

    def test_a_teardown_that_raises_still_releases_the_wait(self, monkeypatch):
        """_prepare_ui() sets its event in a finally, so a panel in a state
        this does not expect costs the visible cleanup, never the wipe — and
        never a thread parked on the 5 s wait for an event nobody will set."""
        stub = _Stub(ui_ready=True,
                     probe=(cs.LINK_PROBE_LINKED, "5521988887777@c.us"))

        def _boom():
            raise RuntimeError("panel already destroyed")

        stub.conversations_panel._stop_audio = _boom

        _run_live(stub, monkeypatch)

        assert stub.wipe_calls == 1
        assert stub.sync_starts == 1


class TestTheStartupPathTouchesNoneOfThat:
    """At startup this runs inside MainWindow.__init__, before init_UI(): no
    panels to tear down, no MainLoop to marshal a CallAfter to (it would
    simply never run, and the 5 s wait would expire on every launch), and the
    first sync still ahead of us rather than in flight."""

    def test_nothing_is_marshalled_and_no_sync_is_started(self, monkeypatch):
        called = []
        monkeypatch.setattr(main_module.wx, "CallAfter",
                            lambda fn, *a, **kw: called.append(fn))
        stub = _Stub(ui_ready=False,
                     probe=(cs.LINK_PROBE_LINKED, "5521988887777@c.us"))

        stub._wipe_local_data_if_another_number_linked()

        assert stub.wipe_calls == 1
        assert called == []
        assert stub.sync_starts == 0
        assert stub._initial_sync_running is False


class TestANonWipingDisconnectLeavesTheCheckArmed:
    """_on_disconnect(wipe=False) is the "resume failed, but that is not proof
    the device was unlinked" path: the user is sent back to the pairing dialog
    and their history deliberately survives.

    That is exactly the state this check protects — a full local history, a
    pairing dialog on screen, and any phone able to scan the code. Clearing
    the recorded number there disarmed it: with nothing to compare against,
    the next pairing takes the "learn it, delete nothing" branch and the two
    accounts merge. The key describes the data on disk, so it goes only when
    that data goes.
    """

    class _DisconnectStub:
        _on_disconnect = MainWindow._on_disconnect

        def __init__(self):
            self.settings = {"privateinfo": {
                "WA_phone_number": "5511999999999",
                "WA_phone_number_linked": "5511999999999",
                "paired": True,
            }}
            self.wipes = 0
            self.ws = None
            self.connect = self
            self.dialogs = 0

        # No stored token: _on_disconnect() otherwise spawns a thread that
        # POSTs close-session to a server nothing here is running.
        def _get_wa_token(self):
            return ""

        def _set_wa_token(self, value):
            pass

        def save_settings(self):
            pass

        def clear_local_data(self):
            self.wipes += 1

        def _reset_startup_probe(self):
            pass

        def show_connection_dial(self):
            self.dialogs += 1

    def test_a_failed_resume_keeps_the_recorded_number(self):
        stub = self._DisconnectStub()

        stub._on_disconnect(wipe=False)

        assert stub.wipes == 0
        privateinfo = stub.settings["privateinfo"]
        assert privateinfo["WA_phone_number_linked"] == "5511999999999"
        # What the user typed is still dropped: it only ever gated reusing a
        # session, and there is no session to reuse any more.
        assert "WA_phone_number" not in privateinfo
        assert stub.dialogs == 1

    def test_a_failed_resume_still_catches_another_phone(self):
        """The whole point, end to end: this is the state the check has to
        survive, because it is the one where the history is intact and the
        pairing dialog is open to anybody's phone."""
        disconnected = self._DisconnectStub()
        disconnected._on_disconnect(wipe=False)

        stub = _Stub(probe=(cs.LINK_PROBE_LINKED, "5521988887777@c.us"))
        stub.settings = disconnected.settings

        stub._wipe_local_data_if_another_number_linked()

        assert stub.wipe_calls == 1

    def test_a_confirmed_logout_drops_it_with_the_data(self):
        stub = self._DisconnectStub()

        stub._on_disconnect(wipe=True)

        assert stub.wipes == 1
        assert "WA_phone_number_linked" not in stub.settings["privateinfo"]


class TestBothCallSitesStayWired:
    """Nothing else in the suite would notice the check being dropped: it is
    called for its side effect, from two places that cannot be constructed in
    a test (a wx.Frame's __init__ and a method ending in ShowModal()), and
    "no wipe happened" is also what a removed call looks like. Source level,
    same approach as test_opening_the_dialog_drops_a_previous_dialogs_capture
    in tests/test_qrcode_repair_preserves_local_data.py."""

    def test_the_startup_check_runs_after_prepare_sync(self):
        """Not at the dialog's own end: that runs before prepare_sync() opens
        the database, and a wipe there would clear media/ and voice_messages/
        and leave messages.db to be read back a few lines later."""
        lines = inspect.getsource(MainWindow.__init__).splitlines()
        prepared = next(i for i, ln in enumerate(lines)
                        if "self.prepare_sync()" in ln)
        guarded = next(i for i, ln in enumerate(lines)
                       if "if self._just_paired:" in ln and i > prepared)
        called = next(i for i, ln in enumerate(lines)
                      if "_wipe_local_data_if_another_number_linked" in ln)
        assert prepared < guarded < called

    def test_the_dialog_checks_once_its_modal_loop_has_returned(self):
        """Before that the session is not linked to anything yet, so
        host-device has nothing to report."""
        lines = inspect.getsource(Connect.show_connection_dial).splitlines()
        shown = next(i for i, ln in enumerate(lines)
                     if "self.connection_dial.ShowModal()" in ln)
        called = next(i for i, ln in enumerate(lines)
                      if "_wipe_local_data_if_another_number_linked" in ln)
        assert shown < called

    def test_the_dialog_never_runs_it_on_the_main_thread(self):
        """show_connection_dial() bounces itself to the main thread, and the
        mid-session dialogs are opened with the MainLoop alive and the main
        window on screen. A 10 s Puppeteer probe plus a file-by-file wipe held
        there is Windows ghosting the window — "(Not Responding)" read out
        over the pairing flow."""
        tail = inspect.getsource(Connect.show_connection_dial).split(
            "self.connection_dial.ShowModal()", 1)[1]
        spawn = tail.index("threading.Thread(")
        called = tail.index("_wipe_local_data_if_another_number_linked")
        assert spawn < called

    def test_the_dialog_only_checks_when_the_ui_is_already_up(self):
        """MainWindow.__init__ ShowModal()s the startup dialog and then runs
        the check itself after prepare_sync(). Letting the dialog spawn its
        own copy there too left two of them, one on a thread racing the rest
        of __init__, separated only by which of them reached `self.db` first.
        _ui_ready_event is the deterministic form of that distinction: unset
        for every startup dialog, set for every mid-session one."""
        tail = inspect.getsource(Connect.show_connection_dial).split(
            "self.connection_dial.ShowModal()", 1)[1]
        gate = tail.index("_ui_ready_event.is_set()")
        spawn = tail.index("threading.Thread(")
        assert gate < spawn
