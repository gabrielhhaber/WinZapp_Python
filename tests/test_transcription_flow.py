"""Transcribing a message from the conversation: what the user hears, where
the focus lands, and what is left on the disk.

`ui.transcription_flow.MessageTranscriptionFlow` is the wx half of part 6b.
The ways it goes wrong are the ones a sighted tester does not notice:

* **The focus comes back to the wrong message.** A transcription takes
  minutes and messages keep arriving; the row the user was on is some other
  message by the end. The message is found again by its id.

* **The voice-filter warning is not said.** It is the only degradation a
  listener cannot detect, so it is spoken every time — in the finished
  sentence and in the empty-result sentence alike.

* **The device is announced before it is known.** Read before the job's
  probe, the reason is None, which narration turns into no sentence; read at
  the loading phase it is the real one. It belongs in that phase's line and
  nowhere else.

* **A failure told twice, or not at all.** The media download is the trap:
  the panel's usual helper reports its own failures in a message box.

* **A private recording left behind.** The decrypted temporary, and the
  converted audio a failed GPU run hands over for a re-run on the processor,
  whether the re-run is accepted or declined.

`MessageTranscriptionFlow` is a plain object, so it is built directly; the
panel it wires into is a stub carrying only what the flow touches, with
ConversationsPanel's own `_find_index_by_msg_id()` / `_focus_message_row()`
bound onto it. The dialogs are fakes defined here — the real ones may not be
put on the desktop by this suite (tests/test_no_desktop_visible_windows.py).
The run behind them is the real `MessageTranscription`, with the job, the
probe and the model listing replaced at their module-level defaults.
"""

import ast
import errno
import inspect
import json
import logging
import os
import pathlib
import sys

from tests.locales import load_strings, registered_locale_codes
import pytest

from core.transcription import (
    audio_prep,
    backend as backend_module,
    device,
    errors,
    external_models,
    job as job_module,
    message_run,
    model_names,
    model_store,
    narration,
    precision,
    preferences,
)
from core.transcription import stored as stored_transcription
from core.transcription.backend import TranscriptionResult
from tests.god_modules import (
    conversations_source_files,
    main_window_source_files,
    status_panel_source_files,
)
from tests.test_transcription_message_run import (
    RESULT,
    WAV,
    _FakeJob,
    _fail_on_gpu_with_handover,
    _leftovers,
    _scan,
    _succeed,
)
from main import MainWindow
from status_panel import StatusPanel
from ui import transcription_flow
from ui.conversations import ConversationsPanel
from ui.dialogs import transcription_result
from ui.transcription_flow import MessageTranscriptionFlow

_ID = "3EB0FEEDFACE00112233"
_CONTACT = "Ana Souza"
_JID = "5511988887777@s.whatsapp.net"

_REPO = pathlib.Path(__file__).resolve().parent.parent


LOCALES = registered_locale_codes()


class _I18n:
    def __init__(self, locale="pt-BR"):
        self.language = locale
        self._table = load_strings(locale)

    def t(self, key):
        return self._table.get(key, key)


I18N = _I18n()


def _t(key, **values):
    return I18N.t(key).format(**values)


# ── Stubs ────────────────────────────────────────────────────────────────────


class _Speech:
    def __init__(self):
        self.spoken = []

    def output(self, text, interrupt=False):
        assert not interrupt, "transcription speech never interrupts"
        self.spoken.append(text)


class _Sound:
    def __init__(self):
        self.played = 0

    def play(self):
        self.played += 1


class _Inline:
    """The background executor, run on the spot."""

    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


class _FakeDb:
    """DatabaseBridge's two transcription calls, recorded. `fail` raises."""

    def __init__(self):
        self.calls = []
        self.fail = False

    def set_message_transcription(self, jids, msg_id, value):
        self.calls.append(("set", tuple(jids), msg_id, value))
        if self.fail:
            raise TimeoutError("db busy")
        return True

    def delete_message_transcription(self, jids, msg_id, deleted_at):
        self.calls.append(("delete", tuple(jids), msg_id, deleted_at))
        if self.fail:
            raise TimeoutError("db busy")
        return True

    def insert_message(self, jid, msg):
        self.calls.append(("insert", jid, (msg.get("key") or {}).get("id")))
        if self.fail:
            raise TimeoutError("db busy")


class _AppSettings:
    """app.json, minus the file and its lock: a models folder and the list of
    external models, nothing else. The real get() and get_strict() differ
    only for an unreadable file, which a test says by setting `unreadable`."""

    def __init__(self, models_dir="", references=()):
        self._values = {
            preferences.MODELS_DIR_SETTING: models_dir,
            external_models.EXTERNAL_MODELS_SETTING: [r.as_dict() for r in references],
        }
        self.unreadable = None

    def get(self, key):
        return self._values[key]

    def get_strict(self, key):
        if self.unreadable is not None:
            raise self.unreadable
        return self._values[key]


class _MainWindow:
    def __init__(self, key, settings=None):
        self.i18n = I18N
        self.speak_output = _Speech()
        self.settings = settings if settings is not None else {"transcription": {"model": "small"}}
        self.key = key
        self._app_settings = None
        self._wa_connected = True
        self.error_sound = _Sound()
        # What MainWindow's own transcription storage reads.
        self.chats = {}
        self.db = _FakeDb()
        self._transcription_write_queue = _Inline()
        self.conversations_panel = None
        self.saves = []
        # The locked-chats vault as _load_chat_lock_vault() leaves it on an
        # account that never set one up: nothing locked, nothing hidden. The
        # flow asks the real is_chat_hidden_by_vault() before showing a text
        # (tests/test_transcription_chat_lock.py sets a vault up).
        self._chat_lock_vault = None
        self._chat_lock_fingerprints = set()
        self._chat_lock_unlocked = False
        self._chat_lock_timeout_timer = None
        self._lid_to_phone = {}
        self._phone_to_lid = {}
        # WhatsApp's Chat Lock set on the phone: none, as on most accounts.
        self._phone_locked_chats = set()

    _normalize_jid = staticmethod(MainWindow._normalize_jid)
    _chat_lock_candidates = MainWindow._chat_lock_candidates
    _chat_lock_vault_holds = MainWindow._chat_lock_vault_holds
    is_chat_phone_locked = MainWindow.is_chat_phone_locked
    is_chat_locked = MainWindow.is_chat_locked
    is_chat_hidden_by_vault = MainWindow.is_chat_hidden_by_vault
    get_chat = MainWindow.get_chat
    _transcription_copies = MainWindow._transcription_copies
    _transcription_storage_jids = MainWindow._transcription_storage_jids
    _say_transcription_not_stored = MainWindow._say_transcription_not_stored
    store_message_transcription = MainWindow.store_message_transcription
    delete_message_transcription = MainWindow.delete_message_transcription

    def _schedule_save(self, dirty_jid=None, contacts_dirty=False):
        self.saves.append(dirty_jid)

    @staticmethod
    def _find_api_ffmpeg():
        return "ffmpeg.exe"

    def output(self, text, interrupt=False):
        self.speak_output.output(text, interrupt=interrupt)


class _List:
    def __init__(self):
        self.calls = []
        self.shown = True

    def Focus(self, idx):
        self.calls.append(("Focus", idx))

    def Select(self, idx, on=True):
        self.calls.append(("Select", idx))

    def EnsureVisible(self, idx):
        self.calls.append(("EnsureVisible", idx))

    def SetFocus(self):
        self.calls.append(("SetFocus",))

    def IsShown(self):
        return self.shown


class _Field:
    """A text field over a Python string, with a caret and a selection."""

    def __init__(self, value="", caret=None, selection=None):
        self.value = value
        self.caret = len(value) if caret is None else caret
        self.selection = selection
        self.focused = False

    def GetSelection(self):
        return self.selection or (self.caret, self.caret)

    def SetInsertionPoint(self, pos):
        self.caret = pos
        self.selection = None

    def GetInsertionPoint(self):
        return self.caret

    def GetRange(self, start, end):
        return self.value[max(0, start):max(0, end)]

    def WriteText(self, text):
        start, end = self.GetSelection()
        self.value = self.value[:start] + text + self.value[end:]
        self.caret = start + len(text)
        self.selection = None

    def SetFocus(self):
        self.focused = True


def _msg(msg_id=_ID, msg_type="audioMessage", remote=_JID, participant=None, from_me=False):
    key = {"id": msg_id, "remoteJid": remote, "fromMe": from_me}
    if participant:
        key["participant"] = participant
    return {"key": key, "messageType": msg_type, "message": {msg_type: {}}}


class _Panel:
    def __init__(self, main_window, messages, download=None):
        self.main_window = main_window
        self.messages_list = _List()
        self.message_field = _Field()
        self._sorted_messages = list(messages)
        self.conversation = {"remoteJid": _JID}
        self.conversation_name = _CONTACT
        self._download = download or (lambda msg, path: False)
        self.ensure_calls = []

    _find_index_by_msg_id = ConversationsPanel._find_index_by_msg_id
    _focus_message_row = ConversationsPanel._focus_message_row
    _is_separator = ConversationsPanel._is_separator

    def _download_media_to_disk(self, msg, media_path):
        return self._download(msg, media_path)

    def _ensure_media_on_disk(self, msg, media_path):
        # The helper that reports on its own. The flow must not reach it.
        self.ensure_calls.append(media_path)
        raise AssertionError("the transcription must not use the self-reporting helper")

    #: What the panel resolves a group participant to. A name by default; the
    #: title tests set it to the number shapes the real fallback produces.
    participant_name = "Bruno"

    def _get_participant_name(self, participant, msg=None):
        return self.participant_name


# ── Fake dialogs ─────────────────────────────────────────────────────────────


class _FakeProgressDialog:
    """TranscriptionProgressDialog's contract, run synchronously."""

    made = []
    before_start = None  # a hook the tests set to act "while it runs"

    def __init__(self, parent, i18n, speak_output, make_job, status_text):
        self.statuses = [status_text]
        self.ticks = []
        self.result = None
        self.error = None
        self.destroyed = False
        self.reports = 0
        self.job = make_job(self._progress, self._finished)
        _FakeProgressDialog.made.append(self)

    def __bool__(self):
        return not self.destroyed

    def run(self):
        if _FakeProgressDialog.before_start is not None:
            _FakeProgressDialog.before_start(self)
        self.job.start()
        self.job.join(10)
        return 0

    def set_status(self, text):
        self.statuses.append(text)

    def _progress(self, tick):
        self.ticks.append(tick)

    def _finished(self, result, error):
        self.reports += 1
        self.result, self.error = result, error

    def Destroy(self):
        self.destroyed = True


class _FakeResultDialog:
    made = []
    choose_insert = False

    def __init__(self, parent, main_window, title, text, notes=(), spoken="", default_file=""):
        self.title = title
        self.text = text
        self.notes = list(notes)
        self.spoken = spoken
        self.default_file = default_file
        self.insert_requested = False
        self.destroyed = False
        _FakeResultDialog.made.append(self)

    def run(self):
        self.insert_requested = _FakeResultDialog.choose_insert
        return 0

    def Destroy(self):
        self.destroyed = True


class _FakeMessageDialog:
    made = []
    answer = None

    def __init__(self, parent, message, caption, style):
        self.message = message
        _FakeMessageDialog.made.append(self)

    def ShowModal(self):
        return _FakeMessageDialog.answer

    def Destroy(self):
        pass


@pytest.fixture
def own_temp_dir(tmp_path, monkeypatch):
    import tempfile

    private = tmp_path / "temp"
    private.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(private))
    return private


@pytest.fixture
def world(tmp_path, own_temp_dir, fernet_key, fernet, monkeypatch):
    """Everything around the flow, faked at its edges. Returns a namespace."""

    class World:
        pass

    w = World()
    w.jobs = []
    w.script = [_succeed]
    w.settings_opened = []
    w.posted = []
    w.temp = own_temp_dir

    for cls in (_FakeProgressDialog, _FakeResultDialog, _FakeMessageDialog):
        cls.made = []
    _FakeProgressDialog.before_start = None
    _FakeResultDialog.choose_insert = False
    _FakeMessageDialog.answer = transcription_flow.wx.ID_NO

    voice = tmp_path / "voice_messages"
    media = tmp_path / "media"
    voice.mkdir()
    media.mkdir()
    (voice / f"{_ID}.msv").write_bytes(fernet.encrypt(WAV))
    w.voice = voice

    def _job_factory(*args, **kwargs):
        # The script is read when the job is built, so a test can give the
        # first run and the re-run different ones.
        script = w.script.pop(0) if len(w.script) > 1 else w.script[0]
        return _FakeJob(script, w.jobs)(*args, **kwargs)

    monkeypatch.setattr(job_module, "TranscriptionJob", _job_factory)
    monkeypatch.setattr(device, "probe_hardware",
                        lambda: device.HardwareProbe(total_ram_mb=16000, available_ram_mb=8000))
    monkeypatch.setattr(model_store, "list_installed", lambda root: ("small",))
    monkeypatch.setattr(backend_module, "available_backend_ids", lambda: ("faster_whisper",))
    monkeypatch.setattr(transcription_flow, "data_path", lambda name: str(tmp_path / name))
    monkeypatch.setattr(transcription_flow, "TranscriptionProgressDialog", _FakeProgressDialog)
    monkeypatch.setattr(transcription_flow, "TranscriptionResultDialog", _FakeResultDialog)
    monkeypatch.setattr(transcription_flow.wx, "MessageDialog", _FakeMessageDialog)
    monkeypatch.setattr(transcription_flow, "open_transcription_settings",
                        lambda mw: w.settings_opened.append(mw))

    def _call_after(func, *args, **kwargs):
        w.posted.append(getattr(func, "__name__", repr(func)))
        func(*args, **kwargs)

    monkeypatch.setattr(transcription_flow.wx, "CallAfter", _call_after)

    w.main_window = _MainWindow(fernet_key)
    # A models folder of the test's own. With none, the run resolves the
    # default one — the developer's real data/global/transcription_models,
    # where a model they downloaded would turn "not installed" into a run.
    w.models_dir = str(tmp_path / "models")
    w.main_window._app_settings = _AppSettings(w.models_dir)
    w.target = _msg()
    w.panel = _Panel(w.main_window, [_msg("A1"), w.target, _msg("A3")])
    # The chat holds the very dicts the panel lists, as it does in the app.
    w.main_window.chats[_JID] = {
        "remoteJid": _JID,
        "messages": {"messages": {"records": list(w.panel._sorted_messages)}},
    }
    w.main_window.conversations_panel = w.panel
    w.fernet = fernet
    return w


def _start(world, msg=None):
    MessageTranscriptionFlow(world.panel, msg or world.target).start()


def _row_focus(panel):
    return [c for c in panel.messages_list.calls if c[0] == "Focus"]


# ── The path users take most ─────────────────────────────────────────────────


class TestAFinishedTranscription:
    def test_the_text_opens_in_the_result_window_titled_by_name(self, world):
        _start(world)
        [dialog] = _FakeResultDialog.made
        assert dialog.text == RESULT.text
        assert _CONTACT in dialog.title
        assert "5511" not in dialog.title
        assert dialog.default_file.endswith(".txt") and _CONTACT in dialog.default_file
        assert dialog.spoken.startswith(_t("transcription_finished"))

    def test_the_focus_returns_to_the_same_message_after_others_arrived(self, world):
        """The regression this pins: the row index at the start is a different
        message by the end. Two messages arrive at the top mid-run."""
        def _messages_arrive(job):
            world.panel._sorted_messages[:0] = [_msg("NEW1"), _msg("NEW2")]
            _succeed(job)

        world.script = [_messages_arrive]
        _start(world)
        assert world.panel._sorted_messages[3] is world.target
        assert _row_focus(world.panel) == [("Focus", 3)]

    def test_a_message_no_longer_listed_leaves_the_focus_on_the_list(self, world):
        def _message_goes(job):
            world.panel._sorted_messages.remove(world.target)
            _succeed(job)

        world.script = [_message_goes]
        _start(world)
        assert _row_focus(world.panel) == []
        assert world.panel.messages_list.calls == [("SetFocus",)]

    def test_the_models_folder_is_the_install_wide_one_the_user_chose(self, world, tmp_path):
        """Read from `_app_settings`, with the underscore — the spelling the
        window really has. Part 5b shipped the other one against a stub that
        invented it, and the user's folder was never read back."""
        chosen = str(tmp_path / "my models")
        world.main_window._app_settings = _AppSettings(chosen)
        _start(world)
        assert world.jobs[0].models_root == chosen

    def test_an_app_json_held_past_the_wait_does_not_escape_the_key_handler(
        self, world, monkeypatch
    ):
        """Read on the wx thread, inside the progress dialog's constructor."""
        from coord_locks import LockTimeout

        def _held(app_settings):
            raise LockTimeout("app.json")

        monkeypatch.setattr(preferences, "stored_models_dir", _held)
        _start(world)
        # "small" is listed in every folder in this world, so the run goes on
        # from the default folder; the references are unknown, not empty.
        assert world.jobs[0].models_root == preferences.resolve_models_dir("")
        assert len(_FakeResultDialog.made) == 1

    def test_unreadable_references_are_unknown_not_none(self, world, monkeypatch):
        world.main_window._app_settings.unreadable = ValueError("half written")
        made = []
        real = message_run.MessageTranscription

        def _spy(*args, **kwargs):
            made.append(kwargs.get("external_references", ()))
            return real(*args, **kwargs)

        monkeypatch.setattr(message_run, "MessageTranscription", _spy)
        _start(world)
        assert made == [None]

    def test_nothing_decrypted_is_left_behind(self, world):
        _start(world)
        assert _leftovers(world.temp) == []

    def test_insert_puts_the_text_at_the_cursor_and_keeps_what_was_typed(self, world):
        _FakeResultDialog.choose_insert = True
        world.panel.message_field = _Field("ok, então", caret=2)
        _start(world)
        field = world.panel.message_field
        assert field.value == f"ok {RESULT.text}, então"
        assert field.focused
        # The one exit where the focus does not go back to the message.
        assert _row_focus(world.panel) == []

    def test_insert_never_replaces_a_selection(self, world):
        _FakeResultDialog.choose_insert = True
        world.panel.message_field = _Field("apagar não", caret=0, selection=(0, 6))
        _start(world)
        assert world.panel.message_field.value.startswith("apagar ")
        assert RESULT.text in world.panel.message_field.value


# ── What is said ─────────────────────────────────────────────────────────────


def _result(**overrides):
    values = dict(text="uma frase", language="pt", language_probability=0.99,
                  duration_seconds=3.0)
    values.update(overrides)
    return TranscriptionResult(**values)


def _script_returning(result):
    def _script(job):
        job.device, job.device_reason = device.DEVICE_CPU, device.REASON_NO_CUDA_FOUND
        job.on_phase(job_module.PHASE_LOADING_MODEL)
        job.on_finished(result, None)
    return _script


class TestTheVoiceFilterWarningIsAlwaysSpoken:
    def test_with_a_result_it_is_in_what_is_spoken_and_what_is_shown(self, world):
        world.script = [_script_returning(_result(vad_used=False))]
        _start(world)
        [dialog] = _FakeResultDialog.made
        warning = _t("transcription_note_vad_unavailable")
        assert warning in dialog.spoken
        assert warning in dialog.notes

    def test_with_an_empty_result_it_is_spoken_and_nothing_opens(self, world):
        world.script = [_script_returning(_result(text="", vad_used=False))]
        _start(world)
        assert _FakeResultDialog.made == []
        [sentence] = world.main_window.speak_output.spoken[-1:]
        assert _t("transcription_note_no_speech") in sentence
        assert _t("transcription_note_vad_unavailable") in sentence
        assert _row_focus(world.panel) == [("Focus", 1)]

    def test_the_other_notes_are_shown_and_only_pointed_to(self, world):
        world.script = [_script_returning(
            _result(language="es", language_probability=0.3, vad_used=True))]
        _start(world)
        [dialog] = _FakeResultDialog.made
        assert _t("transcription_note_low_confidence") in dialog.notes
        assert _t("transcription_note_low_confidence") not in dialog.spoken
        assert dialog.spoken.endswith(_t("transcription_result_has_notes"))


class TestThePhases:
    def test_the_device_is_said_with_the_loading_line_and_nowhere_else(self, world):
        _start(world)
        [progress] = _FakeProgressDialog.made
        # The program too, since there are two (part 9b).
        device_sentence = _t("transcription_running_with_backend_on_cpu", model="small",
                             backend=_t("transcription_backend_faster_whisper"))
        assert progress.statuses == [
            _t("transcription_starting"),
            _t("transcription_phase_preparing_audio"),
            f"{_t('transcription_phase_loading_model')} {device_sentence}",
            _t("transcription_phase_transcribing"),
        ]

    def test_a_download_is_announced(self, world, fernet):
        world.voice.joinpath(f"{_ID}.msv").unlink()

        def _download(msg, path):
            with open(path, "wb") as handle:
                handle.write(world.fernet.encrypt(WAV))
            return True

        world.panel._download = _download
        _start(world)
        [progress] = _FakeProgressDialog.made
        assert progress.statuses[1] == _t("transcription_phase_downloading_audio")

    def test_the_statuses_cross_to_the_wx_thread(self, world):
        _start(world)
        # Preparing, loading, transcribing — the first line is the dialog's own.
        assert world.posted.count("_show_status") == 3


class TestFailuresAreToldOnce:
    def test_offline_is_the_apps_own_offline_sentence_once(self, world):
        world.voice.joinpath(f"{_ID}.msv").unlink()
        world.main_window._wa_connected = False
        _start(world)
        assert world.main_window.speak_output.spoken == [_t("media_download_offline")]
        assert world.panel.ensure_calls == []
        assert _FakeMessageDialog.made == []
        assert _row_focus(world.panel) == [("Focus", 1)]

    def test_a_failed_download_is_said_once(self, world):
        world.voice.joinpath(f"{_ID}.msv").unlink()
        _start(world)
        assert world.main_window.speak_output.spoken == [_t("media_download_failed")]
        assert world.main_window.error_sound.played == 1

    @pytest.mark.parametrize("connected", [True, False])
    def test_an_own_note_still_being_written_is_not_called_a_download(self, world, connected):
        """Alt+Shift+T in the seconds between the row appearing and its file
        being written: no download (the id is local), and no "the link may
        have expired" — nor, offline, "wait for the connection"."""
        world.voice.joinpath(f"{_ID}.msv").unlink()
        world.main_window._wa_connected = connected
        target = _msg(from_me=True)
        target["_local_pending"] = True
        target["_local_id"] = _ID
        downloads = []
        world.panel._download = lambda msg, path: downloads.append(path) or False
        _start(world, target)
        assert world.main_window.speak_output.spoken == [_t("transcription_media_still_preparing")]
        assert downloads == []
        assert world.jobs == []
        assert _FakeResultDialog.made == []

    def test_an_own_note_whose_send_failed_without_a_file_is_not_called_preparing(self, world):
        """The row as ConversationsPanel._mark_message_failed() leaves it after
        the mixed recording's encode failed and its file was deleted. "Try
        again in a moment" would be a promise nothing keeps."""
        world.voice.joinpath(f"{_ID}.msv").unlink()
        target = _msg(from_me=True)
        target.update({"_local_pending": False, "_send_failed": True, "_local_id": _ID})
        downloads = []
        world.panel._download = lambda msg, path: downloads.append(path) or False
        _start(world, target)
        assert world.main_window.speak_output.spoken == [_t("transcription_media_send_failed")]
        assert world.main_window.error_sound.played == 1
        assert downloads == []
        assert world.jobs == []

    def test_a_full_temporary_disk_is_not_called_a_download(self, world, monkeypatch):
        """The voice note is on disk and nothing is being downloaded: the
        downloads' "not enough space for this download" would send the user
        looking for a download, and possibly to the wrong drive."""
        from core.transcription import message_audio

        real_fdopen = os.fdopen

        def _full(handle, *args, **kwargs):
            real_fdopen(handle, *args, **kwargs).close()
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(message_audio.os, "fdopen", _full)
        _start(world)
        # The drive %TEMP% is on, by letter: the disk to free is named, and
        # its folder (which carries the Windows user name) never is.
        # (Or, on a %TEMP% with no letter, the sentence that names none.)
        key = errors.error_i18n_key(errors.TEMP_NO_DISK_SPACE)
        assert world.main_window.speak_output.spoken == [
            _t(key, **errors.error_i18n_values(errors.TEMP_NO_DISK_SPACE))]
        assert str(world.temp) not in world.main_window.speak_output.spoken[0]
        assert world.main_window.error_sound.played == 1
        assert _leftovers(world.temp) == []

    def test_no_backend_is_said(self, world, monkeypatch):
        monkeypatch.setattr(backend_module, "available_backend_ids", lambda: ())
        _start(world)
        assert world.main_window.speak_output.spoken == [
            _t("transcription_error_backend_missing")]
        assert _FakeMessageDialog.made == []

    def test_a_cancel_says_cancelled_and_leaves_nothing(self, world):
        def _cancel_midway(job):
            _FakeProgressDialog.made[-1].job.cancel()
            job.on_finished(None, errors.TranscriptionError(errors.CANCELLED, "x"))

        world.script = [_cancel_midway]
        _start(world)
        assert world.main_window.speak_output.spoken == [_t("transcription_error_cancelled")]
        assert world.main_window.error_sound.played == 0
        assert _leftovers(world.temp) == []
        assert _row_focus(world.panel) == [("Focus", 1)]

    def test_a_failed_job_is_said_and_leaves_nothing(self, world):
        def _fail(job):
            job.on_finished(None, errors.TranscriptionError(errors.FFMPEG_FAILED, "x"))

        world.script = [_fail]
        _start(world)
        assert world.main_window.speak_output.spoken == [_t("transcription_error_ffmpeg_failed")]
        assert _leftovers(world.temp) == []

    def test_a_message_without_audio_opens_nothing(self, world):
        _start(world, _msg(msg_type="imageMessage"))
        assert world.main_window.speak_output.spoken == [_t("transcription_no_audio")]
        assert _FakeProgressDialog.made == []


class TestTheSettingsOffer:
    def test_no_model_offers_the_transcription_tab(self, world, monkeypatch):
        world.main_window.settings = {}
        monkeypatch.setattr(model_store, "list_installed", lambda root: ())
        monkeypatch.setattr(device, "probe_hardware", lambda: device.HardwareProbe())
        _FakeMessageDialog.answer = transcription_flow.wx.ID_YES
        _start(world)
        [dialog] = _FakeMessageDialog.made
        assert dialog.message.startswith(_t("transcription_model_none_unmeasured"))
        assert dialog.message.endswith(_t("transcription_open_settings_question"))
        assert world.settings_opened == [world.main_window]
        assert _row_focus(world.panel) == [("Focus", 1)]
        # The box is the report: nothing is spoken on top of it.
        assert world.main_window.speak_output.spoken == []

    def test_declining_opens_nothing(self, world, monkeypatch):
        monkeypatch.setattr(model_store, "list_installed", lambda root: ("tiny",))
        _start(world)
        [dialog] = _FakeMessageDialog.made
        assert dialog.message.startswith(_t("transcription_error_model_not_installed"))
        assert world.settings_opened == []


def _succeed_on_the_processor(job):
    """What job.py does with a handed-over file and PREFERENCE_CPU: nothing to
    convert, so its first phase is the model load, on the processor, for the
    reason "you asked for it" — which narration deliberately does not say."""
    job.device, job.device_reason = device.DEVICE_CPU, device.REASON_CPU_REQUESTED
    job.on_phase(job_module.PHASE_LOADING_MODEL)
    job.on_phase(job_module.PHASE_TRANSCRIBING)
    job.on_progress(1.0)
    job.on_phase(job_module.PHASE_DONE)
    job.on_finished(RESULT, None)


class TestTheProcessorReRun:
    """The handover is the real `audio_prep.PreparedAudio` and it goes through
    the real `audio_prep.discard()`. A stand-in string with a discard that
    deletes strings would pass just as well if the flow called
    `message_audio.discard_temp(handover)` by mistake — which, handed the real
    object, raises instead of deleting anything."""

    def _handover(self, world):
        path = world.temp / "converted.wav"
        path.write_bytes(b"RIFF")
        prepared = audio_prep.PreparedAudio(path=str(path), duration_seconds=4.0)

        def _script(job):
            job.handover_to_give = prepared
            _fail_on_gpu_with_handover(job)

        world.script = [_script, _succeed_on_the_processor]
        return prepared, path

    def test_accepted_it_runs_on_the_processor_from_the_converted_audio(self, world, monkeypatch):
        discarded = []
        real_discard = audio_prep.discard

        def _watched_discard(prepared):
            discarded.append(prepared)
            real_discard(prepared)

        monkeypatch.setattr(transcription_flow.audio_prep, "discard", _watched_discard)
        prepared, path = self._handover(world)
        _FakeMessageDialog.answer = transcription_flow.wx.ID_YES
        _start(world)
        [question] = _FakeMessageDialog.made
        assert question.message == _t("transcription_retry_on_cpu_vram")
        first, second = world.jobs
        assert second.prepared is prepared
        assert second.device_preference == device.PREFERENCE_CPU
        assert discarded == [prepared]
        assert not path.exists()
        assert len(_FakeResultDialog.made) == 1
        assert _leftovers(world.temp) == []

    def test_the_second_dialog_says_where_the_re_run_is_going(self, world):
        """A new dialog for a new run: it starts from "starting", goes
        straight to the model load (there is nothing left to convert), names
        the processor there — and does not tell a user who agreed to an offer
        that they asked for the processor."""
        self._handover(world)
        _FakeMessageDialog.answer = transcription_flow.wx.ID_YES
        _start(world)
        first_dialog, second_dialog = _FakeProgressDialog.made
        assert second_dialog.statuses == [
            _t("transcription_starting"),
            _t("transcription_phase_loading_model") + " "
            + _t("transcription_running_with_backend_on_cpu", model="small",
                 backend=_t("transcription_backend_faster_whisper")),
            _t("transcription_phase_transcribing"),
        ]
        assert second_dialog.reports == 1
        assert second_dialog.destroyed and first_dialog.destroyed

    def test_declined_the_converted_audio_is_deleted_and_nothing_more_is_said(self, world):
        prepared, path = self._handover(world)
        _start(world)
        assert len(world.jobs) == 1
        assert not path.exists()
        assert world.main_window.speak_output.spoken == []
        assert _row_focus(world.panel) == [("Focus", 1)]


# ── Pure decisions ───────────────────────────────────────────────────────────


class _Run:
    def __init__(self, device_id=None, reason=None, model_id="small"):
        self.device = device_id
        self.device_reason = reason
        self.model_id = model_id
        # What MessageTranscription.model_name answers for a catalogue id.
        self.model_name = model_id


class TestPhaseStatusText:
    def test_the_loading_line_names_the_device_once_known(self):
        text = transcription_flow.phase_status_text(
            I18N, job_module.PHASE_LOADING_MODEL,
            _Run(device.DEVICE_CUDA, device.REASON_CUDA_SELECTED))
        assert _t("transcription_running_on_cuda", model="small") in text

    def test_before_the_probe_nothing_is_claimed(self):
        """None, None is what the loading callback would see if it were read
        early; it must not become "you asked for the processor"."""
        text = transcription_flow.phase_status_text(
            I18N, job_module.PHASE_LOADING_MODEL, _Run())
        assert text == _t("transcription_phase_loading_model")
        assert _t("transcription_device_cpu_requested") not in text

    def test_the_loading_line_names_the_program_the_file_and_the_language(self):
        run = _Run(device.DEVICE_CPU, device.REASON_CUDA_BUILD_MISSING,
                   model_id="ggml-small.en-q5_1")
        run.backend_id = backend_module.BACKEND_WHISPER_CPP
        run.forced_language = "en"
        run.overridden_language = "pt"
        text = transcription_flow.phase_status_text(
            I18N, job_module.PHASE_LOADING_MODEL, run)
        assert _t("transcription_running_with_backend_on_cpu",
                  model=model_names.display_name(I18N, "ggml-small.en-q5_1"),
                  backend=_t("transcription_backend_whisper_cpp")) in text
        assert _t("transcription_device_cuda_build_missing") in text
        assert _t("transcription_note_language_overridden",
                  language=preferences.language_name("en"),
                  chosen=preferences.language_name("pt")) in text
        assert "ggml-" not in text

    def test_a_chosen_precision_is_named_on_the_loading_line(self):
        """Part 11: the loading line says the precision when the user chose
        one, and nothing about it under "automatic"."""
        run = _Run(device.DEVICE_CPU, device.REASON_CPU_REQUESTED)
        run.precision = precision.PrecisionChoice("int8", "int8")
        text = transcription_flow.phase_status_text(
            I18N, job_module.PHASE_LOADING_MODEL, run)
        assert _t("transcription_note_precision_used",
                  precision=_t("transcription_precision_int8")) in text
        run.precision = precision.PrecisionChoice("int8")
        text = transcription_flow.phase_status_text(
            I18N, job_module.PHASE_LOADING_MODEL, run)
        assert _t("transcription_precision_int8") not in text

    def test_a_replaced_precision_is_never_switched_silently(self):
        run = _Run(device.DEVICE_CPU, device.REASON_CPU_REQUESTED)
        run.precision = precision.PrecisionChoice("float32", "float16")
        text = transcription_flow.phase_status_text(
            I18N, job_module.PHASE_LOADING_MODEL, run)
        assert _t("transcription_note_precision_replaced",
                  chosen=_t("transcription_precision_float16"),
                  used=_t("transcription_precision_float32")) in text

    def test_other_phases_never_carry_the_device(self):
        known = _Run(device.DEVICE_CUDA, device.REASON_CUDA_SELECTED)
        for phase in (job_module.PHASE_PREPARING_AUDIO, job_module.PHASE_TRANSCRIBING,
                      message_run.PHASE_DOWNLOADING_MEDIA):
            text = transcription_flow.phase_status_text(I18N, phase, known)
            assert "small" not in text

    def test_terminal_phases_say_nothing(self):
        for phase in job_module.TERMINAL_PHASES:
            assert transcription_flow.phase_status_text(I18N, phase, _Run()) is None


class TestResultSpeech:
    def test_vad_is_spoken_even_alongside_every_other_note(self):
        result = _result(language="es", language_probability=0.2, vad_used=False)
        notes = narration.result_notes(result, "pt")
        announcement = narration.outcome_announcement(result)
        spoken, shown = transcription_flow.split_result_notes(announcement, notes)
        assert [n.i18n_key for n in spoken] == [narration.VAD_UNAVAILABLE_I18N_KEY]
        assert len(shown) == 3

    def test_the_headline_is_not_repeated_as_a_note(self):
        result = _result(text="", vad_used=True)
        notes = narration.result_notes(result, "pt")
        announcement = narration.outcome_announcement(result)
        sentence = transcription_flow.result_speech(I18N, announcement, notes, window=False)
        assert sentence == _t("transcription_note_no_speech")

    def test_no_pointer_when_everything_shown_was_spoken(self):
        result = _result(vad_used=False)
        notes = narration.result_notes(result, "pt")
        announcement = narration.outcome_announcement(result)
        sentence = transcription_flow.result_speech(I18N, announcement, notes, window=True)
        assert _t("transcription_result_has_notes") not in sentence


class TestModelProblemSentence:
    def _resolution(self, model_id, reason=None, substitutions=()):
        return preferences.Resolution("faster_whisper", model_id, device.PREFERENCE_AUTO,
                                      None, tuple(substitutions), reason)

    def test_automatic_choice_never_says_you_chose(self):
        key = transcription_flow.model_problem_i18n_key(
            errors.MODEL_NOT_INSTALLED, self._resolution("small"), {})
        assert key == transcription_flow.AUTO_MODEL_UNAVAILABLE_I18N_KEY

    def test_an_explicit_choice_does(self):
        key = transcription_flow.model_problem_i18n_key(
            errors.MODEL_NOT_INSTALLED, self._resolution("small"),
            {"transcription": {"model": "small"}})
        assert key == errors.error_i18n_key(errors.MODEL_NOT_INSTALLED)

    def test_a_retired_choice_is_automatic_again(self):
        substituted = preferences.Substitution(preferences.SETTING_MODEL, "old")
        key = transcription_flow.model_problem_i18n_key(
            errors.MODEL_NOT_INSTALLED, self._resolution("small", substitutions=[substituted]),
            {"transcription": {"model": "old"}})
        assert key == transcription_flow.AUTO_MODEL_UNAVAILABLE_I18N_KEY

    @pytest.mark.parametrize("reason", [preferences.MODEL_NONE_NOTHING_FITS,
                                        preferences.MODEL_NONE_UNMEASURED])
    def test_no_model_names_why(self, reason):
        key = transcription_flow.model_problem_i18n_key(
            errors.MODEL_NOT_INSTALLED, self._resolution(None, reason), {})
        assert key == preferences.MODEL_NONE_I18N_KEYS[reason]


class TestTheWhisperCppProgramIsMissingOrDamaged:
    """Installed, repaired or removed on the Transcription tab — so the flow
    offers the tab, with the error's own sentence whatever was chosen."""

    @pytest.mark.parametrize("code", [errors.WHISPER_CPP_NOT_INSTALLED,
                                      errors.WHISPER_CPP_CORRUPTED])
    def test_it_is_offered_with_the_tab_that_fixes_it(self, code):
        assert code in transcription_flow.SETTINGS_OFFER_CODES
        for settings in ({}, {"transcription": {"model": "ggml-small"}}):
            assert transcription_flow.model_problem_i18n_key(
                code, None, settings
            ) == errors.error_i18n_key(code)


class TestAPrecisionTheDeviceRefused:
    """Part 11: chosen on the Transcription tab, so the tab is offered, with
    the error's own sentence — never "the model you chose"."""

    def test_it_is_offered_with_the_tab_that_fixes_it(self):
        code = errors.PRECISION_UNSUPPORTED
        assert code in transcription_flow.SETTINGS_OFFER_CODES
        resolution = preferences.Resolution(
            "faster_whisper", "small", device.PREFERENCE_AUTO, None)
        for settings in ({}, {"transcription": {"model": "small"}}):
            assert transcription_flow.model_problem_i18n_key(
                code, resolution, settings
            ) == errors.error_i18n_key(code)


class TestAModelInAnotherFolder:
    """What the flow says when the model is one the user pointed WinZapp at."""

    @staticmethod
    def _custom():
        return external_models.ExternalReference(
            "r1", "folder_a", None, True, (1, 2), key="folder_a"
        )

    @pytest.mark.parametrize("code", [errors.EXTERNAL_MODEL_MISSING,
                                      errors.EXTERNAL_MODEL_CHANGED])
    def test_the_failures_are_offered_with_the_tab_that_fixes_them(self, code):
        assert code in transcription_flow.SETTINGS_OFFER_CODES

    @pytest.mark.parametrize("code", [errors.EXTERNAL_MODEL_MISSING,
                                      errors.EXTERNAL_MODEL_CHANGED])
    def test_their_sentence_is_their_own_whatever_was_chosen(self, code):
        """The folder is gone whether the model was picked or came from
        "automatic": the sentence never says "the model you chose"."""
        for settings in ({}, {"transcription": {"model": "external:r1"}}):
            assert transcription_flow.model_problem_i18n_key(
                code, None, settings
            ) == errors.error_i18n_key(code)

    def test_the_loading_line_names_the_folder_never_the_stored_choice(self):
        run = _Run(device.DEVICE_CUDA, device.REASON_CUDA_SELECTED,
                   model_id="external:r1")
        run.model_name = "folder_a"
        text = transcription_flow.phase_status_text(
            I18N, job_module.PHASE_LOADING_MODEL, run)
        assert "folder_a" in text
        assert "external:" not in text

    def test_a_stored_transcription_names_the_folder_it_was_made_with(self):
        reference = self._custom()
        announcement = transcription_flow.saved_announcement(
            I18N, {"model_id": external_models.custom_choice(reference)},
            (reference,))
        assert announcement.i18n_key == transcription_flow.SAVED_OPENED_I18N_KEY
        assert announcement.values["model"] == "folder_a"

    def test_one_whose_reference_was_forgotten_is_opened_without_a_model(self):
        announcement = transcription_flow.saved_announcement(
            I18N, {"model_id": "external:gone"}, ())
        assert announcement.i18n_key == transcription_flow.SAVED_OPENED_NO_MODEL_I18N_KEY
        assert "external:" not in str(announcement.values)

    def test_a_catalogue_model_is_still_its_own_name(self):
        announcement = transcription_flow.saved_announcement(
            I18N, {"model_id": "small"})
        assert announcement.values["model"] == "small"

    def test_a_whisper_cpp_one_names_the_program_and_the_file_as_the_picker_does(self):
        announcement = transcription_flow.saved_announcement(
            I18N, {"model_id": "ggml-small-q5_1", "backend": "whisper_cpp"})
        assert announcement.i18n_key == transcription_flow.SAVED_OPENED_BACKEND_I18N_KEY
        assert announcement.values["backend"] == _t("transcription_backend_whisper_cpp")
        assert announcement.values["model"] == model_names.display_name(
            I18N, "ggml-small-q5_1")
        assert "ggml-" not in announcement.values["model"]

    def test_a_forgotten_custom_model_still_names_the_program(self):
        announcement = transcription_flow.saved_announcement(
            I18N, {"model_id": "external:gone", "backend": "whisper_cpp"}, ())
        assert announcement.i18n_key == (
            transcription_flow.SAVED_OPENED_NO_MODEL_BACKEND_I18N_KEY)
        assert announcement.values["backend"] == _t("transcription_backend_whisper_cpp")

    @pytest.mark.parametrize("code", [errors.EXTERNAL_MODEL_MISSING,
                                      errors.EXTERNAL_MODEL_CHANGED])
    def test_a_whisper_cpp_model_elsewhere_is_called_a_file(self, code):
        resolution = preferences.Resolution(
            backend_module.BACKEND_WHISPER_CPP, "external:r1", device.PREFERENCE_AUTO, None)
        key = transcription_flow.model_problem_i18n_key(code, resolution, {})
        assert key == errors.error_i18n_key(code) + "_file"
        folder = preferences.Resolution(
            backend_module.BACKEND_FASTER_WHISPER, "external:r1", device.PREFERENCE_AUTO, None)
        assert transcription_flow.model_problem_i18n_key(code, folder, {}) == (
            errors.error_i18n_key(code))

    def test_a_backend_this_version_does_not_know_is_left_unsaid(self):
        announcement = transcription_flow.saved_announcement(
            I18N, {"model_id": "small", "backend": "something_later"})
        assert announcement.i18n_key == transcription_flow.SAVED_OPENED_I18N_KEY


class TestInsertionText:
    @pytest.mark.parametrize(
        "before, after, expected",
        [("", "", "texto"), ("k", "", " texto"), (" ", "", "texto"),
         ("", "x", "texto "), ("\n", "\n", "texto"), ("a", "b", " texto "),
         ("a", ",", " texto"), ("a", "?", " texto")],
    )
    def test_spaces_only_where_words_would_touch(self, before, after, expected):
        assert transcription_flow.insertion_text("  texto ", before, after) == expected

    def test_nothing_to_insert(self):
        assert transcription_flow.insertion_text("   ", "a", "b") == ""


class TestTitleName:
    def test_a_private_chat_is_the_contact(self):
        panel = _Panel(None, [])
        assert transcription_flow.title_name(panel, _msg()) == _CONTACT

    def test_a_group_message_names_who_spoke(self):
        panel = _Panel(None, [])
        panel.conversation_name = "Família"
        msg = _msg(remote="120363000000000000@g.us", participant="5511900000000@s.whatsapp.net")
        assert transcription_flow.title_name(panel, msg) == "Bruno, Família"

    @pytest.mark.parametrize("resolved", [
        # An @lid nobody has mapped: the bare local part of the JID.
        "187612345678901",
        # A participant with no saved or pushed name: the formatted number.
        "+55 11 90000-0000",
        # format_number() found nothing to format and the JID came back whole.
        "5511900000000@s.whatsapp.net",
    ], ids=["lid-digits", "formatted-number", "whole-jid"])
    def test_a_participant_known_only_by_number_leaves_the_group_name(self, resolved):
        panel = _Panel(None, [])
        panel.conversation_name = "Família"
        panel.participant_name = resolved
        msg = _msg(remote="120363000000000000@g.us", participant="187612345678901@lid")
        assert transcription_flow.title_name(panel, msg) == "Família"

    def test_the_real_fallback_for_an_unmapped_lid_is_caught(self):
        """The shape above is the one ConversationsPanel really returns: its
        own `_get_participant_name()`, with nothing known about the @lid."""

        class _MW:
            contacts = {}
            _lid_to_phone = {}
            _phone_to_lid = {}
            _presence_pushname_map = {}
            _is_bad_contact_name = staticmethod(lambda name: not name or name.isdigit())

            def _is_self_jid(self, jid):
                return False

            def get_chat(self, jid):
                return None

            def _normalize_jid(self, jid):
                return jid

            def resolve_lid_jids_via_api(self, jids):
                # The real one is an HTTP call on a background thread; the
                # title never waits for it, so neither does this test.
                pass

        class _GroupPanel:
            conversation_name = "Família"
            main_window = _MW()
            _sorted_messages = []
            _get_participant_name = ConversationsPanel._get_participant_name

        panel = _GroupPanel()
        msg = _msg(remote="120363000000000000@g.us", participant="187612345678901@lid")
        assert panel._get_participant_name("187612345678901@lid", msg) == "187612345678901"
        assert transcription_flow.title_name(panel, msg) == "Família"

    def test_the_file_name_offered_carries_no_number_either(self, world):
        world.panel.conversation_name = "Família"
        world.panel.participant_name = "187612345678901"
        world.target["key"]["remoteJid"] = "120363000000000000@g.us"
        world.target["key"]["participant"] = "187612345678901@lid"
        _start(world)
        [dialog] = _FakeResultDialog.made
        assert "187612345678901" not in dialog.title
        assert "187612345678901" not in dialog.default_file
        assert "Família" in dialog.title


# ── Threads ──────────────────────────────────────────────────────────────────


class TestTheRunsThreadsOnlyCrossWithCallAfter:
    """Everything the run calls back on its own threads, pinned statically:
    a wx control driven from two threads does not fail a test, it crashes a
    user's session."""

    _WORKER_METHODS = ("_on_phase",)

    def test_worker_methods_touch_nothing_directly(self):
        source = inspect.getsource(MessageTranscriptionFlow)
        tree = ast.parse(source)
        methods = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        for name in self._WORKER_METHODS:
            for node in ast.walk(methods[name]):
                if not isinstance(node, ast.Call):
                    continue
                called = ast.unparse(node.func)
                if called.startswith("wx."):
                    assert called == "wx.CallAfter", f"{name} calls {called} directly"
                # Any method of the flow's own is a wx-thread method: it may
                # be handed to wx.CallAfter, never called from here.
                assert not called.startswith("self."), f"{name} calls {called} off the wx thread"
                for forbidden in ("_dialog", "_panel", "_main_window", "speak_output", "output"):
                    assert forbidden not in called, f"{name} calls {called} off the wx thread"

    def test_every_run_is_built_with_those_callbacks(self):
        """The dialog's own posting pair for progress and the report, and
        `_on_phase` — nothing that touches wx directly."""
        source = inspect.getsource(MessageTranscriptionFlow)
        tree = ast.parse(source)
        built = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and ast.unparse(n.func).endswith(("MessageTranscription",
                                                   "MessageTranscription.retry_on_cpu"))]
        assert len(built) == 2
        for call in built:
            given = {kw.arg: ast.unparse(kw.value) for kw in call.keywords}
            assert given["on_phase"] == "self._on_phase"
            assert given["on_progress"] == "on_progress"
            assert given["on_finished"] == "on_finished"


# ── Wiring into the panel ────────────────────────────────────────────────────


def _accelerator_bindings(path):
    """(modifiers, key) of every accelerator tuple in a module, as source."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            text = ast.unparse(node.value)
            if "ACCEL_" in text:
                aliases[node.targets[0].id] = text
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Tuple) and len(node.elts) == 3):
            continue
        key = ast.unparse(node.elts[1])
        mods = ast.unparse(node.elts[0])
        mods = aliases.get(mods, mods)
        if "ACCEL_" not in mods:
            continue
        found.append((frozenset(p.strip() for p in mods.replace("wx.", "").split("|")),
                      key.replace("'", '"').upper(), ast.unparse(node.elts[2])))
    return found


class TestAltShiftTIsOurs:
    """Alt+Shift+T must mean one thing: transcribe locally what the list is
    on. Alt+T (presence) lives in MainWindow's table, which is why a scan of
    the panel alone would not see a clash. The Status tab and its "my status"
    window bind the same chord to the same action on an audio or video status
    (each table is the one active while that window has the focus), and
    nothing else may take it."""

    # Every file MainWindow, ConversationsPanel and StatusPanel are built from
    # (their accelerator tables now sit in main_window/shortcuts.py,
    # conversation_panel/accelerators.py and status_panel.py), plus the media
    # viewer's table. Globbed, so a new mixin is scanned without anyone
    # remembering to list it here.
    @staticmethod
    def _modules():
        files = (main_window_source_files() + conversations_source_files()
                 + status_panel_source_files() + [_REPO / "client/ui/media_viewer.py"])
        return [f.relative_to(_REPO).as_posix() for f in files]

    def test_exactly_one_binding_in_the_whole_window(self):
        alt_shift = frozenset({"ACCEL_ALT", "ACCEL_SHIFT"})
        owners = []
        for module in self._modules():
            for mods, key, target in _accelerator_bindings(_REPO / module):
                if mods == alt_shift and key == 'ORD("T")':
                    owners.append((module, target))
        assert sorted(owners) == [
            ("client/status_panel.py", "self.ID_ALT_SHIFT_T"),
            ("client/status_tab/my_status_transcription.py", "self.ID_ALT_SHIFT_T"),
            ("client/ui/conversation_panel/accelerators.py", "self.ID_ALT_SHIFT_T"),
        ]

    def test_the_scan_sees_the_bindings_it_is_guarding(self):
        """A scanner that found nothing would pass the test above forever."""
        found = [binding for module in self._modules()
                 for binding in _accelerator_bindings(_REPO / module)]
        assert (frozenset({"ACCEL_ALT"}), 'ORD("T")', "self.ID_ALT_T") in found

    def test_the_scan_sees_the_status_tab_table(self):
        """Same guard for the Status tab. Attributed by file, because the media
        viewer registers the very same Ctrl+Left: found only there, the scan
        would look fine with StatusPanel's table out of it."""
        status_files = {f.relative_to(_REPO).as_posix() for f in status_panel_source_files()}
        owners = [module for module in self._modules() if module in status_files
                  for binding in _accelerator_bindings(_REPO / module)
                  if binding == (frozenset({"ACCEL_CTRL"}), "WX.WXK_LEFT", "self.ID_CTRL_LEFT")]
        assert owners, "StatusPanel's Ctrl+Left (previous status) is not in the scan"

    def test_the_scan_covers_every_module_the_windows_are_built_from(self):
        """The StatusPanel split moved its code into client/status_tab/ while
        this scan still read status_panel.py alone, and nothing failed: the
        table happened to stay behind. The classes' own bases are the
        authority on what they are made of, not a list kept by hand, so a
        mixin package missing from the scan fails here whether or not it
        registers a shortcut today."""
        client = (_REPO / "client").resolve()
        scanned = set(self._modules())
        for window in (MainWindow, ConversationsPanel, StatusPanel):
            for cls in window.__mro__:
                source = getattr(sys.modules.get(cls.__module__), "__file__", None)
                if not source:
                    continue
                path = pathlib.Path(source).resolve()
                # wx's bases are a compiled extension inside client/venv.
                if path.suffix != ".py" or not path.is_relative_to(client) \
                        or path.is_relative_to(client / "venv"):
                    continue
                module = path.relative_to(_REPO.resolve()).as_posix()
                assert module in scanned, f"{window.__name__} is built from {module}, not scanned"

    def test_the_shortcut_reaches_the_flow_with_the_selected_message(self, monkeypatch):
        seen = []
        monkeypatch.setattr(transcription_flow, "open_or_transcribe",
                            lambda panel, msg: seen.append(msg))

        class _Stub:
            messages_list = type("L", (), {"GetFirstSelected": lambda self: 1})()
            _sorted_messages = [_msg("A"), _msg("B")]
            _is_separator = ConversationsPanel._is_separator
            _on_menu_transcribe = ConversationsPanel._on_menu_transcribe
            _on_accel_transcribe = ConversationsPanel._on_accel_transcribe
            # Nothing selected: the bulk form (issue #469) is not in play.
            selected_messages = set()

            def _bulk_shortcuts_enabled(self):
                return True

        _Stub()._on_accel_transcribe(None)
        assert [m["key"]["id"] for m in seen] == ["B"]

    def test_the_menu_item_is_guarded_by_is_transcribable_and_shows_the_shortcut(self):
        source = inspect.getsource(ConversationsPanel.on_messages_context_menu)
        tree = ast.parse(source.lstrip())
        guarded = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.If)
            and ast.unparse(n.test) == "message_audio.is_transcribable(msg)"
        ]
        assert len(guarded) == 1
        body = ast.unparse(guarded[0])
        assert "transcribe_message" in body and "Alt+Shift+T" in body

    def test_the_f1_list_has_it(self):
        from ui.dialogs.shortcuts_dialog import ShortcutsDialog

        class _Echo:
            def t(self, key):
                return key

        assert "shortcut_alt_shift_t_label" in ShortcutsDialog._build_text(_Echo())


class TestTheSilentDownloadHelper:
    """`_ensure_media_on_disk()` keeps telling the three existing callers what
    went wrong; `_download_media_to_disk()`, split out of it, tells nobody."""

    class _Stub:
        _download_media_to_disk = ConversationsPanel._download_media_to_disk
        _ensure_media_on_disk = ConversationsPanel._ensure_media_on_disk

        def __init__(self):
            self.main_window = type("MW", (), {})()
            self.main_window.i18n = I18N
            self.main_window.app_name = "WinZapp"
            self.main_window._wa_connected = True
            self.main_window.output = lambda text: None
            self.main_window.handle_audio_message = lambda msg: None

    def test_the_split_out_helper_reports_nothing(self, monkeypatch, tmp_path):
        from ui import conversations

        posted = []
        monkeypatch.setattr(conversations.wx, "CallAfter", lambda *a, **k: posted.append(a))
        assert self._Stub()._download_media_to_disk(_msg(), str(tmp_path / "x.msv")) is False
        assert posted == []

    def test_the_old_helper_still_reports_once(self, monkeypatch, tmp_path):
        from ui import conversations

        posted = []
        monkeypatch.setattr(conversations.wx, "CallAfter", lambda *a, **k: posted.append(a))
        assert self._Stub()._ensure_media_on_disk(_msg(), str(tmp_path / "x.msv")) is False
        # "downloading..." and the one message box, exactly as before.
        assert len(posted) == 2
        assert posted[1][0] is conversations.wx.MessageBox
        assert posted[1][1] == _t("media_download_failed")


class _RaisingSound:
    """error_sound on a device that went away: BASS raises from play()."""

    def __init__(self):
        self.attempts = 0

    def play(self):
        self.attempts += 1
        raise RuntimeError("5, invalid handle")


class TestASoundThatRaisesCostsNothing:
    """docs/traps/audio-devices.md: a sound on an error path must never take
    the outcome with it. Each of these plays the error sound *before* the
    thing the user is waiting for."""

    def test_the_text_still_opens_when_storing_raised(self, world, caplog):
        # The worst of them: the sound comes before the result window, and a
        # raise there lost minutes of transcription to sys.excepthook.
        def _broken(jid, msg_id, value):
            raise RuntimeError("store exploded")

        world.main_window.store_message_transcription = _broken
        world.main_window.error_sound = _RaisingSound()
        caplog.set_level(logging.DEBUG)
        _start(world)

        [dialog] = _FakeResultDialog.made
        assert dialog.text == RESULT.text
        assert _t("transcription_store_failed") in dialog.notes
        assert world.main_window.error_sound.attempts == 1
        assert "could not play the error sound: RuntimeError: 5, invalid handle" in caplog.text
        assert _ID not in caplog.text
        assert RESULT.text not in caplog.text

    def test_a_failure_is_still_said(self, world):
        world.voice.joinpath(f"{_ID}.msv").unlink()
        world.main_window.error_sound = _RaisingSound()
        _start(world)
        assert world.main_window.speak_output.spoken == [_t("media_download_failed")]
        assert _row_focus(world.panel) == [("Focus", 1)]

    def test_a_refused_background_write_is_still_said(self, world):
        # MainWindow._say_transcription_not_stored(), the mixin's own sound.
        world.main_window.db.fail = True
        world.main_window.error_sound = _RaisingSound()
        _start(world)
        assert world.main_window.speak_output.spoken[-1] == _t("transcription_store_failed")
        assert world.main_window.error_sound.attempts == 1


class TestOpeningTheSettingsTab:
    """The flow asks the dialog to open itself on the tab and selects nothing.

    Selecting the page from here is what once entered the tab before the
    window existed (SetSelection() fires the page-change handler); the rule
    lives in SettingsDialog.show_transcription_tab(), pinned in
    tests/test_transcription_settings_tab.py. The fake below has no notebook
    and no ShowModal(), so a flow that went back to doing either fails here.
    """

    def test_the_dialog_opens_itself_on_the_tab(self, monkeypatch):
        from ui.dialogs import settings_dialog

        steps = []

        class _FakeSettingsDialog:
            def __init__(self, parent):
                steps.append("built")

            def show_transcription_tab(self):
                steps.append("shown on the tab")
                return transcription_flow.wx.ID_OK

            def Destroy(self):
                steps.append("destroyed")

        monkeypatch.setattr(settings_dialog, "SettingsDialog", _FakeSettingsDialog)
        transcription_flow.open_transcription_settings(object())
        assert steps == ["built", "shown on the tab", "destroyed"]


# ── A stored transcription (part 7) ──────────────────────────────────────────
# The flow above, with MainWindow's real storage methods bound onto the stub
# window: what a finished run leaves on the message, what reopening it says,
# and what deleting it does. The rules that keep it across resyncs are pinned
# in tests/test_transcription_stored.py.


def _saved_value(text="o texto guardado", vad_used=True, model_id="small", at=1_700_000_000.0):
    return {"text": text, "language": "pt", "language_probability": 0.97,
            "model_id": model_id, "backend": "faster_whisper", "vad_used": vad_used, "at": at}


def _saved(world, **kwargs):
    world.target[stored_transcription.TRANSCRIPTION_KEY] = _saved_value(**kwargs)


class TestAFinishedRunIsKept:
    def test_the_result_is_stored_on_the_message_and_in_the_database(self, world):
        _start(world)
        saved = stored_transcription.saved_transcription(world.target)
        assert saved["text"] == RESULT.text
        assert saved["language"] == "pt" and saved["vad_used"] is True
        [call] = world.main_window.db.calls
        assert call[:3] == ("set", (_JID,), _ID)
        # Nothing about keeping it goes into the notes when it was kept.
        [dialog] = _FakeResultDialog.made
        assert _t("transcription_not_saved_unsent") not in dialog.notes

    def test_an_empty_result_does_not_replace_a_stored_one(self, world):
        _saved(world)
        world.script = [_script_returning(_result(text=""))]
        transcription_flow.transcribe_message(world.panel, world.target)
        assert stored_transcription.saved_transcription(world.target)["text"] == "o texto guardado"
        assert world.main_window.db.calls == []

    def test_transcribing_again_replaces_it(self, world):
        _saved(world)
        transcription_flow.transcribe_message(world.panel, world.target)
        assert len(world.jobs) == 1
        assert stored_transcription.saved_transcription(world.target)["text"] == RESULT.text

    def test_an_own_message_still_being_sent_is_not_kept_and_the_window_says_so(self, world):
        world.target["_local_pending"] = True
        world.target["_local_id"] = _ID
        _start(world)
        assert stored_transcription.TRANSCRIPTION_KEY not in world.target
        assert world.main_window.db.calls == []
        [dialog] = _FakeResultDialog.made
        assert _t("transcription_not_saved_unsent") in dialog.notes
        assert dialog.spoken.endswith(_t("transcription_result_has_notes"))


    def test_a_message_deleted_for_everyone_meanwhile_is_said_and_not_shown(self, world):
        """The contact deleted the note for everyone while it was being
        transcribed: _apply_remote_revoke() turned the very dict the flow
        holds into a protocolMessage. Nothing is kept, no window puts the
        withdrawn content back on screen, and one sentence says why — after
        the focus has gone back to the message, and with the error sound."""
        def _revoked_during_the_run(job):
            job.device, job.device_reason = device.DEVICE_CPU, device.REASON_NO_CUDA_FOUND
            job.on_phase(job_module.PHASE_LOADING_MODEL)
            world.target["message"] = {"protocolMessage": {"type": 3}}
            world.target["messageType"] = "protocolMessage"
            job.on_finished(_result(), None)

        world.script = [_revoked_during_the_run]
        _start(world)

        assert _FakeResultDialog.made == []
        assert stored_transcription.TRANSCRIPTION_KEY not in world.target
        assert world.main_window.db.calls == []
        assert world.main_window.saves == []
        assert world.main_window.speak_output.spoken[-1] == _t("transcription_discarded_withdrawn")
        assert _row_focus(world.panel) == [("Focus", 1)]
        # Sounded like every run that ends without the text asked for.
        assert world.main_window.error_sound.played == 1

    def test_a_database_that_fails_is_said_in_one_sentence(self, world):
        """The result window opens before the background write answers; when
        that write fails, the window must not go on implying the text was
        kept."""
        world.main_window.db.fail = True
        _start(world)
        assert world.main_window.speak_output.spoken[-1] == _t("transcription_store_failed")
        assert world.main_window.error_sound.played == 1

    def test_storing_that_raises_still_shows_the_text_and_says_it_was_not_kept(
            self, world, caplog):
        """store_message_transcription() runs on the wx thread before the
        window opens; a raise there used to reach sys.excepthook's generic
        dialog and take minutes of transcription with it."""
        def _broken(jid, msg_id, value):
            raise RuntimeError("store exploded")

        world.main_window.store_message_transcription = _broken
        caplog.set_level(logging.DEBUG)
        _start(world)

        [dialog] = _FakeResultDialog.made
        assert dialog.text == RESULT.text
        assert _t("transcription_store_failed") in dialog.notes
        assert world.main_window.error_sound.played == 1
        assert "storing a transcription raised: RuntimeError: store exploded" in caplog.text
        assert RESULT.text not in caplog.text

    def test_storing_that_raises_on_a_withdrawn_message_shows_nothing(self, world):
        """The revoke outranks the fallback window: the text is what the
        sender withdrew, whatever storing did."""
        def _revoked_then_broken(jid, msg_id, value):
            world.target["message"] = {"protocolMessage": {"type": 3}}
            world.target["messageType"] = "protocolMessage"
            raise RuntimeError("store exploded")

        world.main_window.store_message_transcription = _revoked_then_broken
        _start(world)

        assert _FakeResultDialog.made == []
        assert world.main_window.speak_output.spoken[-1] == _t("transcription_discarded_withdrawn")
        assert world.main_window.error_sound.played == 1

    def test_a_window_that_raises_does_not_carry_the_storing_error_along(
            self, world, monkeypatch):
        """The window opens after the except block, not inside it: raised from
        in there, the dialog's own error would take the storing one along as
        its __context__ to sys.excepthook, which logs format_exception()
        with nothing scrubbed."""
        def _broken(jid, msg_id, value):
            raise RuntimeError("store exploded")

        def _dialog_breaks(*args, **kwargs):
            raise ValueError("dialog exploded")

        world.main_window.store_message_transcription = _broken
        monkeypatch.setattr(transcription_flow, "TranscriptionResultDialog", _dialog_breaks)
        with pytest.raises(ValueError) as raised:
            _start(world)
        assert raised.value.__context__ is None

    def test_storing_that_raises_asks_the_record_a_sync_put_in_its_place(self, world):
        """A sync swapped the flow's dict for a new copy during the run, and
        the revoke landed on that one: the flow's own dict still looks like
        audio, and asking it alone would put the withdrawn text on screen."""
        def _synced_revoked_then_broken(jid, msg_id, value):
            fresh = _msg()
            fresh["message"] = {"protocolMessage": {"type": 3}}
            fresh["messageType"] = "protocolMessage"
            records = world.main_window.chats[_JID]["messages"]["messages"]["records"]
            records[records.index(world.target)] = fresh
            world.panel._sorted_messages[world.panel._sorted_messages.index(world.target)] = fresh
            raise RuntimeError("store exploded")

        world.main_window.store_message_transcription = _synced_revoked_then_broken
        _start(world)

        assert not stored_transcription.is_withdrawn(world.target)
        assert _FakeResultDialog.made == []
        assert world.main_window.speak_output.spoken[-1] == _t("transcription_discarded_withdrawn")
        assert world.main_window.error_sound.played == 1

    def test_storing_that_raises_still_asks_the_flows_own_dict(self, world):
        """The other way round: the revoke landed on the flow's own dict —
        _apply_remote_revoke() changes only the dict it is handed — and a sync
        then put a new copy, still audio, in its place. Asking the current
        record alone would put the withdrawn text on screen."""
        def _revoked_synced_then_broken(jid, msg_id, value):
            world.target["message"] = {"protocolMessage": {"type": 3}}
            world.target["messageType"] = "protocolMessage"
            fresh = _msg()
            records = world.main_window.chats[_JID]["messages"]["messages"]["records"]
            records[records.index(world.target)] = fresh
            world.panel._sorted_messages[world.panel._sorted_messages.index(world.target)] = fresh
            raise RuntimeError("store exploded")

        world.main_window.store_message_transcription = _revoked_synced_then_broken
        _start(world)

        current = stored_transcription.find_record(
            world.main_window._transcription_copies(_JID, _ID), _ID)
        assert current is not world.target and not stored_transcription.is_withdrawn(current)
        assert _FakeResultDialog.made == []
        assert world.main_window.speak_output.spoken[-1] == _t("transcription_discarded_withdrawn")
        assert world.main_window.error_sound.played == 1

    def test_storing_that_raises_and_a_lookup_that_raises_still_shows_the_text(
            self, world, caplog):
        """Whatever broke storing may break looking the message up again too:
        the text still goes on screen, said not to be kept, and the second
        failure is one line of log with nothing about the message in it."""
        lookup = world.main_window._transcription_copies
        calls = []

        def _second_lookup_breaks(jid, msg_id):
            calls.append(msg_id)
            if len(calls) > 1:
                raise RuntimeError("lookup exploded")
            return lookup(jid, msg_id)

        class _BrokenQueue:
            def submit(self, fn, *args, **kwargs):
                raise RuntimeError("queue exploded")

        # The real store_message_transcription(): its lookup is the first
        # call and succeeds, then the write queue raises.
        world.main_window._transcription_copies = _second_lookup_breaks
        world.main_window._transcription_write_queue = _BrokenQueue()
        caplog.set_level(logging.DEBUG)
        _start(world)

        assert len(calls) == 2
        [dialog] = _FakeResultDialog.made
        assert dialog.text == RESULT.text
        assert _t("transcription_store_failed") in dialog.notes
        assert world.main_window.error_sound.played == 1
        assert "storing a transcription raised: RuntimeError: queue exploded" in caplog.text
        assert "looking the message up again raised: RuntimeError: lookup exploded" in caplog.text
        assert _ID not in caplog.text
        assert RESULT.text not in caplog.text

    def test_a_kept_one_says_nothing_about_keeping(self, world):
        _start(world)
        assert _t("transcription_store_failed") not in world.main_window.speak_output.spoken
        assert world.main_window.error_sound.played == 0

    def test_transcribing_again_is_dated_after_the_stored_one_whatever_the_clock(
            self, world, monkeypatch):
        """The clock was set back since the first transcription: dated by the
        clock alone, the new text would lose to the old one on the next sync."""
        _saved(world, at=2_000_000_000.0)
        monkeypatch.setattr(transcription_flow.time, "time", lambda: 1_000_000_000.0)
        kept = []
        world.main_window.store_message_transcription = (
            lambda jid, msg_id, value: kept.append(value) or stored_transcription.SAVE_STORED
        )
        flow = transcription_flow.MessageTranscriptionFlow(world.panel, world.target)
        flow._store(RESULT)
        [value] = kept
        assert value["at"] > 2_000_000_000.0
        assert value["text"] == RESULT.text


class TestOpeningAStoredTranscription:
    def test_the_shortcut_opens_it_without_running_anything(self, world):
        _saved(world)
        transcription_flow.open_or_transcribe(world.panel, world.target)
        assert _FakeProgressDialog.made == []
        assert world.jobs == []
        [dialog] = _FakeResultDialog.made
        assert dialog.text == "o texto guardado"
        assert _CONTACT in dialog.title
        # The value records its backend, so the headline names it.
        headline = _t("transcription_saved_opened_backend",
                      when=transcription_flow.saved_when(I18N, 1_700_000_000.0), model="small",
                      backend=_t("transcription_backend_faster_whisper"))
        assert dialog.spoken.startswith(headline)
        assert _row_focus(world.panel) == [("Focus", 1)]

    def test_the_voice_filter_warning_is_repeated_every_time(self, world):
        _saved(world, vad_used=False)
        for _ in range(2):
            transcription_flow.open_or_transcribe(world.panel, world.target)
        warning = _t("transcription_note_vad_unavailable")
        for dialog in _FakeResultDialog.made:
            assert warning in dialog.spoken
            assert warning in dialog.notes

    def test_the_date_is_not_a_warning(self, world):
        """The headline carries when and which model; the notes field is
        pointed to as "warnings", so a clean reopening points to nothing."""
        _saved(world)
        transcription_flow.open_or_transcribe(world.panel, world.target)
        [dialog] = _FakeResultDialog.made
        assert dialog.notes == []
        assert _t("transcription_result_has_notes") not in dialog.spoken

    def test_a_stored_value_without_a_model_says_when_and_with_what(self, world):
        _saved(world, model_id="")
        transcription_flow.open_or_transcribe(world.panel, world.target)
        [dialog] = _FakeResultDialog.made
        assert dialog.spoken.startswith(_t(
            "transcription_saved_opened_no_model_backend",
            when=transcription_flow.saved_when(I18N, 1_700_000_000.0),
            backend=_t("transcription_backend_faster_whisper")))

    def test_a_message_with_nothing_stored_runs_as_before(self, world):
        world.target[stored_transcription.TRANSCRIPTION_KEY] = stored_transcription.tombstone(5.0)
        transcription_flow.open_or_transcribe(world.panel, world.target)
        assert len(world.jobs) == 1

    def test_insert_from_a_stored_one_writes_the_stored_text(self, world):
        _saved(world)
        _FakeResultDialog.choose_insert = True
        transcription_flow.open_or_transcribe(world.panel, world.target)
        assert world.panel.message_field.value == "o texto guardado"


class TestDeletingAStoredTranscription:
    def test_no_leaves_it_and_puts_the_focus_back(self, world):
        _saved(world)
        _FakeMessageDialog.answer = transcription_flow.wx.ID_NO
        transcription_flow.delete_transcription(world.panel, world.target)
        assert stored_transcription.saved_transcription(world.target) is not None
        assert world.main_window.db.calls == []
        assert _row_focus(world.panel) == [("Focus", 1)]

    def test_yes_deletes_says_so_and_puts_the_focus_back(self, world):
        _saved(world)
        _FakeMessageDialog.answer = transcription_flow.wx.ID_YES
        transcription_flow.delete_transcription(world.panel, world.target)
        [question] = _FakeMessageDialog.made
        assert question.message == _t("transcription_delete_question")
        assert stored_transcription.saved_transcription(world.target) is None
        assert [c[0] for c in world.main_window.db.calls] == ["delete"]
        assert world.main_window.speak_output.spoken[-1] == _t("transcription_deleted")
        assert _row_focus(world.panel) == [("Focus", 1)]
        # And the next Alt+Shift+T runs a new transcription.
        transcription_flow.open_or_transcribe(world.panel, world.target)
        assert len(world.jobs) == 1

    def test_yes_redraws_the_row_so_its_line_drops_the_text(self, world):
        _saved(world)
        repainted = []
        world.panel._repaint_or_repopulate = repainted.append
        _FakeMessageDialog.answer = transcription_flow.wx.ID_YES
        transcription_flow.delete_transcription(world.panel, world.target)
        assert repainted == [[world.target["key"]["id"]]]

    def test_an_answer_after_the_user_moved_on_redraws_nothing(self, world):
        # _repaint_or_repopulate() would rebuild the conversation now open.
        _saved(world)
        repainted = []
        world.panel._repaint_or_repopulate = repainted.append
        _FakeMessageDialog.answer = transcription_flow.wx.ID_YES
        db_delete = world.main_window.delete_message_transcription

        def moved_on(jid, msg_id, on_done):
            world.panel.conversation = {"remoteJid": "other@s.whatsapp.net"}
            db_delete(jid, msg_id, on_done)

        world.main_window.delete_message_transcription = moved_on
        transcription_flow.delete_transcription(world.panel, world.target)
        assert repainted == []

    def test_a_failed_delete_redraws_nothing(self, world):
        _saved(world)
        repainted = []
        world.panel._repaint_or_repopulate = repainted.append
        _FakeMessageDialog.answer = transcription_flow.wx.ID_YES
        world.main_window.db.fail = True
        transcription_flow.delete_transcription(world.panel, world.target)
        assert repainted == []

    def test_a_failed_delete_keeps_it_and_says_so_with_the_error_sound(self, world):
        _saved(world)
        _FakeMessageDialog.answer = transcription_flow.wx.ID_YES
        world.main_window.db.fail = True
        transcription_flow.delete_transcription(world.panel, world.target)
        assert stored_transcription.saved_transcription(world.target) is not None
        assert world.main_window.speak_output.spoken[-1] == _t("transcription_delete_failed")
        assert world.main_window.error_sound.played == 1


class TestTheMenuOffersWhatIsStored:
    def _branch(self):
        source = inspect.getsource(ConversationsPanel.on_messages_context_menu)
        tree = ast.parse(source.lstrip())
        [guarded] = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.If) and ast.unparse(n.test) == "message_audio.is_transcribable(msg)"
        ]
        [inner] = [n for n in guarded.body if isinstance(n, ast.If)]
        return inner

    @staticmethod
    def _items(statements):
        """[(label key, shortcut shown, handler)] in the order the menu has them.

        Pairs each `x = menu.Append(..., <label>)` with the `self.Bind(...,
        <lambda calling the handler>, x)` that names the same item: three
        items each holding a label and a handler somewhere is not enough — a
        "Ver transcrição" bound to the delete handler would ask a user who
        wanted to read whether to throw the text away.
        """
        labels, order, handlers = {}, [], {}
        for node in statements:
            if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                    and ast.unparse(node.value.func) == "menu.Append"):
                [target] = node.targets
                label = node.value.args[1]
                [key] = [c.args[0].value for c in ast.walk(label)
                         if isinstance(c, ast.Call) and ast.unparse(c.func) == "i18n.t"]
                labels[target.id] = (key, "Alt+Shift+T" in ast.unparse(label))
                order.append(target.id)
            elif (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                    and ast.unparse(node.value.func) == "self.Bind"):
                _event, callback, item = node.value.args
                assert isinstance(callback, ast.Lambda)
                handlers[item.id] = callback.body.func.attr
        assert set(handlers) == set(labels), "an item without its binding, or the reverse"
        return [labels[name] + (handlers[name],) for name in order]

    def test_a_stored_one_offers_view_again_and_delete(self):
        inner = self._branch()
        assert ast.unparse(inner.test) == (
            "stored_transcription.saved_transcription(msg, "
            "stored_transcription.selection_from_settings(self.main_window.settings)) "
            "is not None")
        assert self._items(inner.body) == [
            ("transcription_view", True, "_on_menu_transcribe"),
            ("transcription_transcribe_again", False, "_on_menu_transcribe_again"),
            ("transcription_delete", False, "_on_menu_delete_transcription"),
        ]

    def test_otherwise_the_item_of_part_6(self):
        assert self._items(self._branch().orelse) == [
            ("transcribe_message", True, "_on_menu_transcribe"),
        ]

    def test_the_alt_shift_n_item_names_what_enter_does_not(self):
        from ui.conversation_panel.message_menu import inline_toggle_menu_key
        voice, video = {"messageType": "audioMessage"}, {"messageType": "videoMessage"}
        assert inline_toggle_menu_key(voice, False) == "transcribe_inline_menu"
        assert inline_toggle_menu_key(video, False) == "transcribe_inline_menu"
        assert inline_toggle_menu_key(voice, True) == "play_voice_message_menu"
        assert inline_toggle_menu_key(video, True) == "open_video_menu"
        assert inline_toggle_menu_key({"messageType": "documentMessage"}, True) == (
            "transcribe_inline_menu")

    @pytest.mark.parametrize("handler, entry", [
        ("_on_menu_transcribe", "open_or_transcribe"),
        ("_on_menu_transcribe_again", "transcribe_message"),
        ("_on_menu_delete_transcription", "delete_transcription"),
    ])
    def test_each_item_reaches_its_entry_point(self, monkeypatch, handler, entry):
        seen = []
        monkeypatch.setattr(transcription_flow, entry, lambda panel, msg: seen.append(msg))
        message = _msg("X")
        getattr(ConversationsPanel, handler)(object(), message)
        assert seen == [message]


# ── Privacy and language ─────────────────────────────────────────────────────


def test_the_flow_and_the_result_window_log_nothing_private():
    offenders = _scan(transcription_flow.__file__) + _scan(transcription_result.__file__)
    assert offenders == [], offenders


@pytest.mark.parametrize("locale", LOCALES)
def test_every_key_the_flow_asks_for_exists(locale):
    table = load_strings(locale)
    for key in transcription_flow.FLOW_I18N_KEYS:
        assert table.get(key), f"{locale} lacks {key}"
