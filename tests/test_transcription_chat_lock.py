"""A transcription never reveals a chat the locked-chats vault has hidden.

The case this exists for: the vault is open with a short auto-lock, the user
presses Alt+Shift+T on a long voice note of a locked chat and waits without
touching a key. The timer fires inside the progress dialog's modal loop, the
vault closes the conversation and says so — and minutes later the run
finishes. Before this, the result window opened over the note of the chat the
app had just announced as locked, and read its text out.

So every way the flow shows or says anything about a result asks the vault
again at that moment (`MainWindow.is_chat_hidden_by_vault()`): the fresh
result, an empty one, the re-run on the processor, "Ver transcrição", and the
focus move back to the message — and "Inserir na mensagem" once the window
closes, since the vault can close while the user is reading in it. The result
is still kept when storing still finds the message with the conversation
closed, because keeping it reveals nothing and the user waited for it; one
sentence says whether it was.

The vault is the real one: `ChatLockVault` configured and holding the chat,
and the real `_on_chat_lock_timeout()` / `lock_chat_vault()` bound onto the
flow suite's window stub, fired from inside the job the way the timer fires
inside the modal loop. The harness — the fake dialogs, the job, the panel —
is tests/test_transcription_flow.py's, fixture included.
"""

import types

import pytest

from core.chat_lock_vault import ChatLockVault, jid_fingerprint
from core.transcription import audio_prep, device, errors, job as job_module, narration
from core.transcription import stored as stored_transcription
from main import MainWindow
from tests.test_transcription_flow import (  # noqa: F401  (world, own_temp_dir are fixtures)
    _CONTACT,
    _FakeMessageDialog,
    _FakeProgressDialog,
    _FakeResultDialog,
    _Field,
    _JID,
    _List,
    _Panel,
    _Speech,
    _result,
    _row_focus,
    _saved,
    _start,
    _succeed_on_the_processor,
    _t,
    own_temp_dir,
    world,
)
from tests.test_transcription_message_run import RESULT, _fail_on_gpu_with_handover
from ui import transcription_flow
from ui.conversations import ConversationsPanel

HIDDEN = transcription_flow.HIDDEN_BY_VAULT_I18N_KEY
HIDDEN_NOT_SAVED = transcription_flow.HIDDEN_BY_VAULT_NOT_SAVED_I18N_KEY
INSERT_REFUSED = transcription_flow.INSERT_REFUSED_BY_VAULT_I18N_KEY


# ── The vault around the flow ────────────────────────────────────────────────


class _VaultSpeech(_Speech):
    """The flow suite's speech, which lets exactly one sentence interrupt: the
    vault's own "closed automatically", said with interrupt=True as in the
    app. Nothing from the transcription may."""

    def output(self, text, interrupt=False):
        assert not interrupt or text == _t("chat_lock_timed_out"), text
        self.spoken.append(text)


class _ShowableList(_List):
    def Show(self, show=True):
        self.calls.append(("Show",))


class _Layout:
    def Layout(self):
        pass


class _VaultPanel(_Panel):
    """The flow suite's panel plus what lock_chat_vault() touches on it.

    On screen from the start: the case is a user waiting on a note of the
    locked chat they are looking at, and lock_chat_vault() only sends them
    back to the main list when the vault closed under their eyes. That return
    is the real show_chat_panel(), so the last focus move these tests read is
    the one the app makes.
    """

    def __init__(self, main_window, messages):
        super().__init__(main_window, messages)
        self.conversations_label = _ShowableList()
        self.conversations_list = _ShowableList()
        self.chats_list = []
        self._last_list_focus_jid = None
        self._last_open_jid = _JID
        self.shown = True

    _restore_conversation_selection = ConversationsPanel._restore_conversation_selection
    show_chat_panel = ConversationsPanel.show_chat_panel
    _list_panel_for = ConversationsPanel._list_panel_for
    _apply_panel_layout = ConversationsPanel._apply_panel_layout
    _log_panel_switch = ConversationsPanel._log_panel_switch
    _detail_on_screen = ConversationsPanel._detail_on_screen

    def close_conversation_for_panel_switch(self):
        # What _close_conversation_core() does to the state the flow reads:
        # the conversation goes; _sorted_messages and the message list's own
        # shown flag are left exactly as they were, which is why a focus move
        # back "to the message" would still find it.
        self.conversation = None

    def Show(self, show=True):
        self.shown = show

    def IsShown(self):
        return self.shown

    def Layout(self):
        pass


@pytest.fixture
def vault(world):
    """`world`, with a configured vault holding the chat, open."""
    mw = world.main_window
    mw.speak_output = _VaultSpeech()
    mw.content_panel = _Layout()
    for name in ("_on_chat_lock_timeout", "lock_chat_vault",
                 "_cancel_chat_lock_timeout", "_refresh_chat_lock_navigation"):
        setattr(mw, name, types.MethodType(getattr(MainWindow, name), mw))
    mw._chat_lock_vault = ChatLockVault(mw.key)
    mw._chat_lock_vault.configure("246810", "codigo-secreto")
    mw._chat_lock_vault.lock_chat(_JID)
    mw._chat_lock_unlocked = True

    panel = _VaultPanel(mw, world.panel._sorted_messages)
    world.panel = panel
    mw.conversations_panel = panel
    return world


def _locks_midway(world, finish=lambda job: job.on_finished(RESULT, None)):
    """A run during which the auto-lock timer fires, then `finish(job)`.

    Fired from inside the job, which the fake progress dialog runs from its
    run(): the same place the real timer fires, inside the modal loop.
    """
    def _script(job):
        job.device, job.device_reason = device.DEVICE_CPU, device.REASON_NO_CUDA_FOUND
        job.on_phase(job_module.PHASE_LOADING_MODEL)
        job.on_phase(job_module.PHASE_TRANSCRIBING)
        world.main_window._on_chat_lock_timeout()
        finish(job)
    return _script


def _nothing_of(text, spoken):
    return all(text not in sentence for sentence in spoken)


# ── The case that was found ──────────────────────────────────────────────────


class TestTheVaultClosingDuringTheRun:
    def test_no_window_and_nothing_of_the_note_is_said(self, vault):
        vault.script = [_locks_midway(vault)]
        _start(vault)

        mw = vault.main_window
        assert _FakeResultDialog.made == []
        assert mw.speak_output.spoken == [_t("chat_lock_timed_out"), _t(HIDDEN)]
        assert _nothing_of(RESULT.text, mw.speak_output.spoken)
        assert _t(narration.FINISHED_I18N_KEY) not in mw.speak_output.spoken
        assert mw.error_sound.played == 0

    def test_the_vault_really_closed_the_conversation(self, vault):
        """The state the flow is asked about is the one the real
        lock_chat_vault() leaves, not one this suite set by hand."""
        vault.script = [_locks_midway(vault)]
        _start(vault)
        assert vault.main_window._chat_lock_unlocked is False
        assert vault.panel.conversation is None

    def test_it_is_kept_for_when_the_vault_is_opened(self, vault):
        vault.script = [_locks_midway(vault)]
        _start(vault)
        saved = stored_transcription.saved_transcription(vault.target)
        assert saved["text"] == RESULT.text
        [call] = vault.main_window.db.calls
        assert call[:3] == ("set", (_JID,), vault.target["key"]["id"])

    def test_the_focus_stays_where_the_vault_put_it(self, vault):
        vault.script = [_locks_midway(vault)]
        _start(vault)
        panel = vault.panel
        # The vault's own restore: the chat list, focused.
        assert panel.conversations_list.calls[-1] == ("SetFocus",)
        # And nothing afterwards pulled it back into the closed conversation.
        assert panel.messages_list.calls == []
        assert panel.message_field.focused is False
        assert panel.conversation is None

    def test_an_empty_result_says_neither_no_speech_nor_its_notes(self, vault):
        """"No speech was found" and the voice-filter warning are about the
        recording too. Nothing is kept for an empty result, and the sentence
        says so."""
        empty = _result(text="", vad_used=False)
        vault.script = [_locks_midway(vault, lambda job: job.on_finished(empty, None))]
        _start(vault)

        spoken = vault.main_window.speak_output.spoken
        assert spoken == [_t("chat_lock_timed_out"), _t(HIDDEN_NOT_SAVED)]
        assert _t(narration.NO_SPEECH_I18N_KEY) not in spoken
        assert _nothing_of(_t(narration.VAD_UNAVAILABLE_I18N_KEY), spoken)
        assert stored_transcription.TRANSCRIPTION_KEY not in vault.target
        assert vault.panel.messages_list.calls == []
        assert vault.main_window.error_sound.played == 1

    def test_a_note_deleted_for_everyone_meanwhile_is_only_not_kept(self, vault):
        def _revoked(job):
            vault.target["message"] = {"protocolMessage": {"type": 3}}
            vault.target["messageType"] = "protocolMessage"
            job.on_finished(RESULT, None)

        vault.script = [_locks_midway(vault, _revoked)]
        _start(vault)
        spoken = vault.main_window.speak_output.spoken
        assert spoken[-1] == _t(HIDDEN_NOT_SAVED)
        assert _t(transcription_flow.WITHDRAWN_I18N_KEY) not in spoken
        assert _FakeResultDialog.made == []

    def test_storing_that_raises_shows_nothing_either(self, vault):
        def _broken(jid, msg_id, value):
            raise RuntimeError("store exploded")

        vault.main_window.store_message_transcription = _broken
        vault.script = [_locks_midway(vault)]
        _start(vault)
        assert _FakeResultDialog.made == []
        assert vault.main_window.speak_output.spoken[-1] == _t(HIDDEN_NOT_SAVED)

    def test_a_failure_is_still_said_and_the_focus_still_stays(self, vault):
        """A failure describes the run, not the note: said as always. Only the
        focus move that goes with it is dropped."""
        def _fail(job):
            job.on_finished(None, errors.TranscriptionError(errors.FFMPEG_FAILED, "x"))

        vault.script = [_locks_midway(vault, _fail)]
        _start(vault)
        spoken = vault.main_window.speak_output.spoken
        assert spoken == [_t("chat_lock_timed_out"), _t("transcription_error_ffmpeg_failed")]
        assert vault.panel.messages_list.calls == []

    def test_a_cancel_is_still_said(self, vault):
        def _cancel(job):
            job.on_finished(None, errors.TranscriptionError(errors.CANCELLED, "x"))

        vault.script = [_locks_midway(vault, _cancel)]
        _start(vault)
        assert vault.main_window.speak_output.spoken[-1] == _t("transcription_error_cancelled")
        assert vault.panel.messages_list.calls == []

    def test_a_note_keyed_by_an_unbridged_lid_is_hidden_by_its_chat(self, vault):
        """The message's own key names an @lid the window has no phone for,
        while the chat it is shown in — the one locked — is the phone: only
        asking of the chat the flow was started in hides it."""
        lid = "123456789012345@lid"
        vault.target["key"]["remoteJid"] = lid
        vault.script = [_locks_midway(vault)]
        _start(vault)

        mw = vault.main_window
        # What makes this the case: the key alone is not hidden.
        assert mw.is_chat_hidden_by_vault(lid) is False
        assert _FakeResultDialog.made == []
        assert mw.speak_output.spoken == [_t("chat_lock_timed_out"), _t(HIDDEN)]
        assert _nothing_of(RESULT.text, mw.speak_output.spoken)
        assert vault.panel.messages_list.calls == []

    def test_a_note_loaded_through_older_messages_is_said_not_kept(self, vault):
        """Such a note is only in the panel's lists, not in the chat's records,
        and the panel offers its lists to storing only for the conversation
        open in it — which the vault has just closed. So it is not kept
        (SAVE_MISSING), and the sentence says so."""
        records = vault.main_window.chats[_JID]["messages"]["messages"]["records"]
        records.remove(vault.target)
        vault.script = [_locks_midway(vault)]
        _start(vault)

        mw = vault.main_window
        assert _FakeResultDialog.made == []
        assert mw.speak_output.spoken == [_t("chat_lock_timed_out"), _t(HIDDEN_NOT_SAVED)]
        assert stored_transcription.TRANSCRIPTION_KEY not in vault.target
        assert mw.db.calls == []
        assert mw.error_sound.played == 1

    def test_with_the_vault_open_the_same_note_is_kept(self, vault):
        """The other half of the one above: what loses the note is the
        conversation the vault closed, not where the note was loaded from."""
        records = vault.main_window.chats[_JID]["messages"]["messages"]["records"]
        records.remove(vault.target)
        _start(vault)
        assert stored_transcription.saved_transcription(vault.target)["text"] == RESULT.text
        [dialog] = _FakeResultDialog.made
        assert _t("transcription_not_saved_missing") not in dialog.notes


class TestTheProcessorReRun:
    def _gpu_fails_while_the_vault_closes(self, vault):
        """The first run fails on the card with the converted audio handed
        over — which is what makes the flow offer the processor — and the
        vault closes during it; the re-run succeeds."""
        path = vault.temp / "converted.wav"
        path.write_bytes(b"RIFF")
        prepared = audio_prep.PreparedAudio(path=str(path), duration_seconds=4.0)

        def _first_with_handover(job):
            job.handover_to_give = prepared
            _fail_on_gpu_with_handover(job)

        vault.script = [_locks_midway(vault, _first_with_handover), _succeed_on_the_processor]

    def test_accepted_the_re_run_shows_nothing_and_is_kept(self, vault):
        self._gpu_fails_while_the_vault_closes(vault)
        _FakeMessageDialog.answer = transcription_flow.wx.ID_YES
        _start(vault)
        assert len(vault.jobs) == 2
        assert _FakeResultDialog.made == []
        assert vault.main_window.speak_output.spoken[-1] == _t(HIDDEN)
        assert stored_transcription.saved_transcription(vault.target)["text"] == RESULT.text
        assert vault.panel.messages_list.calls == []

    def test_declined_the_focus_stays(self, vault):
        self._gpu_fails_while_the_vault_closes(vault)
        _start(vault)
        assert len(vault.jobs) == 1
        assert vault.panel.messages_list.calls == []


# ── An open vault, or a chat that is not locked, change nothing ──────────────


class TestNothingChangesOtherwise:
    def test_a_locked_chat_with_the_vault_open_opens_as_always(self, vault):
        _start(vault)
        [dialog] = _FakeResultDialog.made
        assert dialog.text == RESULT.text
        assert _CONTACT in dialog.title
        assert _row_focus(vault.panel) == [("Focus", 1)]
        assert _t(HIDDEN) not in vault.main_window.speak_output.spoken

    def test_a_chat_that_is_not_locked_opens_with_the_vault_closed(self, vault):
        mw = vault.main_window
        mw._chat_lock_vault.unlock_chat(_JID)
        mw._chat_lock_unlocked = False
        vault.script = [_locks_midway(vault)]
        _start(vault)
        [dialog] = _FakeResultDialog.made
        assert dialog.text == RESULT.text
        assert _row_focus(vault.panel) == [("Focus", 1)]


# ── The other ways to the text ───────────────────────────────────────────────


def _close_the_vault(vault):
    vault.main_window.lock_chat_vault(silent=True)
    vault.main_window.speak_output.spoken.clear()


class TestAStoredTranscriptionStaysBehindTheVault:
    """Unreachable from the conversation — the vault closes it — and checked
    anyway, since the flow is the last thing between the text and the
    screen."""

    def test_the_shortcut_and_view_transcription_reveal_nothing(self, vault):
        _saved(vault, vad_used=False)
        _close_the_vault(vault)
        transcription_flow.open_or_transcribe(vault.panel, vault.target)
        assert _FakeResultDialog.made == []
        assert vault.main_window.speak_output.spoken == []
        assert vault.panel.messages_list.calls == []
        assert vault.jobs == []

    def test_transcribing_again_starts_nothing(self, vault):
        _saved(vault)
        _close_the_vault(vault)
        transcription_flow.transcribe_message(vault.panel, vault.target)
        assert _FakeProgressDialog.made == []
        assert vault.jobs == []
        assert vault.main_window.speak_output.spoken == []

    def test_deleting_asks_nothing_and_deletes_nothing(self, vault, monkeypatch):
        _saved(vault)
        _close_the_vault(vault)

        def _no_dialog(*args, **kwargs):
            raise AssertionError("a question was asked about a hidden note")

        monkeypatch.setattr(transcription_flow.wx, "MessageDialog", _no_dialog)
        transcription_flow.delete_transcription(vault.panel, vault.target)
        assert vault.main_window.speak_output.spoken == []
        assert not [c for c in vault.main_window.db.calls if c[0] == "delete"]

    def test_the_window_itself_refuses(self, vault):
        """The one place the text reaches the screen, asked directly: a
        future path that forgets to ask is still caught here."""
        _close_the_vault(vault)
        flow = transcription_flow.MessageTranscriptionFlow(vault.panel, vault.target)
        announcement = narration.outcome_announcement(RESULT, None)
        flow._open_result_window(RESULT, announcement, ())
        assert _FakeResultDialog.made == []


# ── The vault closing while the result window is open ────────────────────────


class _LockingResultDialog(_FakeResultDialog):
    """The result window during whose reading the auto-lock timer fires.

    Keys pressed in a dialog never reach the vault's timer (its hook is on the
    main window), so a user reading a long text in it is idle as far as the
    vault knows. Fired from run(), where the real timer fires: inside the
    window's modal loop.
    """

    def __init__(self, parent, main_window, *args, **kwargs):
        super().__init__(parent, main_window, *args, **kwargs)
        self._main_window = main_window

    def run(self):
        self._main_window._on_chat_lock_timeout()
        return super().run()


class TestInsertingAfterTheVaultClosed:
    """"Inserir na mensagem" pressed after the vault closed the conversation.

    The message field is not the chat's: nothing empties it when the
    conversation closes, and the next one opened — whoever it is with — shows
    what it holds, one Enter away from being sent to them.
    """

    @pytest.fixture(autouse=True)
    def _locks_while_reading(self, vault, monkeypatch):
        monkeypatch.setattr(transcription_flow, "TranscriptionResultDialog", _LockingResultDialog)
        _FakeResultDialog.choose_insert = True
        vault.panel.message_field = _Field("ok, então", caret=2)

    def test_nothing_is_written_and_the_focus_is_not_moved(self, vault):
        _start(vault)

        field = vault.panel.message_field
        assert field.value == "ok, então"
        assert field.focused is False
        assert vault.panel.conversation is None
        assert vault.panel.messages_list.calls == []
        # The vault's own restore is the last focus move.
        assert vault.panel.conversations_list.calls[-1] == ("SetFocus",)
        spoken = vault.main_window.speak_output.spoken
        assert spoken == [_t("chat_lock_timed_out"), _t(INSERT_REFUSED)]
        assert _nothing_of(RESULT.text, spoken)

    def test_the_window_was_really_open_when_it_closed(self, vault):
        """The case is the one reported: the text had been shown, and only
        the insertion is refused."""
        _start(vault)
        [dialog] = _FakeResultDialog.made
        assert dialog.text == RESULT.text
        assert dialog.insert_requested is True

    def test_a_fresh_transcription_says_the_insertion_was_refused(self, vault):
        """The run's own ending was the window, and it was kept: what is said
        after it is the answer to Insert — with the error sound, since what
        was asked for did not happen — not the end-of-run sentence."""
        _start(vault)
        mw = vault.main_window
        assert stored_transcription.saved_transcription(vault.target)["text"] == RESULT.text
        assert mw.speak_output.spoken[-1] == _t(INSERT_REFUSED)
        assert _t(HIDDEN) not in mw.speak_output.spoken
        assert mw.error_sound.played == 1

    def test_a_stored_transcription_opened_again_is_refused_too(self, vault):
        """Nothing ran: "the transcription finished" would be false. Only the
        refusal is said, and it sounds like one."""
        _saved(vault, text="o texto guardado")
        transcription_flow.open_or_transcribe(vault.panel, vault.target)
        mw = vault.main_window
        assert vault.jobs == []
        assert vault.panel.message_field.value == "ok, então"
        assert mw.speak_output.spoken == [_t("chat_lock_timed_out"), _t(INSERT_REFUSED)]
        assert mw.error_sound.played == 1

    def test_a_text_that_was_not_kept_is_refused_the_same_way(self, vault):
        """Storing raised: the window's note already said it was not kept, so
        the refusal is the same sentence, and it promises nothing about the
        vault."""
        def _broken(jid, msg_id, value):
            raise RuntimeError("store exploded")

        vault.main_window.store_message_transcription = _broken
        _start(vault)
        spoken = vault.main_window.speak_output.spoken
        assert vault.panel.message_field.value == "ok, então"
        assert spoken[-1] == _t(INSERT_REFUSED)
        assert _t(HIDDEN_NOT_SAVED) not in spoken
        # Two, not one: the window's "could not be saved" note sounds as it
        # opens, and the refused Insert sounds again afterwards.
        assert vault.main_window.error_sound.played == 2

    def test_with_the_vault_still_open_it_inserts_as_always(self, vault, monkeypatch):
        monkeypatch.setattr(transcription_flow, "TranscriptionResultDialog", _FakeResultDialog)
        _start(vault)
        field = vault.panel.message_field
        assert field.value == f"ok {RESULT.text}, então"
        assert field.focused
        assert _t(INSERT_REFUSED) not in vault.main_window.speak_output.spoken

    def test_a_chat_that_is_not_locked_still_inserts(self, vault):
        """The vault closing is not the reason: the chat being hidden is."""
        vault.main_window._chat_lock_vault.unlock_chat(_JID)
        _start(vault)
        field = vault.panel.message_field
        assert field.value == f"ok {RESULT.text}, então"
        assert field.focused
        assert vault.main_window._chat_lock_unlocked is False


# ── The vault's own rule ─────────────────────────────────────────────────────


class _VaultWindow:
    """Only what is_chat_hidden_by_vault() reads, all of it real."""

    _normalize_jid = staticmethod(MainWindow._normalize_jid)
    _chat_lock_candidates = MainWindow._chat_lock_candidates
    _chat_lock_vault_holds = MainWindow._chat_lock_vault_holds
    _archived_lookup_jids = MainWindow._archived_lookup_jids
    _phone_lock_fp = MainWindow._phone_lock_fp
    is_chat_phone_locked = MainWindow.is_chat_phone_locked
    is_chat_locked = MainWindow.is_chat_locked
    is_chat_hidden_by_vault = MainWindow.is_chat_hidden_by_vault

    def __init__(self, key):
        self.key = key
        self.chats = {}
        self._lid_to_phone = {}
        self._phone_to_lid = {}
        self._phone_locked_chats = set()
        self._chat_lock_vault = ChatLockVault(key)
        self._chat_lock_vault.configure("246810", "codigo-secreto")
        self._chat_lock_vault.lock_chat(_JID)
        self._chat_lock_fingerprints = set()
        self._chat_lock_unlocked = False


class TestIsChatHiddenByVault:
    def test_a_locked_chat_while_the_vault_is_closed(self, fernet_key):
        assert _VaultWindow(fernet_key).is_chat_hidden_by_vault(_JID) is True

    def test_not_while_the_vault_is_open(self, fernet_key):
        mw = _VaultWindow(fernet_key)
        mw._chat_lock_unlocked = True
        assert mw.is_chat_hidden_by_vault(_JID) is False

    def test_never_a_chat_that_is_not_locked(self, fernet_key):
        assert _VaultWindow(fernet_key).is_chat_hidden_by_vault(
            "5511911112222@s.whatsapp.net") is False

    def test_the_lid_of_a_locked_chat_is_the_same_chat(self, fernet_key):
        mw = _VaultWindow(fernet_key)
        mw._lid_to_phone = {"123456789012345@lid": _JID}
        assert mw.is_chat_hidden_by_vault("123456789012345@lid") is True

    def test_an_unreadable_vault_still_hides_by_its_fingerprint(self, fernet_key):
        mw = _VaultWindow(fernet_key)
        mw._chat_lock_vault = None
        mw._chat_lock_fingerprints = {jid_fingerprint(fernet_key, _JID)}
        assert mw.is_chat_hidden_by_vault(_JID) is True

    def test_no_jid_is_no_chat(self, fernet_key):
        assert _VaultWindow(fernet_key).is_chat_hidden_by_vault("") is False

    def test_a_chat_locked_only_on_the_phone_is_hidden_too(self, fernet_key):
        """WhatsApp's Chat Lock set on the phone keeps a chat behind the same
        vault (main_window/phone_chat_lock.py), so its transcription stays
        off the screen while the vault is closed, like one WinZapp locked."""
        mw = _VaultWindow(fernet_key)
        other = "5511911112222@s.whatsapp.net"
        mw._phone_locked_chats = {mw._phone_lock_fp(other)}
        assert mw.is_chat_hidden_by_vault(other) is True
        mw._chat_lock_unlocked = True
        assert mw.is_chat_hidden_by_vault(other) is False


class TestLockChatVaultAsksTheSameRule:
    """lock_chat_vault() closes the open conversation by is_chat_hidden_by_vault()
    — the rule the transcription asks — so the two cannot disagree."""

    def test_it_closes_a_locked_conversation(self, vault):
        vault.main_window.lock_chat_vault(silent=True)
        assert vault.panel.conversation is None

    def test_it_leaves_a_conversation_that_is_not_locked_open(self, vault):
        vault.main_window._chat_lock_vault.unlock_chat(_JID)
        vault.main_window.lock_chat_vault(silent=True)
        assert vault.panel.conversation == {"remoteJid": _JID}


class TestLockChatVaultClosesThePinnedWindow:
    """The pinned messages window of a chat the vault hides (#848) closes with
    the vault; left open, it kept listing that chat's pins."""

    def _pinned_window(self, vault):
        from ui.conversation_panel.pinned_messages import PinnedMessagesMixin
        closed = []
        panel = vault.panel
        panel.close_locked_pinned_window = types.MethodType(
            PinnedMessagesMixin.close_locked_pinned_window, panel)
        panel._pinned_window = (types.SimpleNamespace(
            close_for_lock=lambda: closed.append(True)), _JID)
        return closed

    def test_a_locked_chats_window_closes(self, vault):
        closed = self._pinned_window(vault)
        vault.main_window.lock_chat_vault(silent=True)
        assert closed == [True]

    def test_a_window_of_a_chat_that_is_not_locked_stays(self, vault):
        closed = self._pinned_window(vault)
        vault.main_window._chat_lock_vault.unlock_chat(_JID)
        vault.main_window.lock_chat_vault(silent=True)
        assert closed == []
