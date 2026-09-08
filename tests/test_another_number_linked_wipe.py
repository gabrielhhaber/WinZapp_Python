"""Tests for the post-pairing "is this even the same phone?" check.

start_qrcode_connection(preserve_local_data=...) keys its wipe on "this
installation had a working history a moment ago", which is not the same
question as "is this the same number". Pair account A, open the dialog, switch
to QR mode and scan with phone B and the wipe is skipped: A's messages.db,
media/ and voice_messages/ survive while B's sync merges on top of them — the
merge clear_local_data() exists to prevent. WA_phone_number is not even
updated, since the only place that writes it is the phone-code path.

MainWindow._wipe_local_data_if_another_number_linked() closes that gap by
asking WPPConnect, once pairing has closed, which phone actually linked. It
deletes the user's history, so every test below is really about the same
property: it may act on proof of divergence and on nothing else. In
particular a freshly created multi-account entry is `pending` with an empty
privateinfo and no session token, and must never so much as reach the probe.

The mirror-image half is here too: an install that has only ever paired by QR
carries no WA_phone_number at all, because connect.py's phone-code flow is its
only other writer. Without recording the number the probe reports — which
deletes nothing — the comparison could never fire for that user, and that is
precisely the user this whole check exists for.
"""

import inspect

import connection_state as cs
import main as main_module
from main import (MainWindow, linked_number_differs, linked_phone_digits,
                  same_phone_for_pairing)
from ui.dialogs.connect import Connect


class _Stub:
    """Minimal stand-in for MainWindow for the divergence check.

    Carries only what the method under test touches — the settings it reads
    the stored number from, the token/db that gate the probe, and the two
    JID helpers the comparison goes through.
    """

    def __init__(self, stored_number="5511999999999", paired=True,
                 probe=(cs.LINK_PROBE_LINKED, ""), token="sess1:hash1",
                 db=object(), lid_to_phone=None):
        privateinfo = {}
        if stored_number is not None:
            privateinfo["WA_phone_number"] = stored_number
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

    def _host_device_link_probe(self):
        self.probe_calls += 1
        return self._probe

    def clear_local_data(self):
        self.wipe_calls += 1

    def save_settings(self):
        self.saved += 1

    _normalize_jid = staticmethod(MainWindow._normalize_jid)
    _wipe_local_data_if_another_number_linked = (
        MainWindow._wipe_local_data_if_another_number_linked
    )

    @property
    def stored_number(self):
        return self.settings["privateinfo"].get("WA_phone_number")


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
        assert not linked_number_differs(
            _Stub(), "5511999999999", "5511999999999@c.us")

    def test_the_brazilian_eight_nine_digit_variant_is_the_same_number(self):
        """5511999999999 ↔ 551199999999: the collapse
        MainWindow._phone_digits_equivalent() already does everywhere else in
        the app. Comparing raw strings here would
        wipe the history of a user who re-paired the number they were
        already using — the very bug this whole PR is about."""
        assert not linked_number_differs(
            _Stub(), "5511999999999", "551199999999@s.whatsapp.net")
        assert not linked_number_differs(
            _Stub(), "551199999999", "5511999999999@s.whatsapp.net")

    def test_a_genuinely_different_number_differs(self):
        assert linked_number_differs(
            _Stub(), "5511999999999", "5521988887777@s.whatsapp.net")

    def test_no_stored_number_never_differs(self):
        for stored in (None, "", "   ", 5511999999999):
            assert not linked_number_differs(
                _Stub(), stored, "5521988887777@s.whatsapp.net"), stored

    def test_nothing_readable_from_the_server_never_differs(self):
        for linked in (None, "", "1234", "120363000000000000@g.us",
                       "182736450192837@lid"):
            assert not linked_number_differs(
                _Stub(), "5511999999999", linked), linked


class TestWipeOnlyOnProvenDivergence:
    def test_a_different_number_wipes_and_takes_over_the_stored_number(self):
        stub = _Stub(probe=(cs.LINK_PROBE_LINKED, "5521988887777@c.us"))

        stub._wipe_local_data_if_another_number_linked()

        assert stub.wipe_calls == 1
        # Left at the old number, the next pairing of THIS one would look
        # like another divergence and wipe a second time.
        assert stub.stored_number == "5521988887777"
        assert stub.saved == 1

    def test_the_same_number_keeps_everything(self):
        stub = _Stub(probe=(cs.LINK_PROBE_LINKED, "5511999999999@c.us"))

        stub._wipe_local_data_if_another_number_linked()

        assert stub.wipe_calls == 0
        assert stub.stored_number == "5511999999999"

    def test_the_brazilian_variant_of_the_same_number_keeps_everything(self):
        stub = _Stub(stored_number="5511999999999",
                     probe=(cs.LINK_PROBE_LINKED, "551199999999@s.whatsapp.net"))

        stub._wipe_local_data_if_another_number_linked()

        assert stub.wipe_calls == 0

    def test_a_new_multi_account_entry_is_never_touched_or_even_probed(self):
        """`pending` state: empty privateinfo, no WA_phone_number, no
        `paired`, and — what actually stops it here — no session token.
        Adding an account to the manager must not be able to delete anything,
        in this account or any other, nor go asking a server it holds no
        session on."""
        stub = _Stub(stored_number=None, paired=False, token="")

        stub._wipe_local_data_if_another_number_linked()

        assert stub.wipe_calls == 0
        assert stub.probe_calls == 0
        assert stub.stored_number is None

    def test_a_server_answer_we_cannot_read_keeps_everything(self):
        for linked in ("", None, "not-a-number", "182736450192837@lid"):
            stub = _Stub(probe=(cs.LINK_PROBE_LINKED, linked))

            stub._wipe_local_data_if_another_number_linked()

            assert stub.wipe_calls == 0, linked
            assert stub.stored_number == "5511999999999"

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


class TestLearningTheNumberOfAQrOnlyInstall:
    """WA_phone_number is written in exactly one other place — connect.py's
    phone-code flow — so an install that has only ever paired by QR has none.

    Its session drops, _show_repair_dialog() reopens the pairing dialog with
    `paired` and WA_token intact, somebody else's phone scans the code, and
    B's sync merges straight onto A's database: the scenario this whole check
    exists for, and the one it could not see, because an empty stored number
    returned before anything was compared. Recording what the probe reports
    deletes nothing by itself and arms the comparison from the second pairing
    onwards.
    """

    def test_a_linked_number_is_recorded_without_deleting_anything(self):
        stub = _Stub(stored_number=None,
                     probe=(cs.LINK_PROBE_LINKED, "5511999999999@c.us"))

        stub._wipe_local_data_if_another_number_linked()

        assert stub.wipe_calls == 0
        assert stub.stored_number == "5511999999999"
        assert stub.saved == 1

    def test_an_empty_stored_number_is_treated_the_same(self):
        stub = _Stub(stored_number="",
                     probe=(cs.LINK_PROBE_LINKED, "5511999999999@c.us"))

        stub._wipe_local_data_if_another_number_linked()

        assert stub.wipe_calls == 0
        assert stub.stored_number == "5511999999999"

    def test_the_next_pairing_can_then_see_a_different_phone(self):
        """The point of recording it: the very next divergence is caught."""
        stub = _Stub(stored_number=None,
                     probe=(cs.LINK_PROBE_LINKED, "5511999999999@c.us"))
        stub._wipe_local_data_if_another_number_linked()
        assert stub.wipe_calls == 0

        stub._probe = (cs.LINK_PROBE_LINKED, "5521988887777@c.us")
        stub._wipe_local_data_if_another_number_linked()

        assert stub.wipe_calls == 1
        assert stub.stored_number == "5521988887777"

    def test_nothing_is_recorded_when_the_probe_has_no_verdict(self):
        for outcome in (cs.LINK_PROBE_UNKNOWN, cs.LINK_PROBE_UNLINKED):
            stub = _Stub(stored_number=None,
                         probe=(outcome, "5511999999999@c.us"))

            stub._wipe_local_data_if_another_number_linked()

            assert stub.stored_number is None, outcome
            assert stub.saved == 0, outcome

    def test_nothing_is_recorded_from_an_answer_we_cannot_read(self):
        """An unbridged @lid's digits are not a phone number — storing them
        would make the NEXT pairing of the real number look like a divergence,
        and wipe."""
        for linked in ("182736450192837@lid", "1234", "not-a-number",
                       "120363000000000000@g.us"):
            stub = _Stub(stored_number=None, probe=(cs.LINK_PROBE_LINKED, linked))

            stub._wipe_local_data_if_another_number_linked()

            assert stub.stored_number is None, linked
            assert stub.saved == 0, linked


class TestSamePhoneForPairing:
    """The one predicate both destructive decisions read — this check and
    Connect._can_reuse_existing_session(), whose "no" also ends in
    clear_local_data()."""

    def test_identical_numbers_match(self):
        assert same_phone_for_pairing("5511999999999", "5511999999999")

    def test_the_brazilian_ninth_digit_matches(self):
        assert same_phone_for_pairing("5511999999999", "551199999999")
        assert same_phone_for_pairing("551199999999", "5511999999999")

    def test_the_argentinian_nine_matches(self):
        """54 9 11 XXXX-XXXX is what getWid() reports; users write the number
        without the 9. Never measured disagreeing, but being wrong here
        deletes the history on its own and es-ES is a shipped locale — so
        this errs towards NOT deleting."""
        assert same_phone_for_pairing("5491123456789", "541123456789")

    def test_the_mexican_one_matches(self):
        assert same_phone_for_pairing("5215512345678", "525512345678")

    def test_two_genuinely_different_numbers_do_not_match(self):
        assert not same_phone_for_pairing("5511999999999", "5521988887777")
        assert not same_phone_for_pairing("541123456789", "541123456788")

    def test_a_different_country_code_never_matches(self):
        """The extra digit has to sit in the prefix, not replace the country
        code — otherwise two unrelated people collapse into one."""
        assert not same_phone_for_pairing("5491123456789", "441123456789")

    def test_a_different_subscriber_number_never_matches(self):
        """Same country code and one digit longer, but the last 8 differ."""
        assert not same_phone_for_pairing("5491123456789", "541123456780")

    def test_two_digits_apart_never_matches(self):
        assert not same_phone_for_pairing("54911123456789", "541123456789")


class TestBothCallSitesStayWired:
    """Nothing else in the suite would notice the check being dropped: it is
    called for its side effect, from two places that cannot be constructed in
    a test (a wx.Frame's __init__ and a method ending in ShowModal()), and
    "no wipe happened" is also what a removed call looks like. Source level,
    same approach as test_opening_the_dialog_drops_a_previous_dialogs_capture
    above."""

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
