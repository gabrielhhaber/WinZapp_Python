"""The Local Transcription tab (ui/dialogs/transcription_tab.py): what it says out loud, and what it must not break.

Four failures this pins, each of which is silent in a different way.

* **A tab inserted in the middle renumbers every other one.** The settings
  dialog addresses its pages by hardcoded index — `_notebook.SetSelection(8)`
  here, `SetSelection(4)` in main_window/settings.py — and
  `_refresh_dialog_labels()` retitles them by position. Appending is the one
  position that shifts nothing, but the new index still owes that enumeration
  a line of its own: without it the tab keeps its old caption after a language
  change, and nothing fails.

* **A combobox item is one accessibility object.** The screen reader reads the
  whole string and nothing else, so "medium", "equilibrado", how big it is and
  whether it is already downloaded all have to be inside that one line — and a
  placeholder a locale forgot to fill leaves a literal `{size}` to be read out.

* **The same warning, every single time the dialog opens — or never.**
  `resolve()` reports a substituted value and records nothing;
  `sanitize_section()` is what makes the report stop. Calling them in the wrong
  order sanitizes first and the user is never told at all; calling only the
  first tells them again tomorrow. And calling either of them from
  `_load_transcription_values()`, which runs on every open of the dialog
  whatever tab the user came for, spends the warning on a tab that was never
  put on screen: the value is rewritten, the sentence is never delivered, and
  the choice is gone with no cue of any kind. So the tab writes back only what
  it actually presented.

* **Seconds of driver I/O to open Ctrl+, .** `device.probe_hardware()` imports
  ctranslate2, asks NVML and LoadLibrary's cuBLAS. On the path of building the
  dialog that blocks the wx thread before there is a window for a screen reader
  to announce, and nothing on the tab needed the answer at that point — the
  substitutions do not depend on the probe at all.

* **A button that is there for a state it cannot act on — or missing from the
  one it must.** Remover has to exist for an interrupted install, because a
  removal that could not delete everything leaves exactly that state and its
  own sentence asks the user to remove them again; and Reparar has to survive
  a check that found damage, which `installation_state()` cannot see at all
  since it measures sizes and corruption is the right size with the wrong
  bytes.

* **Bytes written into a folder Cancel throws away.** Procurar records a
  folder and stores nothing until OK, so a model downloaded against the
  *chosen* folder and then a Cancel leaves three gigabytes where nothing in
  the app ever looks again — the model list and `list_unknown_dirs()` both
  walk the folder that is configured, so the picker offers the same download
  over again.

* **A models folder moved by Procurar, or half moved and then claimed.** The
  move belongs to OK: started from the browse handler it leaves the files in a
  folder a Cancel never stores. And a move that did not get everything across
  leaves them split between two folders, only one of which the setting can
  name — the previous one, because from there pressing OK again moves the
  rest, while the new one strands the leftovers where nothing lists or deletes
  them.

* **The CUDA libraries silently forgotten between sessions.** The folder a
  previous run downloaded into is on no loader search path when this process
  starts, so `register_installed_runtime()` has to run at startup and before
  the first device decision, or a user who paid for 550 MB is back on the
  processor with nothing in the log saying why.

SettingsDialog itself is never constructed here: it is a wx.Dialog, which the
suite may not put on the desktop at all (see
tests/test_no_desktop_visible_windows.py). The tab is built by binding its own
unbound methods onto a stub and handing them conftest's off-screen frame — the
house pattern, and the reason the page builder takes its parent as an argument.
"""

import ast
import pathlib
import re
from types import SimpleNamespace

import pytest
import wx

from coord_locks import canonical_dir
from core.transcription import backend as backend_module
from core.transcription import cuda_runtime, device, errors, management, model_catalog
from core.transcription import external_job, external_models, external_view
from core.transcription import model_names, model_store, whisper_cpp_builds, whisper_cpp_catalog
from core.transcription import precision, preferences
from core.transcription import whisper_cpp_runtime
from ui.dialogs import transcription_external, transcription_tab, transcription_whisper_cpp
from ui.dialogs.settings_dialog import SettingsDialog
from ui.dialogs.transcription_external import ExternalModelsMixin
from ui.dialogs.transcription_precision import TranscriptionPrecisionMixin
from ui.dialogs.transcription_whisper_cpp import WhisperCppMixin

from tests.conftest import destroy_now, hidden_frame
from tests.god_modules import main_window_source
from tests.locales import load_strings, registered_locale_codes

REPO = pathlib.Path(__file__).resolve().parent.parent
SETTINGS_DIALOG_SOURCE = (
    REPO / "client" / "ui" / "dialogs" / "settings_dialog.py"
).read_text(encoding="utf-8")
# The tab's own methods moved out of settings_dialog.py; the dialog still hosts
# them (SettingsDialog inherits TranscriptionTabMixin).
TRANSCRIPTION_TAB_SOURCE = (
    REPO / "client" / "ui" / "dialogs" / "transcription_tab.py"
).read_text(encoding="utf-8")
# ...and the section for models in other folders, which the dialog inherits too.
TRANSCRIPTION_EXTERNAL_SOURCE = (
    REPO / "client" / "ui" / "dialogs" / "transcription_external.py"
).read_text(encoding="utf-8")


LOCALES = registered_locale_codes()

# A machine with plenty of RAM and no graphics card — the common case, and the
# one where every "automatic" has an unambiguous answer.
_CPU_ONLY = device.HardwareProbe(total_ram_mb=16_384, available_ram_mb=12_288)


class _I18n:
    """The real translation table, so the assertions are about real strings."""

    def __init__(self, locale="pt-BR"):
        self.language = locale
        self._table = load_strings(locale)

    def t(self, key):
        return self._table.get(key, key)


class _AppSettings:
    """app_settings, minus the file. Raises for a non-global key like it does."""

    def __init__(self, models_dir="", references=()):
        self._values = {
            preferences.MODELS_DIR_SETTING: models_dir,
            external_models.EXTERNAL_MODELS_SETTING: [r.as_dict() for r in references],
        }
        #: An exception get_strict() raises, as the real one does for an
        #: app.json that is there and cannot be read; get() reads that file as
        #: the defaults, and so does this one.
        self.unreadable = None

    def get(self, key):
        if key not in self._values:
            raise KeyError(key)
        if self.unreadable is not None and key == external_models.EXTERNAL_MODELS_SETTING:
            return []
        return self._values[key]

    def get_strict(self, key):
        if self.unreadable is not None:
            raise self.unreadable
        return self.get(key)

    def set(self, key, value):
        if key != preferences.MODELS_DIR_SETTING:
            raise KeyError(key)
        self._values[key] = value

    def update(self, key, change):
        """One locked read-modify-write, like AppSettings.update()."""
        if key != external_models.EXTERNAL_MODELS_SETTING:
            raise KeyError(key)
        self._values[key] = change(list(self._values[key]))
        return self._values[key]


class _SpeakOutput:
    """MainWindow.speak_output, minus accessible_output2. Records what was said.

    Deliberately spelled `output()` and nothing else: that is the single funnel
    every announcement in the app goes through, and it is what makes the two
    Settings > Acessibilidade toggles apply. A tab that reached for `Auto()`
    itself would speak over a user who asked for silence.
    """

    def __init__(self):
        self.spoken = []
        self.interrupts = []

    def output(self, text, interrupt=False):
        self.spoken.append(text)
        self.interrupts.append(interrupt)


class _Sound:
    """main_window.error_sound, minus BASS. Counts what was played.

    The existing error sound and no other: a new sound event would need an
    .ogg in the default pack and in every pack a user has installed, plus a
    line in the Sound Events tab.
    """

    def __init__(self):
        self.plays = 0

    def play(self):
        self.plays += 1


class _MainWindow:
    def __init__(self, settings=None, locale="pt-BR", app_settings=None):
        self.settings = settings if settings is not None else {}
        self.i18n = _I18n(locale)
        # `_app_settings`, with the underscore, because that is the only name
        # MainWindow ever writes (main_window/settings.py,
        # _apply_global_settings()). A stub spelling it without one is how the
        # tab shipped reading an attribute production does not have, with six
        # green tests over it.
        self._app_settings = app_settings
        self.speak_output = _SpeakOutput()
        self.error_sound = _Sound()
        self.saves = 0

    def save_settings(self):
        self.saves += 1


class _TabOwner(ExternalModelsMixin, WhisperCppMixin, TranscriptionPrecisionMixin):
    """Stand-in for SettingsDialog carrying only what the tab touches.

    The section for models in other folders, the whisper.cpp section and the
    precision picker are inherited whole, as the dialog inherits them: their
    methods call one another and the tab's, and binding them one by one here
    is how a stub drifts from what ships.
    """

    def __init__(self, main_window):
        self.main_window = main_window
        self.dirtied = 0

    def _mark_dirty(self, event=None):
        # Same guard as SettingsDialog._mark_dirty: populating the controls
        # while the dialog opens is not an edit. Without it the stub would count
        # what production deliberately ignores, and a dirtied == 0 assertion
        # could only ever be written for code that never runs at load time.
        if getattr(self, "_loading_values", False):
            return
        self.dirtied += 1

    _build_transcription_page = SettingsDialog._build_transcription_page
    _install_wide_settings = SettingsDialog._install_wide_settings
    _stored_transcription_models_dir = SettingsDialog._stored_transcription_models_dir
    _show_transcription_models_dir = SettingsDialog._show_transcription_models_dir
    _populate_transcription_model_choices = (
        SettingsDialog._populate_transcription_model_choices
    )
    _populate_transcription_language_choices = (
        SettingsDialog._populate_transcription_language_choices
    )
    _populate_transcription_backend_choices = (
        SettingsDialog._populate_transcription_backend_choices
    )
    _selected_transcription_model = SettingsDialog._selected_transcription_model
    _selected_transcription_language = SettingsDialog._selected_transcription_language
    _selected_transcription_backend = SettingsDialog._selected_transcription_backend
    _select_transcription_model = SettingsDialog._select_transcription_model
    _select_transcription_language = SettingsDialog._select_transcription_language
    _select_transcription_backend = SettingsDialog._select_transcription_backend
    # Genuinely static on the dialog — rewrapped, or they would be handed the
    # stub as their first argument.
    _selected_id = staticmethod(SettingsDialog._selected_id)
    _select_id = staticmethod(SettingsDialog._select_id)
    _sync_transcription_language_controls = (
        SettingsDialog._sync_transcription_language_controls
    )
    _selected_transcription_device_preference = (
        SettingsDialog._selected_transcription_device_preference
    )
    _show_transcription_substitutions = SettingsDialog._show_transcription_substitutions
    _show_transcription_hardware_notices = (
        SettingsDialog._show_transcription_hardware_notices
    )
    _transcription_hardware_notice_keys = (
        SettingsDialog._transcription_hardware_notice_keys
    )
    _render_transcription_substitutions = (
        SettingsDialog._render_transcription_substitutions
    )
    _show_transcription_cuda_status = SettingsDialog._show_transcription_cuda_status
    _refresh_transcription_models = SettingsDialog._refresh_transcription_models
    _load_transcription_values = SettingsDialog._load_transcription_values
    _enter_transcription_page = SettingsDialog._enter_transcription_page
    _transcription_setting_may_be_written = (
        SettingsDialog._transcription_setting_may_be_written
    )
    _apply_transcription_values = SettingsDialog._apply_transcription_values
    _refresh_transcription_labels = SettingsDialog._refresh_transcription_labels
    _on_transcription_detect_language_toggle = (
        SettingsDialog._on_transcription_detect_language_toggle
    )
    _on_transcription_device_change = SettingsDialog._on_transcription_device_change
    _on_transcription_language_change = SettingsDialog._on_transcription_language_change
    _transcription_custom_model_ids = SettingsDialog._transcription_custom_model_ids
    _refuse_transcription_models_dir = SettingsDialog._refuse_transcription_models_dir
    _on_browse_transcription_models_dir = (
        SettingsDialog._on_browse_transcription_models_dir
    )
    # ── Part 5c-2: the eight action buttons and what they run ───────────────
    _build_transcription_action_row = SettingsDialog._build_transcription_action_row
    _transcription_model_state = SettingsDialog._transcription_model_state
    _transcription_cuda_button_state = SettingsDialog._transcription_cuda_button_state
    _sync_transcription_action_buttons = (
        SettingsDialog._sync_transcription_action_buttons
    )
    _set_transcription_job_running = SettingsDialog._set_transcription_job_running
    _on_transcription_model_change = SettingsDialog._on_transcription_model_change
    _on_transcription_action = SettingsDialog._on_transcription_action
    _start_transcription_action = SettingsDialog._start_transcription_action
    _measure_transcription_download = SettingsDialog._measure_transcription_download
    _confirm_transcription_download = SettingsDialog._confirm_transcription_download
    _ask_transcription_download = SettingsDialog._ask_transcription_download
    _run_transcription_job = SettingsDialog._run_transcription_job
    _report_transcription_job = SettingsDialog._report_transcription_job
    _note_transcription_outcome = SettingsDialog._note_transcription_outcome
    _announce_transcription_result = SettingsDialog._announce_transcription_result
    _refresh_after_transcription_action = (
        SettingsDialog._refresh_after_transcription_action
    )
    _adopt_transcription_probe = SettingsDialog._adopt_transcription_probe
    _restore_transcription_focus = SettingsDialog._restore_transcription_focus
    _transcription_models_dir_applied = (
        SettingsDialog._transcription_models_dir_applied
    )
    _ask_transcription_removal = SettingsDialog._ask_transcription_removal
    _move_transcription_models = SettingsDialog._move_transcription_models


@pytest.fixture
def no_hardware_probe(monkeypatch):
    """Answer the machine's questions from the test instead of the machine.

    probe_hardware() loads NVML and cuda_runtime.installation_state() reads the
    install-wide folder — on the developer's own machine, which is exactly what
    device.py's docstring says a decision must never depend on.
    """
    monkeypatch.setattr(
        transcription_tab.transcription_device, "probe_hardware", lambda: _CPU_ONLY
    )
    # The tab takes its probe on a worker and adopts it through wx.CallAfter;
    # here the probe answers at once and its CallAfter runs inline, so a test
    # sees the tab as it stands once the answer is in. Only that delivery is
    # inline — every other CallAfter stays queued as it was.
    def _probe_at_once(on_done, probe=None):
        answer = (probe or transcription_tab.transcription_device.probe_hardware)()
        # Whatever CallAfter is now — a test may have replaced it too.
        call_after = transcription_tab.wx.CallAfter
        transcription_tab.wx.CallAfter = lambda func, *args, **kwargs: func(*args, **kwargs)
        try:
            on_done(answer)
        finally:
            transcription_tab.wx.CallAfter = call_after

    monkeypatch.setattr(
        transcription_tab.transcription_management, "probe_in_background", _probe_at_once
    )
    monkeypatch.setattr(
        transcription_tab.cuda_runtime,
        "installation_state",
        lambda directory=None: cuda_runtime.RuntimeState(cuda_runtime.STATE_ABSENT, ()),
    )
    # The whisper.cpp builds live in an install-wide folder too.
    monkeypatch.setattr(
        transcription_whisper_cpp.whisper_cpp_runtime,
        "installation_state",
        lambda build, root=None: whisper_cpp_runtime.RuntimeState(
            whisper_cpp_runtime.STATE_ABSENT
        ),
    )


@pytest.fixture
def tab(wx_app, tmp_path, no_hardware_probe):
    """A built local Transcription tab, on an off-screen parent, with an empty folder."""
    frame = hidden_frame()
    owner = _TabOwner(_MainWindow(app_settings=_AppSettings(str(tmp_path))))
    owner._transcription_page = owner._build_transcription_page(frame)
    # The dialog binds EVT_TEXT to _mark_dirty at dialog level and relies on
    # command-event propagation to catch every text control. Without the same
    # binding here, a field written with SetValue() — which fires EVT_TEXT even
    # for identical text — dirties the real dialog and nothing in this suite
    # can see it. That is exactly how merely arriving on the tab came to show
    # the Apply button.
    frame.Bind(wx.EVT_TEXT, owner._mark_dirty)
    try:
        yield owner
    finally:
        destroy_now(frame)


class TestLookingIsNotEditing:
    """Arriving on the tab, or redrawing it, must not make Apply appear.

    The warning field, the CUDA status line and the folder field only *show*
    something. Written with SetValue() they fired EVT_TEXT, the dialog routed it
    to _mark_dirty, and a user who arrowed onto "Transcrição" and changed
    nothing got an Apply button and an extra tab stop — the behaviour
    _mark_dirty was introduced to remove.
    """

    def _open(self, tab):
        # As _load_values() does it in the real dialog: under the load guard.
        tab._loading_values = True
        try:
            tab._load_transcription_values()
        finally:
            tab._loading_values = False
        tab.dirtied = 0

    def test_arriving_on_the_tab_changes_nothing(self, tab):
        self._open(tab)
        tab._enter_transcription_page()
        assert tab.dirtied == 0

    def test_redrawing_what_the_folder_holds_changes_nothing(self, tab):
        self._open(tab)
        tab._enter_transcription_page()
        tab._refresh_transcription_models()
        assert tab.dirtied == 0

    def test_the_binding_can_see_a_real_edit(self, tab):
        """Guards the two tests above from passing because nothing is wired."""
        self._open(tab)
        tab._transcription_models_dir_field.SetValue("typed by hand")
        assert tab.dirtied == 1


class TestTheTabIsAppendedAtTheEnd:
    """Inserting one in the middle renumbers every hardcoded index there is."""

    def _add_pages(self):
        return re.findall(
            r"AddPage\(\s*[^,]+,\s*i18n\.t\(\"([a-z_]+)\"\)\)", SETTINGS_DIALOG_SOURCE
        )

    def _page_texts(self):
        return [
            (int(index), key)
            for index, key in re.findall(
                r"SetPageText\((\d+), i18n\.t\(\"([a-z_]+)\"\)\)",
                SETTINGS_DIALOG_SOURCE,
            )
        ]

    def test_transcription_is_the_last_page_added_before_shortcuts_mixin(self):
        assert self._add_pages()[-1] == "tab_transcription"

    def test_the_retranslation_enumerates_every_page_at_its_own_index(self):
        """Missing line = a tab caption that never follows a language change.

        Every inline AddPage but the last two is retranslated at a fixed index.
        Those two are the AI page and Transcription, after the conditional
        "Locked chats" tab — Transcription's index is 15 or 16 depending on
        whether that tab is shown — so each is looked up with FindPage()
        instead of numbered (the AI page's own line is pinned by
        tests/test_ai_media_wiring.py). Shortcuts is appended and retranslated
        separately by ShortcutsTabMixin; the real notebook order is checked
        in tests/test_settings_files_saving_tab.py."""
        added = self._add_pages()
        assert added[-2:] == ["tab_ai_accessibility", "tab_transcription"]
        assert [key for _index, key in self._page_texts()] == added[:-2]
        assert [index for index, _key in self._page_texts()] == list(range(len(added) - 2))
        assert re.search(
            r"SetPageText\(\s*self\._notebook\.FindPage\(self\._transcription_page\),"
            r"\s*i18n\.t\(\"tab_transcription\"\),?\s*\)",
            SETTINGS_DIALOG_SOURCE,
        )

    def test_no_hardcoded_page_selection_reaches_the_new_tab(self):
        """Every SetSelection() in this file names a page below the new one, so
        appending could not have moved what any of them points at."""
        selections = [
            int(index)
            for index in re.findall(
                r"_notebook\.SetSelection\((\d+)\)", SETTINGS_DIALOG_SOURCE
            )
        ]
        assert selections, "the hardcoded selections are what this guards"
        assert max(selections) < len(self._add_pages()) - 1


class TestTheStartupRegistrationIsWired:
    """Nothing else puts the downloaded CUDA folder on the loader's path."""

    @staticmethod
    def _main_window_init():
        source = (REPO / "client" / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "MainWindow":
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                        return item
        raise AssertionError("MainWindow.__init__ not found")

    @staticmethod
    def _call_lines(function, name):
        return [
            node.lineno
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and getattr(node.func, "attr", None) == name
        ]

    def test_it_is_called_from_main_window_init(self):
        init = self._main_window_init()
        assert self._call_lines(init, "register_installed_runtime")

    def test_it_runs_after_the_settings_are_loaded(self):
        """After load_settings(), so a configured folder is already readable."""
        init = self._main_window_init()
        assert min(self._call_lines(init, "register_installed_runtime")) > min(
            self._call_lines(init, "load_settings")
        )

    def test_it_runs_before_the_first_device_decision(self):
        """A guard for a call that does not exist yet, and says so.

        Registering after the probe answers the old question: the probe decides
        by loading the libraries, so a directory added afterwards is one the
        answer already ignored. `MainWindow.__init__` reaches neither
        `probe_hardware()` nor `resolve_device()` today — the transcription tab
        takes its own probe on first visit — so `probes` is empty and this
        assertion is vacuous *by design*. It is here to fail the day something
        in `__init__` starts asking, which is the only day it can be got wrong;
        an `assert probes` would fail today instead, for nothing.
        """
        init = self._main_window_init()
        registered = min(self._call_lines(init, "register_installed_runtime"))
        probes = self._call_lines(init, "probe_hardware") + self._call_lines(
            init, "resolve_device"
        )
        assert all(registered < line for line in probes)


class TestTheModelChoicesReadAsOneSentence:
    """A combobox item is a single accessibility object: everything the user
    needs to choose between two models has to be inside that one string."""

    @staticmethod
    def _model(model_id):
        return model_catalog.get_model(model_id)

    def test_an_installed_model_says_so_and_quotes_its_disk_size(self):
        i18n = _I18n()
        label = transcription_tab._transcription_model_choice_label(
            i18n, self._model("medium"), model_store.InstallState(model_store.STATE_INSTALLED)
        )
        assert label.startswith("medium: ")
        assert i18n.t("transcription_size_balanced") in label
        assert "1,4 GB" in label
        assert "instalado" in label

    def test_a_model_that_is_not_here_quotes_the_download_instead(self):
        i18n = _I18n()
        label = transcription_tab._transcription_model_choice_label(
            i18n, self._model("tiny"), model_store.InstallState(model_store.STATE_ABSENT)
        )
        assert "75 MB" in label
        assert "não instalado" in label

    def test_an_interrupted_download_is_neither_of_those(self):
        i18n = _I18n()
        states = {
            state: transcription_tab._transcription_model_choice_label(
                i18n, self._model("small"), model_store.InstallState(state)
            )
            for state in (
                model_store.STATE_INSTALLED,
                model_store.STATE_ABSENT,
                model_store.STATE_INCOMPLETE,
            )
        }
        assert len(set(states.values())) == 3

    def test_a_folder_that_could_not_be_measured_reads_as_not_installed(self):
        """None is what a caller with no answer passes, and telling the user a
        model is installed on that basis is the one wrong answer."""
        i18n = _I18n()
        assert transcription_tab._transcription_model_choice_label(
            i18n, self._model("base"), None
        ) == transcription_tab._transcription_model_choice_label(
            i18n, self._model("base"), model_store.InstallState(model_store.STATE_ABSENT)
        )

    @pytest.mark.parametrize("locale", LOCALES)
    def test_no_locale_leaves_a_placeholder_to_be_read_out(self, locale):
        i18n = _I18n(locale)
        for model in model_catalog.list_models():
            for state in (model_store.STATE_INSTALLED, model_store.STATE_ABSENT,
                          model_store.STATE_INCOMPLETE):
                label = transcription_tab._transcription_model_choice_label(
                    i18n, model, model_store.InstallState(state)
                )
                assert "{" not in label and "}" not in label, (locale, model.id)
                assert model.id in label

    def test_the_size_uses_the_locale_decimal_separator(self):
        assert transcription_tab._format_transcription_size(_I18n("pt-BR"), 1_610_612_736) \
            == "1,5 GB"
        assert transcription_tab._format_transcription_size(_I18n("en-US"), 1_610_612_736) \
            == "1.5 GB"

    def test_the_combobox_offers_automatic_first_and_then_the_catalogue(self, tab):
        combo = tab._transcription_model_combo
        assert combo.GetString(0) == tab.main_window.i18n.t("transcription_option_auto")
        assert tab._transcription_model_ids == [preferences.AUTO] + [
            model.id for model in model_catalog.list_models()
        ]
        # Nothing was downloaded into the fixture's folder, so every entry has
        # to say so — a list that claims otherwise sends the user to a run that
        # fails with MODEL_NOT_INSTALLED.
        for index in range(1, combo.GetCount()):
            assert "não instalado" in combo.GetString(index)

    def test_an_installed_model_shows_as_installed_in_the_list(
        self, tab, monkeypatch
    ):
        monkeypatch.setattr(
            transcription_tab.model_store,
            "installation_state",
            lambda root, model: model_store.InstallState(
                model_store.STATE_INSTALLED if model.id == "small"
                else model_store.STATE_ABSENT
            ),
        )
        tab._populate_transcription_model_choices()
        index = tab._transcription_model_ids.index("small")
        assert "instalado" in tab._transcription_model_combo.GetString(index)
        assert "não instalado" not in tab._transcription_model_combo.GetString(index)

    def test_rebuilding_the_list_keeps_the_selection(self, tab):
        tab._select_transcription_model("large-v3")
        tab._populate_transcription_model_choices()
        assert tab._selected_transcription_model() == "large-v3"


class TestTheLanguageControls:
    """The checkbox and the list answer two different questions — which is why
    they are two controls, and why one has to drive the other."""

    def test_the_list_starts_with_the_interface_language_sentinel(self, tab):
        assert tab._transcription_language_codes[0] == preferences.LANGUAGE_INTERFACE
        assert tab._transcription_language_combo.GetString(0) == tab.main_window.i18n.t(
            "transcription_language_interface"
        )

    def test_the_endonyms_are_offered_with_the_apps_own_language_first(self, tab):
        assert tab._transcription_language_codes[1] == "pt"
        assert tab._transcription_language_combo.GetString(1) == "português"

    def test_detecting_automatically_disables_the_list(self, tab):
        tab._transcription_detect_language_check.SetValue(True)
        tab._sync_transcription_language_controls()
        assert not tab._transcription_language_combo.IsEnabled()
        assert not tab._transcription_language_label.IsEnabled()

    def test_turning_detection_off_enables_it_again(self, tab):
        tab._transcription_detect_language_check.SetValue(True)
        tab._sync_transcription_language_controls()
        tab._transcription_detect_language_check.SetValue(False)
        tab._sync_transcription_language_controls()
        assert tab._transcription_language_combo.IsEnabled()
        assert tab._transcription_language_label.IsEnabled()

    def test_the_checkbox_handler_syncs_and_lets_the_event_through(self, tab):
        """The dialog-level _mark_dirty() only fires on events a control
        handler passes on with Skip()."""
        skipped = []

        class _Event:
            def Skip(self):
                skipped.append(True)

        tab._transcription_detect_language_check.SetValue(True)
        tab._on_transcription_detect_language_toggle(_Event())
        assert not tab._transcription_language_combo.IsEnabled()
        assert skipped == [True]


class TestEverySettingIsReadBackAndWritten:
    def test_the_defaults_load_as_the_automatic_positions(self, tab):
        tab._load_transcription_values()
        assert tab._selected_transcription_model() == preferences.AUTO
        assert tab._transcription_device_radio.GetSelection() == 0
        assert tab._transcription_detect_language_check.GetValue() is True
        assert tab._selected_transcription_language() == preferences.LANGUAGE_INTERFACE

    def test_a_stored_choice_is_selected_when_the_tab_opens(self, tab):
        tab.main_window.settings["transcription"] = {
            "model": "large-v3-turbo",
            "device": device.PREFERENCE_CPU,
            "language": "pl",
            "auto_detect_language": False,
        }
        tab._load_transcription_values()
        assert tab._selected_transcription_model() == "large-v3-turbo"
        assert tab._transcription_device_radio.GetSelection() == 2
        assert tab._selected_transcription_language() == "pl"
        assert tab._transcription_detect_language_check.GetValue() is False
        assert tab._transcription_language_combo.IsEnabled()

    def test_what_the_user_picks_is_what_reaches_settings_json(self, tab):
        tab._load_transcription_values()
        tab._select_transcription_model("small")
        tab._transcription_device_radio.SetSelection(1)
        tab._transcription_detect_language_check.SetValue(False)
        tab._select_transcription_language("es")
        tab._apply_transcription_values()

        section = tab.main_window.settings["transcription"]
        assert section["model"] == "small"
        assert section["device"] == device.PREFERENCE_CUDA
        assert section["auto_detect_language"] is False
        assert section["language"] == "es"

    def test_the_language_survives_detection_being_turned_back_on(self, tab):
        """It is a preference, not an instruction: preferred_language() answers
        it whatever the checkbox says, so dropping it would lose a choice the
        user has no way of getting back."""
        tab._load_transcription_values()
        tab._select_transcription_language("pl")
        tab._transcription_detect_language_check.SetValue(True)
        tab._apply_transcription_values()
        assert tab.main_window.settings["transcription"]["language"] == "pl"

    def test_a_round_trip_through_the_tab_changes_nothing_on_its_own(self, tab):
        stored = {
            "backend": preferences.AUTO,
            "model": "base",
            "device": device.PREFERENCE_CUDA,
            "compute_type": device.COMPUTE_INT8_FLOAT16,
            "language": "fr",
            "auto_detect_language": False,
        }
        tab.main_window.settings["transcription"] = dict(stored)
        tab._load_transcription_values()
        tab._apply_transcription_values()
        assert tab.main_window.settings["transcription"] == stored

    def test_the_backend_is_offered_and_written_back(self, tab):
        """Two backends since part 9b: the picker is there, a stored choice is
        selected when the tab opens and reaches settings.json unchanged."""
        assert tab._transcription_backend_combo is not None
        stored = backend_module.BACKEND_WHISPER_CPP
        tab.main_window.settings["transcription"] = {"backend": stored}
        tab._load_transcription_values()
        assert tab._selected_transcription_backend() == stored
        tab._apply_transcription_values()
        assert tab.main_window.settings["transcription"]["backend"] == stored


class TestThePrecisionPicker:
    """Part 11: faster-whisper's compute type, chosen on the tab.

    It lists what the device the run would land on can run, keeps a stored
    choice the device cannot run (with a sentence saying what replaces it,
    rather than OK quietly writing "automatic" over it), and steps aside —
    disabled, in place — while whisper.cpp, whose precision is the file, is
    the backend.
    """

    _INTEL = ("float32", "int16", "int8", "int8_float32")

    class _Event:
        def __init__(self):
            self.skipped = False

        def Skip(self):
            self.skipped = True

    def _probe(self):
        return device.HardwareProbe(total_ram_mb=16_384, available_ram_mb=12_288,
                                    cpu_compute_types=self._INTEL)

    def test_it_sits_right_under_the_device_it_depends_on(self, tab):
        """Tab order is creation order: after the device radio, before the
        language checkbox."""
        children = list(tab._transcription_page.GetChildren())
        label = children.index(tab._transcription_precision_label)
        assert children.index(tab._transcription_device_radio) < label
        assert children.index(tab._transcription_precision_combo) == label + 1
        assert children.index(tab._transcription_detect_language_check) > label + 1

    def test_automatic_by_default_and_everything_listed_before_the_probe(self, tab):
        tab._load_transcription_values()
        assert tab._selected_transcription_precision() == preferences.AUTO
        assert tuple(tab._transcription_precision_values) == (
            (preferences.AUTO,) + precision.COMPUTE_TYPES)
        assert tab._transcription_precision_combo.GetString(0) == (
            tab.main_window.i18n.t(preferences.OPTION_AUTO_I18N_KEY))
        assert tab._transcription_precision_combo.IsEnabled()

    def test_after_the_probe_only_what_the_device_runs_is_listed(self, tab):
        tab._load_transcription_values()
        tab._adopt_transcription_probe(self._probe())
        assert tuple(tab._transcription_precision_values) == (
            preferences.AUTO, "int8", "int8_float32", "int16", "float32")
        i18n = tab.main_window.i18n
        assert tab._transcription_precision_combo.GetString(1) == (
            i18n.t("transcription_precision_int8"))

    def test_a_stored_choice_is_selected_and_written_back(self, tab):
        tab.main_window.settings["transcription"] = {"compute_type": "int8_float16"}
        tab._load_transcription_values()
        assert tab._selected_transcription_precision() == "int8_float16"
        tab._apply_transcription_values()
        assert tab.main_window.settings["transcription"]["compute_type"] == "int8_float16"

    def test_a_choice_the_device_cannot_run_stays_and_the_tab_says_why(self, tab):
        tab.main_window.settings["transcription"] = {"compute_type": "float16"}
        tab._load_transcription_values()
        tab._adopt_transcription_probe(self._probe())
        assert tab._selected_transcription_precision() == "float16"
        i18n = tab.main_window.i18n
        assert i18n.t("transcription_notice_precision_replaced").format(
            chosen=i18n.t("transcription_precision_float16"),
            used=i18n.t("transcription_precision_float32"),
        ) in tab._transcription_substituted_field.GetValue()
        tab._apply_transcription_values()
        assert tab.main_window.settings["transcription"]["compute_type"] == "float16"

    def test_whisper_cpp_disables_it_in_place(self, tab):
        """Its quantization is the model file; the control stays where it is
        so the tab order does not move under the user."""
        tab._load_transcription_values()
        tab._select_transcription_backend(backend_module.BACKEND_WHISPER_CPP)
        event = self._Event()
        tab._on_transcription_backend_change(event)
        assert not tab._transcription_precision_combo.IsEnabled()
        assert not tab._transcription_precision_label.IsEnabled()
        assert event.skipped
        tab._select_transcription_backend(backend_module.BACKEND_FASTER_WHISPER)
        tab._on_transcription_backend_change(self._Event())
        assert tab._transcription_precision_combo.IsEnabled()

    def test_an_unknown_stored_value_is_reported_and_not_written_over(self, tab):
        tab.main_window.settings["transcription"] = {"compute_type": "int4"}
        tab._load_transcription_values()
        assert tab._transcription_substituted_field.GetValue() == (
            tab.main_window.i18n.t("transcription_substituted_compute_type"))
        tab._apply_transcription_values()
        assert tab.main_window.settings["transcription"]["compute_type"] == "int4"

    def test_a_language_change_retranslates_the_list(self, tab):
        tab._load_transcription_values()
        tab.main_window.i18n = _I18n("pl")
        tab._refresh_transcription_labels()
        assert tab._transcription_precision_label.GetLabel() == (
            _I18n("pl").t("transcription_precision_label"))
        assert tab._transcription_precision_combo.GetString(1) == (
            _I18n("pl").t("transcription_precision_int8"))

    def test_choosing_one_lets_the_event_through(self, tab):
        """The dialog-level handler is what shows Apply."""
        tab._load_transcription_values()
        event = self._Event()
        tab._on_transcription_precision_change(event)
        assert event.skipped


class TestTheModelsFolder:
    def test_the_field_shows_the_resolved_folder(self, tab, tmp_path):
        tab._load_transcription_values()
        assert tab._transcription_models_dir_field.GetValue() == str(tmp_path)

    def test_an_empty_setting_resolves_to_the_default_folder(self, wx_app, no_hardware_probe):
        frame = hidden_frame()
        try:
            owner = _TabOwner(_MainWindow(app_settings=_AppSettings("")))
            owner._transcription_page = owner._build_transcription_page(frame)
            assert owner._transcription_models_dir == ""
            assert owner._transcription_models_dir_field.GetValue() == (
                model_store.default_models_dir()
            )
        finally:
            destroy_now(frame)

    def test_the_field_is_not_typed_into(self, tab):
        """Writing the resolved path back would freeze a data folder that is
        meant to be copyable to another machine."""
        assert not tab._transcription_models_dir_field.IsEditable()

    def test_a_changed_folder_is_written_install_wide(self, tab, tmp_path):
        tab._load_transcription_values()
        tab._transcription_models_dir = str(tmp_path / "elsewhere")
        tab._apply_transcription_values()
        assert tab.main_window._app_settings.get(preferences.MODELS_DIR_SETTING) == str(
            tmp_path / "elsewhere"
        )
        # And never into this account's own settings.json — the files are
        # shared by every account.
        assert preferences.MODELS_DIR_SETTING not in tab.main_window.settings.get(
            "transcription", {}
        )

    def test_an_unchanged_folder_is_not_rewritten(self, tab, tmp_path):
        written = []
        tab.main_window._app_settings.set = lambda key, value: written.append(key)
        tab._load_transcription_values()
        tab._apply_transcription_values()
        assert written == []

    def test_a_window_without_app_settings_still_opens_the_tab(
        self, wx_app, no_hardware_probe
    ):
        """account-less/legacy windows: the tab must open, on the default."""
        frame = hidden_frame()
        try:
            owner = _TabOwner(_MainWindow(app_settings=None))
            owner._transcription_page = owner._build_transcription_page(frame)
            owner._load_transcription_values()
            owner._apply_transcription_values()
            assert owner._transcription_models_dir == ""
        finally:
            destroy_now(frame)


class TestTheSubstitutionWarningIsSaidOnceAndOnce:
    def test_a_retired_model_is_reported_when_the_tab_opens(self, tab):
        tab.main_window.settings["transcription"] = {"model": "whisper-from-2019"}
        tab._load_transcription_values()
        assert tab._transcription_substituted_field.GetValue() == tab.main_window.i18n.t(
            "transcription_substituted_model"
        )
        assert tab._transcription_substituted_field.IsShown()

    def test_the_stored_value_itself_is_never_read_out(self, tab):
        tab.main_window.settings["transcription"] = {"model": "whisper-from-2019"}
        tab._load_transcription_values()
        assert "whisper-from-2019" not in tab._transcription_substituted_field.GetValue()

    def test_it_is_spoken_when_the_tab_is_actually_selected(self, tab):
        """A read-only field on a tab nobody selected is a cue for nobody, and
        under NVDA it is not one even with the tab open until focus reaches it.
        Through speak_output, never a second accessible_output2 of our own."""
        tab.main_window.settings["transcription"] = {"model": "whisper-from-2019"}
        tab._load_transcription_values()
        assert tab.main_window.speak_output.spoken == []

        tab._enter_transcription_page()
        assert tab.main_window.speak_output.spoken == [
            tab.main_window.i18n.t("transcription_substituted_model")
        ]

    def test_it_is_spoken_once_per_opening_however_often_the_tab_is_revisited(
        self, tab
    ):
        tab.main_window.settings["transcription"] = {"model": "whisper-from-2019"}
        tab._load_transcription_values()
        tab._enter_transcription_page()
        tab._enter_transcription_page()
        tab._enter_transcription_page()
        assert len(tab.main_window.speak_output.spoken) == 1

    def test_nothing_is_spoken_when_there_was_nothing_to_replace(self, tab):
        tab.main_window.settings["transcription"] = dict(preferences.DEFAULTS)
        tab._load_transcription_values()
        tab._enter_transcription_page()
        assert tab.main_window.speak_output.spoken == []

    def test_opening_the_dialog_without_visiting_the_tab_rewrites_nothing(self, tab):
        """The bug this pins: the user opens Configurações to change the
        interface language, the notebook is on page 0, and the warning is drawn
        and consumed on a tab nobody saw. sanitize_section() is what consumes
        it, so it may not run until the tab has been on screen."""
        tab.main_window.settings["transcription"] = {"model": "whisper-from-2019"}
        tab._load_transcription_values()
        assert tab.main_window.saves == 0
        assert tab.main_window.settings["transcription"]["model"] == "whisper-from-2019"

    def test_the_second_opening_says_nothing(self, tab):
        """sanitize_section() rewrote the dead value, which is the whole point
        of calling it after resolve() rather than instead of it — and after the
        sentence has actually been delivered."""
        tab.main_window.settings["transcription"] = {"model": "whisper-from-2019"}
        tab._load_transcription_values()
        tab._enter_transcription_page()
        assert tab.main_window.saves == 1

        tab._transcription_page_seen = False
        tab._load_transcription_values()
        assert tab._transcription_substituted_field.GetValue() == ""
        assert not tab._transcription_substituted_field.IsShown()
        tab._enter_transcription_page()
        assert tab.main_window.saves == 1

    def test_a_settings_file_with_nothing_wrong_is_never_saved_on_open(self, tab):
        tab.main_window.settings["transcription"] = dict(preferences.DEFAULTS)
        tab._load_transcription_values()
        tab._enter_transcription_page()
        assert tab.main_window.saves == 0
        assert not tab._transcription_substituted_field.IsShown()

    def test_a_model_that_is_merely_not_downloaded_is_not_a_substitution(self, tab):
        """MODEL_NOT_INSTALLED is an offer to download, not a value that was
        replaced — warning about it would be a warning about nothing."""
        tab.main_window.settings["transcription"] = {"model": "large-v3"}
        tab._load_transcription_values()
        assert tab._transcription_substituted_field.GetValue() == ""
        assert tab._selected_transcription_model() == "large-v3"


class TestTheTabWritesBackOnlyWhatItShowed:
    """An OK pressed from another tab must not consume a warning that tab never
    delivered. The controls show `resolve()`'s *replacement*, so writing them
    back is the silent swap preferences.py exists to prevent — one step worse
    than the original, because now it is on disk."""

    def test_a_substituted_model_survives_an_ok_from_another_tab(self, tab):
        tab.main_window.settings["transcription"] = {"model": "whisper-from-2019"}
        tab._load_transcription_values()
        # The combobox has already fallen back to "Automático" — that is the
        # value that must not reach settings.json unannounced.
        assert tab._selected_transcription_model() == preferences.AUTO

        tab._apply_transcription_values()
        assert tab.main_window.settings["transcription"]["model"] == "whisper-from-2019"

    def test_the_same_ok_after_visiting_the_tab_does_write_it(self, tab):
        tab.main_window.settings["transcription"] = {"model": "whisper-from-2019"}
        tab._load_transcription_values()
        tab._enter_transcription_page()
        tab._apply_transcription_values()
        assert tab.main_window.settings["transcription"]["model"] == preferences.AUTO

    def test_a_substituted_language_survives_it_too(self, tab):
        tab.main_window.settings["transcription"] = {
            "language": "klingon", "auto_detect_language": False,
        }
        tab._load_transcription_values()
        tab._apply_transcription_values()
        assert tab.main_window.settings["transcription"]["language"] == "klingon"

    def test_a_substituted_device_survives_it_too(self, tab):
        tab.main_window.settings["transcription"] = {"device": "quantum"}
        tab._load_transcription_values()
        tab._apply_transcription_values()
        assert tab.main_window.settings["transcription"]["device"] == "quantum"

    def test_everything_else_is_written_from_any_tab(self, tab):
        """The gate is only ever about a substituted value: every other control
        is a faithful copy of what is stored, and a dialog that stopped writing
        those would break Apply for the user who did use the tab."""
        tab.main_window.settings["transcription"] = {"model": "whisper-from-2019"}
        tab._load_transcription_values()
        tab._transcription_detect_language_check.SetValue(False)
        tab._select_transcription_language("es")
        tab._apply_transcription_values()

        section = tab.main_window.settings["transcription"]
        assert section["auto_detect_language"] is False
        assert section["language"] == "es"
        assert section["device"] == device.PREFERENCE_AUTO


class TestTheCudaStatusLine:
    """Four situations, and cuda_runtime tells apart the two that share a
    state precisely so this line can say which."""

    def test_absent_says_what_the_download_costs(self):
        i18n = _I18n()
        text = transcription_tab._transcription_cuda_status_text(
            i18n, cuda_runtime.RuntimeState(cuda_runtime.STATE_ABSENT, ("cublas64_12.dll",))
        )
        assert text.startswith(i18n.t("transcription_cuda_runtime_absent").split(".")[0])
        assert transcription_tab._format_transcription_size(
            i18n, cuda_runtime.WHEEL_BYTES
        ) in text

    def test_installed_says_so(self):
        i18n = _I18n()
        assert transcription_tab._transcription_cuda_status_text(
            i18n, cuda_runtime.RuntimeState(cuda_runtime.STATE_INSTALLED, ())
        ) == i18n.t("transcription_cuda_runtime_installed")

    def test_an_earlier_pin_asks_for_an_update_not_for_a_repair(self):
        i18n = _I18n()
        assert transcription_tab._transcription_cuda_status_text(
            i18n,
            cuda_runtime.RuntimeState(cuda_runtime.STATE_INCOMPLETE, (), "12.0.0.0"),
        ) == i18n.t(cuda_runtime.OUTDATED_I18N_KEY)

    def test_missing_files_win_over_the_version(self):
        """Both signals are present at once when a download of the *old* pin
        was interrupted, and "finish the download" describes that directory."""
        i18n = _I18n()
        assert transcription_tab._transcription_cuda_status_text(
            i18n,
            cuda_runtime.RuntimeState(
                cuda_runtime.STATE_INCOMPLETE, ("cublas64_12.dll",), "12.0.0.0"
            ),
        ) == i18n.t("transcription_cuda_runtime_incomplete")

    def test_the_line_is_on_the_tab_and_not_typed_into(self, tab):
        tab._load_transcription_values()
        assert tab._transcription_cuda_field.GetValue() == (
            transcription_tab._transcription_cuda_status_text(
                tab.main_window.i18n,
                cuda_runtime.RuntimeState(cuda_runtime.STATE_ABSENT, ()),
            )
        )
        assert not tab._transcription_cuda_field.IsEditable()

    @pytest.mark.parametrize("locale", LOCALES)
    def test_no_locale_leaves_a_placeholder_in_it(self, locale):
        i18n = _I18n(locale)
        for state in (cuda_runtime.STATE_ABSENT, cuda_runtime.STATE_INCOMPLETE,
                      cuda_runtime.STATE_INSTALLED):
            text = transcription_tab._transcription_cuda_status_text(
                i18n, cuda_runtime.RuntimeState(state, ())
            )
            assert text and "{" not in text and "}" not in text, (locale, state)


class TestOpeningTheDialogProbesNoHardware:
    """probe_hardware() imports ctranslate2, asks NVML and LoadLibrary's cuBLAS
    — seconds on a machine with a card, on the wx thread, before there is a
    window for the screen reader to announce."""

    def test_the_hardware_is_not_probed_while_the_tab_is_only_being_built(
        self, wx_app, tmp_path, monkeypatch
    ):
        probes = []
        started = []
        monkeypatch.setattr(
            transcription_tab.transcription_device,
            "probe_hardware",
            lambda: probes.append(True) or _CPU_ONLY,
        )
        monkeypatch.setattr(
            transcription_tab.transcription_management,
            "probe_in_background",
            lambda on_done, probe=None: started.append(on_done),
        )
        monkeypatch.setattr(
            transcription_tab.cuda_runtime,
            "installation_state",
            lambda directory=None: cuda_runtime.RuntimeState(
                cuda_runtime.STATE_ABSENT, ()
            ),
        )
        frame = hidden_frame()
        try:
            owner = _TabOwner(_MainWindow(app_settings=_AppSettings(str(tmp_path))))
            owner._transcription_page = owner._build_transcription_page(frame)
            owner._load_transcription_values()
            assert probes == [] and started == []

            # Even on the visit it is taken on a worker, never on the wx thread.
            owner._enter_transcription_page()
            assert probes == []
            assert len(started) == 1
        finally:
            destroy_now(frame)

    def test_the_first_visit_shows_the_tab_before_the_probe_answers(
        self, tab, monkeypatch
    ):
        """Until the worker answers, every precision is listed and nothing
        measured is said; the answer redraws in place and speaks nothing."""
        started = []
        monkeypatch.setattr(
            transcription_tab.transcription_management,
            "probe_in_background",
            lambda on_done, probe=None: started.append(on_done),
        )
        tab._load_transcription_values()
        tab._enter_transcription_page()
        assert tab._transcription_probe is None
        assert tuple(tab._transcription_precision_values) == (
            (preferences.AUTO,) + precision.COMPUTE_TYPES)
        assert tab._transcription_hardware_keys == []
        spoken_before = list(tab.main_window.speak_output.spoken)

        adopted = []
        monkeypatch.setattr(
            transcription_tab.wx, "CallAfter",
            lambda func, *args: adopted.append(func.__name__) or func(*args),
        )
        cpu = device.HardwareProbe(total_ram_mb=16_384, available_ram_mb=12_288,
                                   cpu_compute_types=("float32", "int8"))
        started[0](cpu)
        assert adopted == ["_adopt_transcription_probe"]
        assert tab._transcription_probe is cpu
        assert tuple(tab._transcription_precision_values) == (
            preferences.AUTO, "int8", "float32")
        assert tab.main_window.speak_output.spoken == spoken_before

    def test_the_models_folder_is_not_listed_while_the_tab_is_only_being_built(
        self, tab, monkeypatch
    ):
        """models_folder() walks the folder, and _load_transcription_values()
        threw the result away — the model list is measured per model by the
        populate helper against the same folder."""
        listings = []
        monkeypatch.setattr(
            transcription_tab.transcription_preferences,
            "models_folder",
            lambda stored=None: listings.append(stored) or (str(stored or ""), ()),
        )
        tab._load_transcription_values()
        assert listings == []

        tab._enter_transcription_page()
        assert len(listings) == 1

    def test_the_substitutions_are_the_same_with_or_without_a_probe(self, tab):
        """What the tab reads off resolve() is `.substitutions` and nothing
        else, and no substitution is decided against the probe — which is what
        makes handing it an empty one safe rather than merely cheap."""
        settings = {"transcription": {
            "model": "whisper-from-2019",
            "device": "quantum",
            "language": "klingon",
            "auto_detect_language": False,
        }}
        with_probe = preferences.resolve(settings, _CPU_ONLY, ("small",), "pt-BR")
        without = preferences.resolve(settings, device.HardwareProbe(), (), "pt-BR")
        assert with_probe.substitutions == without.substitutions


class TestWhatThisMachineHasToSay:
    """Two sentences that only a measurement can produce, in the same field as
    the substitutions — one warning area, one tab stop."""

    @staticmethod
    def _pick_the_graphics_card(tab):
        tab._transcription_device_radio.SetSelection(
            transcription_tab._TRANSCRIPTION_DEVICE_PREFERENCES.index(
                device.PREFERENCE_CUDA
            )
        )

    @staticmethod
    def _fallback_sentence(i18n, probe):
        """What device.py itself says about asking this machine for the card.

        Read off resolve_device() rather than named here, because which of the
        two reasons applies is device.py's own distinction: an explicit request
        on a machine with no card at all answers CUDA_UNAVAILABLE, while
        NO_CUDA_FOUND is the one "automatic" gets. Pinning the literal key
        would be this test deciding that instead.
        """
        _device_id, reason = device.resolve_device(device.PREFERENCE_CUDA, probe)
        assert reason in (device.REASON_NO_CUDA_FOUND,
                          device.REASON_CUDA_UNAVAILABLE)
        return i18n.t(device.device_reason_i18n_key(reason))

    def test_asking_for_a_card_this_computer_does_not_have_says_so(self, tab):
        """Without this the CUDA line below invites a half-gigabyte download
        that could not help, and nothing anywhere says the transcription would
        run on the processor regardless."""
        tab._load_transcription_values()
        tab._enter_transcription_page()
        self._pick_the_graphics_card(tab)
        tab._show_transcription_hardware_notices()
        assert self._fallback_sentence(tab.main_window.i18n, _CPU_ONLY) in (
            tab._transcription_substituted_field.GetValue()
        )
        assert tab._transcription_substituted_field.IsShown()

    def test_the_automatic_position_is_not_a_disappointed_expectation(self, tab):
        """The same machine, the same fallback — but nobody asked for the card,
        so there is nothing to report. resolve_device() draws the same line."""
        tab._load_transcription_values()
        tab._enter_transcription_page()
        assert tab._transcription_device_radio.GetSelection() == 0
        assert tab._transcription_hardware_keys == []

    def test_the_radio_handler_refreshes_it_and_lets_the_event_through(self, tab):
        """Skip(), or the dialog-level EVT_RADIOBOX never fires and the Apply
        button stays hidden for the one control on this tab that is a RadioBox."""
        skipped = []

        class _Event:
            def Skip(self):
                skipped.append(True)

        tab._load_transcription_values()
        tab._enter_transcription_page()
        self._pick_the_graphics_card(tab)
        tab._on_transcription_device_change(_Event())
        assert skipped == [True]
        assert self._fallback_sentence(tab.main_window.i18n, _CPU_ONLY) in (
            tab._transcription_substituted_field.GetValue()
        )

    def test_a_machine_that_could_not_be_measured_says_which_of_the_two_it_is(
        self, tab, monkeypatch
    ):
        """"Nothing fits" and "nothing could be measured" ask for different
        things — a smaller model against a download — and an empty combobox
        showing "Automático" says neither."""
        monkeypatch.setattr(
            transcription_tab.transcription_device,
            "probe_hardware",
            device.HardwareProbe,
        )
        tab._load_transcription_values()
        tab._enter_transcription_page()
        assert tab._transcription_substituted_field.GetValue() == tab.main_window.i18n.t(
            preferences.MODEL_NONE_I18N_KEYS[preferences.MODEL_NONE_UNMEASURED]
        )

    def test_nothing_fitting_reads_differently_from_nothing_measured(self, tab):
        tiny_machine = device.HardwareProbe(total_ram_mb=512, available_ram_mb=64)
        tab._load_transcription_values()
        tab._transcription_probe = tiny_machine
        tab._show_transcription_hardware_notices()
        assert tab._transcription_substituted_field.GetValue() == tab.main_window.i18n.t(
            preferences.MODEL_NONE_I18N_KEYS[preferences.MODEL_NONE_NOTHING_FITS]
        )

    def test_a_machine_with_room_says_nothing_at_all(self, tab):
        tab._load_transcription_values()
        tab._enter_transcription_page()
        assert tab._transcription_hardware_keys == []
        assert not tab._transcription_substituted_field.IsShown()

    def test_nothing_is_claimed_before_anything_was_measured(self, tab):
        """No probe means no measurement, not a measurement of zero."""
        tab._load_transcription_values()
        tab._show_transcription_hardware_notices()
        assert tab._transcription_hardware_keys == []

    def test_a_selection_the_radio_cannot_have_is_not_read_as_the_processor(
        self, tab
    ):
        """wx.NOT_FOUND is -1 and would index the preference tuple from the end.
        Unreachable in a RadioBox; the guard is what keeps it from being silent
        if the control is ever swapped."""
        tab._load_transcription_values()
        tab._transcription_device_radio.GetSelection = lambda: wx.NOT_FOUND
        assert tab._selected_transcription_device_preference() == device.PREFERENCE_AUTO


class TestTheInstallWideFolderReachesTheAttributeMainWindowActuallyHas:
    """The bug six green tests covered: the tab read `main_window.app_settings`
    and MainWindow only ever writes `_app_settings`, so the folder the user
    chose in Procurar was never stored and never read back — no message, no log,
    the field simply back on the default next time."""

    @staticmethod
    def _self_attributes_assigned_in(source) -> set:
        assigned = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            else:
                continue
            for target in targets:
                if (isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "self"):
                    assigned.add(target.attr)
        return assigned

    def test_main_py_stores_it_under_the_underscored_name_and_no_other(self):
        # Every file MainWindow is built from (main.py and main_window/*.py):
        # a second spelling in any of its mixins is the same bug.
        assigned = self._self_attributes_assigned_in(main_window_source())
        assert "_app_settings" in assigned
        assert "app_settings" not in assigned, (
            "MainWindow grew a second spelling — the settings dialog reads "
            "_app_settings, and the transcription tab has no other route"
        )

    def test_the_tab_asks_for_exactly_that_name(self):
        """Read off the accessor's own source, so renaming the attribute in
        main_window/settings.py without renaming it here fails rather than
        silently answering "the default folder" forever."""
        tree = ast.parse(SETTINGS_DIALOG_SOURCE)
        accessor = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "_install_wide_settings"
        )
        names = [
            node.value for node in ast.walk(accessor)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and node.value.endswith("app_settings")
        ]
        assert names == ["_app_settings"]

    def test_no_method_reaches_for_the_bare_spelling(self):
        """Every method of the dialog, not only the tab's: the two
        switch_behavior call sites spelled it without the underscore until
        part G of #112, and so never read or wrote the shared file
        (tests/test_switch_behavior_install_wide.py)."""
        trees = [ast.parse(SETTINGS_DIALOG_SOURCE), ast.parse(TRANSCRIPTION_TAB_SOURCE),
                 ast.parse(TRANSCRIPTION_EXTERNAL_SOURCE)]
        for node in (n for tree in trees for n in ast.walk(tree)):
            if not isinstance(node, ast.FunctionDef):
                continue
            for inner in ast.walk(node):
                if ((isinstance(inner, ast.Constant) and inner.value == "app_settings")
                        or (isinstance(inner, ast.Attribute)
                            and inner.attr == "app_settings")):
                    raise AssertionError(
                        f"{node.name} names the attribute MainWindow never sets"
                    )

    def test_the_folder_chosen_is_the_folder_read_back(self, tab, tmp_path):
        """End to end over the stub, with the attribute spelled as production
        spells it: what Procurar recorded reaches app.json and comes back."""
        tab._load_transcription_values()
        tab._transcription_models_dir = str(tmp_path / "models")
        tab._apply_transcription_values()
        assert tab._stored_transcription_models_dir() == str(tmp_path / "models")


class TestTheTabIsEnteredThroughTheNotebook:
    """Only the notebook's page change and show_transcription_tab() (below)
    enter the tab, and everything the tab measures, speaks and consumes is
    behind that entry."""

    def test_the_notebook_page_change_is_bound(self):
        assert "wx.EVT_NOTEBOOK_PAGE_CHANGED" in SETTINGS_DIALOG_SOURCE
        assert "self._on_settings_page_changed" in SETTINGS_DIALOG_SOURCE

    def test_the_handler_enters_the_tab_and_lets_the_event_through(self):
        tree = ast.parse(SETTINGS_DIALOG_SOURCE)
        handler = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "_on_settings_page_changed"
        )
        called = {
            node.func.attr for node in ast.walk(handler)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert "_enter_transcription_page" in called
        assert "Skip" in called


class _PageChangedEvent:
    def __init__(self, index):
        self._index = index

    def Skip(self):
        pass

    def GetSelection(self):
        return self._index


class _EventfulNotebook:
    """wx.Notebook's two ways of selecting a page, with wx's difference.

    SetSelection() delivers EVT_NOTEBOOK_PAGE_CHANGED synchronously to the
    handler the dialog binds; ChangeSelection() delivers nothing. A notebook
    fake without that difference is how the flow's own test once passed over
    a SetSelection() that entered the tab before the window existed.
    """

    def __init__(self, owner, pages):
        self._owner = owner
        self._pages = pages
        self.calls = []

    def FindPage(self, page):
        return self._pages.index(page) if page in self._pages else wx.NOT_FOUND

    def GetPageCount(self):
        return len(self._pages)

    def GetPage(self, index):
        return self._pages[index]

    def SetSelection(self, index):
        self.calls.append(("SetSelection", index))
        self._owner._on_settings_page_changed(_PageChangedEvent(index))

    def ChangeSelection(self, index):
        self.calls.append(("ChangeSelection", index))


class _DialogOpenedOnTheTab:
    """SettingsDialog minus wx.Dialog: the notebook above and a modal loop
    that, like the real one, runs what was queued before it once it is up."""

    show_transcription_tab = SettingsDialog.show_transcription_tab
    _on_settings_page_changed = SettingsDialog._on_settings_page_changed

    def __init__(self, queued, with_tab=True):
        self.timeline = []
        self._queued = queued
        self._transcription_page = object()
        pages = [object(), object()] + ([self._transcription_page] if with_tab else [])
        self._notebook = _EventfulNotebook(self, pages)

    def _enter_transcription_page(self):
        self.timeline.append("entered")

    def ShowModal(self):
        self.timeline.append("shown")
        for func, args in list(self._queued):
            func(*args)
        return wx.ID_OK


class TestOpeningStraightOnTheTab:
    """`show_transcription_tab()` enters the tab only once the window is up.

    The case that reaches it most is a replaced model: the user said Yes to
    "open Settings?", and entering the tab speaks "your model was replaced"
    and then sanitizes the setting so it is never said again. Entered from
    inside SetSelection(), before ShowModal(), the sentence is cut by the
    screen reader announcing the new window and the condition is spent all
    the same.
    """

    @pytest.fixture
    def queued(self, monkeypatch):
        posted = []
        monkeypatch.setattr(
            transcription_tab.wx, "CallAfter",
            lambda func, *args: posted.append((func, args)),
        )
        return posted

    def test_the_tab_is_entered_from_inside_the_modal_loop(self, queued):
        dialog = _DialogOpenedOnTheTab(queued)
        assert dialog.show_transcription_tab() == wx.ID_OK
        assert dialog.timeline == ["shown", "entered"]

    def test_the_page_is_selected_without_the_page_changed_event(self, queued):
        dialog = _DialogOpenedOnTheTab(queued)
        dialog.show_transcription_tab()
        assert dialog._notebook.calls == [("ChangeSelection", 2)]

    def test_the_entry_is_queued_not_called(self, queued):
        dialog = _DialogOpenedOnTheTab(queued)
        dialog.ShowModal = lambda: wx.ID_OK
        dialog.show_transcription_tab()
        assert dialog.timeline == []
        assert [func.__name__ for func, _args in queued] == ["_enter_transcription_page"]

    def test_without_the_tab_it_just_opens(self, queued):
        dialog = _DialogOpenedOnTheTab(queued, with_tab=False)
        dialog.show_transcription_tab()
        assert dialog._notebook.calls == []
        assert queued == []
        assert dialog.timeline == ["shown"]


class TestRetranslation:
    def test_the_labels_and_both_lists_follow_a_language_change(self, tab):
        tab._load_transcription_values()
        tab._select_transcription_model("medium")
        tab.main_window.i18n = _I18n("pl")

        tab._refresh_transcription_labels()

        assert tab._transcription_model_label.GetLabel() == tab.main_window.i18n.t(
            "transcription_model_label"
        )
        assert tab._selected_transcription_model() == "medium"
        # The list is rebuilt rather than relabelled, because "the app's own
        # language" is what decides which endonym comes first.
        assert tab._transcription_language_codes[1] == "pl"
        assert tab._transcription_language_combo.GetString(1) == "polski"

    def test_the_warning_is_said_again_in_the_new_language(self, tab):
        """The keys are kept rather than the sentences, so a warning does not
        stay on screen in the language the user just left."""
        tab.main_window.settings["transcription"] = {"model": "whisper-from-2019"}
        tab._load_transcription_values()
        tab.main_window.i18n = _I18n("pl")

        tab._refresh_transcription_labels()

        assert tab._transcription_substituted_field.GetValue() == _I18n("pl").t(
            "transcription_substituted_model"
        )


class TestTheMnemonicsOnThisTab:
    """That the keys exist at all is not checked here any more: every label on
    this tab is asked for as a literal `i18n.t("...")`, which is exactly what
    tests/test_i18n_keys_exist.py scans for against every locale, and the
    three `transcription_model_choice_*` keys — which are not literals — are
    already covered by test_no_locale_leaves_a_placeholder_to_be_read_out
    above, since a missing one renders as its own key name and no longer
    contains the model id."""

    #: Every label on the tab that must have an Alt key, plus the three
    #: buttons on screen whichever tab is showing. Leaving those three out is
    #: what let &Aviso sit on top of &Aplicar and, worse, &Onde on top of the
    #: dialog's own &OK.
    LABELLED = (
        "transcription_substituted_label",
        "transcription_model_label",
        "transcription_device_label",
        "transcription_precision_label",
        "transcription_language_label",
        "transcription_language_detect",
        "transcription_backend_label",
        "transcription_models_dir_label",
        "transcription_models_dir_browse_btn",
        "transcription_cuda_runtime_label",
        "transcription_external_label",
        "transcription_whisper_cpp_label",
        "ok",
        "cancel",
        "apply",
    )

    #: The eight action buttons, which may collide with nothing and are not
    #: owed an Alt key of their own. Deliberately, and measured rather than
    #: assumed: once each label became one word inside a named group, the
    #: letters left over after the twelve above cover four of the eight in
    #: pt-BR, pt-PT and en-US and five in es-ES and pl — "Reparar" and
    #: "Remover" appear twice each on the tab and cannot share a letter. A
    #: label is read out in full every time focus lands on it, several times a
    #: session; Alt+letter is rarely used and is not announced at all, so a
    #: longer label is the more expensive half of that trade.
    ACTION_BUTTONS = (
        "transcription_model_download_btn",
        "transcription_model_verify_btn",
        "transcription_model_repair_btn",
        "transcription_model_remove_btn",
        "transcription_cuda_install_btn",
        "transcription_cuda_repair_btn",
        "transcription_cuda_verify_btn",
        "transcription_cuda_remove_btn",
        # The five of the section for models in other folders, under the same
        # rule: one word each, inside a group named after what they act on.
        "transcription_external_add_btn",
        "transcription_external_find_btn",
        "transcription_external_use_btn",
        "transcription_external_check_btn",
        "transcription_external_forget_btn",
        # The four of the whisper.cpp program, the same again.
        "transcription_whisper_cpp_install_btn",
        "transcription_whisper_cpp_verify_btn",
        "transcription_whisper_cpp_repair_btn",
        "transcription_whisper_cpp_remove_btn",
    )

    #: The two group names. Not tab stops, so not owed a letter — and they
    #: must not quietly spend one either.
    GROUPS = (
        "transcription_model_actions_group",
        "transcription_cuda_actions_group",
        "transcription_external_actions_group",
        "transcription_whisper_cpp_actions_group",
    )

    @staticmethod
    def _mnemonic(value):
        for index, char in enumerate(value):
            if char == "&" and index + 1 < len(value) and value[index + 1] != "&":
                return value[index + 1].casefold()
        return None

    @pytest.mark.parametrize("locale", LOCALES)
    def test_they_do_not_collide(self, locale):
        """Two controls sharing an Alt key means one of them can never be
        reached with it — and which one is undefined. The buttons are in here
        even though they are not owed a letter: the ones that *have* one must
        still not take somebody else's."""
        table = load_strings(locale)
        used = [
            self._mnemonic(table[key])
            for key in self.LABELLED + self.ACTION_BUTTONS
            if self._mnemonic(table[key]) is not None
        ]
        assert len(used) == len(set(used)), f"{locale}: repeated mnemonics in {used}"

    @pytest.mark.parametrize("locale", LOCALES)
    def test_the_group_names_spend_no_letter(self, locale):
        """A wx.StaticBox is not a tab stop, and an & in its label would take
        a letter from the buttons inside it for nothing."""
        table = load_strings(locale)
        for key in self.GROUPS:
            assert table[key], f"{locale}: {key} is empty"
            assert self._mnemonic(table[key]) is None, f"{locale}: {key}"

    @pytest.mark.parametrize("locale", LOCALES)
    def test_a_button_is_one_word_and_the_group_says_what_it_acts_on(self, locale):
        """The whole point of the grouping: the object is announced once, on
        entry, instead of inside all four labels."""
        table = load_strings(locale)
        for key in self.ACTION_BUTTONS:
            label = table[key].replace("&", "")
            assert " " not in label.strip(), f"{locale}: {key} is {label!r}"
        assert "CUDA" in table["transcription_cuda_actions_group"]
        # The group around the five is the list's own name: "Add", "Use" and
        # "Forget" are about the models in other folders, and the group is
        # what says so.
        assert table["transcription_external_actions_group"] == (
            table["transcription_external_label"].replace("&", "")
        )
        # So is the program's: "Install" and "Remove" are about whisper.cpp.
        assert table["transcription_whisper_cpp_actions_group"] == (
            table["transcription_whisper_cpp_label"].replace("&", "")
        )

    @pytest.mark.parametrize("locale", LOCALES)
    def test_every_control_on_the_tab_has_one(self, locale):
        """A control with no mnemonic is one a keyboard user can only reach by
        tabbing past everything above it. transcription_language_detect had
        none in any of the five."""
        table = load_strings(locale)
        without = sorted(
            key for key in self.LABELLED if self._mnemonic(table[key]) is None
        )
        assert without == [], f"{locale}: no Alt key for {without}"




# ── Part 5c-2: the actions the tab offers ────────────────────────────────────
#
# The progress dialog is never constructed here either — it is a wx.Dialog for
# the same reason SettingsDialog is, and its own behaviour is pinned in
# tests/test_transcription_progress_dialog.py. What these need from it is only
# that it ran, with which action, against which folder, and what it answered.


class _FakeProgress:
    """Stands in for TranscriptionProgressDialog: runs nothing, answers what
    the test told it to, and remembers how it was asked.

    It does call the factory it is handed — the job is real, never started,
    and `announcement()` is read off it by the tab, so stubbing that would be
    this file deciding which sentence each outcome gets.
    """

    def __init__(self, parent, i18n, speak_output, make_job, status_text,
                 job_kwargs=None):
        self.status_text = status_text
        self.destroyed = False
        self.job = make_job(lambda tick: None, lambda result, error: None)
        self.action = self.job.action
        self.model_id = self.job.model_id
        self.job_kwargs = dict(job_kwargs or {})

    def run(self):
        return wx.ID_CANCEL if self.error is not None else wx.ID_OK

    def Destroy(self):
        self.destroyed = True


def _install_fake_progress(monkeypatch, answers=None):
    """Replace the progress dialog; return the list of the ones it made.

    `answers` is a list of (result, error, models_before_move), one per job,
    in order; anything past the end is a plain success.
    """
    made = []
    queue = list(answers or [])
    #: What the factory asked ManagementJob for. Captured through the real
    #: constructor rather than through the dialog, because the arguments now
    #: live in the tab's own closure — which is the point of the factory.
    last_kwargs = {}
    real_job = management.ManagementJob

    def _record(action, **kwargs):
        last_kwargs.clear()
        last_kwargs.update(kwargs)
        return real_job(action, **kwargs)

    def _make(*args, **kwargs):
        dialog = _FakeProgress(*args, **kwargs)
        dialog.job_kwargs = dict(last_kwargs)
        result, error, before_move = queue.pop(0) if queue else (None, None, ())
        dialog.result = result
        dialog.error = error
        dialog.job.models_before_move = tuple(before_move)
        made.append(dialog)
        return dialog

    monkeypatch.setattr(
        transcription_tab.transcription_management, "ManagementJob", _record
    )
    monkeypatch.setattr(transcription_tab, "TranscriptionProgressDialog", _make)
    return made


@pytest.fixture
def confirm_yes(monkeypatch):
    """Answer every question with Yes. Removing now asks one."""
    asked = []
    monkeypatch.setattr(
        transcription_tab.wx, "MessageBox",
        lambda *args, **kwargs: asked.append(args) or wx.YES,
    )
    return asked


@pytest.fixture
def inline_call_after(monkeypatch):
    """Run wx.CallAfter inline, so a test sees the other half of the hop.

    The tab's own half of the thread split is the `wx.CallAfter` in
    `_measure_transcription_download()`; what it defers is checked by running
    it rather than by trusting the name.
    """
    monkeypatch.setattr(
        transcription_tab.wx, "CallAfter",
        lambda func, *args, **kwargs: func(*args, **kwargs),
    )


@pytest.fixture
def no_background_probe(monkeypatch):
    """Nothing here may start a real hardware probe: it imports ctranslate2
    and asks this developer's own machine, which is what device.py's docstring
    says a decision must never depend on."""
    started = []

    def _probe_in_background(on_done, probe=None):
        started.append(on_done)
        return None

    monkeypatch.setattr(
        transcription_tab.transcription_management,
        "probe_in_background",
        _probe_in_background,
    )
    return started


def _summary(**overrides):
    """A management.DownloadSummary with plausible figures, minus the disk."""
    values = dict(
        subject=management.SUBJECT_MODEL,
        model_id="small",
        download_bytes=500 * 1024 ** 2,
        installed_bytes=500 * 1024 ** 2,
        required_free_bytes=756 * 1024 ** 2,
        free_bytes=40 * 1024 ** 3,
        enough_space=True,
        destination=r"X:\models\small",
        device=device.DEVICE_CPU,
        device_reason=device.REASON_NO_CUDA_FOUND,
        resumable=True,
        fits_memory=None,
        freed_bytes=0,
    )
    values.update(overrides)
    return management.DownloadSummary(**values)


class TestWhichButtonsCanBePressed:
    """Four states, and the one that keeps being got wrong is INCOMPLETE."""

    def test_nothing_downloaded_offers_only_the_download(self):
        assert transcription_tab._transcription_action_states(
            model_store.STATE_ABSENT, False
        ) == {"download": True, "verify": False, "repair": False, "remove": False}

    def test_an_interrupted_transfer_can_be_finished_repaired_or_removed(self):
        """Removing has to be there: a removal that could not delete
        everything leaves exactly this state, and 5c-1's sentence for it asks
        the user to remove them again — at a button that would not exist."""
        assert transcription_tab._transcription_action_states(
            model_store.STATE_INCOMPLETE, False
        ) == {"download": True, "verify": False, "repair": True, "remove": True}

    def test_a_complete_install_is_not_offered_for_download_again(self):
        assert transcription_tab._transcription_action_states(
            model_store.STATE_INSTALLED, False
        ) == {"download": False, "verify": True, "repair": False, "remove": True}

    def test_corruption_only_a_hash_could_find_is_what_turns_repair_on(self):
        """installation_state() measures sizes, so the wrong bytes at the
        right size read as installed there — and Reparar, which deletes
        first, is the only one of the four that fixes it."""
        assert transcription_tab._transcription_action_states(
            transcription_tab._TRANSCRIPTION_STATE_CORRUPTED, False
        ) == {"download": False, "verify": True, "repair": True, "remove": True}

    @pytest.mark.parametrize("state", [
        model_store.STATE_ABSENT,
        model_store.STATE_INCOMPLETE,
        model_store.STATE_INSTALLED,
        transcription_tab._TRANSCRIPTION_STATE_CORRUPTED,
    ])
    def test_a_running_job_turns_every_button_off(self, state):
        """Two jobs in this process do not fail — they serialize on the models
        lock, and the second waits silently for up to twelve hours behind a
        bar that never moves."""
        assert not any(
            transcription_tab._transcription_action_states(state, True).values()
        )

    def test_nothing_selected_turns_every_button_off(self):
        assert not any(
            transcription_tab._transcription_action_states(None, False).values()
        )


class TestTheButtonsOnTheTab:
    def test_every_action_a_user_can_ask_for_has_a_button(self, tab):
        """Read against management.ACTIONS rather than against a list written
        here, so an action added to that module without a button on this tab
        fails instead of being quietly unreachable. The models-folder move is
        the one exception: it has no button because it happens on OK."""
        offered = set(tab._transcription_action_buttons)
        assert offered == set(management.ACTIONS) - {management.ACTION_MOVE_MODELS}

    def test_each_row_is_a_named_group(self, tab):
        """What lets the buttons be one word each: a wx.StaticBox is a native
        control whose name a screen reader announces on entering it, so the
        object is said once rather than inside all four labels."""
        i18n = tab.main_window.i18n
        assert [box.GetLabel() for box, _key in tab._transcription_action_groups] == [
            i18n.t("transcription_model_actions_group"),
            i18n.t("transcription_cuda_actions_group"),
            i18n.t("transcription_whisper_cpp_actions_group"),
        ]

    def test_the_buttons_live_inside_their_own_group(self, tab):
        """Parented to the box, not to the page — which is what puts them in
        the group for the accessibility layer and not only for the layout."""
        boxes = [box for box, _key in tab._transcription_action_groups]
        for action, _label, _state in (
            transcription_tab._TRANSCRIPTION_CUDA_ACTION_BUTTONS
        ):
            assert tab._transcription_action_buttons[action].GetParent() is boxes[1]

    def test_the_group_names_follow_a_language_change(self, tab):
        tab._load_transcription_values()
        tab.main_window.i18n = _I18n("pl")
        tab._refresh_transcription_labels()
        assert [box.GetLabel() for box, _key in tab._transcription_action_groups] == [
            _I18n("pl").t("transcription_model_actions_group"),
            _I18n("pl").t("transcription_cuda_actions_group"),
            _I18n("pl").t("transcription_whisper_cpp_actions_group"),
        ]

    def test_the_automatic_entry_leaves_every_model_button_off(self, tab):
        tab._load_transcription_values()
        assert tab._selected_transcription_model() == preferences.AUTO
        for action, _label, _state in (
            transcription_tab._TRANSCRIPTION_MODEL_ACTION_BUTTONS
        ):
            assert not tab._transcription_action_buttons[action].IsEnabled()

    def test_a_model_that_is_not_here_offers_the_download_and_nothing_else(self, tab):
        tab._load_transcription_values()
        tab._select_transcription_model("small")
        tab._sync_transcription_action_buttons()
        buttons = tab._transcription_action_buttons
        assert buttons[management.ACTION_DOWNLOAD_MODEL].IsEnabled()
        assert not buttons[management.ACTION_VERIFY_MODEL].IsEnabled()
        assert not buttons[management.ACTION_REMOVE_MODEL].IsEnabled()

    def test_an_installed_model_offers_verifying_and_removing(self, tab, monkeypatch):
        monkeypatch.setattr(
            transcription_tab.model_store, "installation_state",
            lambda root, model: model_store.InstallState(model_store.STATE_INSTALLED),
        )
        tab._populate_transcription_model_choices()
        tab._select_transcription_model("small")
        tab._sync_transcription_action_buttons()
        buttons = tab._transcription_action_buttons
        assert not buttons[management.ACTION_DOWNLOAD_MODEL].IsEnabled()
        assert buttons[management.ACTION_VERIFY_MODEL].IsEnabled()
        assert buttons[management.ACTION_REMOVE_MODEL].IsEnabled()

    def test_a_failed_check_turns_repair_on_for_that_model_alone(self, tab, monkeypatch):
        monkeypatch.setattr(
            transcription_tab.model_store, "installation_state",
            lambda root, model: model_store.InstallState(model_store.STATE_INSTALLED),
        )
        tab._populate_transcription_model_choices()
        tab._note_transcription_outcome(
            management.ACTION_VERIFY_MODEL, "small",
            errors.TranscriptionError(errors.MODEL_CORRUPTED, "digest"),
        )
        tab._select_transcription_model("small")
        tab._sync_transcription_action_buttons()
        assert tab._transcription_action_buttons[
            management.ACTION_REPAIR_MODEL].IsEnabled()

        tab._select_transcription_model("medium")
        tab._sync_transcription_action_buttons()
        assert not tab._transcription_action_buttons[
            management.ACTION_REPAIR_MODEL].IsEnabled()

    def test_a_repair_that_worked_forgets_the_corruption(self, tab):
        tab._note_transcription_outcome(
            management.ACTION_VERIFY_MODEL, "small",
            errors.TranscriptionError(errors.MODEL_CORRUPTED, "digest"),
        )
        tab._note_transcription_outcome(management.ACTION_REPAIR_MODEL, "small", None)
        assert tab._transcription_corrupted_models == set()

    def test_a_cancelled_check_found_nothing_and_changes_nothing(self, tab):
        tab._note_transcription_outcome(
            management.ACTION_VERIFY_MODEL, "small",
            errors.TranscriptionError(errors.CANCELLED, "by the user"),
        )
        assert tab._transcription_corrupted_models == set()

    def test_the_cuda_buttons_follow_what_is_installed(self, tab, monkeypatch):
        monkeypatch.setattr(
            transcription_tab.cuda_runtime, "installation_state",
            lambda directory=None: cuda_runtime.RuntimeState(
                cuda_runtime.STATE_INCOMPLETE, ("cublas64_12.dll",)
            ),
        )
        tab._show_transcription_cuda_status()
        buttons = tab._transcription_action_buttons
        assert buttons[management.ACTION_INSTALL_CUDA_RUNTIME].IsEnabled()
        assert buttons[management.ACTION_REPAIR_CUDA_RUNTIME].IsEnabled()
        # The one 5c-1 asked for by name: a removal that could not delete
        # everything lands here, and its sentence sends the user back to it.
        assert buttons[management.ACTION_REMOVE_CUDA_RUNTIME].IsEnabled()
        assert not buttons[management.ACTION_VERIFY_CUDA_RUNTIME].IsEnabled()

    def test_the_model_picker_redraws_them_and_lets_the_event_through(self, tab):
        """Skip(), or the dialog-level EVT_COMBOBOX never runs and the Apply
        button stays hidden for the model picker."""
        skipped = []

        class _Event:
            def Skip(self):
                skipped.append(True)

        tab._load_transcription_values()
        tab._select_transcription_model("small")
        tab._on_transcription_model_change(_Event())
        assert skipped == [True]
        assert tab._transcription_action_buttons[
            management.ACTION_DOWNLOAD_MODEL].IsEnabled()

    def test_the_button_pressed_is_the_action_that_runs(self, tab):
        started = []
        tab._start_transcription_action = lambda action: started.append(action)

        class _Event:
            def __init__(self, obj):
                self._obj = obj

            def GetEventObject(self):
                return self._obj

        for _action, button in tab._transcription_action_buttons.items():
            tab._on_transcription_action(_Event(button))
        assert sorted(started) == sorted(tab._transcription_action_buttons)


class TestTheQuestionBeforeADownload:
    """The five things §5 of the issue asks for, before the bytes start."""

    def test_it_names_the_model_the_size_the_space_the_folder_and_the_device(self):
        i18n = _I18n()
        text = transcription_tab._transcription_download_confirmation(i18n, _summary())
        assert "small" in text
        assert transcription_tab._format_transcription_size(i18n, 500 * 1024 ** 2) in text
        assert transcription_tab._format_transcription_size(i18n, 756 * 1024 ** 2) in text
        assert r"X:\models\small" in text
        assert i18n.t("transcription_confirm_device_cpu") in text

    def test_it_says_the_card_when_that_is_where_it_would_run(self):
        i18n = _I18n()
        text = transcription_tab._transcription_download_confirmation(
            i18n, _summary(device=device.DEVICE_CUDA)
        )
        assert i18n.t("transcription_confirm_device_cuda") in text
        assert i18n.t("transcription_confirm_device_cpu") not in text

    def test_the_cuda_download_says_it_cannot_be_resumed(self):
        """553 MB with no resume: cancelling means starting over, and that is
        the user's decision to make before it starts, not after."""
        i18n = _I18n()
        text = transcription_tab._transcription_download_confirmation(
            i18n, _summary(
                subject=management.SUBJECT_CUDA_RUNTIME, model_id=None,
                resumable=False, download_bytes=cuda_runtime.WHEEL_BYTES,
            )
        )
        assert transcription_tab._format_transcription_size(
            i18n, cuda_runtime.WHEEL_BYTES
        ) in text
        assert i18n.t("transcription_confirm_no_resume").split("{")[0] in text

    def test_a_resumable_model_download_does_not_mention_starting_over(self):
        i18n = _I18n()
        text = transcription_tab._transcription_download_confirmation(i18n, _summary())
        assert i18n.t("transcription_confirm_no_resume").split("{")[0] not in text

    def test_a_repair_says_what_it_deletes_first(self):
        i18n = _I18n()
        text = transcription_tab._transcription_download_confirmation(
            i18n, _summary(), repair=True
        )
        assert text.startswith(
            i18n.t("transcription_confirm_model_repair").format(model="small")
        )

    def test_a_model_too_big_for_the_device_says_so(self):
        i18n = _I18n()
        assert i18n.t("transcription_confirm_does_not_fit") in (
            transcription_tab._transcription_download_confirmation(
                i18n, _summary(fits_memory=False)
            )
        )
        assert i18n.t("transcription_confirm_does_not_fit") not in (
            transcription_tab._transcription_download_confirmation(
                i18n, _summary(fits_memory=None)
            )
        )

    def test_space_that_could_not_be_measured_reads_as_unknown(self):
        i18n = _I18n()
        text = transcription_tab._transcription_download_confirmation(
            i18n, _summary(free_bytes=None, enough_space=None)
        )
        assert i18n.t("transcription_confirm_space_unknown") in text

    @pytest.mark.parametrize("locale", LOCALES)
    def test_no_locale_leaves_a_placeholder_to_be_read_out(self, locale):
        i18n = _I18n(locale)
        for summary in (
            _summary(),
            _summary(enough_space=None, free_bytes=None),
            _summary(enough_space=False, free_bytes=1024),
            _summary(fits_memory=False, device=device.DEVICE_CUDA),
            _summary(subject=management.SUBJECT_CUDA_RUNTIME, model_id=None,
                     resumable=False),
        ):
            for repair in (False, True):
                text = transcription_tab._transcription_download_confirmation(
                    i18n, summary, repair
                )
                assert "{" not in text and "}" not in text, (locale, summary)


class TestWhatHappensWhenThereIsNoRoom:
    """Not enough space is not a question, and unmeasurable space is not a no."""

    @staticmethod
    def _boxes(monkeypatch, answer):
        raised = []
        monkeypatch.setattr(
            transcription_tab.wx, "MessageBox",
            lambda *args, **kwargs: raised.append(args) or answer,
        )
        return raised

    def test_not_enough_space_is_told_and_not_offered(self, tab, monkeypatch):
        raised = self._boxes(monkeypatch, wx.YES)
        assert tab._ask_transcription_download(
            _summary(enough_space=False, free_bytes=1024), False
        ) is False
        assert len(raised) == 1
        style = raised[0][2]
        assert style & wx.ICON_ERROR
        # No "Sim" for something the download's own gate would refuse anyway.
        assert not style & wx.YES_NO

    def test_space_that_could_not_be_measured_is_still_offered(self, tab, monkeypatch):
        """ensure_free_space() lets an unmeasurable volume through, so
        refusing here would make downloading impossible on a disk nothing can
        measure."""
        raised = self._boxes(monkeypatch, wx.YES)
        assert tab._ask_transcription_download(
            _summary(enough_space=None, free_bytes=None), False
        ) is True
        assert raised[0][2] & wx.YES_NO

    def test_a_no_stops_the_download(self, tab, monkeypatch):
        self._boxes(monkeypatch, wx.NO)
        assert tab._ask_transcription_download(_summary(), False) is False


class TestTheMeasurementsAreTakenOffTheWxThread:
    """Half a second on the wx thread is half a second in which the screen
    reader cannot announce the screen the user just opened."""

    def test_the_probe_and_the_free_space_are_taken_together_in_the_background(
        self, tab, monkeypatch, no_background_probe, inline_call_after
    ):
        measured = []
        monkeypatch.setattr(
            transcription_tab.model_store, "free_bytes",
            lambda path: measured.append(path) or 40 * 1024 ** 3,
        )
        monkeypatch.setattr(
            transcription_tab.wx, "MessageBox", lambda *args, **kwargs: wx.NO
        )
        tab._load_transcription_values()
        tab._select_transcription_model("small")
        tab._start_transcription_action(management.ACTION_DOWNLOAD_MODEL)

        # Handed to the probe thread and not yet run: the disk query has not
        # happened on this thread.
        assert len(no_background_probe) == 1
        assert measured == []

        no_background_probe[0](_CPU_ONLY)
        assert len(measured) == 1

    def test_the_buttons_are_off_while_the_measurement_runs(
        self, tab, no_background_probe
    ):
        tab._load_transcription_values()
        tab._select_transcription_model("small")
        tab._start_transcription_action(management.ACTION_DOWNLOAD_MODEL)
        assert tab._transcription_job_running
        assert not tab._transcription_action_buttons[
            management.ACTION_DOWNLOAD_MODEL].IsEnabled()

    def test_a_second_press_during_it_starts_nothing(self, tab, no_background_probe):
        tab._load_transcription_values()
        tab._select_transcription_model("small")
        tab._start_transcription_action(management.ACTION_DOWNLOAD_MODEL)
        tab._start_transcription_action(management.ACTION_DOWNLOAD_MODEL)
        assert len(no_background_probe) == 1

    def test_the_fresh_probe_replaces_the_one_the_tab_had(
        self, tab, monkeypatch, no_background_probe, inline_call_after
    ):
        """Installing the libraries is exactly what changes the answer, so a
        cached probe would go on saying the card cannot be used."""
        monkeypatch.setattr(
            transcription_tab.model_store, "free_bytes", lambda path: 40 * 1024 ** 3
        )
        monkeypatch.setattr(
            transcription_tab.wx, "MessageBox", lambda *args, **kwargs: wx.NO
        )
        with_card = device.HardwareProbe(
            total_ram_mb=16_384, available_ram_mb=12_288,
            cuda_device_count=1, cuda_libraries_ok=True,
        )
        tab._load_transcription_values()
        tab._start_transcription_action(management.ACTION_INSTALL_CUDA_RUNTIME)
        no_background_probe[0](with_card)
        assert tab._transcription_probe is with_card

    def test_the_tab_being_closed_mid_probe_touches_nothing(self, tab):
        """The user can still press OK during the measurement, and the answer
        then comes back to controls that no longer exist."""
        tab.__class__.__bool__ = lambda self: False
        try:
            tab._confirm_transcription_download(
                management.ACTION_DOWNLOAD_MODEL, "small", _CPU_ONLY, 1
            )
        finally:
            del tab.__class__.__bool__

    def test_a_probe_answering_after_the_dialog_closed_touches_nothing(self, tab):
        """The first visit's probe runs on a worker, and the dialog can be
        closed before it answers."""
        tab.__class__.__bool__ = lambda self: False
        try:
            tab._adopt_transcription_probe(_CPU_ONLY)
        finally:
            del tab.__class__.__bool__
        assert tab._transcription_probe is None


class TestTheTabIsRedrawnAfterEveryAction:
    """The buttons and the state line have to say what is on disk *now*."""

    @staticmethod
    def _spy(tab):
        seen = []
        tab._refresh_transcription_models = lambda: seen.append("models")
        tab._show_transcription_cuda_status = lambda: seen.append("cuda")
        return seen

    def test_a_model_action_redraws_the_model_list(
        self, tab, monkeypatch, confirm_yes
    ):
        _install_fake_progress(monkeypatch)
        tab._load_transcription_values()
        tab._select_transcription_model("small")
        seen = self._spy(tab)
        tab._start_transcription_action(management.ACTION_REMOVE_MODEL)
        assert seen == ["models"]

    def test_a_cuda_action_redraws_the_cuda_line(
        self, tab, monkeypatch, no_background_probe
    ):
        _install_fake_progress(monkeypatch)
        tab._load_transcription_values()
        seen = self._spy(tab)
        tab._start_transcription_action(management.ACTION_VERIFY_CUDA_RUNTIME)
        assert seen == ["cuda"]

    def test_the_card_is_measured_again_after_the_libraries_change(
        self, tab, monkeypatch, no_background_probe, confirm_yes
    ):
        _install_fake_progress(monkeypatch)
        tab._load_transcription_values()
        tab._start_transcription_action(management.ACTION_REMOVE_CUDA_RUNTIME)
        assert len(no_background_probe) == 1

    def test_the_buttons_are_on_again_once_the_action_is_over(
        self, tab, monkeypatch, confirm_yes
    ):
        _install_fake_progress(monkeypatch)
        tab._load_transcription_values()
        tab._select_transcription_model("small")
        tab._start_transcription_action(management.ACTION_REMOVE_MODEL)
        assert tab._transcription_job_running is False

    def test_the_action_is_run_against_the_folder_that_is_actually_stored(
        self, tab, monkeypatch, tmp_path
    ):
        made = _install_fake_progress(monkeypatch)
        tab._load_transcription_values()
        tab._select_transcription_model("small")
        tab._start_transcription_action(management.ACTION_VERIFY_MODEL)
        assert made[0].job_kwargs["models_root"] == str(tmp_path)
        assert made[0].model_id == "small"
        assert made[0].destroyed


class TestWhatTheUserIsTold:
    def test_a_failure_plays_the_error_sound_and_is_spoken(
        self, tab, monkeypatch, confirm_yes
    ):
        _install_fake_progress(monkeypatch, [(
            None,
            errors.TranscriptionError(errors.MODEL_DOWNLOAD_FAILED, "no line"),
            (),
        )])
        tab._load_transcription_values()
        tab._select_transcription_model("small")
        tab._start_transcription_action(management.ACTION_REMOVE_MODEL)
        assert tab.main_window.error_sound.plays == 1
        assert tab.main_window.speak_output.spoken == [
            tab.main_window.i18n.t(errors.error_i18n_key(errors.MODEL_DOWNLOAD_FAILED))
        ]

    def test_a_sound_that_raises_does_not_cost_the_sentence(
        self, tab, monkeypatch, confirm_yes
    ):
        """docs/traps/audio-devices.md: a failure told by nothing at all is
        what an unguarded play() on a vanished device produced here."""
        class _Raising:
            def play(self):
                raise RuntimeError("5, invalid handle")

        tab.main_window.error_sound = _Raising()
        _install_fake_progress(monkeypatch, [(
            None,
            errors.TranscriptionError(errors.MODEL_DOWNLOAD_FAILED, "no line"),
            (),
        )])
        tab._load_transcription_values()
        tab._select_transcription_model("small")
        tab._start_transcription_action(management.ACTION_REMOVE_MODEL)
        assert tab.main_window.speak_output.spoken == [
            tab.main_window.i18n.t(errors.error_i18n_key(errors.MODEL_DOWNLOAD_FAILED))
        ]

    def test_a_success_is_spoken_with_no_sound(self, tab, monkeypatch, confirm_yes):
        _install_fake_progress(monkeypatch, [(True, None, ())])
        tab._load_transcription_values()
        tab._select_transcription_model("small")
        tab._start_transcription_action(management.ACTION_REMOVE_MODEL)
        assert tab.main_window.error_sound.plays == 0
        assert tab.main_window.speak_output.spoken == [
            tab.main_window.i18n.t(management.MODEL_REMOVED_I18N_KEY).format(
                model="small"
            )
        ]

    def test_a_cancel_is_not_announced_as_a_failure(self, tab, monkeypatch):
        _install_fake_progress(monkeypatch, [(
            None, errors.TranscriptionError(errors.CANCELLED, "by the user"), ()
        )])
        tab._load_transcription_values()
        tab._select_transcription_model("small")
        tab._start_transcription_action(management.ACTION_VERIFY_MODEL)
        assert tab.main_window.error_sound.plays == 0
        assert tab.main_window.speak_output.spoken == [
            tab.main_window.i18n.t(management.CANCELLED_I18N_KEY)
        ]

    def test_nothing_is_ever_said_with_interrupt(
        self, tab, monkeypatch, confirm_yes
    ):
        """Cutting the screen reader off mid-sentence is worse than waiting."""
        _install_fake_progress(monkeypatch)
        tab._load_transcription_values()
        tab._select_transcription_model("small")
        tab._start_transcription_action(management.ACTION_REMOVE_MODEL)
        assert tab.main_window.speak_output.interrupts == [False]

    def test_the_sentence_is_left_on_screen_as_well(
        self, tab, monkeypatch, confirm_yes
    ):
        """Spoken is not enough for the low-vision reader the two read-only
        fields on this tab exist for."""
        _install_fake_progress(monkeypatch, [(True, None, ())])
        tab._load_transcription_values()
        tab._select_transcription_model("small")
        tab._start_transcription_action(management.ACTION_REMOVE_MODEL)
        shown = tab._transcription_substituted_field.GetValue()
        assert tab.main_window.i18n.t(
            management.MODEL_REMOVED_I18N_KEY
        ).format(model="small") in shown
        assert tab._transcription_substituted_field.IsShown()

    def test_it_is_said_again_in_the_new_language(
        self, tab, monkeypatch, confirm_yes
    ):
        """Kept as key and values, not as the rendered sentence."""
        _install_fake_progress(monkeypatch, [(True, None, ())])
        tab._load_transcription_values()
        tab._select_transcription_model("small")
        tab._start_transcription_action(management.ACTION_REMOVE_MODEL)
        tab.main_window.i18n = _I18n("pl")
        tab._refresh_transcription_labels()
        assert _I18n("pl").t(management.MODEL_REMOVED_I18N_KEY).format(
            model="small"
        ) in tab._transcription_substituted_field.GetValue()

    def test_a_move_names_the_models_as_the_picker_does(self):
        """The announcement carries ids; a GGML id is a file name a screen
        reader spells out, and the filter's model is nobody's choice."""
        i18n = _I18n("en-US")
        vad = whisper_cpp_catalog.VAD_MODEL.id
        moved = transcription_tab._transcription_announcement_text(i18n, management.announcement(
            management.ACTION_MOVE_MODELS, ("small", "ggml-small-q5_1", vad)))
        partial = transcription_tab._transcription_announcement_text(i18n, management.announcement(
            management.ACTION_MOVE_MODELS,
            error=SimpleNamespace(code=errors.MODEL_MOVE_FAILED, moved=("ggml-small-q5_1", vad)),
            models_before_move=("small", "ggml-small-q5_1", vad)))

        for sentence in (moved, partial):
            assert model_names.display_name(i18n, "ggml-small-q5_1") in sentence
            assert model_names.display_name(i18n, "small") in sentence
            assert "ggml-" not in sentence
        assert moved == i18n.t(management.MODELS_MOVED_I18N_KEY).format(
            models=model_names.LIST_SEPARATOR.join((
                model_names.display_name(i18n, "small"),
                model_names.display_name(i18n, "ggml-small-q5_1"))))

    def test_showing_the_result_does_not_make_apply_appear(
        self, tab, monkeypatch, confirm_yes
    ):
        """The field is written with ChangeValue for exactly this reason."""
        _install_fake_progress(monkeypatch)
        tab._loading_values = True
        try:
            tab._load_transcription_values()
        finally:
            tab._loading_values = False
        tab._select_transcription_model("small")
        tab.dirtied = 0
        tab._start_transcription_action(management.ACTION_REMOVE_MODEL)
        assert tab.dirtied == 0


class TestTheModelsMoveOnlyAfterOk:
    """A move started in Procurar leaves the files in a folder a Cancel then
    never stores, so the setting goes on naming the folder they left."""

    @staticmethod
    def _function(name):
        # The handlers live in the mixins the dialog inherits, not in the
        # dialog file itself.
        for source in (TRANSCRIPTION_TAB_SOURCE, TRANSCRIPTION_EXTERNAL_SOURCE,
                       SETTINGS_DIALOG_SOURCE):
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, ast.FunctionDef) and node.name == name:
                    return node
        raise AssertionError(f"{name} is defined nowhere")

    @staticmethod
    def _called(function):
        return {
            node.func.attr for node in ast.walk(function)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }

    def test_the_browse_handler_moves_nothing(self):
        called = self._called(self._function("_on_browse_transcription_models_dir"))
        assert "_move_transcription_models" not in called
        assert "_run_transcription_job" not in called

    def test_applying_is_what_moves_them(self):
        called = self._called(self._function("_apply_transcription_values"))
        assert "_move_transcription_models" in called

    def test_a_previous_folder_with_nothing_in_it_opens_no_dialog(
        self, tab, monkeypatch, tmp_path
    ):
        made = _install_fake_progress(monkeypatch)
        tab._load_transcription_values()
        tab._transcription_models_dir = str(tmp_path / "elsewhere")
        tab._apply_transcription_values()
        assert made == []
        assert tab.main_window._app_settings.get(
            preferences.MODELS_DIR_SETTING
        ) == str(tmp_path / "elsewhere")

    def test_a_folder_with_models_in_it_is_moved_and_the_new_one_stored(
        self, tab, monkeypatch, tmp_path
    ):
        (tmp_path / "small").mkdir()
        made = _install_fake_progress(monkeypatch, [(("small",), None, ("small",))])
        tab._load_transcription_values()
        tab._transcription_models_dir = str(tmp_path / "elsewhere")
        tab._apply_transcription_values()

        assert len(made) == 1
        assert made[0].action == management.ACTION_MOVE_MODELS
        assert made[0].job_kwargs["models_root"] == str(tmp_path)
        assert made[0].job_kwargs["new_models_root"] == str(tmp_path / "elsewhere")
        assert tab.main_window._app_settings.get(
            preferences.MODELS_DIR_SETTING
        ) == str(tmp_path / "elsewhere")

    def test_a_half_finished_move_keeps_the_setting_on_the_previous_folder(
        self, tab, monkeypatch, tmp_path
    ):
        """move_models() skips whatever already arrived, so from the previous
        folder pressing OK again moves exactly the rest. Naming the new folder
        would strand the leftovers where nothing lists or deletes them."""
        (tmp_path / "small").mkdir()
        failure = errors.TranscriptionError(errors.MODEL_MOVE_FAILED, "half way")
        failure.moved = ("small",)
        _install_fake_progress(monkeypatch, [(None, failure, ("small", "medium"))])
        tab._load_transcription_values()
        tab._transcription_models_dir = str(tmp_path / "elsewhere")
        tab._apply_transcription_values()

        assert tab.main_window._app_settings.get(
            preferences.MODELS_DIR_SETTING
        ) == str(tmp_path)
        assert tab._transcription_models_dir == str(tmp_path)

    def test_a_half_finished_move_says_which_are_where_and_which_folder_won(
        self, tab, monkeypatch, tmp_path
    ):
        (tmp_path / "small").mkdir()
        failure = errors.TranscriptionError(errors.MODEL_MOVE_FAILED, "half way")
        failure.moved = ("small",)
        _install_fake_progress(monkeypatch, [(None, failure, ("small", "medium"))])
        tab._load_transcription_values()
        tab._transcription_models_dir = str(tmp_path / "elsewhere")
        tab._apply_transcription_values()

        i18n = tab.main_window.i18n
        said = tab.main_window.speak_output.spoken[-1]
        assert i18n.t(management.MODELS_MOVE_PARTIAL_I18N_KEY).format(
            moved="small", remaining="medium"
        ) in said
        assert i18n.t("transcription_models_dir_kept_previous") in said
        assert i18n.t("transcription_models_dir_kept_previous") in (
            tab._transcription_substituted_field.GetValue()
        )

    def test_a_move_that_got_everything_across_keeps_the_new_folder(
        self, tab, monkeypatch, tmp_path
    ):
        (tmp_path / "small").mkdir()
        _install_fake_progress(monkeypatch, [(("small",), None, ("small",))])
        tab._load_transcription_values()
        tab._transcription_models_dir = str(tmp_path / "elsewhere")
        tab._apply_transcription_values()
        assert tab.main_window.i18n.t("transcription_models_dir_kept_previous") not in (
            tab.main_window.speak_output.spoken[-1]
        )

    def test_a_move_that_raised_with_everything_already_across_says_so(
        self, tab, monkeypatch, tmp_path
    ):
        """The setting names the new folder — rightly, the models are there —
        while the sentence for the outcome is a failure. Hearing "could not be
        completed" as the folder silently changes is the worst of both."""
        (tmp_path / "small").mkdir()
        failure = errors.TranscriptionError(errors.MODEL_MOVE_FAILED, "after the last one")
        failure.moved = ("small",)
        _install_fake_progress(monkeypatch, [(None, failure, ("small",))])
        tab._load_transcription_values()
        tab._transcription_models_dir = str(tmp_path / "elsewhere")
        tab._apply_transcription_values()

        assert tab.main_window._app_settings.get(
            preferences.MODELS_DIR_SETTING
        ) == str(tmp_path / "elsewhere")
        assert tab.main_window.i18n.t("transcription_models_dir_now_the_new_one") in (
            tab.main_window.speak_output.spoken[-1]
        )

    def test_a_move_that_broke_before_it_started_keeps_the_previous_folder(
        self, tab, monkeypatch, tmp_path
    ):
        """The worst shape of failure: the job never got far enough to say
        what crossed, so nothing crossed. Read as "everything is still in the
        old folder" it is harmless; read as success it names a folder the
        models are not in and the picker offers to download them all again."""
        (tmp_path / "small").mkdir()
        _install_fake_progress(monkeypatch, [(
            None, errors.TranscriptionError(errors.MODELS_BUSY, "another window"), ()
        )])
        tab._load_transcription_values()
        tab._transcription_models_dir = str(tmp_path / "elsewhere")
        tab._apply_transcription_values()

        assert tab.main_window._app_settings.get(
            preferences.MODELS_DIR_SETTING
        ) == str(tmp_path)
        assert tab.main_window.i18n.t("transcription_models_dir_kept_previous") in (
            tab.main_window.speak_output.spoken[-1]
        )

    def test_a_cancelled_move_that_got_nothing_across_keeps_it_too(
        self, tab, monkeypatch, tmp_path
    ):
        """"Operação cancelada." says nothing about the folder, and the user
        would otherwise press OK and find it silently back on the old one."""
        (tmp_path / "small").mkdir()
        _install_fake_progress(monkeypatch, [(
            None, errors.TranscriptionError(errors.CANCELLED, "by the user"),
            ("small",),
        )])
        tab._load_transcription_values()
        tab._transcription_models_dir = str(tmp_path / "elsewhere")
        tab._apply_transcription_values()

        assert tab.main_window._app_settings.get(
            preferences.MODELS_DIR_SETTING
        ) == str(tmp_path)

    def test_moving_the_folder_says_nothing_about_the_cuda_libraries(
        self, tab, monkeypatch, tmp_path
    ):
        """A move is neither a model action nor a CUDA one, and a successful
        one is no reason to forget that the libraries failed their check."""
        (tmp_path / "small").mkdir()
        _install_fake_progress(monkeypatch, [(("small",), None, ("small",))])
        tab._note_transcription_outcome(
            management.ACTION_VERIFY_CUDA_RUNTIME, None,
            errors.TranscriptionError(errors.CUDA_RUNTIME_CORRUPTED, "digest"),
        )
        tab._load_transcription_values()
        tab._transcription_models_dir = str(tmp_path / "elsewhere")
        tab._apply_transcription_values()
        assert tab._transcription_cuda_corrupted is True


class TestFoldersNothingHereCanDelete:
    """A model a later version retires stops being listed anywhere, and
    remove_model() refuses a name the catalogue cannot look up — up to 3 GB
    sitting there, invisible and undeletable from inside the app."""

    def test_they_are_named_in_the_warning_field(self, tab, tmp_path):
        (tmp_path / "whisper-from-2019").mkdir()
        tab._load_transcription_values()
        tab._refresh_transcription_models()
        shown = tab._transcription_substituted_field.GetValue()
        assert "whisper-from-2019" in shown
        assert tab._transcription_substituted_field.IsShown()

    def test_they_are_spoken_when_the_tab_is_put_on_screen(self, tab, tmp_path):
        """A read-only field is not a cue under NVDA until focus reaches it,
        and a folder nobody knows about is what nobody goes looking for."""
        (tmp_path / "whisper-from-2019").mkdir()
        tab._load_transcription_values()
        tab._enter_transcription_page()
        assert any(
            "whisper-from-2019" in said
            for said in tab.main_window.speak_output.spoken
        )

    def test_a_folder_the_catalogue_knows_is_not_one_of_them(self, tab, tmp_path):
        (tmp_path / model_catalog.list_models()[0].id).mkdir()
        tab._load_transcription_values()
        tab._refresh_transcription_models()
        assert tab._transcription_unknown_dirs == ()
        assert not tab._transcription_substituted_field.IsShown()

    def test_an_empty_folder_says_nothing(self, tab):
        tab._load_transcription_values()
        tab._enter_transcription_page()
        assert tab.main_window.speak_output.spoken == []


class TestNothingIsWrittenIntoAFolderCancelThrowsAway:
    """The other half of "the models move on OK", seen from Cancel's side.

    Procurar records a folder and stores nothing until OK. A 3 GB download
    written against that folder and then a Cancel leaves the setting on the
    old one, and nothing in the app ever looks at the new folder again — not
    the model list, not list_unknown_dirs(), both of which walk the folder
    that is *configured*. The picker then says "not installed" and offers the
    same 3 GB over again.
    """

    @staticmethod
    def _refuse(monkeypatch):
        told = []
        monkeypatch.setattr(
            transcription_tab.wx, "MessageBox",
            lambda *args, **kwargs: told.append(args) or wx.OK,
        )
        return told

    @pytest.mark.parametrize("action", list(management.MODEL_ACTIONS))
    def test_every_model_action_is_refused_while_the_folder_is_only_chosen(
        self, tab, monkeypatch, tmp_path, action
    ):
        made = _install_fake_progress(monkeypatch)
        told = self._refuse(monkeypatch)
        tab._load_transcription_values()
        tab._select_transcription_model("small")
        tab._transcription_models_dir = str(tmp_path / "elsewhere")

        tab._start_transcription_action(action)

        assert made == []
        assert told, "the user has to be told why nothing happened"
        assert told[0][0] == tab.main_window.i18n.t("transcription_apply_folder_first")

    def test_nothing_is_measured_either(self, tab, monkeypatch, tmp_path,
                                        no_background_probe):
        """The refusal comes before the probe, so a download that cannot start
        does not spend a second of somebody's machine deciding that."""
        self._refuse(monkeypatch)
        tab._load_transcription_values()
        tab._select_transcription_model("small")
        tab._transcription_models_dir = str(tmp_path / "elsewhere")
        tab._start_transcription_action(management.ACTION_DOWNLOAD_MODEL)
        assert no_background_probe == []

    def test_the_cuda_libraries_are_not_held_up_by_the_models_folder(
        self, tab, monkeypatch, tmp_path, no_background_probe, confirm_yes
    ):
        """They live in their own install-wide folder, which this setting does
        not move — refusing those too would be a rule with no reason."""
        made = _install_fake_progress(monkeypatch)
        tab._load_transcription_values()
        tab._transcription_models_dir = str(tmp_path / "elsewhere")
        tab._start_transcription_action(management.ACTION_VERIFY_CUDA_RUNTIME)
        assert len(made) == 1

    def test_the_same_action_runs_once_the_folder_has_been_applied(
        self, tab, monkeypatch, tmp_path
    ):
        made = _install_fake_progress(monkeypatch)
        self._refuse(monkeypatch)
        tab._load_transcription_values()
        tab._select_transcription_model("small")
        tab._transcription_models_dir = str(tmp_path / "elsewhere")
        tab._apply_transcription_values()

        tab._start_transcription_action(management.ACTION_VERIFY_MODEL)
        assert len(made) == 1
        assert made[0].job_kwargs["models_root"] == str(tmp_path / "elsewhere")


class TestRemovingIsAskedAboutFirst:
    """Three gigabytes on one Enter, from a button one arrow key away from
    Verificar on the same row."""

    @staticmethod
    def _answer(monkeypatch, reply):
        asked = []
        monkeypatch.setattr(
            transcription_tab.wx, "MessageBox",
            lambda *args, **kwargs: asked.append(args) or reply,
        )
        return asked

    def test_removing_a_model_asks_and_names_it(self, tab, monkeypatch):
        made = _install_fake_progress(monkeypatch)
        asked = self._answer(monkeypatch, wx.YES)
        tab._load_transcription_values()
        tab._select_transcription_model("small")
        tab._start_transcription_action(management.ACTION_REMOVE_MODEL)
        assert len(asked) == 1
        assert "small" in asked[0][0]
        assert asked[0][2] & wx.YES_NO
        assert len(made) == 1

    def test_no_removes_nothing(self, tab, monkeypatch):
        made = _install_fake_progress(monkeypatch)
        self._answer(monkeypatch, wx.NO)
        tab._load_transcription_values()
        tab._select_transcription_model("small")
        tab._start_transcription_action(management.ACTION_REMOVE_MODEL)
        assert made == []

    def test_removing_the_libraries_asks_too(
        self, tab, monkeypatch, no_background_probe
    ):
        made = _install_fake_progress(monkeypatch)
        asked = self._answer(monkeypatch, wx.NO)
        tab._load_transcription_values()
        tab._start_transcription_action(management.ACTION_REMOVE_CUDA_RUNTIME)
        assert len(asked) == 1
        assert asked[0][0] == tab.main_window.i18n.t("transcription_confirm_remove_cuda")
        assert made == []

    def test_verifying_is_not_asked_about(self, tab, monkeypatch):
        """Only the two that delete. A question in front of a harmless action
        is a question users learn to press through."""
        _install_fake_progress(monkeypatch)
        asked = self._answer(monkeypatch, wx.YES)
        tab._load_transcription_values()
        tab._select_transcription_model("small")
        tab._start_transcription_action(management.ACTION_VERIFY_MODEL)
        assert asked == []


class TestTheFocusNeverStaysOnADisabledButton:
    """A download that worked takes the model to INSTALLED, which is exactly
    the state in which Baixar is off — and the focus is on Baixar, because
    that is the button the user pressed. A disabled control answers nothing to
    a screen reader."""

    @staticmethod
    def _watch(tab):
        taken = []
        for action, button in tab._transcription_action_buttons.items():
            button.SetFocus = lambda action=action: taken.append(action)
        tab._transcription_model_combo.SetFocus = lambda: taken.append("model combo")
        tab._transcription_cuda_field.SetFocus = lambda: taken.append("cuda field")
        return taken

    def test_it_moves_to_the_next_button_that_can_be_pressed(self, tab):
        tab._load_transcription_values()
        tab._select_transcription_model("small")
        tab._sync_transcription_action_buttons()
        taken = self._watch(tab)
        # Verificar is off for a model that is not downloaded; Baixar is on.
        tab._restore_transcription_focus(management.ACTION_VERIFY_MODEL)
        assert taken == [management.ACTION_DOWNLOAD_MODEL]

    def test_a_button_that_is_still_enabled_keeps_the_focus(self, tab):
        tab._load_transcription_values()
        tab._select_transcription_model("small")
        tab._sync_transcription_action_buttons()
        taken = self._watch(tab)
        tab._restore_transcription_focus(management.ACTION_DOWNLOAD_MODEL)
        assert taken == []

    def test_a_row_with_nothing_left_falls_back_to_its_own_control(self, tab):
        tab._load_transcription_values()
        # "Automático" names no model, so all four are off.
        assert tab._selected_transcription_model() == preferences.AUTO
        taken = self._watch(tab)
        tab._restore_transcription_focus(management.ACTION_REMOVE_MODEL)
        assert taken == ["model combo"]

    def test_the_models_folder_move_has_no_button_and_moves_no_focus(self, tab):
        tab._load_transcription_values()
        taken = self._watch(tab)
        tab._restore_transcription_focus(management.ACTION_MOVE_MODELS)
        assert taken == []


# ── Part 10b: models the user already has in other folders ───────────────────
#
# The section is `ExternalModelsMixin`, inherited whole by the stub. Nothing
# here touches a real folder of anybody's: reference_state() is replaced by what
# the test says the disk holds, the progress dialog by a fake that answers what
# it is told, and every worker thread is held until the test lets it run — which
# is also how "off the wx thread" is checked rather than assumed.


def _reference(tmp_path, reference_id, model_id=None, name="folder_a"):
    folder = str(tmp_path / name)
    return external_models.ExternalReference(
        reference_id, folder, model_id, True, (1, 2), key=folder
    )


@pytest.fixture
def tab_with(wx_app, tmp_path, no_hardware_probe):
    """Builds a Transcription tab that already has references stored."""
    frames = []

    def _make(*references):
        frame = hidden_frame()
        frames.append(frame)
        owner = _TabOwner(_MainWindow(
            app_settings=_AppSettings(str(tmp_path), references)
        ))
        owner._transcription_page = owner._build_transcription_page(frame)
        frame.Bind(wx.EVT_TEXT, owner._mark_dirty)
        return owner

    try:
        yield _make
    finally:
        for frame in frames:
            destroy_now(frame)


class _Workers:
    """The worker threads the section started, held until `run_all()`."""

    def __init__(self):
        self.pending = []

    def run_all(self):
        while self.pending:
            self.pending.pop(0)()


@pytest.fixture
def workers(monkeypatch):
    """Hold every worker thread, and run wx.CallAfter inline.

    `threading` is replaced in the section's own namespace only: the stub
    thread never starts anything, so a test that forgot to run a worker sees
    its answer missing instead of a real thread touching a real disk.
    """
    held = _Workers()

    class _Thread:
        def __init__(self, target=None, daemon=None, name=None):
            self._target = target
            self.name = name

        def start(self):
            held.pending.append(self._target)

    monkeypatch.setattr(
        transcription_external, "threading", SimpleNamespace(Thread=_Thread)
    )
    monkeypatch.setattr(
        transcription_external.wx, "CallAfter",
        lambda func, *args, **kwargs: func(*args, **kwargs),
    )
    return held


def _measure_as(monkeypatch, states):
    """What the disk holds, by reference id; recorded per call."""
    measured = []

    def _state(reference):
        measured.append(reference.id)
        return states[reference.id]

    monkeypatch.setattr(external_models, "reference_state", _state)
    return measured


def _questions(monkeypatch, answer):
    asked = []
    monkeypatch.setattr(
        transcription_external.wx, "MessageBox",
        lambda text, caption="", *args, **kwargs: asked.append((text, caption)) or answer,
    )
    return asked


def _dir_dialog(monkeypatch, module, code, path=""):
    """Replace wx.DirDialog in `module` with one that answers without a window."""
    class _Dir:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def ShowModal(self):
            return code

        def GetPath(self):
            return path

    monkeypatch.setattr(module.wx, "DirDialog", _Dir)


class _FakeExternalProgress:
    """Stands in for TranscriptionProgressDialog, as _FakeProgress does for the
    management jobs: the job is real and never started, the answer is told."""

    def __init__(self, answers, made, parent, i18n, speak_output, make_job,
                 status_text):
        self.status_text = status_text
        self.job = make_job(lambda tick: None, lambda result, error: None)
        self.result, self.error = answers.pop(0) if answers else (None, None)
        self.destroyed = False
        made.append(self)

    def run(self):
        return wx.ID_CANCEL if self.error is not None else wx.ID_OK

    def Destroy(self):
        self.destroyed = True


def _fake_external_progress(monkeypatch, answers=()):
    made = []
    queue = list(answers)
    monkeypatch.setattr(
        transcription_external, "TranscriptionProgressDialog",
        lambda *args: _FakeExternalProgress(queue, made, *args),
    )
    return made


class _Event:
    def Skip(self):
        pass


class TestTheSectionIsOnTheTab:
    def test_a_list_and_five_buttons_in_the_order_they_are_offered(self, tab):
        assert isinstance(tab._transcription_external_list, wx.ListBox)
        assert list(tab._transcription_external_buttons) == [
            "add", "find", "use", "check", "forget"
        ]
        i18n = tab.main_window.i18n
        for name, key in transcription_external._EXTERNAL_BUTTONS:
            assert tab._transcription_external_buttons[name].GetLabel() == i18n.t(key)

    def test_nothing_referenced_leaves_only_adding_and_searching(self, tab):
        buttons = tab._transcription_external_buttons
        assert tab._transcription_external_list.GetCount() == 0
        assert {n: b.IsEnabled() for n, b in buttons.items()} == {
            "add": True, "find": True, "use": False, "check": False, "forget": False,
        }

    def test_it_is_not_one_of_the_model_action_groups(self, tab):
        assert tab._transcription_external_box not in [
            box for box, _key in tab._transcription_action_groups
        ]

    def test_the_labels_and_the_rows_follow_a_language_change(
        self, tab_with, tmp_path
    ):
        reference = _reference(tmp_path, "r1", "small")
        tab = tab_with(reference)
        tab.main_window.i18n = _I18n("pl")
        tab._refresh_transcription_labels()
        pl = tab.main_window.i18n
        assert tab._transcription_external_buttons["forget"].GetLabel() == pl.t(
            "transcription_external_forget_btn"
        )
        assert tab._transcription_external_box.GetLabel() == pl.t(
            "transcription_external_actions_group"
        )
        assert tab._transcription_external_list.GetString(0) == (
            external_view.row_label(pl, reference, None)
        )


class TestTheListIsMeasuredOffTheWxThread:
    def test_a_reference_reads_as_checking_until_the_worker_answers(
        self, tab_with, tmp_path, workers, monkeypatch
    ):
        reference = _reference(tmp_path, "r1", "small")
        measured = _measure_as(monkeypatch, {"r1": external_models.REF_READY})
        tab = tab_with(reference)
        i18n = tab.main_window.i18n
        tab._refresh_external_models()
        assert measured == []
        assert tab._transcription_external_list.GetString(0) == (
            external_view.row_label(i18n, reference, None)
        )
        workers.run_all()
        assert measured == ["r1"]
        assert tab._transcription_external_list.GetString(0) == (
            external_view.row_label(i18n, reference, external_models.REF_READY)
        )

    def test_with_nothing_referenced_no_worker_is_started(self, tab, workers):
        tab._refresh_external_models()
        assert workers.pending == []

    def test_an_answer_overtaken_by_a_newer_refresh_is_dropped(
        self, tab_with, tmp_path, workers, monkeypatch
    ):
        reference = _reference(tmp_path, "r1", "small")
        _measure_as(monkeypatch, {"r1": external_models.REF_READY})
        tab = tab_with(reference)
        tab._refresh_external_models()
        tab._refresh_external_models()
        first, second = workers.pending
        workers.pending.clear()
        first()
        assert tab._transcription_external_states == {}
        second()
        assert tab._transcription_external_states == {
            "r1": external_models.REF_READY
        }

    def test_a_measuring_that_failed_keeps_saying_checking(
        self, tab_with, tmp_path, workers, monkeypatch
    ):
        def _boom(reference):
            raise OSError("share is down")

        monkeypatch.setattr(external_models, "reference_state", _boom)
        tab = tab_with(_reference(tmp_path, "r1", "small"))
        tab._refresh_external_models()
        workers.run_all()
        assert tab._transcription_external_states == {}

    def test_a_dialog_closed_mid_measurement_touches_nothing(
        self, tab_with, tmp_path, workers, monkeypatch
    ):
        _measure_as(monkeypatch, {"r1": external_models.REF_READY})
        tab = tab_with(_reference(tmp_path, "r1", "small"))
        tab._refresh_external_models()
        tab.__class__.__bool__ = lambda self: False
        try:
            workers.run_all()
        finally:
            del tab.__class__.__bool__
        assert tab._transcription_external_states == {}

    def test_no_call_that_reaches_a_folder_runs_outside_a_worker(self):
        """A drive that is unplugged makes one stat wait out a network
        timeout; on the wx thread that freezes the window and the screen
        reader. Each of these may only appear inside a lambda handed to
        `_external_in_background()` (or inside a function that is itself the
        worker's)."""
        tree = ast.parse(TRANSCRIPTION_EXTERNAL_SOURCE)
        reaching = {
            "reference_state", "discover_hf_cache", "folder_candidates",
            "new_snapshots", "forget_reference", "canonical_dir",
            "accept_catalogue_folder", "accept_custom_folder",
        }
        parents = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent
        found = 0
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in reaching):
                continue
            found += 1
            ancestor, inside_lambda = node, False
            while ancestor in parents:
                ancestor = parents[ancestor]
                inside_lambda = inside_lambda or isinstance(ancestor, ast.Lambda)
            assert inside_lambda, f"{node.func.attr}() runs on the calling thread"
        assert found >= 4


class TestWhichExternalButtonsCanBePressed:
    def test_use_needs_a_folder_that_is_ready_now(self, tab_with, tmp_path):
        tab = tab_with(_reference(tmp_path, "r1", "small"))
        buttons = tab._transcription_external_buttons
        # Still "checking": nothing selected can be used yet, but it can be
        # re-checked or forgotten.
        assert not buttons["use"].IsEnabled()
        assert buttons["check"].IsEnabled() and buttons["forget"].IsEnabled()
        tab._transcription_external_states = {"r1": external_models.REF_READY}
        tab._redraw_external_list()
        assert buttons["use"].IsEnabled()
        tab._transcription_external_states = {"r1": external_models.REF_FOLDER_MISSING}
        tab._redraw_external_list()
        assert not buttons["use"].IsEnabled()
        assert buttons["forget"].IsEnabled()

    def test_a_running_job_turns_all_five_off(self, tab_with, tmp_path):
        tab = tab_with(_reference(tmp_path, "r1", "small"))
        tab._transcription_external_states = {"r1": external_models.REF_READY}
        tab._set_transcription_job_running(True)
        tab._sync_external_buttons()
        assert not any(
            b.IsEnabled() for b in tab._transcription_external_buttons.values()
        )

    def test_the_selection_decides_what_the_buttons_act_on(self, tab_with, tmp_path):
        tab = tab_with(
            _reference(tmp_path, "r1", "small", "folder_a"),
            _reference(tmp_path, "r2", None, "folder_b"),
        )
        tab._transcription_external_states = {
            "r1": external_models.REF_FOLDER_MISSING,
            "r2": external_models.REF_READY,
        }
        tab._redraw_external_list()
        assert tab._selected_external_reference().id == "r1"
        assert not tab._transcription_external_buttons["use"].IsEnabled()
        tab._transcription_external_list.SetSelection(1)
        tab._on_external_selection(_Event())
        assert tab._selected_external_reference().id == "r2"
        assert tab._transcription_external_buttons["use"].IsEnabled()

    def test_redrawing_the_same_rows_does_not_rewrite_the_list(
        self, tab_with, tmp_path
    ):
        """A screen reader re-reads a list control that was rewritten: the
        redraw keeps the very list it drew when nothing it says changed."""
        tab = tab_with(_reference(tmp_path, "r1", "small"))
        drawn = tab._transcription_external_row_labels
        tab._redraw_external_list()
        assert tab._transcription_external_row_labels is drawn
        tab._transcription_external_states = {"r1": external_models.REF_READY}
        tab._redraw_external_list()
        assert tab._transcription_external_row_labels is not drawn


class TestTheModelPickerCountsWhatLivesElsewhere:
    def test_a_custom_model_is_listed_after_the_catalogue(self, tab_with, tmp_path):
        tab = tab_with(_reference(tmp_path, "r2", None))
        tab._populate_transcription_model_choices()
        ids = tab._transcription_model_ids
        assert ids[0] == preferences.AUTO
        assert ids[-1] == "external:r2"
        assert ids.index("small") < ids.index("external:r2")

    def test_a_catalogue_model_in_another_folder_reads_as_such_once_measured(
        self, tab_with, tmp_path
    ):
        tab = tab_with(_reference(tmp_path, "r1", "small"))
        i18n = tab.main_window.i18n
        model = model_catalog.get_model("small")
        index = tab._transcription_model_ids.index("small")
        before = tab._transcription_model_combo.GetString(index)
        tab._transcription_external_states = {"r1": external_models.REF_READY}
        tab._populate_transcription_model_choices()
        after = tab._transcription_model_combo.GetString(index)
        assert after == transcription_tab._transcription_model_choice_label(
            i18n, model, None, external=True
        )
        assert after != before

    def test_a_folder_that_is_gone_does_not_make_its_model_available(
        self, tab_with, tmp_path
    ):
        tab = tab_with(_reference(tmp_path, "r1", "small"))
        index = tab._transcription_model_ids.index("small")
        before = tab._transcription_model_combo.GetString(index)
        tab._transcription_external_states = {
            "r1": external_models.REF_FOLDER_MISSING
        }
        tab._populate_transcription_model_choices()
        assert tab._transcription_model_combo.GetString(index) == before

    def test_the_stored_custom_choice_is_selected_when_the_tab_opens(
        self, tab_with, tmp_path
    ):
        tab = tab_with(_reference(tmp_path, "r2", None))
        tab.main_window.settings["transcription"] = {
            preferences.SETTING_MODEL: "external:r2"
        }
        tab._load_transcription_values()
        assert tab._selected_transcription_model() == "external:r2"
        assert tab._transcription_substituted_settings == set()

    def test_a_forgotten_custom_choice_is_a_substitution(self, tab_with):
        tab = tab_with()
        tab.main_window.settings["transcription"] = {
            preferences.SETTING_MODEL: "external:gone"
        }
        tab._load_transcription_values()
        assert tab._selected_transcription_model() == preferences.AUTO
        assert preferences.SETTING_MODEL in tab._transcription_substituted_settings

    def test_opening_the_page_keeps_a_custom_choice_that_still_exists(
        self, tab_with, tmp_path, workers
    ):
        tab = tab_with(_reference(tmp_path, "r2", None))
        tab.main_window.settings["transcription"] = dict(
            preferences.DEFAULTS, **{preferences.SETTING_MODEL: "external:r2"}
        )
        tab._load_transcription_values()
        tab._enter_transcription_page()
        assert tab.main_window.settings["transcription"][
            preferences.SETTING_MODEL] == "external:r2"
        assert tab.main_window.saves == 0
        assert tab.main_window.speak_output.spoken == []

    def test_opening_the_page_rewrites_a_forgotten_choice_to_automatic(
        self, tab_with
    ):
        tab = tab_with()
        tab.main_window.settings["transcription"] = {
            preferences.SETTING_MODEL: "external:gone"
        }
        tab._load_transcription_values()
        tab._enter_transcription_page()
        assert tab.main_window.settings["transcription"][
            preferences.SETTING_MODEL] == preferences.AUTO
        assert tab.main_window.saves == 1


class TestUsingAModelFromAnotherFolder:
    def test_a_catalogue_model_is_chosen_by_its_id(
        self, tab_with, tmp_path, workers, monkeypatch
    ):
        _measure_as(monkeypatch, {"r1": external_models.REF_READY})
        tab = tab_with(_reference(tmp_path, "r1", "small", "folder_a"))
        tab._refresh_external_models()
        workers.run_all()
        tab.dirtied = 0
        tab._on_external_use(_Event())
        assert tab._selected_transcription_model() == "small"
        assert tab.dirtied == 1
        assert tab.main_window.speak_output.spoken == [
            tab.main_window.i18n.t(external_view.USE_DONE_I18N_KEY).format(
                name="folder_a"
            )
        ]

    def test_a_custom_model_is_chosen_by_the_external_prefix_and_its_id(
        self, tab_with, tmp_path, workers, monkeypatch
    ):
        _measure_as(monkeypatch, {"r2": external_models.REF_READY})
        tab = tab_with(_reference(tmp_path, "r2", None))
        tab._refresh_external_models()
        workers.run_all()
        tab._on_external_use(_Event())
        assert tab._selected_transcription_model() == "external:r2"

    def test_what_is_not_ready_cannot_be_chosen(self, tab_with, tmp_path):
        tab = tab_with(_reference(tmp_path, "r1", "small"))
        tab._on_external_use(_Event())
        assert tab._selected_transcription_model() == preferences.AUTO
        assert tab.main_window.speak_output.spoken == []


class TestForgettingAModel:
    def test_a_no_changes_nothing(self, tab_with, tmp_path, workers, monkeypatch):
        asked = _questions(monkeypatch, wx.NO)
        tab = tab_with(_reference(tmp_path, "r1", "small"))
        tab._on_external_forget(_Event())
        assert len(asked) == 1
        assert workers.pending == []
        assert len(tab.main_window._app_settings.get(
            external_models.EXTERNAL_MODELS_SETTING)) == 1

    def test_the_question_says_the_files_are_not_touched(
        self, tab_with, tmp_path, monkeypatch
    ):
        asked = _questions(monkeypatch, wx.NO)
        tab = tab_with(_reference(tmp_path, "r1", "small", "folder_a"))
        tab._on_external_forget(_Event())
        assert asked[0][0] == tab.main_window.i18n.t(
            external_view.FORGET_QUESTION_I18N_KEY
        ).format(name="folder_a")

    def test_a_yes_drops_the_reference_and_leaves_the_folder_alone(
        self, tab_with, tmp_path, workers, monkeypatch
    ):
        _questions(monkeypatch, wx.YES)
        folder = tmp_path / "folder_a"
        folder.mkdir()
        (folder / "model.bin").write_bytes(b"weights")
        tab = tab_with(_reference(tmp_path, "r1", "small", "folder_a"))
        tab._on_external_forget(_Event())
        # Working: every button is off until the worker answers.
        assert tab._transcription_job_running
        assert not any(
            b.IsEnabled() for b in tab._transcription_external_buttons.values()
        )
        workers.run_all()
        assert tab.main_window._app_settings.get(
            external_models.EXTERNAL_MODELS_SETTING) == []
        assert tab._transcription_external_list.GetCount() == 0
        assert (folder / "model.bin").read_bytes() == b"weights"
        assert tab._transcription_job_running is False
        assert tab.main_window.speak_output.spoken == [
            tab.main_window.i18n.t(external_view.FORGOTTEN_I18N_KEY).format(
                name="folder_a"
            )
        ]

    def test_forgetting_the_chosen_custom_model_puts_the_picker_back_on_automatic(
        self, tab_with, tmp_path, workers, monkeypatch
    ):
        _questions(monkeypatch, wx.YES)
        tab = tab_with(_reference(tmp_path, "r2", None, "folder_b"))
        tab._select_transcription_model("external:r2")
        tab.dirtied = 0
        tab._on_external_forget(_Event())
        workers.run_all()
        i18n = tab.main_window.i18n
        assert tab._selected_transcription_model() == preferences.AUTO
        assert tab.dirtied == 1
        assert tab.main_window.speak_output.spoken == [
            " ".join([
                i18n.t(external_view.FORGOTTEN_I18N_KEY).format(name="folder_b"),
                i18n.t(external_view.FORGOTTEN_RESET_I18N_KEY),
            ])
        ]

    def test_forgetting_a_model_that_is_not_the_choice_does_not_dirty_the_dialog(
        self, tab_with, tmp_path, workers, monkeypatch
    ):
        _questions(monkeypatch, wx.YES)
        tab = tab_with(_reference(tmp_path, "r1", "small"))
        tab._on_external_forget(_Event())
        workers.run_all()
        assert tab.dirtied == 0

    def test_a_write_that_failed_says_so_and_keeps_the_row(
        self, tab_with, tmp_path, workers, monkeypatch
    ):
        _questions(monkeypatch, wx.YES)
        tab = tab_with(_reference(tmp_path, "r1", "small"))

        def _refuse(key, change):
            raise OSError("locked")

        tab.main_window._app_settings.update = _refuse
        tab._on_external_forget(_Event())
        workers.run_all()
        assert tab._transcription_external_list.GetCount() == 1
        assert tab._transcription_job_running is False
        assert tab.main_window.error_sound.plays == 1
        assert tab.main_window.speak_output.spoken == [
            tab.main_window.i18n.t(management.FAILED_I18N_KEY)
        ]

    def test_with_nothing_selected_it_asks_nothing(self, tab, monkeypatch):
        asked = _questions(monkeypatch, wx.YES)
        tab._on_external_forget(_Event())
        assert asked == []


class TestAddingAFolder:
    def test_cancelling_the_folder_dialog_starts_nothing(
        self, tab, workers, monkeypatch
    ):
        _dir_dialog(monkeypatch, transcription_external, wx.ID_CANCEL)
        tab._on_external_add(_Event())
        assert workers.pending == []
        assert tab._transcription_job_running is False

    def test_the_folder_is_looked_at_on_a_worker_and_the_buttons_wait(
        self, tab, workers, monkeypatch, tmp_path
    ):
        chosen = str(tmp_path / "picked")
        _dir_dialog(monkeypatch, transcription_external, wx.ID_OK, chosen)
        looked_at = []
        monkeypatch.setattr(
            external_view, "folder_candidates",
            lambda path: looked_at.append(path) or (external_view.Candidate(path),),
        )
        checked = []
        tab._check_external_folder = (
            lambda folder, reference=None, backend_id=None: checked.append((folder, backend_id)))
        tab._on_external_add(_Event())
        assert looked_at == [] and checked == []
        assert tab._transcription_job_running
        assert not tab._transcription_external_buttons["add"].IsEnabled()
        workers.run_all()
        # Checked as a faster-whisper folder, said rather than defaulted.
        assert looked_at == [chosen] and checked == [
            (chosen, backend_module.BACKEND_FASTER_WHISPER)]
        assert tab._transcription_job_running is False
        assert tab._transcription_external_buttons["add"].IsEnabled()

    def test_a_folder_that_could_not_be_looked_at_is_still_checked_by_itself(
        self, tab, workers, monkeypatch, tmp_path
    ):
        """The check that follows is the one that says why it is not usable."""
        chosen = str(tmp_path / "picked")
        _dir_dialog(monkeypatch, transcription_external, wx.ID_OK, chosen)

        def _boom(path):
            raise OSError("share is down")

        monkeypatch.setattr(external_view, "folder_candidates", _boom)
        checked = []
        tab._check_external_folder = (
            lambda folder, reference=None, backend_id=None: checked.append((folder, backend_id)))
        tab._on_external_add(_Event())
        workers.run_all()
        assert checked == [(chosen, backend_module.BACKEND_FASTER_WHISPER)]

    def test_several_models_in_one_folder_are_a_list_to_pick_from(
        self, tab, workers, monkeypatch, tmp_path
    ):
        first = external_view.Candidate(str(tmp_path / "a"), "0123456789abcdef", "small")
        second = external_view.Candidate(str(tmp_path / "b"), "fedcba9876543210")
        offered = []

        class _Choice:
            def __init__(self, parent, prompt, title, labels):
                offered.append(labels)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def ShowModal(self):
                return wx.ID_OK

            def GetSelection(self):
                return 1

        monkeypatch.setattr(transcription_external.wx, "SingleChoiceDialog", _Choice)
        i18n = tab.main_window.i18n
        assert tab._external_pick((first, second)) == second.path
        assert offered == [[
            external_view.snapshot_label(i18n, first),
            external_view.snapshot_label(i18n, second),
        ]]

    def test_a_single_model_is_not_a_question(self, tab, monkeypatch, tmp_path):
        monkeypatch.setattr(
            transcription_external.wx, "SingleChoiceDialog",
            lambda *args: pytest.fail("a list of one was asked about"),
        )
        only = external_view.Candidate(str(tmp_path / "a"))
        assert tab._external_pick((only,)) == only.path

    def test_a_second_press_while_something_runs_starts_nothing(
        self, tab, workers, monkeypatch
    ):
        _dir_dialog(
            monkeypatch, transcription_external, wx.ID_OK, "unused"
        )
        tab._set_transcription_job_running(True)
        tab._on_external_add(_Event())
        assert workers.pending == []


class TestSearchingTheHuggingFaceCache:
    def test_it_says_it_is_searching_and_does_the_searching_on_a_worker(
        self, tab, workers, monkeypatch
    ):
        searched = []
        monkeypatch.setattr(
            external_models, "discover_hf_cache",
            lambda: searched.append(1) or (),
        )
        tab._on_external_find(_Event())
        assert searched == []
        assert tab.main_window.speak_output.spoken == [
            tab.main_window.i18n.t(external_view.SEARCHING_I18N_KEY)
        ]
        workers.run_all()
        assert searched == [1]

    def test_nothing_new_is_said_not_left_silent(self, tab, workers, monkeypatch):
        monkeypatch.setattr(external_models, "discover_hf_cache", lambda: ())
        tab._on_external_find(_Event())
        workers.run_all()
        assert tab.main_window.speak_output.spoken[-1] == (
            tab.main_window.i18n.t(external_view.FIND_NONE_I18N_KEY)
        )
        assert tab._transcription_job_running is False

    def test_a_search_that_failed_is_told_as_nothing_found(
        self, tab, workers, monkeypatch
    ):
        def _boom():
            raise OSError("cache unreadable")

        monkeypatch.setattr(external_models, "discover_hf_cache", _boom)
        tab._on_external_find(_Event())
        workers.run_all()
        assert tab.main_window.speak_output.spoken[-1] == (
            tab.main_window.i18n.t(external_view.FIND_NONE_I18N_KEY)
        )

    def test_even_one_find_is_listed_before_hashing_gigabytes(
        self, tab, workers, monkeypatch, tmp_path
    ):
        found = external_view.Candidate(str(tmp_path / "a"), "0123456789abcdef", "small")
        monkeypatch.setattr(external_models, "discover_hf_cache", lambda: (found,))
        monkeypatch.setattr(
            external_view, "new_snapshots", lambda snapshots, references: tuple(snapshots)
        )
        asked = []
        tab._external_pick = lambda candidates, always_ask=False: (
            asked.append(always_ask) or None
        )
        tab._on_external_find(_Event())
        workers.run_all()
        assert asked == [True]


class TestCheckingAFolder:
    @staticmethod
    def _outcome(code, reference=None, model_id=None):
        identification = (
            external_models.Identification(
                "x", external_models.MATCH_NONE, model_id
            )
        )
        return external_models.AcceptOutcome(code, reference, identification)

    def test_a_verified_catalogue_model_is_announced_and_selected_in_the_list(
        self, tab, monkeypatch, tmp_path
    ):
        reference = _reference(tmp_path, "r1", "small", "folder_a")
        made = _fake_external_progress(monkeypatch, [(
            self._outcome(external_models.ACCEPT_ADDED, reference), None
        )])
        refreshed = []
        tab._refresh_external_models = lambda select_id=None: refreshed.append(select_id)
        tab._check_external_folder(reference.path)
        assert [d.job.kind for d in made] == [external_job.KIND_VERIFY]
        assert made[0].destroyed
        assert refreshed == ["r1"]
        assert tab.main_window.speak_output.spoken == [
            tab.main_window.i18n.t(external_job.ADDED_I18N_KEY).format(
                name="folder_a", model="small"
            )
        ]
        assert tab._transcription_job_running is False

    def test_the_progress_dialog_names_the_folder(self, tab, monkeypatch, tmp_path):
        made = _fake_external_progress(monkeypatch)
        tab._refresh_external_models = lambda select_id=None: None
        tab._check_external_folder(str(tmp_path / "folder_a"))
        assert made[0].status_text == tab.main_window.i18n.t(
            external_job.STATUS_I18N_KEYS[external_job.KIND_VERIFY]
        ).format(name="folder_a")

    def test_the_job_is_checked_against_the_models_folder_in_force(
        self, tab, monkeypatch, tmp_path
    ):
        made = _fake_external_progress(monkeypatch)
        tab._refresh_external_models = lambda select_id=None: None
        tab._check_external_folder(str(tmp_path / "folder_a"))
        assert made[0].job._models_root == str(tmp_path)

    def test_a_folder_no_catalogue_entry_claims_is_asked_about_then_loaded(
        self, tab, monkeypatch, tmp_path
    ):
        reference = _reference(tmp_path, "r2", None, "folder_b")
        made = _fake_external_progress(monkeypatch, [
            (self._outcome(external_models.ACCEPT_NOT_IDENTIFIED), None),
            (self._outcome(external_models.ACCEPT_ADDED, reference), None),
        ])
        asked = _questions(monkeypatch, wx.YES)
        tab._refresh_external_models = lambda select_id=None: None
        tab._check_external_folder(reference.path)
        i18n = tab.main_window.i18n
        assert [d.job.kind for d in made] == [
            external_job.KIND_VERIFY, external_job.KIND_CUSTOM
        ]
        assert asked == [(
            external_view.custom_question(
                i18n, reference.path, external_models.ACCEPT_NOT_IDENTIFIED, None
            ),
            i18n.t("transcription_external_question_title"),
        )]
        # One sentence for the whole thing, not one per job.
        assert tab.main_window.speak_output.spoken == [
            i18n.t(external_job.CUSTOM_ADDED_I18N_KEY).format(
                name="folder_b", model=""
            )
        ]

    def test_a_no_to_the_question_loads_nothing_and_says_nothing_was_added(
        self, tab, monkeypatch, tmp_path
    ):
        made = _fake_external_progress(monkeypatch, [
            (self._outcome(external_models.ACCEPT_NOT_IDENTIFIED), None),
        ])
        _questions(monkeypatch, wx.NO)
        tab._refresh_external_models = lambda select_id=None: None
        tab._check_external_folder(str(tmp_path / "folder_b"))
        assert len(made) == 1
        assert tab.main_window.speak_output.spoken == [
            tab.main_window.i18n.t(external_job.NOT_ADDED_I18N_KEY).format(
                name="folder_b"
            )
        ]
        assert tab.main_window.error_sound.plays == 0

    def test_a_folder_with_a_models_sizes_and_other_weights_says_which_model(
        self, tab, monkeypatch, tmp_path
    ):
        _fake_external_progress(monkeypatch, [(
            self._outcome(
                external_models.ACCEPT_DIGEST_MISMATCH, model_id="large-v3"
            ), None
        )])
        asked = _questions(monkeypatch, wx.NO)
        tab._refresh_external_models = lambda select_id=None: None
        tab._check_external_folder(str(tmp_path / "folder_b"))
        assert "large-v3" in asked[0][0]

    def test_a_catalogue_model_that_failed_to_be_read_is_not_asked_about(
        self, tab, monkeypatch, tmp_path
    ):
        _fake_external_progress(monkeypatch, [(
            None, errors.TranscriptionError(errors.MODEL_CORRUPTED, "cannot read")
        )])
        asked = _questions(monkeypatch, wx.YES)
        tab._refresh_external_models = lambda select_id=None: None
        tab._check_external_folder(str(tmp_path / "folder_a"))
        assert asked == []
        assert tab.main_window.error_sound.plays == 1
        assert tab.main_window.speak_output.spoken == [
            tab.main_window.i18n.t(external_job.READ_FAILED_I18N_KEY).format(
                name="folder_a"
            )
        ]

    def test_checking_a_custom_reference_again_goes_straight_to_the_load(
        self, tab, monkeypatch, tmp_path
    ):
        reference = _reference(tmp_path, "r2", None, "folder_b")
        made = _fake_external_progress(monkeypatch, [(
            self._outcome(external_models.ACCEPT_UPDATED, reference), None
        )])
        asked = _questions(monkeypatch, wx.YES)
        tab._refresh_external_models = lambda select_id=None: None
        tab._check_external_folder(reference.path, reference)
        assert [d.job.kind for d in made] == [external_job.KIND_CUSTOM]
        assert asked == []
        assert tab.main_window.speak_output.spoken == [
            tab.main_window.i18n.t(external_job.CUSTOM_CHECKED_I18N_KEY).format(
                name="folder_b", model=""
            )
        ]

    def test_a_cancel_plays_no_error_sound(self, tab, monkeypatch, tmp_path):
        _fake_external_progress(monkeypatch, [(
            None, errors.TranscriptionError(errors.CANCELLED, "by the user")
        )])
        tab._refresh_external_models = lambda select_id=None: None
        tab._check_external_folder(str(tmp_path / "folder_a"))
        assert tab.main_window.error_sound.plays == 0
        assert tab.main_window.speak_output.spoken == [
            tab.main_window.i18n.t(management.CANCELLED_I18N_KEY)
        ]

    def test_the_sentence_is_left_on_screen_and_apply_does_not_appear(
        self, tab, monkeypatch, tmp_path
    ):
        reference = _reference(tmp_path, "r1", "small", "folder_a")
        _fake_external_progress(monkeypatch, [(
            self._outcome(external_models.ACCEPT_ADDED, reference), None
        )])
        tab._refresh_external_models = lambda select_id=None: None
        tab.dirtied = 0
        tab._check_external_folder(reference.path)
        assert tab.main_window.i18n.t(external_job.ADDED_I18N_KEY).format(
            name="folder_a", model="small"
        ) in tab._transcription_substituted_field.GetValue()
        assert tab.dirtied == 0

    def test_checking_again_is_the_check_button(self, tab_with, tmp_path, monkeypatch):
        reference = _reference(tmp_path, "r1", "small")
        tab = tab_with(reference)
        checked = []
        tab._check_external_folder = lambda folder, ref=None: checked.append((folder, ref))
        tab._on_external_check(_Event())
        assert checked == [(reference.path, reference)]


class TestTheBrowseButtonOfTheModelsFolder:
    def test_a_folder_that_contains_a_reference_is_refused_and_named(
        self, tab_with, tmp_path, monkeypatch
    ):
        inner = tmp_path / "parent" / "folder_a"
        inner.mkdir(parents=True)
        reference = external_models.ExternalReference(
            "r1", str(inner), "small", True, (1, 2), key=canonical_dir(str(inner))
        )
        tab = tab_with(reference)
        tab._load_transcription_values()
        before = tab._transcription_models_dir
        _dir_dialog(
            monkeypatch, transcription_tab, wx.ID_OK, str(tmp_path / "parent")
        )
        shown = []
        monkeypatch.setattr(
            transcription_tab.wx, "MessageBox",
            lambda text, *args, **kwargs: shown.append(text) or wx.OK,
        )
        tab.dirtied = 0
        tab._on_browse_transcription_models_dir(_Event())
        assert tab._transcription_models_dir == before
        assert tab.dirtied == 0
        assert len(shown) == 1 and "folder_a" in shown[0]

    def test_an_unrelated_folder_is_accepted_as_before(
        self, tab_with, tmp_path, monkeypatch
    ):
        reference = _reference(tmp_path, "r1", "small", "folder_a")
        tab = tab_with(reference)
        tab._load_transcription_values()
        chosen = str(tmp_path / "elsewhere")
        _dir_dialog(monkeypatch, transcription_tab, wx.ID_OK, chosen)
        tab.dirtied = 0
        tab._on_browse_transcription_models_dir(_Event())
        assert tab._transcription_models_dir == chosen
        assert tab.dirtied == 1


class TestTheFocusInTheSectionForModelsElsewhere:
    @staticmethod
    def _watch(tab):
        taken = []
        for name, button in tab._transcription_external_buttons.items():
            button.SetFocus = lambda name=name: taken.append(name)
        tab._transcription_external_list.SetFocus = lambda: taken.append("list")
        return taken

    def test_nothing_moves_it_while_something_runs(self, tab, workers, monkeypatch):
        """Moving it onto the list mid-search announced the list over
        "Searching…"."""
        monkeypatch.setattr(external_models, "discover_hf_cache", lambda: ())
        taken = self._watch(tab)
        tab._on_external_find(_Event())
        assert taken == []
        assert tab.main_window.speak_output.spoken == [
            tab.main_window.i18n.t(external_view.SEARCHING_I18N_KEY)
        ]

    def test_find_still_pressable_afterwards_keeps_it(self, tab, workers, monkeypatch):
        monkeypatch.setattr(external_models, "discover_hf_cache", lambda: ())
        taken = self._watch(tab)
        tab._on_external_find(_Event())
        workers.run_all()
        assert taken == []

    def test_forgetting_one_of_several_leaves_it_on_forget(
        self, tab_with, tmp_path, workers, monkeypatch
    ):
        """The list selects the next reference, so Forget is on again and
        acts on that one: nothing to move."""
        _questions(monkeypatch, wx.YES)
        _measure_as(monkeypatch, {"r2": external_models.REF_FOLDER_MISSING})
        tab = tab_with(
            _reference(tmp_path, "r1", "small", "folder_a"),
            _reference(tmp_path, "r2", "small", "folder_b"),
        )
        taken = self._watch(tab)
        tab._on_external_forget(_Event())
        assert taken == []
        workers.run_all()
        assert tab._selected_external_reference().id == "r2"
        assert tab._transcription_external_buttons["forget"].IsEnabled()
        assert taken == []

    def test_a_disabled_button_with_rows_left_falls_back_to_the_list(
        self, tab_with, tmp_path
    ):
        tab = tab_with(_reference(tmp_path, "r1", "small"))
        # Still "checking": Use is off for it.
        assert not tab._transcription_external_buttons["use"].IsEnabled()
        taken = self._watch(tab)
        tab._restore_external_focus("use")
        assert taken == ["list"]

    def test_forgetting_the_last_one_lands_on_the_first_button_still_on(
        self, tab_with, tmp_path, workers, monkeypatch
    ):
        _questions(monkeypatch, wx.YES)
        tab = tab_with(_reference(tmp_path, "r1", "small"))
        taken = self._watch(tab)
        tab._on_external_forget(_Event())
        workers.run_all()
        assert taken == ["add"]

    def test_a_button_still_on_keeps_it(self, tab_with, tmp_path):
        tab = tab_with(_reference(tmp_path, "r1", "small"))
        taken = self._watch(tab)
        tab._restore_external_focus("check")
        assert taken == []


class TestReferencesThatCouldNotBeRead:
    """app.json unreadable for a moment is not "nothing referenced": nothing
    may be rewritten, reported or written back for a reference's absence."""

    @staticmethod
    def _unreadable_tab(tab_with, tmp_path):
        tab = tab_with(_reference(tmp_path, "r2", None))
        tab.main_window._app_settings.unreadable = OSError("held by a share")
        tab._transcription_external_references, tab._transcription_external_known = (
            external_models.read_references(tab.main_window._app_settings)
        )
        tab.main_window.settings["transcription"] = dict(
            preferences.DEFAULTS, **{preferences.SETTING_MODEL: "external:r2"}
        )
        return tab

    def test_the_custom_choice_is_not_reported_as_replaced(self, tab_with, tmp_path):
        tab = self._unreadable_tab(tab_with, tmp_path)
        tab._load_transcription_values()
        assert tab._transcription_substituted_settings == set()

    def test_opening_the_page_does_not_retire_it(self, tab_with, tmp_path, workers):
        tab = self._unreadable_tab(tab_with, tmp_path)
        tab._load_transcription_values()
        tab._enter_transcription_page()
        assert tab.main_window.settings["transcription"][
            preferences.SETTING_MODEL] == "external:r2"
        assert tab.main_window.saves == 0

    def test_ok_does_not_write_the_stand_in_back(self, tab_with, tmp_path, workers):
        tab = self._unreadable_tab(tab_with, tmp_path)
        tab._load_transcription_values()
        tab._enter_transcription_page()
        assert tab._selected_transcription_model() == preferences.AUTO
        tab._apply_transcription_values()
        assert tab.main_window.settings["transcription"][
            preferences.SETTING_MODEL] == "external:r2"

    def test_a_model_the_user_picks_is_still_written(self, tab_with, tmp_path):
        tab = self._unreadable_tab(tab_with, tmp_path)
        tab._load_transcription_values()
        tab._select_transcription_model("small")
        tab._apply_transcription_values()
        assert tab.main_window.settings["transcription"][
            preferences.SETTING_MODEL] == "small"

    def test_a_refresh_keeps_the_list_it_had(self, tab_with, tmp_path, workers):
        tab = tab_with(_reference(tmp_path, "r1", "small"))
        tab.main_window._app_settings.unreadable = OSError("held by a share")
        tab._refresh_external_models()
        assert tab._transcription_external_known is False
        assert tab._transcription_external_list.GetCount() == 1


class TestTheModelsFolderCannotSwallowAFolderAddedAfterwards:
    """Procurar X, then add X/small, then OK: the move would put WinZapp's
    models — and its Remover — on top of the user's folder."""

    @staticmethod
    def _inside(tmp_path):
        inner = tmp_path / "chosen" / "small"
        inner.mkdir(parents=True)
        return external_models.ExternalReference(
            "r1", str(inner), "small", True, (1, 2), key=canonical_dir(str(inner))
        )

    def test_ok_refuses_the_move_says_so_and_keeps_the_folder(
        self, tab_with, tmp_path, monkeypatch
    ):
        tab = tab_with()
        tab._load_transcription_values()
        stored = tab._stored_transcription_models_dir()
        tab._transcription_models_dir = str(tmp_path / "chosen")
        # Added after Procurar, so only app.json has it.
        tab.main_window._app_settings._values[
            external_models.EXTERNAL_MODELS_SETTING] = [self._inside(tmp_path).as_dict()]
        moved = []
        tab._move_transcription_models = lambda: moved.append(1) or "moved"
        shown = []
        monkeypatch.setattr(
            transcription_tab.wx, "MessageBox",
            lambda text, *args, **kwargs: shown.append(text) or wx.OK,
        )
        tab._apply_transcription_values()
        i18n = tab.main_window.i18n
        assert moved == []
        assert tab._stored_transcription_models_dir() == stored
        assert tab._transcription_models_dir == stored
        assert len(shown) == 1
        assert "small" in shown[0]
        assert shown[0].endswith(i18n.t("transcription_external_root_not_applied"))

    def test_a_reference_only_this_dialog_holds_counts_too(
        self, tab_with, tmp_path, monkeypatch
    ):
        tab = tab_with()
        tab._load_transcription_values()
        tab._transcription_models_dir = str(tmp_path / "chosen")
        tab._transcription_external_references = (self._inside(tmp_path),)
        moved = []
        tab._move_transcription_models = lambda: moved.append(1) or "moved"
        monkeypatch.setattr(transcription_tab.wx, "MessageBox", lambda *a, **k: wx.OK)
        tab._apply_transcription_values()
        assert moved == []

    def test_a_folder_elsewhere_is_moved_as_before(self, tab_with, tmp_path):
        tab = tab_with()
        tab._load_transcription_values()
        tab._transcription_models_dir = str(tmp_path / "chosen")
        moved = []
        tab._move_transcription_models = lambda: moved.append(1) or "moved"
        tab._apply_transcription_values()
        assert moved == [1]

    def test_adding_a_folder_under_the_chosen_root_is_checked_against_it(
        self, tab, monkeypatch, tmp_path
    ):
        made = _fake_external_progress(monkeypatch)
        tab._refresh_external_models = lambda select_id=None: None
        tab._load_transcription_values()
        tab._transcription_models_dir = str(tmp_path / "chosen")
        tab._check_external_folder(str(tmp_path / "chosen" / "small"))
        assert made[0].job._other_roots == (str(tmp_path / "chosen"),)

    def test_with_nothing_chosen_there_is_no_second_root(
        self, tab, monkeypatch, tmp_path
    ):
        made = _fake_external_progress(monkeypatch)
        tab._refresh_external_models = lambda select_id=None: None
        tab._load_transcription_values()
        tab._check_external_folder(str(tmp_path / "folder_a"))
        assert made[0].job._other_roots == ()


class TestTheModelPickerIsNotRewrittenForNothing:
    def test_the_same_lines_leave_the_list_alone(self, tab, monkeypatch):
        tab._load_transcription_values()
        drawn = tab._transcription_model_labels
        tab._populate_transcription_model_choices()
        assert tab._transcription_model_labels is drawn

    def test_a_measurement_that_changes_a_line_does_redraw(self, tab_with, tmp_path):
        tab = tab_with(_reference(tmp_path, "r1", "small"))
        drawn = tab._transcription_model_labels
        tab._transcription_external_states = {"r1": external_models.REF_READY}
        tab._populate_transcription_model_choices()
        assert tab._transcription_model_labels is not drawn

    def test_entering_the_page_draws_the_hardware_notices_once(self, tab, monkeypatch):
        """Once on entering, and once more when the probe, taken on a worker,
        brings something new to draw them from."""
        started = []
        monkeypatch.setattr(
            transcription_tab.transcription_management, "probe_in_background",
            lambda on_done, probe=None: started.append(on_done),
        )
        monkeypatch.setattr(
            transcription_tab.wx, "CallAfter", lambda func, *args: func(*args)
        )
        drawn = []
        real = tab._show_transcription_hardware_notices
        tab._show_transcription_hardware_notices = lambda: drawn.append(1) or real()
        tab._load_transcription_values()
        drawn.clear()
        tab._enter_transcription_page()
        assert drawn == [1]
        started[0](_CPU_ONLY)
        assert drawn == [1, 1]


# ── Part 9b: the whisper.cpp backend ─────────────────────────────────────────
#
# The section is `WhisperCppMixin`, inherited whole by the stub; the install-wide
# folder its builds live in is answered by `no_hardware_probe`.

_CPU_BUILD = whisper_cpp_builds.BUILD_CPU
_CUDA_BUILD = whisper_cpp_builds.BUILD_CUDA


def _with_card(capability):
    return device.HardwareProbe(
        cuda_available=True, cuda_device_count=1, compute_capability=capability,
        total_vram_mb=12_000, free_vram_mb=10_000, total_ram_mb=16_384,
        available_ram_mb=12_288, cuda_libraries_ok=True,
    )


def _builds_on_disk(monkeypatch, installed):
    monkeypatch.setattr(
        transcription_whisper_cpp.whisper_cpp_runtime, "installation_state",
        lambda build, root=None: whisper_cpp_runtime.RuntimeState(
            whisper_cpp_runtime.STATE_INSTALLED if build in installed
            else whisper_cpp_runtime.STATE_ABSENT
        ),
    )


class _Skippable:
    def __init__(self):
        self.skipped = False

    def Skip(self):
        self.skipped = True


class TestTheModelPickerFollowsTheBackend:
    def test_each_backend_lists_its_own_catalogue(self, tab):
        tab._load_transcription_values()
        assert tab._transcription_model_ids[1:] == [
            model.id for model in model_catalog.list_models()
        ]
        tab._select_transcription_backend(backend_module.BACKEND_WHISPER_CPP)
        tab._on_transcription_backend_change(_Skippable())
        assert tab._transcription_model_ids[1:] == [
            model.id for model in preferences.catalogue_models(
                backend_module.BACKEND_WHISPER_CPP)
        ]

    def test_a_ggml_line_says_the_model_and_its_bits_not_the_file(self, tab):
        tab._load_transcription_values()
        tab._select_transcription_backend(backend_module.BACKEND_WHISPER_CPP)
        tab._populate_transcription_model_choices()
        line = tab._transcription_model_labels[
            tab._transcription_model_ids.index("ggml-small-q5_1")]
        assert "ggml-" not in line and "q5_1" not in line

    def test_a_model_of_the_other_backend_falls_back_to_automatic(self, tab):
        tab._load_transcription_values()
        tab._select_transcription_model("small")
        tab._select_transcription_backend(backend_module.BACKEND_WHISPER_CPP)
        event = _Skippable()
        tab._on_transcription_backend_change(event)
        assert tab._selected_transcription_model() == preferences.AUTO
        # Skip(): the dialog-level handler is what shows Apply.
        assert event.skipped

    def test_a_stored_ggml_model_is_selected_when_the_tab_opens(self, tab):
        tab.main_window.settings["transcription"] = {
            "backend": backend_module.BACKEND_WHISPER_CPP, "model": "ggml-small-q5_1",
        }
        tab._load_transcription_values()
        assert tab._selected_transcription_model() == "ggml-small-q5_1"

    def test_an_english_only_model_with_detection_on_says_it_forces_english(self, tab):
        tab._load_transcription_values()
        tab._enter_transcription_page()
        tab._select_transcription_model("small.en")
        tab._show_transcription_hardware_notices()
        assert tab._transcription_hardware_keys == [
            transcription_tab._TRANSCRIPTION_LANGUAGE_FORCED]

    def test_another_language_chosen_says_it_is_replaced(self, tab):
        tab._load_transcription_values()
        tab._enter_transcription_page()
        tab._select_transcription_model("kb-whisper-small")
        tab._transcription_detect_language_check.SetValue(False)
        tab._select_transcription_language("pt")
        tab._show_transcription_hardware_notices()
        assert tab._transcription_hardware_keys == [
            transcription_tab._TRANSCRIPTION_LANGUAGE_OVERRIDDEN]


class TestTheWhisperCppProgram:
    def test_the_processor_build_is_always_listed_and_first(self, tab):
        tab._load_transcription_values()
        tab._enter_transcription_page()
        assert tab._whisper_cpp_build_ids == [_CPU_BUILD.id]

    @pytest.mark.parametrize("capability, listed", [
        ((8, 6), [_CPU_BUILD.id, _CUDA_BUILD.id]),
        ((12, 0), [_CPU_BUILD.id]),
        (None, [_CPU_BUILD.id]),
    ])
    def test_the_graphics_build_only_where_the_card_can_run_it(
        self, tab, capability, listed
    ):
        tab._load_transcription_values()
        tab._adopt_transcription_probe(_with_card(capability))
        assert tab._whisper_cpp_build_ids == listed

    def test_a_graphics_build_already_installed_stays_removable(self, tab, monkeypatch):
        _builds_on_disk(monkeypatch, {_CPU_BUILD, _CUDA_BUILD})
        tab._load_transcription_values()
        tab._adopt_transcription_probe(_with_card((12, 0)))
        assert tab._whisper_cpp_build_ids == [_CPU_BUILD.id, _CUDA_BUILD.id]

    def test_each_line_says_the_build_its_state_and_its_size(self):
        i18n = _I18n()
        installed = transcription_whisper_cpp.whisper_cpp_build_label(
            i18n, _CUDA_BUILD, whisper_cpp_runtime.STATE_INSTALLED)
        absent = transcription_whisper_cpp.whisper_cpp_build_label(
            i18n, _CUDA_BUILD, whisper_cpp_runtime.STATE_ABSENT)
        name = transcription_tab._whisper_cpp_build_name(i18n, _CUDA_BUILD.id)
        size = transcription_tab._format_transcription_size(
            i18n, _CUDA_BUILD.archive_bytes)
        assert name in installed and name in absent
        # The size is the download's cost: a build already here costs none.
        assert size in absent and size not in installed
        assert installed != absent

    def test_nothing_installed_offers_the_install_and_nothing_else(self, tab):
        tab._load_transcription_values()
        buttons = tab._transcription_action_buttons
        assert buttons[management.ACTION_INSTALL_WHISPER_CPP].IsEnabled()
        for action in (management.ACTION_VERIFY_WHISPER_CPP,
                       management.ACTION_REPAIR_WHISPER_CPP,
                       management.ACTION_REMOVE_WHISPER_CPP):
            assert not buttons[action].IsEnabled()

    def test_an_installed_build_offers_checking_and_removing(self, tab, monkeypatch):
        _builds_on_disk(monkeypatch, {_CPU_BUILD})
        tab._load_transcription_values()
        buttons = tab._transcription_action_buttons
        assert not buttons[management.ACTION_INSTALL_WHISPER_CPP].IsEnabled()
        assert buttons[management.ACTION_VERIFY_WHISPER_CPP].IsEnabled()
        assert buttons[management.ACTION_REMOVE_WHISPER_CPP].IsEnabled()

    def test_choosing_a_build_is_not_an_edit(self, tab):
        tab._load_transcription_values()
        event = _Skippable()
        tab._on_whisper_cpp_build_change(event)
        assert not event.skipped
        assert tab.dirtied == 0

    def test_removing_the_processor_build_says_the_other_goes_too(self, tab, monkeypatch):
        asked = []
        monkeypatch.setattr(transcription_whisper_cpp.wx, "MessageBox",
                            lambda *args, **kwargs: asked.append(args) or wx.NO)
        tab._load_transcription_values()
        assert tab._ask_whisper_cpp_removal(_CPU_BUILD.id) is False
        assert asked[0][0] == tab.main_window.i18n.t(
            "transcription_confirm_remove_whisper_cpp_cpu")

    def test_the_action_reaches_the_job_with_its_build_and_the_card(self, tab):
        ran = []
        tab._run_transcription_job = lambda action, **kwargs: ran.append(
            (action, kwargs)) or (None, None, None)
        tab._announce_transcription_result = lambda job, result, error: None
        tab._load_transcription_values()
        tab._run_whisper_cpp_job(management.ACTION_VERIFY_WHISPER_CPP, _CPU_BUILD.id,
                                 _with_card((8, 6)))
        assert ran == [(management.ACTION_VERIFY_WHISPER_CPP,
                        {"build_id": _CPU_BUILD.id, "compute_capability": (8, 6)})]

    def test_installing_the_graphics_build_says_the_processor_one_comes_first(self):
        i18n = _I18n()
        text = transcription_tab._transcription_download_confirmation(i18n, _summary(
            subject=management.SUBJECT_WHISPER_CPP, model_id=None,
            build_id=_CUDA_BUILD.id, includes_cpu_build=True, resumable=False,
        ))
        assert i18n.t("transcription_confirm_whisper_cpp_with_cpu") in text
        assert transcription_tab._whisper_cpp_build_name(i18n, _CUDA_BUILD.id) in text

    def test_the_labels_follow_a_language_change(self, tab):
        tab._load_transcription_values()
        tab.main_window.i18n = _I18n("pl")
        tab._refresh_transcription_labels()
        pl = tab.main_window.i18n
        assert tab._whisper_cpp_label.GetLabel() == pl.t("transcription_whisper_cpp_label")
        assert tab._transcription_action_buttons[
            management.ACTION_INSTALL_WHISPER_CPP].GetLabel() == pl.t(
            "transcription_whisper_cpp_install_btn")
        assert tab._whisper_cpp_combo.GetString(0) == (
            transcription_whisper_cpp.whisper_cpp_build_label(
                pl, _CPU_BUILD, whisper_cpp_runtime.STATE_ABSENT))
