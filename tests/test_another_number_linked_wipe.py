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
nothing". That branch is not the migration, though — it is reached for the
first time on a pairing, which is precisely the pairing that may already have
linked another phone. record_linked_phone_if_unknown() is the migration: it
learns the number from an ordinary connection, before anybody scans anything.

The other half of these tests is what surrounds the wipe mid-session. By the
time this check starts, a sync of the newly paired account is nearly always
already in flight — started synchronously by the event that concluded the
pairing, before the dialog even closed — holding the previous account's chats.
The wipe alone does not deal with it: _try_start_sync_thread() refuses to start
anything while that round lives, so the corrective full sync silently never
happens, and the round keeps writing until it exits.
"""

import inspect
import logging
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


class _FakeI18n:
    """t() answers with the key, so an assertion names the key rather than a
    sentence five files would have to be kept in step with."""

    def t(self, key):
        return key


class _InFlightSync:
    """The sync round that is already running when the check starts.

    Nothing of start_sync() except the two things this code can observe: the
    thread stays alive until it is released, and — like start_sync()'s own
    finally — it clears _initial_sync_running on the way out, whoever set it.
    """

    def __init__(self, stub):
        self._stub = stub
        self._release = threading.Event()
        stub._initial_sync_running = True
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name="fake-initial-sync")
        stub.sync_thread = self.thread
        self.thread.start()

    def _run(self):
        try:
            self._release.wait(timeout=10)
        finally:
            self._stub.events.append("in-flight-sync-ended")
            self._stub._initial_sync_running = False

    def finish(self):
        self._release.set()
        self.thread.join(timeout=5)
        assert not self.thread.is_alive()


def _resync_thread():
    """The handoff thread, by the name the code gives it.

    It is created before the check returns, so looking it up right after is
    deterministic — and it must be looked up before the in-flight round is
    released, since a finished thread is gone from enumerate().
    """
    for thread in threading.enumerate():
        if thread.name == "another-number-resync":
            return thread
    return None


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
        self.sync_thread = None
        self.sync_starts = 0
        self.syncs_started = 0
        self.i18n = _FakeI18n()
        self.spoken = []
        self.full_sync_latches = []

    def _host_device_link_probe(self):
        self.probe_calls += 1
        self.events.append(("probe", self._initial_sync_running))
        return self._probe

    def clear_local_data(self):
        self.wipe_calls += 1
        self.events.append(("wipe", self._initial_sync_running))
        # The real one drops the recorded number along with the data it
        # describes, which is what makes "the number is written afterwards"
        # an assertion about ordering rather than about nothing.
        self.settings["privateinfo"].pop("WA_phone_number_linked", None)

    def save_settings(self):
        self.saved += 1

    def _persist_full_sync_pending(self, reason):
        """The on-disk half of _force_full_sync.

        The real one writes into the same system_metadata table the wipe
        empties, so when it runs matters as much as whether it runs — hence
        the event as well as the count.
        """
        self.full_sync_latches.append(reason)
        self.events.append(("full-latch", reason))

    def output(self, text, interrupt=False):
        self.spoken.append(text)
        self.events.append(("spoken", text))

    def _try_start_sync_thread(self):
        """Honest about the one answer that matters here.

        The real one holds _sync_start_lock, sees a live self.sync_thread and
        returns True having started nothing — which is why a check that simply
        called it while the contaminated round was still running requested a
        corrective full sync that never happened.
        """
        self.sync_starts += 1
        self.events.append(
            ("sync", self._sync_completed, self._force_full_sync))
        existing = getattr(self, "sync_thread", None)
        if existing is not None and existing.is_alive():
            return True
        self.syncs_started += 1
        return True

    _normalize_jid = staticmethod(MainWindow._normalize_jid)
    _wipe_local_data_if_another_number_linked = (
        MainWindow._wipe_local_data_if_another_number_linked
    )
    _apply_another_number_wipe = MainWindow._apply_another_number_wipe
    _teardown_conversation_ui = MainWindow._teardown_conversation_ui
    _restart_sync_after_another_number_wipe = (
        MainWindow._restart_sync_after_another_number_wipe
    )
    _ANOTHER_NUMBER_SYNC_JOIN_ROUNDS = MainWindow._ANOTHER_NUMBER_SYNC_JOIN_ROUNDS

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


class TestTheNumberIsLearnedBeforeItIsEverNeeded:
    """The migration, and why it cannot wait for the check itself.

    The check only runs on a pairing (_just_paired) or when a mid-session
    pairing dialog closes. So an install that predates this code — and every
    QR-only install, which has never written a number anywhere — reaches it
    for the first time on a pairing, and that is exactly the pairing that may
    already have linked somebody else's phone. With no recorded number it
    takes the "learn it, delete nothing" branch there: the feature would arm
    itself only from the *second* divergent pairing onwards, and it is the
    first one that costs the history.

    record_linked_phone_if_unknown() reads the same host-device answer
    check_wa_connection_http() already fetches every poll, so the number is on
    file from the first launch after the update. It only ever writes.
    """

    def test_an_account_with_no_number_on_file_learns_it(self):
        stub = _Stub(recorded_number=None)

        assert main_module.record_linked_phone_if_unknown(
            stub, "5511999999999@c.us") is True
        assert stub.recorded_number == "5511999999999"

    def test_an_empty_value_counts_as_none(self):
        stub = _Stub(recorded_number="")

        assert main_module.record_linked_phone_if_unknown(
            stub, "5511999999999@c.us") is True
        assert stub.recorded_number == "5511999999999"

    def test_a_number_already_on_file_is_never_overwritten(self):
        """This runs on every connection, including the one right after a
        different phone linked. Overwriting there would erase the very
        divergence the check is about to be asked to find."""
        stub = _Stub(recorded_number="5511999999999")

        assert main_module.record_linked_phone_if_unknown(
            stub, "5521988887777@c.us") is False
        assert stub.recorded_number == "5511999999999"

    def test_an_answer_that_is_not_a_phone_number_records_nothing(self):
        """Storing an unbridged @lid's digits would make the NEXT pairing of
        the real number look like a divergence, and wipe."""
        for value in ("182736450192837@lid", "1234", "not-a-number", "",
                      None, {"_serialized": "5511999999999@c.us"}):
            stub = _Stub(recorded_number=None)

            assert main_module.record_linked_phone_if_unknown(
                stub, value) is False, value
            assert stub.recorded_number is None, value

    def test_privateinfo_that_is_not_a_dict_is_left_alone(self):
        stub = _Stub(recorded_number=None)
        stub.settings = {"privateinfo": None}

        assert main_module.record_linked_phone_if_unknown(
            stub, "5511999999999@c.us") is False

    def test_it_deletes_nothing_of_its_own(self):
        """Guard on the property the whole helper rests on: it is reachable
        from a code path that runs every few seconds."""
        stub = _Stub(recorded_number=None)

        main_module.record_linked_phone_if_unknown(stub, "5511999999999@c.us")

        assert stub.wipe_calls == 0
        assert stub.probe_calls == 0

    def test_the_first_divergent_pairing_is_then_caught(self):
        """End to end, on the install this exists for: it learns the number
        from an ordinary connection, and the very next pairing — the first
        one after the update — sees the difference."""
        stub = _Stub(recorded_number=None)
        main_module.record_linked_phone_if_unknown(stub, "5511999999999@c.us")

        stub._probe = (cs.LINK_PROBE_LINKED, "5521988887777@c.us")
        stub._wipe_local_data_if_another_number_linked()

        assert stub.wipe_calls == 1
        assert stub.recorded_number == "5521988887777"

    def test_the_connection_check_is_where_it_is_wired(self):
        """Source level, same approach as TestBothCallSitesStayWired below:
        it is called for its side effect, from a method no test can construct,
        and "nothing was recorded" is also what a removed call looks like."""
        source = inspect.getsource(MainWindow.check_wa_connection_http)
        assert "record_linked_phone_if_unknown(self, wuid)" in source
        # Written straight through to settings.json, or the next launch reads
        # an install that still has nothing on file.
        after = source.split("record_linked_phone_if_unknown(self, wuid)", 1)[1]
        assert "self.save_settings()" in after


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
        # With nothing in flight it really starts, rather than being answered
        # "there is already one running" — see the class below.
        assert stub.syncs_started == 1
        assert stub._sync_completed is False
        assert stub._force_full_sync is True

    def test_the_full_mode_is_latched_on_disk_as_well(self, monkeypatch):
        """_force_full_sync alone lives in RAM. F5 deletes strictly less than
        this and still latches it (_persist_full_sync_pending("manual-resync"))
        so full mode survives a restart in the middle of the round; this path
        latched nothing, so closing the app during the corrective sync had the
        next launch read force_full_pending=False — out of the very table the
        wipe had just emptied — and run an incremental round over an empty
        database. No content is lost by that, only the depth of the mode.

        After the wipe, not before: the latch is written into the metadata
        table clear_local_data() empties, so the other order writes it and
        then deletes it.
        """
        stub = _Stub(ui_ready=True,
                     probe=(cs.LINK_PROBE_LINKED, "5521988887777@c.us"))

        _run_live(stub, monkeypatch)

        assert stub.full_sync_latches == ["another-number-wipe"]
        names = [e if isinstance(e, str) else e[0] for e in stub.events]
        assert names.index("wipe") < names.index("full-latch")

    def test_the_deletion_is_announced_before_anything_disappears(
            self, monkeypatch):
        """Nothing else says it. The list empties on its own, which a
        screen-reader user cannot see, and the sync that follows announces a
        synchronization rather than a deletion — so the one clue would be a
        history that is simply gone. Ahead of the teardown, so the reason
        arrives before the effect."""
        stub = _Stub(ui_ready=True,
                     probe=(cs.LINK_PROBE_LINKED, "5521988887777@c.us"))

        _run_live(stub, monkeypatch)

        assert stub.spoken == ["another_number_linked_data_cleared"]
        names = [e if isinstance(e, str) else e[0] for e in stub.events]
        for step in ("audio-stopped", "list-cleared", "wipe"):
            assert names.index("spoken") < names.index(step), step

    def test_nothing_is_announced_when_nothing_is_deleted(self, monkeypatch):
        stub = _Stub(ui_ready=True,
                     probe=(cs.LINK_PROBE_LINKED, "5511999999999@c.us"))

        _run_live(stub, monkeypatch)

        assert stub.spoken == []

    def test_the_claim_is_released_when_nothing_was_wiped(self, monkeypatch):
        """The overwhelmingly common outcome. Holding _initial_sync_running
        after it would block every later sync for the rest of the session.

        Both halves are asserted, because the end state alone proves nothing:
        the stub starts at False, so "it is False afterwards" passes just as
        well against a version that never claimed it at all. The probe event
        records the flag as it was when the probe ran.
        """
        stub = _Stub(ui_ready=True,
                     probe=(cs.LINK_PROBE_LINKED, "5511999999999@c.us"))

        _run_live(stub, monkeypatch)

        assert stub.wipe_calls == 0
        assert ("probe", True) in stub.events
        assert stub._initial_sync_running is False
        assert stub.sync_starts == 0

    def test_the_claim_is_released_when_the_probe_says_nothing(self, monkeypatch):
        stub = _Stub(ui_ready=True, probe=(cs.LINK_PROBE_UNKNOWN, ""))

        _run_live(stub, monkeypatch)

        assert ("probe", True) in stub.events
        assert stub._initial_sync_running is False

    def test_a_restart_that_raises_gives_the_claim_back(self, monkeypatch):
        """handed_off is set before the call, which is the right order against
        the race — start_sync() can be running before that line returns — but
        it means a throw there leaks the claim, and a leaked claim stops every
        sync for the rest of the session."""
        stub = _Stub(ui_ready=True,
                     probe=(cs.LINK_PROBE_LINKED, "5521988887777@c.us"))

        def _boom():
            raise RuntimeError("thread creation failed")

        stub._try_start_sync_thread = _boom

        _run_live(stub, monkeypatch)

        assert stub.wipe_calls == 1
        assert stub._initial_sync_running is False


class TestTheRoundThatWasAlreadyRunningWhenPairingEnded:
    """Mid-session, a sync is nearly always in flight before this check even
    starts, and it is not started by anything this can see coming.

    The event that concludes the pairing does it synchronously and before the
    dialog closes: websocket_client's on_wpp_session_logged() calls
    on_connection_update({state: "open"}) — _set_wa_connected(True) →
    _sync_completed = False → trigger_sync_if_needed() — and then
    on_messages_set(), which goes straight to _try_start_sync_thread() without
    consulting _initial_sync_running at all. Only afterwards does
    on_pairing_complete() end the modal loop, so show_connection_dial() can
    return and start the thread this check runs on.

    That round captured self.chats holding account A's chats and is merging
    B's list onto them. Two separate consequences, and the wipe fixes neither:
    _try_start_sync_thread() answers "there is already one running" and starts
    nothing, so the corrective full sync never happens and nothing ever fires
    again; and the round keeps writing until it exits, so what it commits
    after the wipe survives it — in self.chats too, which the corrective round
    would merge onto rather than replace.
    """

    def _diverged(self):
        return _Stub(ui_ready=True,
                     probe=(cs.LINK_PROBE_LINKED, "5521988887777@c.us"))

    def test_the_wipe_still_happens_immediately(self, monkeypatch):
        """It cannot wait for a round that may take minutes: a process killed
        in between must find the database already emptied."""
        stub = self._diverged()
        in_flight = _InFlightSync(stub)

        _run_live(stub, monkeypatch)

        assert stub.wipe_calls == 1
        in_flight.finish()

    def test_no_sync_is_requested_while_that_round_is_still_alive(
            self, monkeypatch):
        """Asking there is the same as not asking: the answer is True and
        nothing starts."""
        stub = self._diverged()
        in_flight = _InFlightSync(stub)

        _run_live(stub, monkeypatch)

        assert stub.syncs_started == 0
        in_flight.finish()

    def test_the_restart_waits_for_it_and_then_really_starts(self, monkeypatch):
        stub = self._diverged()
        in_flight = _InFlightSync(stub)

        _run_live(stub, monkeypatch)
        worker = _resync_thread()
        assert worker is not None
        in_flight.finish()
        worker.join(timeout=5)

        assert not worker.is_alive()
        assert stub.syncs_started == 1
        assert stub._sync_completed is False
        assert stub._force_full_sync is True
        names = [e if isinstance(e, str) else e[0] for e in stub.events]
        assert names.index("in-flight-sync-ended") < names.index("sync")

    def test_the_wipe_runs_again_once_that_round_has_exited(self, monkeypatch):
        """The round kept writing until it exited, so everything it committed
        after the first wipe — into the database and into self.chats — is
        still there. Without this the corrective full sync merges onto the
        previous account's chats instead of replacing them, and the merge
        becomes permanent."""
        stub = self._diverged()
        in_flight = _InFlightSync(stub)

        _run_live(stub, monkeypatch)
        worker = _resync_thread()
        in_flight.finish()
        worker.join(timeout=5)

        assert stub.wipe_calls == 2
        names = [e if isinstance(e, str) else e[0] for e in stub.events]
        assert names.index("in-flight-sync-ended") < len(names) - 1
        # The second wipe is before the sync it is clearing the ground for.
        assert [i for i, n in enumerate(names) if n == "wipe"][-1] < names.index("sync")
        # And the recorded number survives that second wipe: clear_local_data()
        # drops it, this writes it back.
        assert stub.recorded_number == "5521988887777"

    def test_the_claim_is_retaken_after_the_join(self, monkeypatch):
        """The round we waited for clears _initial_sync_running in its own
        finally, whoever set it — so between its exit and the corrective sync
        the slot would be free for the 60 s incremental poll to walk into."""
        stub = self._diverged()
        in_flight = _InFlightSync(stub)

        _run_live(stub, monkeypatch)
        worker = _resync_thread()
        in_flight.finish()
        worker.join(timeout=5)

        assert stub.events[-1][0] == "sync"
        assert stub._initial_sync_running is True

    def test_a_restart_that_raises_after_the_join_gives_the_claim_back(
            self, monkeypatch):
        stub = self._diverged()
        in_flight = _InFlightSync(stub)

        def _boom():
            raise RuntimeError("thread creation failed")

        _run_live(stub, monkeypatch)
        worker = _resync_thread()
        stub._try_start_sync_thread = _boom
        in_flight.finish()
        worker.join(timeout=5)

        assert stub._initial_sync_running is False

    def test_yet_another_round_that_started_in_the_gap_is_waited_for_too(
            self, monkeypatch):
        """Bounded, but not one-shot: the claim can only be retaken after the
        join, and anything reaching _try_start_sync_thread() in that gap gets
        a thread of its own — which would leave the corrective sync refused
        again, for a round that started before the second wipe."""
        stub = self._diverged()
        first = _InFlightSync(stub)

        _run_live(stub, monkeypatch)
        worker = _resync_thread()
        second = _InFlightSync(stub)
        first.finish()
        # The worker joins `second` too; nothing else can end it.
        worker.join(timeout=1)
        assert worker.is_alive()
        assert stub.syncs_started == 0

        second.finish()
        worker.join(timeout=5)

        assert stub.syncs_started == 1
        assert stub.wipe_calls == 2

    def test_the_claim_of_a_round_we_did_not_start_is_not_released(
            self, monkeypatch):
        """The common path: same number, nothing deleted — and a
        post-pairing sync running, whose claim this check would otherwise
        clear in its finally. Left cleared, the 60 s incremental poll
        (_initial_sync_running is the only thing it consults) and F5 both walk
        straight into the initial sync and write self.chats underneath it."""
        stub = _Stub(ui_ready=True,
                     probe=(cs.LINK_PROBE_LINKED, "5511999999999@c.us"))
        in_flight = _InFlightSync(stub)

        _run_live(stub, monkeypatch)

        assert stub.wipe_calls == 0
        assert stub._initial_sync_running is True

        # And it is that round's own finally that gives it back, on its own
        # schedule — not this check's.
        in_flight.finish()
        assert stub._initial_sync_running is False

    def test_the_restart_latches_the_full_mode_on_disk_too(self, monkeypatch):
        """The second wipe empties the metadata table again, so the latch the
        first pass wrote is gone by the time this round starts."""
        stub = self._diverged()
        in_flight = _InFlightSync(stub)

        _run_live(stub, monkeypatch)
        worker = _resync_thread()
        in_flight.finish()
        worker.join(timeout=5)

        assert stub.full_sync_latches == ["another-number-wipe"] * 2
        names = [e if isinstance(e, str) else e[0] for e in stub.events]
        wipes = [i for i, n in enumerate(names) if n == "wipe"]
        latches = [i for i, n in enumerate(names) if n == "full-latch"]
        assert wipes[-1] < latches[-1]

    def test_a_wipe_that_raises_does_not_release_a_round_somebody_else_started(
            self, monkeypatch):
        """The same defect the check's own finally already avoids, two methods
        up, and it has to be avoided here for the same reason.

        This raising says nothing about who holds the slot: the second wipe
        can throw (a wx.CallAfter after the MainLoop is gone is the reachable
        one) while on_messages_set() → _try_start_sync_thread() has already
        started a round of its own in the gap. Zeroing the claim under it
        releases the 60 s incremental poll and F5 to write self.chats while it
        runs — which is the whole reason the claim exists.
        """
        stub = self._diverged()
        first = _InFlightSync(stub)

        _run_live(stub, monkeypatch)
        worker = _resync_thread()

        second = []

        def _boom(*args, **kwargs):
            second.append(_InFlightSync(stub))
            raise RuntimeError("CallAfter after the MainLoop was destroyed")

        stub._apply_another_number_wipe = _boom
        first.finish()
        worker.join(timeout=5)

        assert stub._initial_sync_running is True
        # And that round gives it back on its own schedule, as it always does.
        second[0].finish()
        assert stub._initial_sync_running is False

    def test_giving_up_after_the_bound_leaves_a_line_in_the_log(
            self, monkeypatch, caplog):
        """Three rounds born back to back in the gaps and the bound is spent:
        the second wipe then runs beside a live round, _try_start_sync_thread()
        answers "there is already one running" and starts nothing, and the
        corrective full sync never happens — the bug this whole thread exists
        to fix, back again.

        Practically unreachable, which is exactly why the line matters: with
        the loop falling out silently, the only way to diagnose it afterwards
        would be to guess.
        """
        stub = self._diverged()
        stub._ANOTHER_NUMBER_SYNC_JOIN_ROUNDS = 1
        first = _InFlightSync(stub)

        _run_live(stub, monkeypatch)
        worker = _resync_thread()
        second = _InFlightSync(stub)
        # Distinct from the first round's, so the assertion below is about the
        # line naming the round that would not end.
        second.thread.name = "the-round-that-would-not-end"
        with caplog.at_level(logging.WARNING):
            first.finish()
            worker.join(timeout=5)

        assert not worker.is_alive()
        # The state the line is there to explain.
        assert stub.syncs_started == 0
        assert "another_number_check" in caplog.text.replace("[", "").replace("]", "")
        # Named, or the next reader cannot tell which round would not end.
        assert "the-round-that-would-not-end" in caplog.text

        second.finish()

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
        # Not announced either. The user paired seconds ago and init_UI() has
        # not run, so there is no list whose emptying would need explaining —
        # which is the whole of the reason. speak_output IS reachable by then
        # (it is built earlier in the same __init__, well before this check
        # runs), so the silence is a decision rather than a limitation, and
        # the startup case is the one where the user is told least about a
        # history that is gone.
        assert stub.spoken == []


class TestBothWipesTearTheSameUIDown:
    """F5 and the account switch had a verbatim copy each of the same ~25-line
    panel teardown, and nothing held them together.

    The way that bites is quiet: a list cache added to ConversationsPanel and
    cleared in the F5 copy alone leaves the archived-conversations panel
    rendering the PREVIOUS account's rows after a switch — rows whose chats no
    longer exist, read out by the screen reader like any other.
    """

    def test_both_wipe_paths_clear_the_same_panel_caches(self, monkeypatch):
        """One teardown, so there is one place for the next cache to be added
        to. The archived panel is optional — it does not exist on every
        install — and it is the one the drifting copy used to miss."""
        _install_inline_call_after(monkeypatch)
        stub = _Stub(ui_ready=True)
        stub.archived_conversations_panel = _Panel(stub.events)

        stub._teardown_conversation_ui()

        for panel in (stub.conversations_panel,
                      stub.archived_conversations_panel):
            assert panel.chats_list == []
            assert panel.chat_names == []
            assert panel._all_chats_list == []
            assert panel._all_chat_names == []
            assert panel._displayed_jids is None
        assert stub.events.count("list-cleared") == 2
        # The two that are about the open conversation rather than the list:
        # a .msv still playing blocks its own deletion.
        assert "audio-stopped" in stub.events
        assert "conversation-closed" in stub.events

    def test_neither_wipe_path_keeps_a_copy_of_it(self):
        """Source level, because the failure is two copies drifting apart and
        no behavioural test of either one alone can see it."""
        for method in (MainWindow._resync_all_worker,
                       MainWindow._apply_another_number_wipe):
            source = inspect.getsource(method)
            assert "self._teardown_conversation_ui()" in source, method.__name__
            assert "DeleteAllItems" not in source, method.__name__


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
            # The real one drops the recorded number itself, after emptying
            # the database — which is why _on_disconnect() no longer does it
            # on its way past.
            self.settings["privateinfo"].pop("WA_phone_number_linked", None)

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
        """Through clear_local_data(), which drops it only once the database
        is actually empty. _on_disconnect() dropping it on its way past put it
        back in front of that — a window a killed process leaves half applied,
        with the key gone and account A's messages still on disk."""
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
        before, inside = tail.split("def _check_another_number():", 1)
        # The only mention is inside the function the thread is handed, never
        # in the body still running on the main thread.
        assert "_wipe_local_data_if_another_number_linked" not in before
        assert "_wipe_local_data_if_another_number_linked" in inside
        assert "target=_check_another_number" in inside

    def test_a_check_that_raises_still_reaches_the_log(self):
        """Passed as the thread target directly, an exception escaping the
        check goes to threading.excepthook — stderr, which the frozen build
        does not have. A wipe that failed would then leave nothing at all in
        log.log, the file the user attaches to the bug report, and "my history
        is still there" would be indistinguishable from the check never having
        run."""
        import ast
        import textwrap

        tree = ast.parse(textwrap.dedent(
            inspect.getsource(Connect.show_connection_dial)))
        wrapper = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "_check_another_number")
        assert len(wrapper.body) == 1
        guarded = wrapper.body[0]
        assert isinstance(guarded, ast.Try)
        handler = guarded.handlers[0]
        assert ast.unparse(handler.type) == "Exception"
        assert any(isinstance(node, ast.Call)
                   and ast.unparse(node.func) == "logging.exception"
                   for node in ast.walk(handler))

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
