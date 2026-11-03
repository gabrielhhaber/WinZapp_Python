"""The Transcription tab of Settings, split out of settings_dialog.py.

Everything the tab owns: the pure helpers that decide what it says and which
buttons are enabled, and `TranscriptionTabMixin`, the methods that build the
page, load and apply its values and run its model/CUDA jobs. The mixin is the
tab of `SettingsDialog` — it reads the dialog's own `self` (notebook, dirty
flag, main_window) — so it lives beside it rather than as a page of its own,
the way AISettingsPage can: that one owns its state, this one shares the
dialog's Apply/OK/language-change life cycle.

The user-facing name is "Transcrição local" (Whisper, on this computer). The
online AI tab, "IA online", is `ai_settings_page.AISettingsPage`.
"""

import logging
import os

import wx

from core.combo_search import bind_incremental_search
# Aliased: the settings dialog this tab belongs to talks about audio devices
# everywhere, so a bare `device` would read as one of those rather than as "GPU
# or processor".
from core.transcription import (
    backend as transcription_backend,
    cuda_runtime,
    device as transcription_device,
    errors as transcription_errors,
    external_models,
    external_view,
    management as transcription_management,
    management_text,
    management_whisper_cpp,
    model_catalog,
    model_names,
    model_store,
    preferences as transcription_preferences,
    reused_runtimes,
)
from ui.dialogs.transcription_result_field import VERIFY_ACTIONS
from ui.dialogs.transcription_progress import (
    TranscriptionProgressDialog,
    progress_status_text,
)


# The order the transcription device options are offered in, and the map from
# a radio index back to the value that reaches settings.json. A tuple rather
# than the dict's own iteration order because the index the user picked is
# meaningless unless the order is fixed here, in one place, for both
# directions.
_TRANSCRIPTION_DEVICE_PREFERENCES = (
    transcription_device.PREFERENCE_AUTO,
    transcription_device.PREFERENCE_CUDA,
    transcription_device.PREFERENCE_CPU,
)


# A fourth state, for the action buttons only. `installation_state()` and
# `cuda_runtime.installation_state()` measure names and sizes, so the one
# thing neither can see is a file that is exactly the right size and the wrong
# bytes — which is precisely what verify_model()/verify_installation() answer
# MODEL_CORRUPTED and CUDA_RUNTIME_CORRUPTED for. Without a state of its own
# the check that found the damage would leave the Reparar button disabled, one
# keypress after telling the user to press it.
_TRANSCRIPTION_STATE_CORRUPTED = "corrupted"

# The two rows of action buttons, each entry (action, label key, state key) in
# the order they are offered — which is also their order in the tab. Declared
# as data because three separate places walk them: building the row, enabling
# them against what is on disk, and retranslating the labels.
_TRANSCRIPTION_MODEL_ACTION_BUTTONS = (
    (transcription_management.ACTION_DOWNLOAD_MODEL,
     "transcription_model_download_btn", "download"),
    (transcription_management.ACTION_VERIFY_MODEL,
     "transcription_model_verify_btn", "verify"),
    (transcription_management.ACTION_REPAIR_MODEL,
     "transcription_model_repair_btn", "repair"),
    (transcription_management.ACTION_REMOVE_MODEL,
     "transcription_model_remove_btn", "remove"),
)
_TRANSCRIPTION_CUDA_ACTION_BUTTONS = (
    (transcription_management.ACTION_INSTALL_CUDA_RUNTIME,
     "transcription_cuda_install_btn", "download"),
    (transcription_management.ACTION_REPAIR_CUDA_RUNTIME,
     "transcription_cuda_repair_btn", "repair"),
    (transcription_management.ACTION_VERIFY_CUDA_RUNTIME,
     "transcription_cuda_verify_btn", "verify"),
    (transcription_management.ACTION_REMOVE_CUDA_RUNTIME,
     "transcription_cuda_remove_btn", "remove"),
)

#: What the tab says about a single-language model (the `.en`, distilled and
#: third-party ones): its language replaces the one chosen below, or the
#: detection. No language named — the model's is in its picker line, just
#: above — so the notice stays a key like the others in its field.
_TRANSCRIPTION_LANGUAGE_FORCED = "transcription_notice_language_forced"
_TRANSCRIPTION_LANGUAGE_OVERRIDDEN = "transcription_notice_language_overridden"

#: The name of each group box. Deliberately without a mnemonic: a static box
#: is not a tab stop, and the two letters it would cost are letters the
#: buttons inside it need more.
_TRANSCRIPTION_MODEL_ACTIONS_GROUP = "transcription_model_actions_group"
_TRANSCRIPTION_CUDA_ACTIONS_GROUP = "transcription_cuda_actions_group"

# The four actions that spend the user's bandwidth, and therefore the four
# that have to be agreed to first.
_TRANSCRIPTION_CONFIRMED_ACTIONS = (
    transcription_management.ACTION_DOWNLOAD_MODEL,
    transcription_management.ACTION_REPAIR_MODEL,
    transcription_management.ACTION_INSTALL_CUDA_RUNTIME,
    transcription_management.ACTION_REPAIR_CUDA_RUNTIME,
)

_TRANSCRIPTION_REPAIR_ACTIONS = (
    transcription_management.ACTION_REPAIR_MODEL,
    transcription_management.ACTION_REPAIR_CUDA_RUNTIME,
)

#: The two that delete. They are asked about for the same reason the downloads
#: are: gigabytes either way, and Remover sits on the same row as Verificar,
#: one arrow key apart under a screen reader.
_TRANSCRIPTION_REMOVE_ACTIONS = (
    transcription_management.ACTION_REMOVE_MODEL,
    transcription_management.ACTION_REMOVE_CUDA_RUNTIME,
)

#: The CUDA half of ACTIONS, which is deliberately not "everything that is not
#: a model action": ACTION_MOVE_MODELS is neither, and says nothing at all
#: about the libraries.
_TRANSCRIPTION_CUDA_ACTIONS = tuple(
    action for action, _label_key, _state_key in _TRANSCRIPTION_CUDA_ACTION_BUTTONS
)


def _transcription_action_states(state, job_running) -> dict:
    """Which of the four buttons may be pressed for something in `state`.

    One table for the models and for the CUDA libraries, because it is the
    same question about the same states: model_store and cuda_runtime spell
    ABSENT/INCOMPLETE/INSTALLED with the same three literals and say so in
    each other's terms. Two copies of this could disagree about whether
    Remover exists for an interrupted install — and it must, because a removal
    that could not delete everything leaves exactly that state, and the
    sentence telling the user to remove them again would then point at a
    button that is not there.

    `state` is None when there is nothing selected to act on ("Automático" in
    the model picker names no model), and then every button is off — as they
    all are while a job runs: two jobs in this process do not fail, they
    serialize on the models lock, but the second one waits *silently* for up
    to twelve hours behind a bar that never moves.
    """
    if job_running or state is None:
        return {"download": False, "verify": False, "repair": False, "remove": False}
    return {
        # An install already complete has nothing left to fetch, and a
        # corrupted one is Reparar's: every file is there at its right size,
        # so a download would keep every wrong byte.
        "download": state in (model_store.STATE_ABSENT, model_store.STATE_INCOMPLETE),
        # Both checks read every byte and compare it against a digest, which
        # can only be done once every byte is there.
        "verify": state in (model_store.STATE_INSTALLED,
                            _TRANSCRIPTION_STATE_CORRUPTED),
        "repair": state in (model_store.STATE_INCOMPLETE,
                            _TRANSCRIPTION_STATE_CORRUPTED),
        # Anything at all on disk can be removed, leftovers of an interrupted
        # transfer included — that is the only route the user has to them.
        "remove": state != model_store.STATE_ABSENT,
    }


def _transcription_download_confirmation(i18n, summary, repair=False) -> str:
    """Everything the user is agreeing to, before the first byte moves.

    The five things the issue asks for — what it is, how big the download is,
    how much disk it needs, where it goes, and whether it will end up running
    on the processor or on the card — plus the two that only sometimes apply:
    a transfer that cannot be resumed, and a model that will not fit the
    memory of the device it would run on. `summary` is a
    management.DownloadSummary, so the figures here are the same ones the
    free-space gate will use rather than a second calculation of them.
    """
    if summary.subject == transcription_management.SUBJECT_MODEL:
        head = i18n.t(
            "transcription_confirm_model_repair" if repair
            else "transcription_confirm_model"
        ).format(model=model_names.display_name(i18n, summary.model_id) or "")
    elif summary.subject == transcription_management.SUBJECT_WHISPER_CPP:
        head = i18n.t(
            "transcription_confirm_whisper_cpp_repair" if repair
            else "transcription_confirm_whisper_cpp"
        ).format(build=_whisper_cpp_build_name(i18n, summary.build_id))
        if summary.includes_cpu_build:
            # Two downloads under one Yes: the user is told the second exists.
            head = " ".join((head, i18n.t("transcription_confirm_whisper_cpp_with_cpu")))
    else:
        head = i18n.t(
            "transcription_confirm_cuda_repair" if repair
            else "transcription_confirm_cuda"
        )

    lines = [
        head,
        i18n.t("transcription_confirm_download_size").format(
            size=_format_transcription_size(i18n, summary.download_bytes)
        ),
        i18n.t("transcription_confirm_required_space").format(
            size=_format_transcription_size(i18n, summary.required_free_bytes)
        ),
    ]
    if summary.enough_space is None:
        # "Unknown", never "enough": ensure_free_space() lets an unmeasurable
        # volume through, and claiming it fits would be inventing an answer.
        lines.append(i18n.t("transcription_confirm_space_unknown"))
    elif summary.enough_space:
        lines.append(i18n.t("transcription_confirm_free_space").format(
            size=_format_transcription_size(i18n, summary.free_bytes)
        ))
    else:
        lines.append(i18n.t("transcription_confirm_not_enough_space").format(
            required=_format_transcription_size(i18n, summary.required_free_bytes),
            free=_format_transcription_size(i18n, summary.free_bytes),
        ))
    lines.append(i18n.t("transcription_confirm_destination").format(
        folder=summary.destination
    ))
    lines.append(i18n.t(
        "transcription_confirm_device_cuda"
        if summary.device == transcription_device.DEVICE_CUDA
        else "transcription_confirm_device_cpu"
    ))
    if not summary.resumable:
        # The CUDA libraries: a cancelled or dropped transfer starts from zero
        # next time, which is 553 MB the user decides about *now*.
        lines.append(i18n.t("transcription_confirm_no_resume").format(
            size=_format_transcription_size(i18n, summary.download_bytes)
        ))
    if summary.fits_memory is False:
        lines.append(i18n.t("transcription_confirm_does_not_fit"))
    return "\n".join(lines)


def _transcription_unknown_dirs_notice(i18n, names) -> str:
    """What to say about folders in the models folder nothing here can remove.

    A model a later version retires stops being listed anywhere, and
    remove_model() refuses to delete a name the catalogue cannot look up — so
    up to 3 GB would sit there, invisible and undeletable from inside the app,
    with nothing ever mentioning it.
    """
    if not names:
        return ""
    return i18n.t("transcription_unknown_dirs").format(folders=", ".join(names))


# Where the wording of an action's outcome lives, shared with the main window
# (a download that finished in the background is said from there).
_whisper_cpp_build_name = management_text.build_name
_transcription_announcement_text = management_text.announcement_text


def _format_transcription_size(i18n, size_bytes) -> str:
    """A model or download size as a short, speakable figure ("1,5 GB").

    Binary units and the locale's own decimal separator, like every other size
    WinZapp shows (see ConversationsPanel._format_filesize). Coarser than that
    one on purpose: everything measured here is between 70 MB and 3 GB, and a
    second decimal buys the user nothing while making the combobox item longer
    to listen to.
    """
    try:
        size = int(size_bytes or 0)
    except (TypeError, ValueError):
        return ""
    sep = i18n.t("decimal_separator")
    if size >= 1024 ** 3:
        return f"{size / 1024 ** 3:.1f}".replace(".", sep) + " GB"
    return f"{size / 1024 ** 2:.0f} MB"


def _transcription_model_choice_label(i18n, model, state, external=False) -> str:
    """One line of the model picker, written as a sentence.

    A combobox item is a single accessibility object: the screen reader reads
    the whole string and nothing else, so everything the user needs to choose
    between two models has to be inside it — which model, how good it is, what
    it costs, and whether it is already here. Three sentences rather than one
    with a swappable tail, because the size means different things in each:
    disk already spent, a download still to pay for, or a download to finish.

    `state` is a model_store.InstallState; an unknown state reads as "not
    installed", which is the honest answer for a folder we could not measure.
    `external` is whether WinZapp's own folder lacks it and a verified copy in
    a folder of the user's stands in (external_view.choice_is_external()): the
    size is then what is on disk there, and the line says where.
    """
    size_class = i18n.t(
        model_catalog.size_class_i18n_key(model.size_class) or model.size_class
    )
    if external:
        key, size = external_view.CHOICE_EXTERNAL_I18N_KEY, model.disk_bytes
    elif state is not None and state.state == model_store.STATE_INSTALLED:
        key, size = "transcription_model_choice_installed", model.disk_bytes
    elif state is not None and state.state == model_store.STATE_INCOMPLETE:
        key, size = "transcription_model_choice_incomplete", model.download_bytes
    else:
        key, size = "transcription_model_choice_available", model.download_bytes
    return i18n.t(key).format(
        # "small, 5 bits" / "small.en, English only": the quantization and the
        # language are what tell two lines of the same model apart.
        name=model_names.display_name(i18n, model.id) or model.id,
        size_class=size_class,
        size=_format_transcription_size(i18n, size),
    )


def _transcription_cuda_status_text(i18n, state) -> str:
    """The one line saying where the CUDA libraries stand.

    Four situations, not three: a complete install of an earlier pin and an
    install interrupted half way are both INCOMPLETE, and cuda_runtime tells
    them apart through `installed_version` precisely so this line can say
    "update them" rather than "finish the download" (see RuntimeState). Its own
    rule is honoured here too — `missing` wins, so the version only speaks when
    nothing is missing.
    """
    if state is None:
        return ""
    if state.state == cuda_runtime.STATE_INSTALLED:
        return i18n.t("transcription_cuda_runtime_installed")
    if state.state == cuda_runtime.STATE_INCOMPLETE:
        if not state.missing and state.installed_version:
            return i18n.t(cuda_runtime.OUTDATED_I18N_KEY)
        return i18n.t("transcription_cuda_runtime_incomplete")
    return i18n.t("transcription_cuda_runtime_absent").format(
        size=_format_transcription_size(i18n, cuda_runtime.WHEEL_BYTES)
    )


class TranscriptionTabMixin:
    """The Local Transcription tab's methods for `SettingsDialog`."""

    def _build_transcription_page(self, parent):
        """The Transcrição tab, built inside `parent` and returned.

        A method rather than one more block inside _build_ui(), and the reason
        is the test: SettingsDialog is a wx.Dialog, which the suite may not
        construct at all (tests/test_no_desktop_visible_windows.py — a dialog
        owns its own construction, takes the desktop focus and has crashed a
        developer's screen reader), so everything this tab offers would
        otherwise only be checkable by reading the source. Taking the parent as
        an argument is what lets the method be bound onto a stub and built
        against conftest's off-screen frame instead.

        Each thing the user can *do* sits on its own row directly under the
        control it acts on — the four model buttons after the model picker, the
        four CUDA ones after the CUDA status line. A button that acts on a
        choice belongs next to that choice in the tab order, not collected at
        the bottom of the page past the language and the folder.
        """
        i18n = self.main_window.i18n
        page = wx.Panel(parent)
        sizer = wx.BoxSizer(wx.VERTICAL)

        #: The models folder as it is stored install-wide: "" for the default
        #: folder, an absolute path otherwise. Read once, before anything below
        #: is drawn, because the model list is drawn *against* it — and kept in
        #: this attribute rather than read back out of the field, which shows
        #: the resolved path and must never be written back (see
        #: preferences.resolve_models_dir()).
        self._transcription_models_dir = self._stored_transcription_models_dir()

        #: What is actually on disk in that folder, re-read whenever the folder
        #: changes. Empty until the tab is first put on screen — listing it is
        #: disk I/O, and _enter_transcription_page() is where this tab's I/O
        #: lives (see _load_transcription_values() for why none of it is on the
        #: path of simply opening the dialog).
        self._transcription_installed_ids = ()
        #: device.probe_hardware()'s answer, or None while nobody has measured.
        #: Taken once, on the first visit to this tab, and never on open.
        self._transcription_probe = None
        #: Whether this tab has actually been on screen. Everything that
        #: *consumes* a substitution warning is gated on it — see
        #: _enter_transcription_page() and
        #: _transcription_setting_may_be_written().
        self._transcription_page_seen = False
        #: Which settings resolve() had to replace, as setting names.
        self._transcription_substituted_settings = set()
        #: Every action button, by its management action. Filled in by
        #: _build_transcription_action_row() as each row is laid out, which is
        #: why everything that walks it tolerates a button that is not there
        #: yet: the populate helpers below run while the page is half built.
        self._transcription_action_buttons = {}
        #: (static box, title key) per group, for the retranslation.
        self._transcription_action_groups = []
        #: Whether an action is under way — the measurement before a download
        #: included, since that is a thread too and a second press during it
        #: would start a second job.
        self._transcription_job_running = False
        #: The models the user pointed WinZapp at in folders of their own, as
        #: stored (no disk access to read them), and what each one's folder was
        #: last measured as — on a worker thread, see ExternalModelsMixin.
        #: Empty until measured, which every reader treats as "not known".
        #: `_known` is False while app.json could not be read: the list is then
        #: not "none", and nothing may be rewritten for a reference's absence.
        (self._transcription_external_references,
         self._transcription_external_known) = external_models.read_references(
            self._install_wide_settings()
        )
        self._transcription_external_states = {}
        #: Which measurement is the latest, so a slow answer that was
        #: overtaken is dropped instead of drawn over the newer one.
        self._transcription_external_generation = 0
        #: What the model list last measured, by model id. Read by the buttons
        #: instead of measuring again on every arrow key through the picker.
        self._transcription_model_states = {}
        #: cuda_runtime.installation_state()'s own state, kept by
        #: _show_transcription_cuda_status() for the same reason.
        self._transcription_cuda_state = None
        #: What only a hash could have found: see
        #: _TRANSCRIPTION_STATE_CORRUPTED and _note_transcription_outcome().
        self._transcription_corrupted_models = set()
        self._transcription_cuda_corrupted = False
        #: Folders under the models root no catalogue entry claims.
        self._transcription_unknown_dirs = ()
        #: The last action's Announcement, so the sentence the user heard is
        #: also on screen. Key and values rather than the rendered sentence, so
        #: a language change re-says it instead of leaving the old one there.
        self._transcription_last_outcome = None
        self._transcription_last_outcome_extra = ()

        #: Three lines of the font actually in use, for the two read-only
        #: fields below. A pixel count (52 px was two lines at 100% scaling)
        #: crops the text at the display scaling and font size a low-vision
        #: user runs — the one reader these two fields exist for.
        text_block_height = page.GetTextExtent("Xg").GetHeight() * 3
        # Groups of options in windows of their own (transcription_windows.py):
        # the tab had more controls than there are letters for Alt keys.
        self._build_transcription_windows(page)
        folders = self._transcription_folders_panel
        folders_sizer = self._transcription_folders_sizer
        cuda = self._transcription_cuda_panel
        cuda_sizer = self._transcription_cuda_sizer
        whisper = self._transcription_whisper_panel
        whisper_sizer = self._transcription_whisper_sizer

        # What resolve() had to replace, when it had to replace anything.
        # First on the page and in the tab order because it is about the
        # controls below it — and a read-only wx.TextCtrl rather than a
        # wx.StaticText because static text is not focusable: a screen-reader
        # user tabbing through the tab would never reach it.
        self._transcription_substituted_label = wx.StaticText(
            page, label=i18n.t("transcription_substituted_label")
        )
        sizer.Add(self._transcription_substituted_label, 0, wx.LEFT | wx.TOP | wx.RIGHT, 8)
        self._transcription_substituted_field = wx.TextCtrl(
            page, style=wx.TE_MULTILINE | wx.TE_BESTWRAP, size=(-1, text_block_height)
        )
        # SetEditable(False), never Disable(): a disabled control drops out of
        # the tab order entirely, which is the same as not showing the warning
        # at all to the user it is written for (see ConversationsPanel's
        # read-only composer for the same call and the same reasoning).
        self._transcription_substituted_field.SetEditable(False)
        sizer.Add(self._transcription_substituted_field, 0, wx.EXPAND | wx.ALL, 8)
        self._transcription_substituted_label.Hide()
        self._transcription_substituted_field.Hide()
        # Stops a download that runs in the background (and what is queued
        # behind it). Shown only while one does: see
        # TranscriptionBackgroundTabMixin._show_transcription_cancel().
        self._transcription_cancel_btn = wx.Button(
            page, label=i18n.t("transcription_background_cancel_btn")
        )
        self._transcription_cancel_btn.Bind(
            wx.EVT_BUTTON, self._on_transcription_cancel_background
        )
        sizer.Add(self._transcription_cancel_btn, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self._transcription_cancel_btn.Hide()
        self._transcription_substitution_keys = []
        #: The same field also carries what the *machine* has to say about the
        #: current choices — kept apart from the substitutions because the two
        #: are recomputed at different moments (see
        #: _show_transcription_hardware_notices()).
        self._transcription_hardware_keys = []

        # The backend picker exists only where there is something to pick.
        # Keyed on BACKEND_IDS — what WinZapp knows — and not on
        # available_backend_ids(), which is what runs on this machine today:
        # measuring that means importing the optional backend just to draw a
        # tab, and a user must be able to choose the component they are about
        # to install. With one id there is nothing to choose, and a combobox
        # with a single entry is a stop in the tab order that answers nothing.
        # Above the model picker, because it decides what that lists: each
        # backend loads its own files (see WhisperCppMixin).
        self._transcription_backend_label = None
        self._transcription_backend_combo = None
        self._transcription_backend_ids = []
        if len(transcription_backend.BACKEND_IDS) > 1:
            self._transcription_backend_label = wx.StaticText(
                page, label=i18n.t("transcription_backend_label")
            )
            sizer.Add(
                self._transcription_backend_label, 0, wx.LEFT | wx.TOP | wx.RIGHT, 8
            )
            self._transcription_backend_combo = wx.ComboBox(page, style=wx.CB_READONLY)
            bind_incremental_search(self._transcription_backend_combo)
            sizer.Add(self._transcription_backend_combo, 0, wx.EXPAND | wx.ALL, 8)
            self._populate_transcription_backend_choices()
            self._transcription_backend_combo.Bind(
                wx.EVT_COMBOBOX, self._on_transcription_backend_change
            )

        self._transcription_model_label = wx.StaticText(
            page, label=i18n.t("transcription_model_label")
        )
        sizer.Add(self._transcription_model_label, 0, wx.LEFT | wx.TOP | wx.RIGHT, 8)
        # A list rather than a drop-down: the models can be marked for download
        # (Ctrl+Space, as in the lists of chats and messages — see
        # TranscriptionModelMarksMixin), and a closed combo box has no place
        # to read "selected" from and no Space that is not its own. The row
        # the list is on is still the chosen model; the marks are the queue.
        self._transcription_model_list = wx.ListBox(
            page, style=wx.LB_SINGLE,
            size=(-1, page.GetTextExtent("Xg").GetHeight() * 7),
        )
        sizer.Add(self._transcription_model_list, 0, wx.EXPAND | wx.ALL, 8)
        # Before the first populate: the rows are drawn with their marks.
        self._init_transcription_model_marks()
        #: Parallel to the list items: index -> the value stored in
        #: settings.json. The item text is a whole sentence, so it cannot be
        #: mapped back to an id by reading it.
        self._transcription_model_ids = []
        #: The item texts as last set, so a redraw that would say the same
        #: thing leaves the list alone (a screen reader re-reads a list that
        #: was rewritten, and a measurement finishing is not news).
        self._transcription_model_labels = []
        self._populate_transcription_model_choices()
        # Which buttons below make sense depends on the model selected, and
        # SetSelection() fires nothing — only the user's own choice does.
        self._transcription_model_list.Bind(
            wx.EVT_LISTBOX, self._on_transcription_model_change
        )
        self._build_transcription_action_row(
            page, sizer, _TRANSCRIPTION_MODEL_ACTION_BUTTONS,
            _TRANSCRIPTION_MODEL_ACTIONS_GROUP,
        )
        self._build_transcription_cache(self._transcription_saved_panel,
                                        self._transcription_saved_sizer)
        self._add_transcription_saved_button(page, sizer)

        self._transcription_device_radio = wx.RadioBox(
            page,
            label=i18n.t("transcription_device_label"),
            choices=[
                i18n.t(transcription_preferences.DEVICE_PREFERENCE_I18N_KEYS[pref])
                for pref in _TRANSCRIPTION_DEVICE_PREFERENCES
            ],
            majorDimension=1,
            style=wx.RA_SPECIFY_COLS,
        )
        sizer.Add(self._transcription_device_radio, 0, wx.EXPAND | wx.ALL, 8)
        # Asking for the graphics card on a machine that has none is worth
        # saying so the moment it is asked, not at the end of the first
        # transcription — and the CUDA line further down otherwise invites a
        # half-gigabyte download that would not help.
        self._transcription_device_radio.Bind(
            wx.EVT_RADIOBOX, self._on_transcription_device_change
        )
        # faster-whisper's precision (TranscriptionPrecisionMixin), with the
        # other advanced options in their own window.
        self._build_transcription_precision(self._transcription_advanced_panel,
                                            self._transcription_advanced_sizer)

        # A checkbox that enables the list, rather than a "detect
        # automatically" entry at the top of the list itself: they are two
        # different questions ("per message?" and "which language, when not?"),
        # and this is the pair a screen reader reads best. See
        # preferences._resolve_language() for the whole argument.
        self._transcription_detect_language_check = wx.CheckBox(
            page, label=i18n.t(transcription_preferences.LANGUAGE_DETECT_I18N_KEY)
        )
        sizer.Add(self._transcription_detect_language_check, 0, wx.ALL, 8)

        self._transcription_language_label = wx.StaticText(
            page, label=i18n.t("transcription_language_label")
        )
        sizer.Add(self._transcription_language_label, 0, wx.LEFT | wx.TOP | wx.RIGHT, 8)
        self._transcription_language_combo = wx.ComboBox(page, style=wx.CB_READONLY)
        bind_incremental_search(self._transcription_language_combo)
        sizer.Add(self._transcription_language_combo, 0, wx.EXPAND | wx.ALL, 8)
        self._transcription_language_codes = []
        self._populate_transcription_language_choices()
        self._transcription_detect_language_check.Bind(
            wx.EVT_CHECKBOX, self._on_transcription_detect_language_toggle
        )
        # An English-only model has something to say about the language.
        self._transcription_language_combo.Bind(
            wx.EVT_COMBOBOX, self._on_transcription_language_change
        )
        # The on/off options (TranscriptionOptionsMixin); those for statuses
        # in their own window.
        self._build_transcription_options(page, sizer)
        self._build_transcription_advanced_options(self._transcription_advanced_panel,
                                                   self._transcription_advanced_sizer)
        self._add_transcription_advanced_button(page, sizer)
        self._build_transcription_status_options(self._transcription_statuses_panel,
                                                 self._transcription_statuses_sizer)
        self._add_transcription_statuses_button(page, sizer)


        self._transcription_models_dir_label = wx.StaticText(
            folders, label=i18n.t("transcription_models_dir_label")
        )
        folders_sizer.Add(self._transcription_models_dir_label, 0, wx.LEFT | wx.TOP | wx.RIGHT, 8)
        # Field and button on one row, as on the Files and saving tab: the path
        # is the long part and the button is a fixed word.
        models_dir_row = wx.BoxSizer(wx.HORIZONTAL)
        self._transcription_models_dir_field = wx.TextCtrl(folders, style=wx.TE_DONTWRAP)
        # Shows the *resolved* folder, and is not typed into: an empty stored
        # value means "the default folder", and typing the resolved path back
        # into it would freeze a data directory that legitimately moves (see
        # preferences.resolve_models_dir()).
        self._transcription_models_dir_field.SetEditable(False)
        models_dir_row.Add(self._transcription_models_dir_field, 1, wx.EXPAND | wx.RIGHT, 8)
        self._transcription_models_dir_browse_btn = wx.Button(
            folders, label=i18n.t("transcription_models_dir_browse_btn")
        )
        models_dir_row.Add(self._transcription_models_dir_browse_btn, 0)
        folders_sizer.Add(models_dir_row, 0, wx.EXPAND | wx.ALL, 8)
        self._transcription_models_dir_browse_btn.Bind(
            wx.EVT_BUTTON, self._on_browse_transcription_models_dir
        )
        self._show_transcription_models_dir()
        # Models the user already has in other folders, after the folder of
        # the ones WinZapp keeps.
        self._build_external_models_group(folders, folders_sizer)

        self._transcription_cuda_label = wx.StaticText(
            cuda, label=i18n.t("transcription_cuda_runtime_label")
        )
        cuda_sizer.Add(self._transcription_cuda_label, 0, wx.LEFT | wx.TOP | wx.RIGHT, 8)
        self._transcription_cuda_field = wx.TextCtrl(
            cuda, style=wx.TE_MULTILINE | wx.TE_BESTWRAP, size=(-1, text_block_height)
        )
        self._transcription_cuda_field.SetEditable(False)
        cuda_sizer.Add(self._transcription_cuda_field, 0, wx.EXPAND | wx.ALL, 8)
        self._build_transcription_action_row(
            cuda, cuda_sizer, _TRANSCRIPTION_CUDA_ACTION_BUTTONS,
            _TRANSCRIPTION_CUDA_ACTIONS_GROUP,
        )
        # The folder to reuse CUDA libraries from (TranscriptionReuseMixin).
        self._build_transcription_reuse_cuda(cuda, cuda_sizer)
        # The whisper.cpp program: its builds and their four buttons, as the
        # other download that is not a model, and the folder to reuse it from.
        self._build_whisper_cpp_section(whisper, whisper_sizer)
        self._build_transcription_reuse_whisper_cpp(whisper, whisper_sizer)
        self._add_transcription_folders_button(page, sizer)
        self._add_transcription_cuda_button(page, sizer)
        self._add_transcription_whisper_cpp_button(page, sizer)
        self._finish_transcription_windows()
        self._sync_transcription_action_buttons()
        # A download the window is running in the background is this tab's too.
        self._watch_transcription_background()

        page.SetSizer(sizer)
        return page

    def _build_transcription_action_row(self, page, sizer, buttons, title_key):
        """One named group of action buttons, wired and remembered by action.

        **A wx.StaticBox, and that is what lets each button be one word.** The
        group is a native control: MSAA exposes the grouping and a screen
        reader announces its name when focus enters it, so "Instalar" inside
        "Bibliotecas CUDA" carries everything "Instalar bibliotecas CUDA"
        carried — with the object read once on entry instead of again on every
        one of the four. A button's label is its whole identity and is read in
        full every single time focus lands on it, several times a session,
        which is a far larger cost than the Alt key it buys.

        Children are parented to the box rather than to the page, which is
        what wx wants for a static box and what keeps them inside the group
        for the accessibility layer as well as for the layout. Tab order is
        creation order, so it still walks them left to right.

        All eight share `_on_transcription_action`: the button *is* the
        action, and eight near-identical handlers would be eight places for
        the next action to be wired into the wrong one.
        """
        i18n = self.main_window.i18n
        box = wx.StaticBox(page, label=i18n.t(title_key))
        self._transcription_action_groups.append((box, title_key))
        group = wx.StaticBoxSizer(box, wx.VERTICAL)
        # A wrap sizer inside it so a long translation folds onto a second
        # line instead of setting the width of every other tab in the dialog.
        row = wx.WrapSizer(wx.HORIZONTAL)
        for action, label_key, _state_key in buttons:
            button = wx.Button(box, label=i18n.t(label_key))
            button.Bind(wx.EVT_BUTTON, self._on_transcription_action)
            self._transcription_action_buttons[action] = button
            row.Add(button, 0, wx.RIGHT, 8)
        group.Add(row, 1, wx.EXPAND | wx.ALL, 4)
        sizer.Add(group, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

    # ── Transcription tab ────────────────────────────────────────────────────
    # Kept together rather than spread across this file's Helpers/Event
    # handlers sections: the tab is one subject, and the populate/load/apply
    # halves only make sense read against each other.

    def _stored_transcription_models_dir(self) -> str:
        """The install-wide models folder as stored: "" means the default.

        Install-wide, so it comes from app_settings and not from this account's
        settings.json — the model files are shared by every account (see
        app_settings' own comment on the key).
        """
        return transcription_preferences.stored_models_dir(
            self._install_wide_settings()
        )

    def _refresh_transcription_models(self):
        """Redraw everything that depends on what the models folder holds.

        The folder, the model list and the hardware notices, in that order —
        "nothing fits" can stop or start being true with the models on disk.
        One place for it because more than one event changes that: choosing
        another folder, and downloading, repairing, removing or moving a
        model. _enter_transcription_page() runs once per opening and cannot
        serve as the refresh after an action.
        """
        self._show_transcription_models_dir()
        self._populate_transcription_model_choices()
        models_dir, self._transcription_installed_ids = (
            transcription_preferences.models_folder(self._transcription_models_dir)
        )
        self._transcription_unknown_dirs = model_store.list_unknown_dirs(models_dir)
        self._show_transcription_hardware_notices()

    def _show_transcription_models_dir(self):
        """Put the *resolved* folder in the field, whatever is stored."""
        # ChangeValue, never SetValue: wx fires EVT_TEXT for SetValue even
        # with identical text, and the dialog routes EVT_TEXT to _mark_dirty.
        # This field only *shows* a folder — the Browse button is what
        # changes it, and it marks the dialog dirty itself.
        self._transcription_models_dir_field.ChangeValue(
            transcription_preferences.resolve_models_dir(self._transcription_models_dir)
        )

    def _populate_transcription_model_choices(self):
        """Rebuild the model list, keeping whatever was selected selected.

        Every entry is re-measured against the folder in force *now*, which is
        also why this is called again after the folder changes: a list drawn
        from the old folder would tell the user a model is installed when the
        run would not find it.
        """
        i18n = self.main_window.i18n
        models_dir = transcription_preferences.resolve_models_dir(
            self._transcription_models_dir
        )
        labels = [i18n.t(transcription_preferences.OPTION_AUTO_I18N_KEY)]
        model_ids = [transcription_preferences.AUTO]
        # The same measurement the label is written from is what the action
        # buttons read, rather than a second one of their own per keystroke.
        self._transcription_model_states = {}
        external_ready = external_view.ready_model_ids(
            self._transcription_external_references,
            self._transcription_external_states,
        )
        backend_id = self._transcription_picker_backend()
        for model in transcription_preferences.catalogue_models(backend_id):
            state = model_store.installation_state(models_dir, model)
            self._transcription_model_states[model.id] = state.state
            labels.append(_transcription_model_choice_label(
                i18n, model, state,
                external=external_view.choice_is_external(
                    model, state.state, external_ready
                ),
            ))
            model_ids.append(model.id)
        # A custom model is only ever there because the user chose it: after
        # the catalogue, never among what "automatic" picks from.
        for choice, label in external_view.custom_choices(
                i18n, self._transcription_external_references, backend_id):
            labels.append(label)
            model_ids.append(choice)

        # What the queue can still mean: marks of models this list no longer
        # offers (the other backend's) are forgotten, and the rest are drawn.
        self._transcription_model_base_labels = labels
        labels = self._marked_model_labels(labels, model_ids)

        if labels != self._transcription_model_labels or (
                model_ids != self._transcription_model_ids):
            selected = self._selected_transcription_model()
            # Set() replaces the whole list in one call, so there is nothing for
            # Freeze()/Thaw() to batch here — that pair is for the row-by-row
            # mutation of a list control, where it saves the screen reader a
            # flood of one event per row.
            self._transcription_model_list.Set(labels)
            self._transcription_model_ids = model_ids
            self._transcription_model_labels = labels
            self._select_transcription_model(selected)
        self._sync_transcription_action_buttons()

    def _populate_transcription_language_choices(self):
        """Rebuild the language list, keeping whatever was selected selected.

        Endonyms, with WinZapp's own language first — both decided by
        language_choices(). The "the language WinZapp is in" sentinel is an
        entry of its own at the very top, because it is the stored default and
        a value with no entry to select is a value the dialog cannot show.
        """
        i18n = self.main_window.i18n
        labels = [i18n.t(transcription_preferences.LANGUAGE_INTERFACE_I18N_KEY)]
        codes = [transcription_preferences.LANGUAGE_INTERFACE]
        for code, endonym in transcription_preferences.language_choices(i18n.language):
            labels.append(endonym)
            codes.append(code)

        selected = self._selected_transcription_language()
        self._transcription_language_combo.Set(labels)
        self._transcription_language_codes = codes
        self._select_transcription_language(selected)

    def _populate_transcription_backend_choices(self):
        """Rebuild the backend list. Only ever called where there is one."""
        i18n = self.main_window.i18n
        labels = [i18n.t(transcription_preferences.OPTION_AUTO_I18N_KEY)]
        backend_ids = [transcription_preferences.AUTO]
        for backend_id in transcription_backend.BACKEND_IDS:
            labels.append(i18n.t(
                transcription_preferences.BACKEND_I18N_KEYS.get(backend_id, backend_id)
            ))
            backend_ids.append(backend_id)

        selected = self._selected_transcription_backend()
        self._transcription_backend_combo.Set(labels)
        self._transcription_backend_ids = backend_ids
        self._select_transcription_backend(selected)

    def _selected_transcription_model(self):
        return self._selected_id(
            self._transcription_model_list, self._transcription_model_ids
        )

    def _selected_transcription_language(self):
        return self._selected_id(
            self._transcription_language_combo, self._transcription_language_codes
        )

    def _selected_transcription_backend(self):
        if self._transcription_backend_combo is None:
            return None
        return self._selected_id(
            self._transcription_backend_combo, self._transcription_backend_ids
        )

    @staticmethod
    def _selected_id(combo, values):
        """The stored value behind the selected item, or None.

        The item text is a whole sentence (see
        _transcription_model_choice_label()), so the id can only come from the
        parallel list — never from reading the label back.
        """
        index = combo.GetSelection()
        if index == wx.NOT_FOUND or not 0 <= index < len(values):
            return None
        return values[index]

    def _select_transcription_model(self, model_id):
        self._select_id(
            self._transcription_model_list, self._transcription_model_ids, model_id
        )

    def _select_transcription_language(self, code):
        self._select_id(
            self._transcription_language_combo, self._transcription_language_codes, code
        )

    def _select_transcription_backend(self, backend_id):
        if self._transcription_backend_combo is not None:
            self._select_id(
                self._transcription_backend_combo,
                self._transcription_backend_ids,
                backend_id,
            )

    @staticmethod
    def _select_id(combo, values, value):
        """Select the item standing for `value`, falling back to "automatic".

        Index 0 is the automatic entry in every one of these lists, and it is
        the right fallback for a value with no entry: sanitize_section() has
        already rewritten anything permanently meaningless, so what is left
        here is a value this build cannot show — and showing the automatic
        entry is what the run would do with it anyway.
        """
        if combo.GetCount() == 0:
            return
        combo.SetSelection(values.index(value) if value in values else 0)

    def _selected_transcription_device_preference(self):
        """The device preference the radio is on, never a negative index.

        `wx.NOT_FOUND` is -1, which indexes the preference tuple from the end
        and would answer "processor" for something the user never chose. A
        wx.RadioBox always has a selection so this cannot happen today; the
        guard is what keeps it from happening silently if the control is ever
        swapped for one that can be left unset.
        """
        index = self._transcription_device_radio.GetSelection()
        if not 0 <= index < len(_TRANSCRIPTION_DEVICE_PREFERENCES):
            return transcription_device.PREFERENCE_AUTO
        return _TRANSCRIPTION_DEVICE_PREFERENCES[index]

    def _sync_transcription_language_controls(self):
        """The language list is only for the user who turned detection off.

        Disabled rather than hidden, like the custom save folder: a control
        that appears and disappears is harder to follow under a screen reader
        than one that is consistently there and consistently unavailable.
        (Not a tab-order argument — a disabled control is skipped exactly as a
        hidden one is; consistency is the whole of the reason.)
        """
        detect = self._transcription_detect_language_check.GetValue()
        for control in (self._transcription_language_label,
                        self._transcription_language_combo):
            control.Enable(not detect)

    def _show_transcription_substitutions(self, resolution):
        """Say which stored choices could not be honoured, or show nothing.

        One sentence per replaced setting, never the stored value itself: an
        id read out to a blind user says nothing they can act on, which is why
        Substitution keeps the id for the log and the sentence for here.
        """
        #: The keys rather than the rendered sentences, so a language change
        #: can say the same thing again in the new language instead of leaving
        #: the old one on screen.
        self._transcription_substitution_keys = [
            substitution.i18n_key for substitution in resolution.substitutions
        ]
        #: The same substitutions as setting names, which is what decides
        #: whether OK may write that control back — see
        #: _transcription_setting_may_be_written().
        self._transcription_substituted_settings = {
            substitution.setting for substitution in resolution.substitutions
        }
        self._render_transcription_substitutions()

    def _show_transcription_hardware_notices(self):
        """Redraw the two sentences that only a measurement can produce.

        A no-op until the tab has been on screen once: with no probe there is
        nothing measured to report, and taking one here would put the cost
        back on the path this whole split exists to keep clear.
        """
        # The precision list follows the same device, probe and backend.
        self._sync_transcription_precision()
        self._transcription_hardware_keys = (
            [] if self._transcription_probe is None
            else self._transcription_hardware_notice_keys(self._transcription_probe)
        )
        self._render_transcription_substitutions()

    def _transcription_hardware_notice_keys(self, probe):
        """The i18n keys for what this machine says about the current choices.

        Resolved against the controls as they stand rather than against
        settings.json, because both notices answer a question the user is
        asking right now — "what happens if I pick the graphics card?" — and
        the answer has to follow the radio button, not what OK last wrote.

        `preferences.resolve()` rather than `device.auto_select_model()` plus
        an `available_memory_mb()` of our own: telling "nothing fits" apart
        from "nothing could be measured" is a decision preferences.py already
        makes against this same probe, and a second copy of it here is a copy
        that can disagree with the one the run will use.
        """
        preference = self._selected_transcription_device_preference()
        language = self._selected_transcription_language()
        live = {transcription_preferences.SECTION: {
            transcription_preferences.SETTING_MODEL:
                self._selected_transcription_model()
                or transcription_preferences.AUTO,
            transcription_preferences.SETTING_DEVICE: preference,
            # What fits depends on the precision chosen too.
            transcription_preferences.SETTING_COMPUTE_TYPE:
                self._selected_transcription_precision()
                or transcription_preferences.AUTO,
            transcription_preferences.SETTING_BACKEND:
                self._selected_transcription_backend()
                or transcription_preferences.AUTO,
            transcription_preferences.SETTING_AUTO_DETECT_LANGUAGE:
                self._transcription_detect_language_check.GetValue(),
            transcription_preferences.SETTING_LANGUAGE:
                language or transcription_preferences.LANGUAGE_INTERFACE,
        }}
        cuda_build = self._whisper_cpp_cuda_installed()
        resolution = transcription_preferences.resolve(
            live, probe,
            external_view.usable_ids(
                self._transcription_installed_ids,
                self._transcription_external_references,
                self._transcription_external_states,
            ),
            self.main_window.i18n.language,
            whisper_cpp_cuda_installed=cuda_build,
        )

        keys = []
        if resolution.backend_id == transcription_backend.BACKEND_WHISPER_CPP:
            device_id, reason = transcription_device.resolve_whisper_cpp_device(
                preference, probe, cuda_build
            )
            # whisper.cpp's two reasons are said under "automatic" too: the
            # card is there and works, and the processor is chosen only
            # because of the program — which the user can do something about.
            worth_saying = reason in (
                transcription_device.REASON_CUDA_BUILD_UNSUPPORTED,
                transcription_device.REASON_CUDA_BUILD_MISSING,
            )
        else:
            device_id, reason = transcription_device.resolve_device(preference, probe)
            worth_saying = False
        if worth_saying or (preference == transcription_device.PREFERENCE_CUDA
                            and device_id != transcription_device.DEVICE_CUDA):
            # Otherwise only for the user who asked for the card. Under
            # "automatic" the processor is not a disappointed expectation,
            # which is the same distinction resolve_device() itself draws.
            keys.append(transcription_device.device_reason_i18n_key(reason))
        if resolution.model_none_reason is not None:
            keys.append(
                transcription_preferences.MODEL_NONE_I18N_KEYS[
                    resolution.model_none_reason
                ]
            )
        if resolution.language_overridden is not None:
            # Said as the choice is made, not only by the first run: the
            # language below is not the one this model will transcribe in.
            keys.append(_TRANSCRIPTION_LANGUAGE_OVERRIDDEN)
        elif resolution.language_forced:
            keys.append(_TRANSCRIPTION_LANGUAGE_FORCED)
        return keys

    def _render_transcription_substitutions(self):
        i18n = self.main_window.i18n
        # Every source shares the one field, and that is deliberate: they are
        # the same kind of thing to the person reading them, and a second
        # read-only box would be a second tab stop saying so. The result of
        # the action the user just took goes first, because it is the one they
        # are waiting for; the folder's own notice goes last, because it is
        # about the folder rather than about anything they just did.
        lines = []
        if self._transcription_last_outcome is not None:
            lines.append(_transcription_announcement_text(
                i18n, self._transcription_last_outcome
            ))
            lines.extend(
                i18n.t(key) for key in self._transcription_last_outcome_extra
            )
        lines.extend(i18n.t(key) for key in self._transcription_substitution_keys)
        lines.extend(i18n.t(key) for key in self._transcription_hardware_keys)
        precision_notice = self._transcription_precision_notice()
        if precision_notice:
            lines.append(precision_notice)
        notice = _transcription_unknown_dirs_notice(
            i18n, self._transcription_unknown_dirs
        )
        if notice:
            lines.append(notice)
        text = "\n".join(lines)
        # ChangeValue: showing a warning is not an edit. With SetValue, merely
        # arriving on this tab made the Apply button appear and gain a tab
        # stop — exactly what _mark_dirty exists to prevent.
        self._transcription_substituted_field.ChangeValue(text)
        self._transcription_substituted_label.Show(bool(text))
        self._transcription_substituted_field.Show(bool(text))
        self._transcription_substituted_field.GetParent().Layout()

    def _show_transcription_cuda_status(self):
        """Put the one line about the CUDA libraries into its own field."""
        state = cuda_runtime.installation_state()
        self._transcription_cuda_state = state.state
        text = _transcription_cuda_status_text(self.main_window.i18n, state)
        if (state.state != cuda_runtime.STATE_INSTALLED
                and reused_runtimes.registered_cuda_dir()):
            # The libraries come from the folder the user chose to reuse: said,
            # or the line would offer the very download the folder replaces.
            text = f"{text} {self.main_window.i18n.t('transcription_reuse_in_use_note')}"
        # ChangeValue for the same reason as the warning field above.
        self._transcription_cuda_field.ChangeValue(text)
        self._sync_transcription_action_buttons()

    # ── The eight actions ────────────────────────────────────────────────────

    def _transcription_model_state(self):
        """What the selected model's buttons have to answer to, or None."""
        model_id = self._selected_transcription_model()
        if model_id is None or model_id == transcription_preferences.AUTO:
            return None
        if model_id in self._transcription_corrupted_models:
            return _TRANSCRIPTION_STATE_CORRUPTED
        return self._transcription_model_states.get(model_id)

    def _transcription_cuda_button_state(self):
        """The same, for the CUDA libraries."""
        if self._transcription_cuda_corrupted:
            return _TRANSCRIPTION_STATE_CORRUPTED
        return self._transcription_cuda_state

    def _sync_transcription_action_buttons(self):
        """Enable exactly the buttons that can act on what is on disk now."""
        for buttons, state in (
            (_TRANSCRIPTION_MODEL_ACTION_BUTTONS, self._transcription_model_state()),
            (_TRANSCRIPTION_CUDA_ACTION_BUTTONS,
             self._transcription_cuda_button_state()),
        ):
            allowed = _transcription_action_states(
                state, self._transcription_job_running
            )
            for action, _label_key, state_key in buttons:
                button = self._transcription_action_buttons.get(action)
                # Absent while the page is still being built: the populate
                # helpers run before the row they belong to exists.
                if button is not None:
                    button.Enable(allowed[state_key])
        # Marked models make Download available whatever the list is on.
        download = self._transcription_action_buttons.get(
            transcription_management.ACTION_DOWNLOAD_MODEL
        )
        if (download is not None and not self._transcription_job_running
                and self._transcription_queue_to_download()):
            download.Enable(True)
        self._sync_transcription_clear_cache_button()
        self._sync_whisper_cpp_buttons()

    def _set_transcription_job_running(self, running):
        """One action at a time, and every other button says so."""
        self._transcription_job_running = bool(running)
        self._sync_transcription_action_buttons()
        # Choosing another models folder moves the models on OK, under the
        # same lock a running download holds: not offered while one runs.
        browse = getattr(self, "_transcription_models_dir_browse_btn", None)
        if browse is not None:
            browse.Enable(not self._transcription_job_running)
        # The buttons of the section for models in other folders wait for the
        # same job: two at once would serialize behind a bar that does not move.
        self._sync_external_buttons()

    def _on_transcription_model_change(self, event):
        """Another model, another set of buttons that can act on it — and
        perhaps an English-only one, which has something to say about the
        language."""
        self._forget_transcription_outcome()
        self._sync_transcription_action_buttons()
        self._show_transcription_hardware_notices()
        # A list box fires EVT_LISTBOX, which the dialog does not route to
        # _mark_dirty the way it does EVT_COMBOBOX (it has other list boxes
        # that are not settings), so this one says so itself.
        self._mark_dirty(event)
        event.Skip()

    def _on_transcription_action(self, event):
        """Every action button comes here; the button is the action."""
        pressed = event.GetEventObject()
        for action, button in self._transcription_action_buttons.items():
            if button is pressed:
                self._start_transcription_action(action)
                return

    def _start_transcription_action(self, action):
        """Work out what the action needs, ask if it costs a download, run it."""
        if self._transcription_job_running:
            return
        if action in transcription_management.WHISPER_CPP_ACTIONS:
            self._start_whisper_cpp_action(action)
            return
        model_id = None
        if action in transcription_management.MODEL_ACTIONS:
            if not self._transcription_models_dir_applied():
                return
            # Marked models turn Download into a queue; every other action is
            # about the model the list is on.
            if (action == transcription_management.ACTION_DOWNLOAD_MODEL
                    and self._start_transcription_queue()):
                return
            model_id = self._selected_transcription_model()
            if model_id is None or model_id == transcription_preferences.AUTO:
                # "Automático" names no model. The buttons are already off in
                # that state; this is the guard for a press that got through.
                return
        if action in _TRANSCRIPTION_CONFIRMED_ACTIONS:
            self._measure_transcription_download(action, model_id)
            return
        if action in _TRANSCRIPTION_REMOVE_ACTIONS and not self._ask_transcription_removal(
                action, model_id):
            return
        self._start_transcription_job(action, model_id=model_id)

    def _transcription_models_dir_applied(self) -> bool:
        """Refuse to touch the models while the folder is only *chosen*.

        The other half of "the move happens on OK": a folder picked in
        Procurar is not stored until OK, so bytes written against it now are
        bytes Cancel throws away. A 3 GB download into the chosen folder
        followed by Cancel leaves the setting on the old folder, and nothing
        in the app ever looks at the new one again — not the model list, not
        `list_unknown_dirs()`, which both walk the folder that is *configured*
        — so the picker says "not installed" and offers the same 3 GB again.

        Only the model actions: the CUDA libraries live in their own
        install-wide folder that this setting does not move.
        """
        if self._transcription_models_dir == self._stored_transcription_models_dir():
            return True
        i18n = self.main_window.i18n
        wx.MessageBox(
            i18n.t("transcription_apply_folder_first"),
            i18n.t("settings_title"),
            wx.OK | wx.ICON_INFORMATION,
            self,
        )
        return False

    def _measure_transcription_download(self, action, model_id):
        """Take the measurements the question needs, off the wx thread.

        `probe_hardware()` was measured at 0.67 s on a machine with no card at
        all, and a wx thread busy that long is a screen reader with nothing to
        read. The free space goes with it, on the same thread, because it is
        the same wait for the same question — and both are taken again on
        every press rather than cached, since installing the libraries is
        precisely the thing that changes the answer.
        """
        self._set_transcription_job_running(True)
        models_root = transcription_preferences.resolve_models_dir(
            self._transcription_models_dir
        )
        root = (models_root if action in transcription_management.MODEL_ACTIONS
                else cuda_runtime.default_cuda_runtime_dir())

        def _measured(probe):
            # Still on the probe's own thread, which is where the disk query
            # belongs too rather than back on the one we just left.
            free = model_store.free_bytes(root)
            wx.CallAfter(
                self._confirm_transcription_download, action, model_id, probe, free
            )

        transcription_management.probe_in_background(_measured)

    def _confirm_transcription_download(self, action, model_id, probe, free_bytes):
        """Back on the wx thread with the measurements: ask, then run."""
        if not self:
            # Configurações was closed while the probe ran, and every control
            # below went with it.
            return
        self._adopt_transcription_probe(probe)
        self._set_transcription_job_running(False)

        repair = action in _TRANSCRIPTION_REPAIR_ACTIONS
        preference = self._selected_transcription_device_preference()
        if action in transcription_management.MODEL_ACTIONS:
            summary = transcription_management.model_download_summary(
                model_id,
                transcription_preferences.resolve_models_dir(
                    self._transcription_models_dir
                ),
                probe, free_bytes, preference, repair=repair,
                whisper_cpp_cuda_installed=self._whisper_cpp_cuda_installed(),
                compute_type_preference=self._selected_transcription_precision()
                or transcription_preferences.AUTO,
            )
        else:
            summary = transcription_management.cuda_runtime_download_summary(
                probe, free_bytes, device_preference=preference, repair=repair
            )
        if summary is None:
            # An id the catalogue no longer knows: there is nothing to quote
            # and nothing to fetch.
            return
        if not self._ask_transcription_download(summary, repair):
            return
        self._start_transcription_job(action, model_id=model_id)

    def _ask_transcription_download(self, summary, repair) -> bool:
        i18n = self.main_window.i18n
        text = _transcription_download_confirmation(i18n, summary, repair)
        title = i18n.t("transcription_download_confirm_title")
        if summary.enough_space is False:
            # Not a question. The free-space gate inside the download would
            # refuse this transfer anyway, and offering "Sim" for something
            # that cannot start is offering to fail. A volume that could not
            # be *measured* is a different answer and does go through, because
            # ensure_free_space() lets that one through too — refusing here
            # would make downloading impossible on a disk nothing can measure.
            wx.MessageBox(text, title, wx.OK | wx.ICON_ERROR, self)
            return False
        return wx.MessageBox(text, title, wx.YES_NO | wx.ICON_QUESTION, self) == wx.YES

    def _ask_transcription_removal(self, action, model_id) -> bool:
        """Ask before deleting. No size in the question, on purpose: it would
        be the catalogued one, which is a lie about a half-downloaded model —
        and the figure is already in the picker line the user just read."""
        i18n = self.main_window.i18n
        if action == transcription_management.ACTION_REMOVE_MODEL:
            text = i18n.t("transcription_confirm_remove_model").format(
                model=model_names.display_name(i18n, model_id) or ""
            )
        else:
            text = i18n.t("transcription_confirm_remove_cuda")
        return wx.MessageBox(
            text, i18n.t("transcription_remove_confirm_title"),
            wx.YES_NO | wx.ICON_QUESTION, self,
        ) == wx.YES

    def _transcription_job_factory(self, action, model_id=None, models_root=None,
                                   new_models_root=None, build_id=None,
                                   compute_capability=None):
        """`make_job(on_progress, on_finished)` for one management action.

        The one place that decides what a job is built with, so that the
        progress dialog and the main window's background runner (see
        TranscriptionBackgroundTabMixin) build the same job.
        """
        if models_root is None:
            # The folder that is actually in force, never the one the tab is
            # merely showing — see _transcription_models_dir_applied(), which
            # is what stops the two from differing here at all.
            models_root = transcription_preferences.resolve_models_dir(
                self._stored_transcription_models_dir()
            )
        job_kwargs = {}
        if action in transcription_management.MODEL_ACTIONS:
            job_kwargs["models_root"] = models_root
        elif action == transcription_management.ACTION_MOVE_MODELS:
            job_kwargs["models_root"] = models_root
            job_kwargs["new_models_root"] = new_models_root
        elif action in transcription_management.WHISPER_CPP_ACTIONS:
            job_kwargs["build_id"] = build_id
            job_kwargs["compute_capability"] = compute_capability
            # Where the program's install puts the voice-activity model.
            job_kwargs["models_root"] = models_root

        def _make_job(on_progress, on_finished):
            return transcription_management.ManagementJob(
                action, model_id=model_id,
                on_progress=on_progress, on_finished=on_finished,
                **job_kwargs,
            )

        return _make_job

    def _transcription_status_text(self, action, model_id=None, build_id=None):
        """The sentence saying which action is running."""
        i18n = self.main_window.i18n
        return progress_status_text(
            i18n, action, model_names.display_name(i18n, model_id),
            build=_whisper_cpp_build_name(i18n, build_id),
        )

    def _run_transcription_job(self, action, model_id=None, models_root=None,
                               new_models_root=None, build_id=None,
                               compute_capability=None):
        """Run one action behind the progress dialog. Returns what it answered.

        Deliberately does not report: the models-folder move has to decide
        which folder the setting names *before* anything is said, so that the
        sentence and the setting cannot disagree.
        """
        dialog = TranscriptionProgressDialog(
            self,
            self.main_window.i18n,
            self.main_window.speak_output,
            self._transcription_job_factory(
                action, model_id=model_id, models_root=models_root,
                new_models_root=new_models_root, build_id=build_id,
                compute_capability=compute_capability,
            ),
            self._transcription_status_text(action, model_id, build_id),
        )
        self._set_transcription_job_running(True)
        try:
            dialog.run()
            return dialog.job, dialog.result, dialog.error
        finally:
            dialog.Destroy()
            self._set_transcription_job_running(False)

    def _report_transcription_job(self, action, model_id, job, result, error,
                                  extra_keys=(), restore_focus=True):
        """Say what happened, remember it, and redraw what it changed.

        `restore_focus` is False for a job that ended in the background: the
        user is somewhere else by then, and a focus pulled onto a button of
        Settings would be the surprise the background option exists to avoid.
        """
        self._note_transcription_outcome(action, model_id, error)
        self._announce_transcription_result(job, result, error, extra_keys)
        self._refresh_after_transcription_action(action)
        if restore_focus:
            self._restore_transcription_focus(action)

    def _restore_transcription_focus(self, action):
        """Never leave the focus on a button the action just switched off.

        A download that worked takes the model to INSTALLED, which is exactly
        the state in which Baixar is disabled — and the focus was on Baixar,
        because that is what the user pressed. A disabled control answers
        nothing to a screen reader, so the user is left on a button that will
        not say what it is. The nearest thing that *can* is the next enabled
        button on the same row, and failing that the control the row belongs
        to.
        """
        if action in VERIFY_ACTIONS and self._focus_transcription_result(
                self._transcription_action_buttons.get(action)):
            return
        button = self._transcription_action_buttons.get(action)
        if button is None or button.IsEnabled():
            # No button at all for the models-folder move, and nothing to do
            # when the one pressed is still available.
            return
        if action in transcription_management.MODEL_ACTIONS:
            row, fallback = (_TRANSCRIPTION_MODEL_ACTION_BUTTONS,
                             self._transcription_model_list)
        else:
            row, fallback = (_TRANSCRIPTION_CUDA_ACTION_BUTTONS,
                             self._transcription_cuda_field)
        for other, _label_key, _state_key in row:
            candidate = self._transcription_action_buttons.get(other)
            if candidate is not None and candidate.IsEnabled():
                candidate.SetFocus()
                return
        fallback.SetFocus()

    def _note_transcription_outcome(self, action, model_id, error):
        """Remember damage only a hash could have found.

        installation_state() measures names and sizes, so a file that is the
        right size and the wrong bytes reads as installed there — and without
        somewhere to keep what verify_*() found, the Reparar button the user
        has just been told to press would be disabled the moment the check
        that found the problem finished. A cancelled check changes nothing:
        it found nothing either way.
        """
        code = getattr(error, "code", None)
        if action in transcription_management.MODEL_ACTIONS:
            if code == transcription_errors.MODEL_CORRUPTED:
                self._transcription_corrupted_models.add(model_id)
            elif error is None:
                self._transcription_corrupted_models.discard(model_id)
            return
        if action not in _TRANSCRIPTION_CUDA_ACTIONS:
            # Moving the models folder is neither, and a move that worked is
            # no reason to forget that the libraries failed their check.
            return
        if code == transcription_errors.CUDA_RUNTIME_CORRUPTED:
            self._transcription_cuda_corrupted = True
        elif error is None:
            self._transcription_cuda_corrupted = False

    def _announce_transcription_result(self, job, result, error, extra_keys=()):
        """Say it once, through the app's own funnel, and leave it on screen.

        The sound is only for a failure, and it is the error sound that
        already exists: a new sound event would need an .ogg in the default
        pack *and* in every pack a user has installed, plus a line in the
        Sound Events tab — and for the other three outcomes the sentence is
        the whole of the news.
        """
        announcement = job.announcement(result, error)
        self._transcription_last_outcome = announcement
        self._transcription_last_outcome_extra = tuple(extra_keys)
        self._render_transcription_substitutions()

        i18n = self.main_window.i18n
        sentences = [_transcription_announcement_text(i18n, announcement)]
        sentences.extend(i18n.t(key) for key in self._transcription_last_outcome_extra)
        if announcement.outcome == transcription_management.OUTCOME_FAILED:
            # Guarded (docs/traps/audio-devices.md): a sound that raises would
            # otherwise take the sentence below with it, and the failure would
            # be told by nothing at all.
            try:
                self.main_window.error_sound.play()
            except Exception as exc:
                logging.warning("[transcription] could not play the error sound: %s",
                                transcription_errors.exception_report(exc))
        # One call, and no interrupt: two announcements in a row talk over
        # each other, and cutting the reader off to say this is worse than
        # waiting for it to finish what it was reading.
        self.main_window.speak_output.output(" ".join(sentences))

    def _refresh_after_transcription_action(self, action):
        """Redraw whatever the action left different on the disk."""
        if (action in transcription_management.MODEL_ACTIONS
                or action == transcription_management.ACTION_MOVE_MODELS):
            self._refresh_transcription_models()
            return
        self._show_transcription_cuda_status()
        # Installing or removing the libraries changes what the card can do,
        # and the probe taken before it says the old answer — which is the
        # "you asked for the card and it cannot be used" notice still claiming
        # the libraries are missing right after they were installed.
        transcription_management.probe_in_background(
            lambda probe: wx.CallAfter(self._adopt_transcription_probe, probe)
        )

    def _adopt_transcription_probe(self, probe):
        """A fresh measurement of this machine, back on the wx thread."""
        if not self:
            return
        self._transcription_probe = probe
        # The card's capability decides which whisper.cpp builds are offered.
        self._populate_whisper_cpp_builds()
        self._show_transcription_hardware_notices()

    def _move_transcription_models(self) -> str:
        """Move what is downloaded into the folder the user chose, and answer
        which folder the setting must now name.

        Run from OK/Apply and never from Procurar: a move started in the
        browse handler would leave the files in a folder a Cancel then never
        stores, so the setting would name the folder they left.

        A move that did not get everything across leaves the models split
        between the two folders, and the setting can name only one of them. It
        names the **previous** one, and that is not a toss-up: move_models()
        skips whatever already arrived, so from the previous folder pressing
        OK again moves exactly what is left, and the models that did not cross
        stay usable in the meantime. Naming the new folder instead would
        strand the leftovers where nothing lists them, remove_model() never
        reaches them, and the picker offers to download them all over again.

        "Did not get everything across" is deliberately the *default* reading
        of a failure rather than a case of it: a job that broke before it
        could even read `models_before_move` reports no models on either side,
        and treating that as success would name the new folder for a move that
        never happened at all.
        """
        previous = self._stored_transcription_models_dir()
        chosen = self._transcription_models_dir
        old_root = transcription_preferences.resolve_models_dir(previous)
        new_root = transcription_preferences.resolve_models_dir(chosen)
        try:
            nothing_there = not os.path.isdir(old_root) or not os.listdir(old_root)
        except OSError:
            nothing_there = False
        if nothing_there:
            # The common case, on an install where nothing was ever
            # downloaded. A progress dialog that opens and closes within a
            # frame is a focus change a screen reader announces for nothing.
            return chosen

        job, result, error = self._run_transcription_job(
            transcription_management.ACTION_MOVE_MODELS,
            models_root=old_root,
            new_models_root=new_root,
        )
        moved = tuple(getattr(error, "moved", ()) or ())
        stayed = [m for m in job.models_before_move if m not in moved]
        everything_crossed = error is None or (moved and not stayed)
        extra_keys = ()
        folder = chosen
        if not everything_crossed:
            folder = previous
            self._transcription_models_dir = previous
            extra_keys = ("transcription_models_dir_kept_previous",)
        elif error is not None:
            # Everything arrived and the move still raised. The sentence for
            # that is a failure, and the folder has nonetheless changed —
            # hearing "could not be completed" while the setting silently
            # moves is the worst of both.
            extra_keys = ("transcription_models_dir_now_the_new_one",)
        self._report_transcription_job(
            transcription_management.ACTION_MOVE_MODELS, None, job, result, error,
            extra_keys,
        )
        return folder

    def _load_transcription_values(self):
        """Populate the Transcrição tab. Measures nothing and consumes nothing.

        Two things this deliberately does *not* do, both of which it used to.

        **It does not probe the hardware.** `device.probe_hardware()` imports
        ctranslate2, counts CUDA devices, asks NVML and, where a card is
        present, LoadLibrary's cuBLAS — seconds on a machine with a graphics
        card, on the wx thread, inside `SettingsDialog.__init__`, i.e. before
        there is a window for a screen reader to announce. device.py accepts
        that cost against "nothing next to a transcription"; the budget for
        opening Ctrl+, is not the same budget. Nothing here needs the answer:
        the substitutions below are provably independent of it (neither
        `_resolve_backend()`, `_resolve_device_preference()` nor
        `_resolve_language()` is even handed the probe, and `_resolve_model()`
        appends its own before `auto_select_model()` is reached), so an empty
        probe and an empty installed list produce the identical warning list.
        The measurement happens on the first visit to the tab instead, on a
        worker that `_enter_transcription_page()` starts, and its answer is
        adopted back on the wx thread (`_adopt_transcription_probe()`) — which
        is what `model_none_reason` and the "you asked for a card this machine
        has not got" notice are read off. Part 5c needs the same answer for its
        download offers: take it from there, or off the UI thread altogether,
        never from here.

        **It does not sanitize.** `resolve()` reports a value it had to replace
        and `sanitize_section()` is what stops that report repeating — but it
        stops it whether or not anybody was told, and this method runs on every
        single open of this dialog, including the one where the user came to
        change the interface language and never looked at this tab. Consuming
        the warning there is the exact silent swap preferences.py exists to
        prevent. Both the sanitize and the write-back are therefore gated on
        the tab having actually been shown.
        """
        i18n = self.main_window.i18n
        settings = self.main_window.settings

        self._transcription_models_dir = self._stored_transcription_models_dir()
        self._show_transcription_models_dir()

        # available_backends stays None — "nobody measured" — on purpose:
        # measuring means importing the optional backend, and _resolve_backend()
        # then checks a stored id against the ids this version knows about
        # rather than inventing an obstacle we did not observe. The empty probe
        # and the empty installed list are the same kind of "nobody measured",
        # and for the reason in this method's docstring: only .substitutions is
        # read from what comes back, and no substitution depends on either.
        resolution = transcription_preferences.resolve(
            settings,
            transcription_device.HardwareProbe(),
            (),
            i18n.language,
            # Read from app.json (no disk): a custom model whose reference was
            # forgotten is a substitution, and this is the one place the
            # tab says so — the installed list stays empty for the reason above.
            custom_model_ids=self._transcription_custom_model_ids(),
        )
        self._show_transcription_substitutions(resolution)

        section = transcription_preferences.read_section(settings)
        # The backend first: it decides which catalogue the model list holds,
        # and a whisper.cpp model has no entry in faster-whisper's.
        if self._transcription_backend_combo is not None:
            self._populate_transcription_backend_choices()
            self._select_transcription_backend(
                section[transcription_preferences.SETTING_BACKEND]
            )
        self._populate_transcription_model_choices()
        self._select_transcription_model(section[transcription_preferences.SETTING_MODEL])

        stored_device = section[transcription_preferences.SETTING_DEVICE]
        self._transcription_device_radio.SetSelection(
            _TRANSCRIPTION_DEVICE_PREFERENCES.index(stored_device)
            if stored_device in _TRANSCRIPTION_DEVICE_PREFERENCES else 0
        )
        self._load_transcription_precision(section)

        detect = section[transcription_preferences.SETTING_AUTO_DETECT_LANGUAGE]
        self._transcription_detect_language_check.SetValue(bool(detect))
        self._populate_transcription_language_choices()
        self._select_transcription_language(
            section[transcription_preferences.SETTING_LANGUAGE]
        )
        self._sync_transcription_language_controls()
        self._load_transcription_options()
        self._load_transcription_cache()
        self._load_transcription_reuse()

        self._show_transcription_cuda_status()
        self._show_whisper_cpp_status()

    def _enter_transcription_page(self):
        """The tab has been put on screen: measure it, say it, and only then
        let what it says be spent.

        Everything the tab costs the user is here rather than in
        `_load_transcription_values()`, for two separate reasons.

        The probe and the folder listing are disk and driver I/O, and paying
        for them on every Ctrl+, blocks the wx thread before there is even a
        window for the screen reader to announce.

        The warning is the other half. A read-only field on a tab that was
        never selected is not a cue for anybody, and under NVDA it is not a
        cue even with the tab open until focus reaches it — so the sentence is
        also spoken once, through `speak_output` like every other announcement
        in the app. `sanitize_section()` runs only *after* that: it rewrites
        the dead value so the warning never comes back, which is exactly why
        it must not run before the warning was delivered.

        Idempotent by design — a user switching tabs back and forth is not a
        reason to re-measure, and re-speaking the same sentence on every visit
        would be its own kind of noise.
        """
        if self._transcription_page_seen:
            return
        self._transcription_page_seen = True

        # One call rather than resolving the folder and listing it separately:
        # a tab that lists from one folder while the run reads another shows
        # the user a model that is not the one a transcription would find.
        models_dir, self._transcription_installed_ids = (
            transcription_preferences.models_folder(self._transcription_models_dir)
        )
        # Folders no catalogue entry claims, listed with the rest of the
        # folder rather than on its own pass — see
        # _transcription_unknown_dirs_notice() for what makes them worth a
        # sentence at all.
        self._transcription_unknown_dirs = model_store.list_unknown_dirs(models_dir)
        # The probe off the wx thread, as every other one in this tab: it
        # imports ctranslate2, asks NVML and the precisions both devices run —
        # seconds on a machine with a card, during which NVDA could not read
        # the tab just selected. Until it answers, nothing measured is shown:
        # no hardware notice, and every precision listed (as before any
        # probe). The answer redraws those in place and says nothing.
        transcription_management.probe_in_background(
            lambda probe: wx.CallAfter(self._adopt_transcription_probe, probe)
        )
        self._show_transcription_cuda_status()
        self._show_whisper_cpp_status()
        # Measure the folders of the models the user pointed WinZapp at, on a
        # worker, now that someone is looking: with none, nothing is started.
        # It also redraws the model list and the hardware notices, so they are
        # not drawn here a second time.
        self._refresh_external_models()

        i18n = self.main_window.i18n
        # The same argument as the substitutions: a read-only field is not a
        # cue under NVDA until focus reaches it, and folders the app cannot
        # delete are exactly the thing nobody goes looking for.
        spoken = [i18n.t(key) for key in self._transcription_substitution_keys]
        unknown = _transcription_unknown_dirs_notice(
            i18n, self._transcription_unknown_dirs
        )
        if unknown:
            spoken.append(unknown)
        if spoken:
            self.main_window.speak_output.output(" ".join(spoken))
        if transcription_preferences.sanitize_section(
                self.main_window.settings,
                custom_model_ids=self._transcription_custom_model_ids()):
            # Saved here rather than left to OK/Apply: the point of rewriting a
            # value that can never be valid again is that the warning above is
            # not repeated, and a user who closes this dialog with Cancel would
            # otherwise be told the same thing again on every open.
            self.main_window.save_settings()

    def _transcription_custom_model_ids(self):
        """The custom references' ids, each with the backend that loads it, for
        resolve() and sanitize_section() — or None ("not measured") while
        app.json could not be read, so that a moment of an unreadable file
        never retires the user's custom model."""
        if not self._transcription_external_known:
            return None
        return external_models.custom_reference_backends(
            self._transcription_external_references
        )

    def _transcription_setting_may_be_written(self, setting) -> bool:
        """Whether OK may write this control back over what is stored.

        A setting `resolve()` had to substitute is showing the *replacement*,
        not what is on disk: writing that back from an OK pressed on some other
        tab consumes the warning the user was never given, and leaves them with
        "Automático" selected and nothing to say their choice was dropped.
        Every other setting is unaffected — its control is a faithful copy of
        what is stored, so writing it back changes nothing.

        One more case of the same thing: a custom model while app.json could
        not be read has no entry in the picker, which fell back to
        "Automático" — a stand-in, not the user's answer.
        """
        if (setting == transcription_preferences.SETTING_MODEL
                and not self._transcription_external_known
                and self._selected_transcription_model() == transcription_preferences.AUTO):
            stored = transcription_preferences.read_section(
                self.main_window.settings
            )[transcription_preferences.SETTING_MODEL]
            if stored not in self._transcription_model_ids:
                return False
        return (self._transcription_page_seen
                or setting not in self._transcription_substituted_settings)

    def _apply_transcription_values(self):
        """Write the Transcrição tab back. Called from _apply_values().

        Only what the tab actually presented — see
        _transcription_setting_may_be_written().
        """
        section = self.main_window.settings.setdefault(
            transcription_preferences.SECTION, {}
        )
        model_id = self._selected_transcription_model()
        if model_id is not None and self._transcription_setting_may_be_written(
                transcription_preferences.SETTING_MODEL):
            section[transcription_preferences.SETTING_MODEL] = model_id
        if self._transcription_setting_may_be_written(
                transcription_preferences.SETTING_DEVICE):
            section[transcription_preferences.SETTING_DEVICE] = (
                self._selected_transcription_device_preference()
            )
        self._apply_transcription_precision(section)
        # Not gated: the checkbox is never substituted (a value that is not a
        # bool carries no intent to have been overridden, which is why
        # _resolve_language() does not report it either), so the control always
        # shows what is stored.
        section[transcription_preferences.SETTING_AUTO_DETECT_LANGUAGE] = (
            self._transcription_detect_language_check.GetValue()
        )
        self._apply_transcription_options()
        self._apply_transcription_cache()
        self._apply_transcription_reuse()
        # Written even while detection is on: it is the language the user would
        # rather hear, which preferred_language() answers for part 6 whatever
        # the checkbox says, and losing it every time detection is ticked would
        # make the choice unrecoverable.
        language = self._selected_transcription_language()
        if language is not None and self._transcription_setting_may_be_written(
                transcription_preferences.SETTING_LANGUAGE):
            section[transcription_preferences.SETTING_LANGUAGE] = language
        # The backend key is left exactly as it was found where there is no
        # picker: with one backend the stored value carries no choice of the
        # user's, and writing over it would be this dialog inventing one.
        backend_id = self._selected_transcription_backend()
        if backend_id is not None and self._transcription_setting_may_be_written(
                transcription_preferences.SETTING_BACKEND):
            section[transcription_preferences.SETTING_BACKEND] = backend_id

        # The models folder is install-wide, so it goes to app_settings and not
        # into this account's settings.json — app_settings.set() raises KeyError
        # for anything that is not global, which is what keeps it that way.
        app_settings = self._install_wide_settings()
        background_running = getattr(self.main_window, "transcription_background_running", None)
        if (app_settings is not None
                and self._transcription_models_dir != self._stored_transcription_models_dir()
                and callable(background_running) and background_running()):
            # The move waits on the models lock the download holds — up to
            # hours, behind a modal window. Refused instead; the choice stays
            # shown for an Apply once the download is over.
            wx.MessageBox(
                self.main_window.i18n.t("transcription_models_dir_busy"),
                self.main_window.i18n.t("settings_title"),
                wx.OK | wx.ICON_INFORMATION, self,
            )
            return
        if (app_settings is not None
                and self._transcription_models_dir != self._stored_transcription_models_dir()):
            # Checked again here and not only in Procurar: a folder of the
            # user's can be added *after* the new root was chosen, and the
            # move below would put WinZapp's models — and its Remover — on top
            # of it. Read afresh from app.json, plus what this dialog holds,
            # so that a file unreadable right now cannot let one through.
            stored = external_models.load_references(app_settings)
            known = {reference.id for reference in stored}
            references = stored + tuple(
                reference for reference in self._transcription_external_references
                if reference.id not in known
            )
            if self._refuse_transcription_models_dir(
                    references, self._transcription_models_dir, applying=True):
                self._transcription_models_dir = self._stored_transcription_models_dir()
                self._refresh_transcription_models()
                return
            # The files move here and nowhere else — see
            # _move_transcription_models(), which also decides which of the two
            # folders the setting ends up naming when only half of them cross.
            app_settings.set(
                transcription_preferences.MODELS_DIR_SETTING,
                self._move_transcription_models(),
            )

    def _refresh_transcription_labels(self):
        """Retranslate the Transcrição tab after a language change.

        More than SetLabel() calls: both comboboxes carry text built out of
        translations (the size class and the installed state of every model,
        and the interface-language entry), so they are rebuilt rather than
        relabelled — which is also why the populate helpers preserve the
        selection instead of resetting it.
        """
        i18n = self.main_window.i18n
        self._transcription_substituted_label.SetLabel(
            i18n.t("transcription_substituted_label")
        )
        self._transcription_cancel_btn.SetLabel(
            i18n.t("transcription_background_cancel_btn")
        )
        self._render_transcription_substitutions()
        self._transcription_model_label.SetLabel(i18n.t("transcription_model_label"))
        self._populate_transcription_model_choices()
        self._transcription_device_radio.SetLabel(i18n.t("transcription_device_label"))
        for index, preference in enumerate(_TRANSCRIPTION_DEVICE_PREFERENCES):
            self._transcription_device_radio.SetItemLabel(
                index,
                i18n.t(transcription_preferences.DEVICE_PREFERENCE_I18N_KEYS[preference]),
            )
        self._refresh_transcription_precision_labels()
        self._transcription_detect_language_check.SetLabel(
            i18n.t(transcription_preferences.LANGUAGE_DETECT_I18N_KEY)
        )
        self._transcription_language_label.SetLabel(
            i18n.t("transcription_language_label")
        )
        # Rebuilt, not just relabelled: language_choices() puts WinZapp's own
        # language first, and "its own language" is exactly what just changed.
        self._populate_transcription_language_choices()
        if self._transcription_backend_combo is not None:
            self._transcription_backend_label.SetLabel(
                i18n.t("transcription_backend_label")
            )
            self._populate_transcription_backend_choices()
        self._transcription_models_dir_label.SetLabel(
            i18n.t("transcription_models_dir_label")
        )
        self._transcription_models_dir_browse_btn.SetLabel(
            i18n.t("transcription_models_dir_browse_btn")
        )
        self._refresh_external_labels()
        self._refresh_transcription_options_labels()
        self._refresh_transcription_reuse_labels()
        self._refresh_transcription_cache_labels()
        self._refresh_transcription_window_labels()
        self._refresh_whisper_cpp_labels()
        self._transcription_cuda_label.SetLabel(i18n.t("transcription_cuda_runtime_label"))
        self._show_transcription_cuda_status()
        for action, label_key, _state_key in (
            _TRANSCRIPTION_MODEL_ACTION_BUTTONS + _TRANSCRIPTION_CUDA_ACTION_BUTTONS
        ):
            button = self._transcription_action_buttons.get(action)
            if button is not None:
                button.SetLabel(i18n.t(label_key))
        # The group names carry what the buttons no longer spell out, so they
        # have to follow a language change like any other label.
        for box, title_key in self._transcription_action_groups:
            box.SetLabel(i18n.t(title_key))

    def show_transcription_tab(self):
        """Show the dialog already on the Transcription tab; return the modal code.

        For callers that send the user straight here (a transcription that has
        no model to run with). The tab is found by its page object, never by
        its index: tabs are inserted over time, and a hardcoded number
        silently opens the wrong one.

        `ChangeSelection()`, not `SetSelection()`: the second fires
        EVT_NOTEBOOK_PAGE_CHANGED, and `_on_settings_page_changed()` would
        then enter the tab from inside it — before `ShowModal()`, with no
        window on screen. Entering is what speaks the tab's one-time warning
        (a replaced model, folders the app cannot delete) and then lets
        `sanitize_section()` spend it; spoken before the window exists, the
        screen reader's announcement of the new window cuts it off, and the
        warning is never said again. So the entry is queued with
        `wx.CallAfter` before `ShowModal()` and runs from inside the modal
        loop, once the window is up — the same arrangement
        `TranscriptionProgressDialog.run()` uses, for the same reason.
        """
        # The panel holding its place: the page itself is built when shown.
        page = vars(self).get("_transcription_holder") or self._transcription_page
        index = self._notebook.FindPage(page)
        if index != wx.NOT_FOUND:
            self._notebook.ChangeSelection(index)
            wx.CallAfter(self._enter_transcription_page)
        return self.ShowModal()

    def _on_transcription_detect_language_toggle(self, event):
        self._sync_transcription_language_controls()
        self._show_transcription_hardware_notices()
        event.Skip()

    def _on_transcription_language_change(self, event):
        """An English-only model with another language is said as it is chosen."""
        self._show_transcription_hardware_notices()
        # Skip(), as for the device radio: the dialog-level handler is what
        # shows Apply.
        event.Skip()

    def _on_transcription_device_change(self, event):
        """Re-answer "and what would that actually run on?" as it is asked."""
        self._show_transcription_hardware_notices()
        # Skip() or the dialog-level EVT_RADIOBOX never runs and the Apply
        # button stays hidden — see _mark_dirty()'s docstring.
        event.Skip()

    def _refuse_transcription_models_dir(self, references, root, applying=False) -> bool:
        """Say so and answer True when `root` contains a folder of the user's.

        WinZapp deletes inside its models folder when a model is removed, so
        a root with a referenced folder under it would turn "Remover" into
        deleting somebody else's files. A message box, which a screen reader
        reads whole; on OK/Apply it also says the folder was left as it was,
        since the dialog goes on to apply everything else.
        """
        inside = external_models.references_inside_root(references, root)
        if not inside:
            return False
        i18n = self.main_window.i18n
        text = i18n.t("transcription_external_root_contains").format(
            folders=", ".join(
                external_models.display_name(reference) for reference in inside
            )
        )
        if applying:
            text = " ".join((text, i18n.t("transcription_external_root_not_applied")))
        wx.MessageBox(text, i18n.t("settings_title"), wx.OK | wx.ICON_WARNING, self)
        return True

    def _on_browse_transcription_models_dir(self, event):
        """Choose the folder the models are downloaded into.

        **This records the preference and moves nothing.** The files move in
        `_move_transcription_models()`, from OK/Apply — a move started here
        would leave them in a folder a Cancel then never stores, so the
        setting would go on naming the folder they had already left. The model
        list is redrawn against the new folder immediately, so what the tab
        says about each model stays true either way.
        """
        i18n = self.main_window.i18n
        current = transcription_preferences.resolve_models_dir(
            self._transcription_models_dir
        )
        try:
            default_path = current if os.path.isdir(current) else ""
        except (OSError, ValueError):
            default_path = ""
        with wx.DirDialog(
            self,
            message=i18n.t("transcription_models_dir_browse_dialog_title"),
            defaultPath=default_path,
            style=wx.DD_DEFAULT_STYLE,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            chosen = dlg.GetPath()

        # A models folder that *contains* a folder of the user's would put it
        # where "Remove" deletes: refused, naming the folders in the way.
        if self._refuse_transcription_models_dir(
                self._transcription_external_references, chosen):
            return

        # Choosing the default folder stores the empty sentinel rather than the
        # path it resolved to: an absolute path written here would freeze a data
        # directory that legitimately moves — the whole WinZapp data folder is
        # meant to be copyable to another machine.
        default_dir = model_store.default_models_dir()
        try:
            is_default = os.path.normcase(os.path.abspath(chosen)) == os.path.normcase(
                os.path.abspath(default_dir)
            )
        except (OSError, ValueError):
            is_default = False
        self._transcription_models_dir = "" if is_default else chosen
        self._refresh_transcription_models()
        self._transcription_models_dir_field.SetFocus()
        self._mark_dirty()
