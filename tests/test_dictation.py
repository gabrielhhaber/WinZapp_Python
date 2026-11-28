"""Dictation into the message field (ACC-03).

The state machine, the refusals and the text arithmetic are pure and tested
directly. The microphone runs against a fake audio module, the transcription
against a fake job, and the panel's handlers on a plain stub: no window opens
and no hardware is touched (tests/test_no_desktop_visible_windows.py).
"""

import pathlib
import re
import types
import wave

import pytest

import core.keyboard_shortcuts as keyboard_shortcuts
from core import dictation, dictation_capture
from core.shortcut_catalog import SHORTCUTS
from core.transcription import device, errors, job as job_module, temp_sweep
from core.transcription.backend import TranscriptionResult
from core.transcription.dictation_run import DictationTranscription
from tests.locales import load_strings, registered_locale_codes
from ui.conversation_panel.dictation import DictationMixin

CLIENT = pathlib.Path(__file__).resolve().parents[1] / "client"
LOCALES = list(registered_locale_codes())


# ── The state machine ────────────────────────────────────────────────────────


class TestTheKey:
    def test_each_state_has_its_action(self):
        assert dictation.press_action(dictation.IDLE) == dictation.ACTION_START
        assert dictation.press_action(dictation.LISTENING) == dictation.ACTION_STOP
        # Slower than a keypress: the same key is the way out.
        assert dictation.press_action(dictation.STARTING) == dictation.ACTION_CANCEL
        assert dictation.press_action(dictation.TRANSCRIBING) == dictation.ACTION_CANCEL


class TestTheStates:
    def test_the_whole_way_through(self):
        d = dictation.Dictation()
        token = d.begin()
        assert d.state == dictation.STARTING
        assert d.opened(token) and d.state == dictation.LISTENING
        assert d.stop(token) and d.state == dictation.TRANSCRIBING
        assert d.end(token) and d.state == dictation.IDLE

    def test_a_second_begin_while_busy_is_refused(self):
        d = dictation.Dictation()
        assert d.begin() is not None
        assert d.begin() is None

    def test_a_failed_opening_goes_back_to_idle(self):
        d = dictation.Dictation()
        token = d.begin()
        assert d.end(token) and d.state == dictation.IDLE
        assert d.begin() is not None

    def test_stop_only_applies_while_listening(self):
        d = dictation.Dictation()
        token = d.begin()
        assert not d.stop(token)
        assert d.state == dictation.STARTING

    def test_a_cancel_voids_what_is_still_in_flight(self):
        d = dictation.Dictation()
        old = d.begin()
        assert d.cancel() == dictation.STARTING
        assert d.state == dictation.IDLE
        assert not d.opened(old)
        new = d.begin()
        assert new != old
        # A late callback of the cancelled session does not touch the new one.
        assert not d.end(old)
        assert d.state == dictation.STARTING

    def test_cancelling_while_idle_changes_nothing(self):
        d = dictation.Dictation()
        assert d.cancel() == dictation.IDLE


class TestRefusals:
    def test_a_call_and_a_recording_are_refused_with_their_sentence(self):
        assert dictation.refusal_key(False, True) == dictation.REFUSED_CALL_I18N_KEY
        assert dictation.refusal_key(True, False) == dictation.REFUSED_RECORDING_I18N_KEY
        # The call is the more specific news.
        assert dictation.refusal_key(True, True) == dictation.REFUSED_CALL_I18N_KEY

    def test_no_audio_library_uses_the_existing_sentence(self):
        assert dictation.refusal_key(False, False, capture_missing=True) == "voice_recording_unavailable"

    def test_nothing_in_the_way(self):
        assert dictation.refusal_key(False, False) is None


# ── The text ─────────────────────────────────────────────────────────────────


class TestInsertion:
    def test_spaces_and_lines_collapse(self):
        assert dictation.normalize_transcript("  oi,\n tudo   bem?  ") == "oi, tudo bem?"
        assert dictation.normalize_transcript(None) == ""

    def test_in_an_empty_field_the_text_goes_in_as_is(self):
        assert dictation.insertion_piece("tudo bem", "", "") == "tudo bem"

    def test_after_a_word_a_space_is_added(self):
        assert dictation.insertion_piece("tudo bem", "k", "") == " tudo bem"

    def test_after_a_space_or_a_newline_none_is_added(self):
        assert dictation.insertion_piece("tudo bem", " ", "") == "tudo bem"
        assert dictation.insertion_piece("tudo bem", "\n", "") == "tudo bem"

    def test_before_a_word_a_space_is_added_but_not_before_punctuation(self):
        assert dictation.insertion_piece("a", " ", "b") == "a "
        assert dictation.insertion_piece("a", " ", ",") == "a"

    def test_nothing_to_insert(self):
        assert dictation.insertion_piece("  \n ", "x", "y") == ""

    def test_a_short_text_is_read_whole(self):
        assert dictation.spoken_excerpt("oi  tudo bem") == ("oi tudo bem", False)

    def test_a_long_text_is_cut_at_a_word(self):
        text = " ".join(["palavra"] * 100)
        excerpt, cut = dictation.spoken_excerpt(text, 30)
        assert cut
        assert len(excerpt) <= 30
        assert excerpt.split(" ") == ["palavra"] * len(excerpt.split(" "))


class TestTheCapture:
    def test_seconds_of_pcm(self):
        assert dictation.capture_seconds(32000, 16000, 1) == 1.0
        assert dictation.capture_seconds(64000, 16000, 2) == 1.0
        assert dictation.capture_seconds(-5, 16000, 1) == 0

    def test_a_key_bounce_is_too_short(self):
        assert dictation.is_too_short(0.1)
        assert not dictation.is_too_short(1.0)

    def test_the_wav_holds_the_samples(self, tmp_path):
        path = str(tmp_path / "a.wav")
        dictation.write_wav(path, [b"\x01\x00" * 800, b"\x02\x00" * 800], 16000, 1)
        with wave.open(path, "rb") as wav:
            assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
            assert wav.getnframes() == 1600


# ── The microphone ───────────────────────────────────────────────────────────


class _Stream:
    def __init__(self, fail_start=False):
        self.fail_start = fail_start
        self.started = self.stopped = self.closed = False

    def start_stream(self):
        if self.fail_start:
            raise OSError("no")
        self.started = True

    def stop_stream(self):
        self.stopped = True

    def close(self):
        self.closed = True


class _PyAudio:
    paInt16 = 8
    paContinue = 0

    def __init__(self):
        self.opened = []
        self.terminated = False
        self.refuse = set()
        self.streams = []

    def open(self, **kwargs):
        self.opened.append(kwargs)
        if kwargs["input_device_index"] in self.refuse:
            raise OSError("refused")
        stream = _Stream()
        self.streams.append(stream)
        return stream

    def terminate(self):
        self.terminated = True


class _AudioModule:
    paInt16 = 8
    paContinue = 0

    def __init__(self, pa):
        self._pa = pa

    def PyAudio(self):
        return self._pa


@pytest.fixture
def audio(monkeypatch):
    pa = _PyAudio()
    monkeypatch.setattr(dictation_capture, "recording_configs_for",
                        lambda index, pa_, prefer_stereo=False: [(16000, 1)])
    monkeypatch.setattr(dictation_capture, "find_input_device_index", lambda name, pa_: 3)
    monkeypatch.setattr(dictation_capture, "fallback_input_device_indices",
                        lambda pa_, exclude=(): [7])
    return pa, _AudioModule(pa)


class TestTheMicrophone:
    def test_it_opens_the_chosen_device_in_mono_and_collects_chunks(self, audio):
        pa, module = audio
        capture = dictation_capture.MicCapture("My mic", audio_module=module)
        assert capture.open()
        assert pa.opened[0]["input_device_index"] == 3
        assert (capture.rate, capture.channels) == (16000, 1)
        capture._callback(b"\x00\x00" * 10, 10, None, 0)
        assert capture.finish() == [b"\x00\x00" * 10]
        assert pa.streams[0].stopped and pa.streams[0].closed and pa.terminated

    def test_with_no_device_chosen_the_system_default_is_asked_for(self, audio):
        pa, module = audio
        assert dictation_capture.MicCapture("", audio_module=module).open()
        assert pa.opened[0]["input_device_index"] is None

    def test_a_vanished_device_falls_back_to_the_default(self, audio):
        pa, module = audio
        pa.refuse = {3}
        assert dictation_capture.MicCapture("gone", audio_module=module).open()
        assert [o["input_device_index"] for o in pa.opened] == [3, None]

    def test_every_device_refusing_is_a_failure_that_releases_everything(self, audio):
        pa, module = audio
        pa.refuse = {3, None, 7}
        capture = dictation_capture.MicCapture("gone", audio_module=module)
        assert not capture.open()
        assert pa.terminated

    def test_without_the_audio_library_it_does_not_open(self):
        capture = dictation_capture.MicCapture("")
        capture._audio = None
        assert not capture.open()

    def test_the_ceiling_stops_collecting_and_says_so_once(self, audio, monkeypatch):
        pa, module = audio
        monkeypatch.setattr(dictation, "MAX_SECONDS", 1.0)
        full = []
        capture = dictation_capture.MicCapture("", on_full=lambda: full.append(1),
                                               audio_module=module)
        capture.open()
        capture._callback(b"\x00" * 32000, 16000, None, 0)
        capture._callback(b"\x00" * 32000, 16000, None, 0)
        assert full == [1]
        assert len(capture.finish()) == 1

    def test_close_drops_what_was_heard(self, audio):
        pa, module = audio
        capture = dictation_capture.MicCapture("", audio_module=module)
        capture.open()
        capture._callback(b"\x00\x00", 1, None, 0)
        capture.close()
        assert capture.chunks == []


# ── The transcription ────────────────────────────────────────────────────────

RESULT = TranscriptionResult(text="zzqq texto ditado", language="pt",
                             language_probability=0.9, duration_seconds=2.0)


class _Job:
    seen = []

    def __init__(self, audio_path, ffmpeg, models_root, model_id, on_finished=None, **kwargs):
        self.audio_path = audio_path
        self.kwargs = kwargs
        self.on_finished = on_finished
        self.prepared_handover = None

    def cancel(self):
        pass

    def start(self):
        with wave.open(self.audio_path, "rb") as wav:
            _Job.seen.append((wav.getframerate(), wav.getnframes(), self.audio_path))
        self.on_finished(RESULT, None)

    def join(self, timeout=None):
        pass


def _run(tmp_path, monkeypatch, installed=("small",), settings=None, chunks=(b"\x00\x00" * 1600,)):
    import tempfile
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    _Job.seen = []
    done = []
    run = DictationTranscription(
        settings if settings is not None else {"transcription": {"model": "small"}},
        chunks, 16000, 1,
        stored_models_dir=str(tmp_path / "models"),
        find_ffmpeg=lambda: "ffmpeg.exe",
        on_finished=lambda result, error: done.append((result, error)),
        probe=lambda: device.HardwareProbe(total_ram_mb=16000, available_ram_mb=8000),
        list_installed=lambda root: installed,
        available_backends=lambda: ("faster_whisper",),
        make_job=_Job,
        clock=lambda: 0.0,
    )
    return run, done


class TestTheRun:
    def test_it_transcribes_the_captured_audio_and_leaves_nothing_on_disk(
        self, tmp_path, monkeypatch
    ):
        run, done = _run(tmp_path, monkeypatch)
        run.start()
        run.join(10)
        assert done == [(RESULT, None)]
        [(rate, frames, path)] = _Job.seen
        assert (rate, frames) == (16000, 1600)
        assert path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1] == "dictation.wav"
        assert list(tmp_path.glob("winzapp-dictation-*")) == []

    def test_the_vocabulary_of_the_settings_reaches_the_job(self, tmp_path, monkeypatch):
        settings = {"transcription": {"model": "small", "vocabulary": "WinZapp"}}
        run, done = _run(tmp_path, monkeypatch, settings=settings)
        run.start()
        run.join(10)
        assert done[0][1] is None
        assert run.job.kwargs.get("initial_prompt")

    def test_no_model_stops_before_anything_is_written_or_loaded(self, tmp_path, monkeypatch):
        run, done = _run(tmp_path, monkeypatch, installed=("tiny",))
        run.start()
        run.join(10)
        [(result, error)] = done
        assert result is None and error.code == errors.MODEL_NOT_INSTALLED
        assert _Job.seen == []
        assert list(tmp_path.glob("winzapp-dictation-*")) == []

    def test_the_preflight_raises_the_same_codes_without_a_job(self, tmp_path, monkeypatch):
        run, _done = _run(tmp_path, monkeypatch, installed=("tiny",))
        with pytest.raises(errors.TranscriptionError) as raised:
            run.preflight()
        assert raised.value.code == errors.MODEL_NOT_INSTALLED
        assert run.job is None

    def test_a_cancel_before_the_start_ends_cancelled_and_clean(self, tmp_path, monkeypatch):
        run, done = _run(tmp_path, monkeypatch)
        run.cancel()
        run.start()
        run.join(10)
        assert done[0][1].code == errors.CANCELLED
        assert list(tmp_path.glob("winzapp-dictation-*")) == []

    def test_a_killed_run_is_swept_at_the_next_start(self):
        assert "winzapp-dictation-" in temp_sweep.PREFIXES


# ── The panel ────────────────────────────────────────────────────────────────


class _I18n:
    language = "en-US"

    def t(self, key):
        return {"dictation_inserted": "Dictated: {text}",
                "dictation_inserted_long": "Long {count}: {text}"}.get(key, key)


class _Speech:
    def __init__(self):
        self.said = []

    def output(self, text, **options):
        self.said.append(text)


class _Field:
    def __init__(self, value="", selection=(0, 0), editable=True):
        self.value = value
        self.selection = selection
        self.editable = editable
        self.focus_calls = 0

    def IsShown(self):
        return True

    def IsEnabled(self):
        return True

    def IsEditable(self):
        return self.editable

    def GetSelection(self):
        return self.selection

    def GetRange(self, start, end):
        return self.value[max(0, start):end]

    def WriteText(self, text):
        start, end = self.selection
        self.value = self.value[:start] + text + self.value[end:]
        self.selection = (start + len(text),) * 2

    def SetFocus(self):
        self.focus_calls += 1


class _Window:
    def __init__(self, capturing=False, call=None):
        self.i18n = _I18n()
        self.speak_output = _Speech()
        self.settings = {}
        self._capturing = capturing
        self._active_voice_call = call
        self.hidden = False

    def _audio_capture_in_progress(self):
        return self._capturing

    def is_chat_hidden_by_vault(self, jid):
        return self.hidden


class _Panel(DictationMixin):
    def __init__(self, field=None, window=None, jid="a@s.whatsapp.net"):
        self.message_field = field or _Field()
        self.main_window = window or _Window()
        self.conversation = {"remoteJid": jid}
        self._dictation_jid = jid


def _finished(panel, text=RESULT.text, vad=True, empty=False):
    result = TranscriptionResult(text="" if empty else text, language="pt",
                                 language_probability=0.9, duration_seconds=2.0, vad_used=vad)
    panel._dictation_get().begin()
    panel._dictation_get().opened(1)
    panel._dictation_get().stop(1)
    panel._dictation_finished(1, result, None, None)


class TestInsertingTheText:
    def test_it_goes_in_at_the_caret_and_is_read_back(self):
        field = _Field("ola mundo", (3, 3))
        panel = _Panel(field)
        _finished(panel, "tudo bem")
        assert field.value == "ola tudo bem mundo"
        assert panel.main_window.speak_output.said == ["Dictated: tudo bem"]

    def test_a_space_is_added_only_where_needed(self):
        field = _Field("ola", (3, 3))
        _finished(_Panel(field), "tudo bem")
        assert field.value == "ola tudo bem"

    def test_a_selection_is_replaced_as_an_editor_does(self):
        field = _Field("ola ruim fim", (4, 8))
        _finished(_Panel(field), "bom")
        assert field.value == "ola bom fim"

    def test_the_focus_is_never_moved_and_nothing_is_sent(self):
        field = _Field()
        panel = _Panel(field)
        _finished(panel, "oi")
        assert field.focus_calls == 0
        assert not hasattr(panel, "_on_send_message")

    def test_a_long_text_is_announced_by_its_start(self):
        panel = _Panel()
        _finished(panel, " ".join(["palavra"] * 80))
        [said] = panel.main_window.speak_output.said
        assert said.startswith("Long 639: palavra")
        assert len(said) < 300

    def test_nothing_heard_writes_nothing(self):
        field = _Field("x", (1, 1))
        panel = _Panel(field)
        _finished(panel, empty=True)
        assert field.value == "x"
        assert panel.main_window.speak_output.said == ["dictation_no_speech"]

    def test_the_voice_filter_warning_is_always_spoken(self):
        panel = _Panel()
        _finished(panel, "oi", vad=False)
        [said] = panel.main_window.speak_output.said
        assert said.startswith("Dictated: oi ") and "transcription" in said.lower()

    def test_a_changed_conversation_gets_nothing_and_the_text_is_not_said(self):
        field = _Field()
        panel = _Panel(field)
        panel.conversation = {"remoteJid": "other@s.whatsapp.net"}
        _finished(panel, "segredo")
        assert field.value == ""
        assert panel.main_window.speak_output.said == ["dictation_not_inserted"]

    def test_a_chat_hidden_by_the_vault_gets_nothing(self):
        panel = _Panel()
        panel.main_window.hidden = True
        _finished(panel, "segredo")
        assert panel.message_field.value == ""
        assert "segredo" not in " ".join(panel.main_window.speak_output.said)

    def test_a_field_that_cannot_take_text_is_said(self):
        panel = _Panel(_Field(editable=False))
        _finished(panel, "oi")
        assert panel.main_window.speak_output.said == ["dictation_not_inserted"]

    def test_a_stale_session_does_nothing(self):
        panel = _Panel()
        panel._dictation_get().begin()
        panel._dictation_get().cancel()
        panel._dictation_finished(1, RESULT, None, None)
        assert panel.message_field.value == ""
        assert panel.main_window.speak_output.said == []

    def test_a_cancelled_run_says_nothing(self):
        panel = _Panel()
        panel._dictation_get().begin()
        panel._dictation_get().opened(1)
        panel._dictation_get().stop(1)
        panel._dictation_finished(
            1, None, errors.TranscriptionError(errors.CANCELLED, "x"), None)
        assert panel.main_window.speak_output.said == []
        assert panel._dictation_get().state == dictation.IDLE


class TestStartingIsRefused:
    def test_during_a_call_it_says_so_and_does_not_start(self):
        panel = _Panel(window=_Window(call={"id": "c"}))
        panel._start_dictation()
        assert panel.main_window.speak_output.said == [dictation.REFUSED_CALL_I18N_KEY]
        assert panel._dictation_get().state == dictation.IDLE

    def test_during_a_recording_it_says_so_and_does_not_start(self):
        panel = _Panel(window=_Window(capturing=True))
        panel._start_dictation()
        assert panel.main_window.speak_output.said == [dictation.REFUSED_RECORDING_I18N_KEY]
        assert panel._dictation_get().state == dictation.IDLE

    def test_a_voice_recording_refuses_to_start_while_dictating(self):
        panel = _Panel()
        assert not dictation.is_active(panel)
        assert not dictation.is_active(object())
        panel._dictation_get().begin()
        assert dictation.is_active(panel)
        for rel in ("voice_recording.py", "system_audio_recording.py"):
            source = (CLIENT / "ui/conversation_panel" / rel).read_text(encoding="utf-8")
            assert "if dictation.is_active(self):" in source

    def test_a_press_while_it_works_cancels_and_voids_the_session(self):
        panel = _Panel()
        token = panel._dictation_get().begin()
        panel._on_dictation_shortcut()
        assert panel._dictation_get().state == dictation.IDLE
        assert not panel._dictation_get().is_current(token)
        assert panel.main_window.speak_output.said == ["dictation_cancelled"]

    def test_a_conversation_change_cancels_silently(self):
        panel = _Panel()
        panel._dictation_get().begin()
        panel._cancel_dictation()
        assert panel._dictation_get().state == dictation.IDLE
        assert panel.main_window.speak_output.said == []


class TestTheWindowKnows:
    def test_a_dictation_is_an_audio_capture_for_the_background_work(self):
        source = (CLIENT / "main_window" / "calls.py").read_text(encoding="utf-8")
        assert '"_dictation_listening"' in source

    def test_the_silence_while_recording_setting_covers_it(self):
        from main_window import shortcuts

        window = types.SimpleNamespace(
            settings={"speech_content": {"silence_while_recording": True}},
            conversations_panel=types.SimpleNamespace(_dictation_listening=True),
        )
        owner = next(c for c in vars(shortcuts).values()
                     if isinstance(c, type) and "_voice_recording_silence_active" in vars(c))
        assert owner._voice_recording_silence_active(window) is True
        window.conversations_panel._dictation_listening = False
        assert owner._voice_recording_silence_active(window) is False
        window.settings = {}
        window.conversations_panel._dictation_listening = True
        assert owner._voice_recording_silence_active(window) is False


# ── The key, the strings ─────────────────────────────────────────────────────


def _row():
    [row] = [s for s in SHORTCUTS if s.id == "messages.ID_DICTATE"]
    return row


class TestTheKeyIsInTheCatalogue:
    def test_it_is_a_row_the_shortcuts_tab_can_change(self):
        row = _row()
        assert (row.scope, row.label, row.mod, row.key) == (
            "messages", "shortcut_dictate_label", 6, ord("J"))

    @pytest.mark.parametrize("locale", LOCALES)
    def test_no_other_command_shares_it_in_any_language(self, locale):
        table = load_strings(locale)
        translate = table.__getitem__
        row = _row()
        binding = keyboard_shortcuts.default_binding(row, translate)
        assert keyboard_shortcuts.conflicts(row, binding, {}, SHORTCUTS, translate) == []
        same = [
            other.id for other in SHORTCUTS
            if other.id != row.id
            and keyboard_shortcuts.default_binding(other, translate) == binding
            and keyboard_shortcuts.scopes_overlap(row.scope, other.scope)
        ]
        assert same == []

    @pytest.mark.parametrize("locale", LOCALES)
    def test_the_label_names_its_key(self, locale):
        assert load_strings(locale)["shortcut_dictate_label"].startswith("Ctrl+Shift+J: ")

    def test_it_is_in_the_messages_table_bound_and_listed_under_f1(self):
        accelerators = (CLIENT / "ui/conversation_panel/accelerators.py").read_text(encoding="utf-8")
        assert 'ord("J"),          self.ID_DICTATE),' in accelerators
        assert "self._on_dictation_shortcut" in accelerators
        dialog = (CLIENT / "ui/dialogs/shortcuts_dialog.py").read_text(encoding="utf-8")
        assert 'i18n.t("shortcut_dictate_label")' in dialog


class TestTheWords:
    @pytest.mark.parametrize("locale", LOCALES)
    def test_every_spoken_string_exists_with_its_placeholders(self, locale):
        table = load_strings(locale)
        for key in dictation.I18N_KEYS:
            assert table[key].strip(), key
        assert "{text}" in table["dictation_inserted"]
        assert {"{count}", "{text}"} <= set(re.findall(r"\{\w+\}", table["dictation_inserted_long"]))

    def test_the_modules_log_no_text(self):
        import ast
        for rel in ("core/dictation.py", "core/dictation_capture.py",
                    "ui/conversation_panel/dictation.py",
                    "core/transcription/dictation_run.py"):
            tree = ast.parse((CLIENT / rel).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "logging"):
                    names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
                    assert not names & {"text", "result", "chunks", "excerpt"}, (rel, node.lineno)


def test_dictation_menu_offered_and_label_follow_state():
    from types import SimpleNamespace as NS
    from ui.conversation_panel.dictation import DictationMixin

    def fake_panel(*, editable=True, enabled=True, shown=True,
                   conversation="chat@s.whatsapp.net", dictation_state=dictation.IDLE):
        p = NS(
            conversation={"remoteJid": conversation} if conversation else None,
            message_field=NS(
                IsEnabled=lambda: enabled,
                IsEditable=lambda: editable,
                IsShown=lambda: shown,
            ),
            shortcut_labels={"messages.ID_DICTATE": "Ctrl+Shift+J"},
            main_window=NS(i18n=NS(t=lambda k: k), settings={}),
            _dict_state=NS(state=dictation_state),
        )
        p._dictation_get = lambda: p._dict_state
        return p

    offered = DictationMixin._dictation_menu_offered
    label = DictationMixin._dictation_menu_label

    assert offered(fake_panel(dictation_state=dictation.IDLE)) is True
    assert label(fake_panel(dictation_state=dictation.IDLE)) == "dictation_menu_start\tCtrl+Shift+J"
    assert label(fake_panel(dictation_state=dictation.LISTENING)) == "dictation_menu_stop\tCtrl+Shift+J"
    assert offered(fake_panel(conversation=None)) is False
    assert offered(fake_panel(editable=False)) is False
    assert offered(fake_panel(enabled=False)) is False
    assert offered(fake_panel(shown=False)) is False
