"""transcription_progress.py — the dialog a long background job runs behind.

Downloading a 3 GB model, hashing it, moving the models folder, installing the
CUDA libraries — and, in part 6, transcribing a voice message — are minutes of
work on a background thread, and a user with no window to read has no way to
tell a working download from a hung one. So the shape is node_download.py's: a
line saying what is happening, a gauge, and Cancel.

**The dialog knows nothing about which job it is running.** It is handed a
factory and a line of text, and it neither builds a job nor looks a sentence
up — that is what lets part 6 run its own `job.TranscriptionJob` behind it
instead of writing the app's second progress dialog. A caller whose job has
phases (part 6's has three, two of them with no fraction to report) keeps the
line current through `set_status()`, which is reached from its own `on_phase`
callback: by the time any phase fires, `run()` is executing and the dialog
object it closes over exists.

Four things this shape is paying for:

* **Every callback arrives on the job's thread.** `ManagementJob` and
  `TranscriptionJob` both import no wx at all, deliberately, so each callback
  crosses to the wx thread through `wx.CallAfter` here and nowhere else.
  Driving wx controls from two threads is not a race that shows up while
  testing; it is a crash in somebody else's session.

* **The reports come per megabyte.** ~3000 of them for large-v3, which is
  ~3000 cross-thread posts *and* a screen reader reading numbers for the
  length of the download. The job's own throttle is what reduces that to a
  gauge move twice a second and three spoken figures, so this dialog passes no
  throttle of its own.

* **The job could finish before there is a loop to end.** `EndModal()` on a
  dialog whose `ShowModal()` is not running asserts — the same wx rule the
  pairing dialogs are built around — and a finished report with nowhere to go
  would leave this dialog on screen for good. The job is therefore started
  from a `wx.CallAfter` queued *before* `ShowModal()`, so the first thing it
  can possibly report to is the modal loop already dispatching its start. For
  the same reason a `start()` that *raises* is turned into a finished report
  rather than allowed to propagate: nothing above a queued call catches it,
  and a dialog with no close box whose Cancel waits for a report that will
  never come is the settings window locked shut.

* **A wx.StaticText is not read by a screen reader that is not looking at it.**
  NVDA reads the static text of a dialog as it opens; JAWS and Narrator do
  not, and none of them re-read it when it changes. So the line is spoken as
  well as shown — it is the only thing that says *which* of the actions is
  running — and every later change to it is spoken too.
"""

import logging

import wx

from core.transcription import errors, management

log = logging.getLogger(__name__)

#: What each management action says it is doing, while it is doing it. Written
#: as a sentence per action rather than one string with a swappable verb:
#: "moving the models to the new folder" and "downloading the medium model"
#: are not the same sentence with a different word in any of the five
#: languages. Read by the *caller* — see the module docstring.
_STATUS_I18N_KEYS = {
    management.ACTION_DOWNLOAD_MODEL: "transcription_progress_download_model",
    management.ACTION_REPAIR_MODEL: "transcription_progress_repair_model",
    management.ACTION_VERIFY_MODEL: "transcription_progress_verify_model",
    management.ACTION_REMOVE_MODEL: "transcription_progress_remove_model",
    management.ACTION_MOVE_MODELS: "transcription_progress_move_models",
    management.ACTION_INSTALL_CUDA_RUNTIME: "transcription_progress_install_cuda",
    management.ACTION_REPAIR_CUDA_RUNTIME: "transcription_progress_repair_cuda",
    management.ACTION_VERIFY_CUDA_RUNTIME: "transcription_progress_verify_cuda",
    management.ACTION_REMOVE_CUDA_RUNTIME: "transcription_progress_remove_cuda",
    management.ACTION_INSTALL_WHISPER_CPP: "transcription_progress_install_whisper_cpp",
    management.ACTION_REPAIR_WHISPER_CPP: "transcription_progress_repair_whisper_cpp",
    management.ACTION_VERIFY_WHISPER_CPP: "transcription_progress_verify_whisper_cpp",
    management.ACTION_REMOVE_WHISPER_CPP: "transcription_progress_remove_whisper_cpp",
}


def progress_status_text(i18n, action, model_id=None, build=None) -> str:
    """The line saying what is happening, for one management action.

    Module level and taking `i18n` rather than reading it off a dialog, so the
    wording of every action can be checked in every locale without a window.
    `model_id` is the model as it is to be said; `build` the whisper.cpp
    program's build ("for the processor"), already in the user's language.
    """
    return i18n.t(_STATUS_I18N_KEYS[action]).format(
        model=model_id or "", build=build or ""
    )


class TranscriptionProgressDialog(wx.Dialog):
    """Runs exactly one background job and shows it. Used once, then destroyed.

    `make_job(on_progress, on_finished)` returns the job — started, cancelled
    and awaited by this dialog, never built by it. `run()` returns the modal
    code; `result`, `error` and `job` are what the caller reports on
    afterwards, because turning an outcome into a sentence is the caller's
    business and not a dialog's.
    """

    def __init__(self, parent, i18n, speak_output, make_job, status_text, title=None):
        self._i18n = i18n
        self._speak_output = speak_output
        self._status = status_text
        super().__init__(
            parent,
            title=title or i18n.t("transcription_progress_title"),
            # No close box: the only way out is Cancel, which has to tell the
            # job to stop rather than abandon it still writing to the disk.
            style=wx.DEFAULT_DIALOG_STYLE & ~wx.CLOSE_BOX,
        )

        #: What the job answered. Exactly one of the two is set, once.
        self.result = None
        self.error = None
        self._cancel_requested = False

        self._build_ui()

        self.job = make_job(self._post_progress, self._post_finished)
        # Alt+F4 still reaches a dialog with no close box, and it must mean
        # the same thing the button does.
        self.Bind(wx.EVT_CLOSE, self._on_cancel)

    def _build_ui(self):
        self._status_label = wx.StaticText(self, label=self._status)
        self._gauge = wx.Gauge(self, range=100, style=wx.GA_HORIZONTAL | wx.GA_SMOOTH)
        # wx.ID_CANCEL is what makes Escape reach this button: wxDialog turns
        # the key into a click on whichever button carries that id, and our
        # handler is what then runs instead of the dialog closing itself.
        self._cancel_btn = wx.Button(self, wx.ID_CANCEL, label=self._i18n.t("cancel"))
        self._cancel_btn.Bind(wx.EVT_BUTTON, self._on_cancel)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self._status_label, 0, wx.ALL | wx.EXPAND, 12)
        sizer.Add(self._gauge, 0, wx.ALL | wx.EXPAND, 12)
        sizer.Add(self._cancel_btn, 0, wx.ALIGN_CENTER | wx.BOTTOM, 12)
        self.SetSizer(sizer)
        sizer.Fit(self)
        self.SetMinSize((460, -1))
        self.Centre()
        # Cancel is the only thing on this dialog anybody can do, so focus
        # starts there rather than on the gauge, which answers nothing.
        self._cancel_btn.SetFocus()

    def run(self):
        """Show the dialog with the job running inside its own modal loop."""
        # Queued, not started here: see the module docstring. A job started
        # before ShowModal() can report back before there is a loop to end,
        # and EndModal() on a loop that is not running asserts.
        wx.CallAfter(self._start_job)
        return self.ShowModal()

    def set_status(self, text):
        """Say and show what is happening now. Callable from any thread.

        **Public, and therefore reached from a job's own thread.** Part 6
        drives this from `TranscriptionJob.on_phase`, which runs on the worker
        exactly as the two `_post_*` callbacks below do — so the hop this
        module exists to enforce is enforced *here*, rather than left to every
        caller to remember. Getting it wrong is a wx control driven from two
        threads, which does not raise and does not reproduce on the machine of
        whoever wrote it.

        Spoken as well as shown because a wx.StaticText nobody is focused on
        is silent under JAWS and Narrator, and silent under NVDA too once the
        dialog has finished opening.
        """
        if not wx.IsMainThread():
            wx.CallAfter(self.set_status, text)
            return
        self._status = text
        self._status_label.SetLabel(text)
        self.Layout()
        self._speak_output.output(text)

    def _start_job(self):
        if self._cancel_requested:
            # Cancelled between the dialog appearing and this call. A job that
            # was never started reports nothing at all (by design — see
            # ManagementJob), so nothing else would ever close this dialog.
            self._on_finished(
                None, errors.TranscriptionError(errors.CANCELLED, "cancelled by the user")
            )
            return
        # The line is spoken here rather than in _build_ui(): the dialog is on
        # screen by now, so this lands after the screen reader's own
        # announcement of it instead of underneath it.
        self._speak_output.output(self._status)
        try:
            self.job.start()
        except Exception as exc:
            # Nothing above a wx.CallAfter catches anything, and this dialog
            # has no close box: an exception here would leave the settings
            # window locked behind a modal whose Cancel is waiting for a
            # report that can no longer come. A thread that cannot be started
            # is exactly that case.
            logging.error("[transcription] the progress dialog could not start its job: %s",
                          errors.exception_report(exc))
            self._on_finished(
                None,
                errors.TranscriptionError(
                    errors.BACKEND_ERROR, f"{type(exc).__name__}: {exc}"
                ),
            )

    # ── Called on the job's thread ───────────────────────────────────────────
    # Nothing below this line may touch a wx control directly.

    def _post_progress(self, tick):
        wx.CallAfter(self._on_progress, tick)

    def _post_finished(self, result, error):
        wx.CallAfter(self._on_finished, result, error)

    # ── Called on the wx thread ──────────────────────────────────────────────

    def _on_progress(self, tick):
        if not self:
            # A report that was already queued when the dialog was destroyed.
            return
        if tick.update_bar:
            if tick.percent is None:
                # Nothing to measure against — a moving bar is the only honest
                # thing to show, and it is still a "something is happening".
                self._gauge.Pulse()
            else:
                self._gauge.SetValue(tick.percent)
        if tick.speak and tick.percent is not None:
            # No interrupt: cutting the reader off mid-sentence to say "50%"
            # is worse than the figure arriving a moment late. The throttle
            # has already decided this is one of the three worth saying.
            self._speak_output.output(
                self._i18n.t("transcription_progress_percent").format(
                    percent=tick.percent
                )
            )

    def _on_finished(self, result, error):
        if not self:
            return
        self.result = result
        self.error = error
        if not self.IsModal():
            # Unreachable while the job is started from inside the modal loop
            # (see run()), and EndModal() here would assert rather than close
            # anything. Logged instead of ignored: a caller that ever starts
            # the job itself would hang on this line with no clue why.
            log.warning(
                "[transcription] a job finished outside the progress dialog's modal loop"
            )
            return
        self.EndModal(wx.ID_CANCEL if error is not None else wx.ID_OK)

    def _on_cancel(self, _event=None):
        """Ask the job to stop, and keep the dialog until it actually has.

        Closing on the keypress would leave the action still writing to the
        disk behind a settings tab already offering the next one — and the
        finished report, the only thing that says what it managed to do before
        it stopped, would arrive with nobody left to say it. The cancel is
        cooperative, so the wait is until the next check, not until the end.
        """
        if self._cancel_requested:
            return
        self._cancel_requested = True
        self._cancel_btn.Disable()
        self.set_status(self._i18n.t("transcription_progress_cancelling"))
        self.job.cancel()
